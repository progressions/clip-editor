"""Native GTK4 clip editor. Not a browser window."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Graphene, Gtk, Pango  # noqa: E402

from clip_editor.aspects import ASPECTS, cover_crop, dest_size
from clip_editor.eagle import apply_omarchy_theme, theme_rgb
from clip_editor.export import ExportError, default_out_path, run_export
from clip_editor.probe import ProbeError, probe, which_ffmpeg
from clip_editor.project import (
    ClipInst,
    MediaItem,
    Project,
    ProjectError,
    clear_autosave,
    next_media_id,
    read_autosave,
    read_project,
    write_autosave,
    write_project,
)

APP_ID = "local.clip.Editor"
HISTORY_LIMIT = 80
JOIN_EPS = 0.04


def _same_path(a: Path | None, b: Path | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


def _load_frame(path: Path) -> GdkPixbuf.Pixbuf:
    raw = subprocess.check_output(
        [
            which_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "0",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "pipe:1",
        ],
        timeout=30,
    )
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(raw)
    loader.close()
    pb = loader.get_pixbuf()
    if pb is None:
        raise ProbeError(f"could not decode a frame from {path.name}")
    if pb.get_width() > 1600:
        scale = 1600 / pb.get_width()
        pb = pb.scale_simple(
            1600,
            max(1, int(pb.get_height() * scale)),
            GdkPixbuf.InterpType.BILINEAR,
        )
    return pb


class CoverPreview(Gtk.Widget):
    """Cover-crop preview. The same pan applies to the still, playback, and export."""

    __gtype_name__ = "ClipCoverPreview"

    def __init__(self) -> None:
        super().__init__()
        self.pixbuf: GdkPixbuf.Pixbuf | None = None
        self.pan_x = 0.5
        self.pan_y = 0.5
        self.on_pan = None
        self.on_pan_end = None
        self._drag_pan = (0.5, 0.5)
        self._texture: Gdk.Texture | None = None
        self._media: Gtk.MediaFile | None = None
        self._inv_id = 0
        self.blank = False
        self.set_layout_manager(Gtk.BinLayout())
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_cursor_from_name("grab")
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:  # noqa: N802
        return 32, 240, -1, -1

    def set_pixbuf(self, pb: GdkPixbuf.Pixbuf | None) -> None:
        self.pixbuf = pb
        self._texture = Gdk.Texture.new_for_pixbuf(pb) if pb is not None else None
        self.queue_draw()

    def set_media(self, media: Gtk.MediaFile | None) -> None:
        if self._media is not None and self._inv_id:
            try:
                self._media.disconnect(self._inv_id)
            except (TypeError, RuntimeError):
                pass
            self._inv_id = 0
        self._media = media
        if media is not None:
            self._inv_id = media.connect("invalidate-contents", lambda *_: self.queue_draw())
        self.queue_draw()

    def set_blank(self, blank: bool) -> None:
        if self.blank == blank:
            return
        self.blank = blank
        self.queue_draw()

    def _paintable(self) -> Gdk.Paintable | None:
        if self.blank:
            return None
        if self._media is not None:
            iw = int(self._media.get_intrinsic_width() or 0)
            ih = int(self._media.get_intrinsic_height() or 0)
            if iw > 0 and ih > 0:
                return self._media
        return self._texture

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:  # noqa: N802
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        r, g, b = theme_rgb("dark_background", (0.0, 0.0, 0.0))
        bg = Gdk.RGBA()
        bg.red, bg.green, bg.blue, bg.alpha = r, g, b, 1.0
        snapshot.append_color(bg, Graphene.Rect().init(0, 0, w, h))
        p = self._paintable()
        if p is None:
            return
        iw = int(p.get_intrinsic_width() or 0)
        ih = int(p.get_intrinsic_height() or 0)
        if iw <= 0 or ih <= 0:
            return
        src_a = iw / ih
        dst_a = w / h
        if src_a > dst_a:
            scale = h / ih
            sw, sh = iw * scale, float(h)
            x = -(sw - w) * self.pan_x
            y = 0.0
        else:
            scale = w / iw
            sw, sh = float(w), ih * scale
            x = 0.0
            y = -(sh - h) * self.pan_y
        snapshot.push_clip(Graphene.Rect().init(0, 0, w, h))
        snapshot.save()
        snapshot.translate(Graphene.Point().init(x, y))
        p.snapshot(snapshot, sw, sh)
        snapshot.restore()
        snapshot.pop()

    def _overflow(self) -> tuple[float, float]:
        p = self._paintable()
        w, h = self.get_width(), self.get_height()
        if p is None or w <= 0 or h <= 0:
            return 0.0, 0.0
        pw = int(p.get_intrinsic_width() or 0)
        ph = int(p.get_intrinsic_height() or 0)
        if pw <= 0 or ph <= 0:
            return 0.0, 0.0
        src_a = pw / ph
        dst_a = w / h
        if src_a > dst_a:
            return pw * (h / ph) - w, 0.0
        return 0.0, ph * (w / pw) - h

    def _drag_begin(self, *_args: object) -> None:
        self._drag_pan = (self.pan_x, self.pan_y)
        self.set_cursor_from_name("grabbing")

    def _drag_update(self, _g: Gtk.GestureDrag, dx: float, dy: float) -> None:
        ox, oy = self._overflow()
        px, py = self._drag_pan
        if ox > 1:
            self.pan_x = min(1.0, max(0.0, px - dx / ox))
        if oy > 1:
            self.pan_y = min(1.0, max(0.0, py - dy / oy))
        self.queue_draw()
        if callable(self.on_pan):
            self.on_pan()

    def _drag_end(self, *_args: object) -> None:
        self.set_cursor_from_name("grab")
        if callable(self.on_pan_end):
            self.on_pan_end()


def _round_rect(cr, x: float, y: float, w: float, h: float, r: float) -> None:  # noqa: ANN001
    if w <= 0 or h <= 0:
        return
    r = min(r, h / 2.0, w / 2.0)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5708, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
    cr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    cr.arc(x + r, y + r, r, 3.1416, 4.7124)
    cr.close_path()


class Timeline(Gtk.DrawingArea):
    """Ruler plus video and audio lanes. Drag either clip or the playhead."""

    __gtype_name__ = "ClipTimeline"

    _GUTTER = 22.0
    _PAD_RIGHT = 8.0
    _RULER_H = 16.0
    _LANE_H = 28.0
    _LANE_GAP = 6.0
    _BOTTOM = 16.0
    _EDGE = 8.0
    _MIN = 0.05
    _SNAP_PX = 10.0
    _TRAIL_PX = 160.0
    _TRAIL_MIN_S = 8.0
    _MIN_PPS = 8.0

    def __init__(self) -> None:
        super().__init__()
        self.duration = 0.0
        self.video_dur = 0.0
        self.video_start = 0.0
        self.video_name = ""
        self.audio_name = ""
        self.audio_start = 0.0
        self.audio_dur = 0.0
        self.audio_in = 0.0
        self.audio_out = 0.0
        self.audio_kind = ""
        self.in_s = 0.0
        self.out_s = 0.0
        self.vclips: list[ClipInst] = []
        self.aclips: list[ClipInst] = []
        self.src_durs: dict[str, float] = {}
        self.clip_names: dict[str, str] = {}
        self.sel_v = -1
        self.sel_a = -1
        self.playhead = 0.0
        self.on_seek = None
        self.on_video_move = None
        self.on_audio_move = None
        self.on_video_trim = None
        self.on_audio_trim = None
        self.on_place = None
        self.on_select = None
        self._drag_mode = ""
        self._drag_index = -1
        self._drag_v0 = 0.0
        self._drag_span = 0.0
        self._drag_inner = 1.0
        self._snap_line: float | None = None
        self._drop_hover: tuple[str, float] | None = None
        self.set_hexpand(False)
        self.set_vexpand(False)
        self.set_content_width(200)
        self.set_content_height(94)
        self.set_size_request(200, 94)
        self.set_draw_func(self._draw)
        self.connect("resize", self._on_resize)
        self.set_sensitive(False)
        self.set_cursor_from_name("col-resize")
        self.set_tooltip_text(
            "Drag a clip to slide it. Drag either edge to trim. "
            "Drag the ruler or playhead to seek. T splits at the playhead. "
            "Del removes the selected clip (A-track too)."
        )
        click = Gtk.GestureClick()
        click.connect("pressed", self._on_pressed)
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.add_controller(motion)
        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY)
        drop.set_preload(True)
        drop.connect("enter", self._on_bin_enter)
        drop.connect("motion", self._on_bin_motion)
        drop.connect("leave", self._on_bin_leave)
        drop.connect("drop", self._on_bin_drop)
        self.add_controller(drop)

    def _recompute_span(self) -> None:
        ends = [0.0]
        for c in self.vclips:
            d = self._clip_src_dur(c, "v")
            ends.append(c.start + max(c.out_s, d))
            ends.append(c.start + c.out_s)
        for c in self.aclips:
            d = self._clip_src_dur(c, "a")
            ends.append(c.start + max(c.out_s, d))
            ends.append(c.start + c.out_s)
        self.duration = max(ends)
        self.set_sensitive(
            self.duration > 0.04
            or bool(self.vclips or self.aclips)
            or self.video_dur > 0.04
            or bool(self.src_durs)
            or (self.audio_kind == "replace" and self.audio_dur > 0.04)
        )
        self._sync_canvas()

    def set_duration(self, duration: float) -> None:
        self.video_dur = max(0.0, float(duration))
        if self.out_s <= 0 or (self.video_dur > 0 and self.out_s > self.video_dur):
            self.out_s = self.video_dur
        self._recompute_span()
        self.queue_draw()

    def set_clips(
        self,
        *,
        video_name: str = "",
        video_dur: float = 0.0,
        video_start: float = 0.0,
        audio_name: str = "",
        audio_start: float = 0.0,
        audio_dur: float = 0.0,
        audio_in: float = 0.0,
        audio_out: float = 0.0,
        audio_kind: str = "",
        vclips: list[ClipInst] | None = None,
        aclips: list[ClipInst] | None = None,
        sel_v: int = 0,
        sel_a: int = 0,
        src_durs: dict[str, float] | None = None,
        clip_names: dict[str, str] | None = None,
    ) -> None:
        self.video_name = video_name
        self.video_dur = max(0.0, float(video_dur))
        self.audio_name = audio_name
        self.audio_dur = max(0.0, float(audio_dur))
        self.audio_kind = audio_kind
        self.src_durs = dict(src_durs or {})
        self.clip_names = dict(clip_names or {})
        if vclips is not None:
            self.vclips = [c.copy() for c in vclips]
        elif video_dur > 0:
            self.vclips = [
                ClipInst(start=float(video_start), in_s=self.in_s, out_s=self.out_s or video_dur)
            ]
        else:
            self.vclips = []
        if aclips is not None:
            self.aclips = [c.copy() for c in aclips]
        elif audio_dur > 0:
            aout = float(audio_out) if audio_out else audio_dur
            self.aclips = [
                ClipInst(start=float(audio_start), in_s=max(0.0, float(audio_in)), out_s=aout)
            ]
        else:
            self.aclips = []
        self.sel_v = sel_v if self.vclips else -1
        self.sel_a = sel_a if self.aclips else -1
        self._mirror_sel()
        self._recompute_span()
        self.queue_draw()

    def _mirror_sel(self) -> None:
        if 0 <= self.sel_v < len(self.vclips):
            c = self.vclips[self.sel_v]
            self.video_start = c.start
            self.in_s = c.in_s
            self.out_s = c.out_s
        if 0 <= self.sel_a < len(self.aclips):
            c = self.aclips[self.sel_a]
            self.audio_start = c.start
            self.audio_in = c.in_s
            self.audio_out = c.out_s

    def set_range(self, in_s: float, out_s: float) -> None:
        self.in_s = max(0.0, float(in_s))
        self.out_s = max(self.in_s, float(out_s))
        if 0 <= self.sel_v < len(self.vclips):
            self.vclips[self.sel_v].in_s = self.in_s
            self.vclips[self.sel_v].out_s = self.out_s
        self._recompute_span()
        self.queue_draw()

    def set_playhead(self, t: float) -> None:
        if self._drag_mode:
            return
        self.playhead = max(0.0, float(t))
        span = self._map_span()
        if span > 0:
            self.playhead = min(self.playhead, span)
        self.queue_draw()

    def _inner(self, width: float) -> tuple[float, float]:
        left = self._GUTTER
        inner = max(1.0, float(width) - left - self._PAD_RIGHT)
        return left, inner

    def _trail_px(self, inner: float) -> float:
        return min(self._TRAIL_PX, max(64.0, inner * 0.28))

    def _viewport_width(self) -> float:
        w = float(self.get_width() or 0)
        p = self.get_parent()
        for _ in range(4):
            if p is None:
                break
            if isinstance(p, Gtk.ScrolledWindow):
                w = float(p.get_width() or w)
                break
            p = p.get_parent()
        return max(w, 200.0)

    def _desired_width(self) -> int:
        vp = self._viewport_width()
        content = max(self.duration, 0.0)
        trail_px = self._TRAIL_PX
        inner_vp = max(1.0, vp - self._GUTTER - self._PAD_RIGHT)
        if content <= 0.04:
            return int(vp)
        content_px = max(1.0, inner_vp - trail_px)
        fit_pps = content_px / content
        pps = max(self._MIN_PPS, fit_pps)
        width = int(self._GUTTER + content * pps + trail_px + self._PAD_RIGHT)
        return max(width, int(vp))

    def _sync_canvas(self) -> None:
        w = self._desired_width()
        cur = int(self.get_size_request()[0] or 0)
        if cur != w:
            self.set_size_request(w, 94)
            self.set_content_width(w)

    def _on_resize(self, _area: Gtk.DrawingArea, _width: int, _height: int) -> None:
        self._sync_canvas()

    def _map_span(self) -> float:
        if self._drag_mode and self._drag_mode != "seek" and self._drag_span > 0:
            return self._drag_span
        content = max(self.duration, 0.0)
        w = max(1.0, float(self.get_width()))
        _left, inner = self._inner(w)
        trail_px = self._trail_px(inner)
        if content <= 0.04:
            return self._TRAIL_MIN_S
        content_px = max(1.0, inner - trail_px)
        return content * inner / content_px

    def _clip_src_dur(self, c: ClipInst, lane: str = "v") -> float:
        if c.media_id and c.media_id in self.src_durs:
            return self.src_durs[c.media_id]
        return self.video_dur if lane == "v" else self.audio_dur

    def _clip_used(self, c: ClipInst, src_dur: float) -> tuple[float, float]:
        inn = max(0.0, c.in_s)
        out = c.out_s if c.out_s > inn else src_dur
        if src_dur > 0:
            out = min(out, src_dur)
        return inn, max(inn, out)

    def _clip_times(self, c: ClipInst, src_dur: float) -> tuple[float, float]:
        inn, out = self._clip_used(c, src_dur)
        return c.start + inn, c.start + out

    def _video_used(self) -> tuple[float, float]:
        c = self._vclip()
        if c is None:
            return 0.0, 0.0
        return self._clip_used(c, self._clip_src_dur(c, "v"))

    def _audio_used(self) -> tuple[float, float]:
        c = self._aclip()
        if c is None:
            return 0.0, 0.0
        return self._clip_used(c, self._clip_src_dur(c, "a"))

    def _vclip(self, idx: int | None = None) -> ClipInst | None:
        i = self._drag_index if idx is None and self._drag_mode.startswith("video") else idx
        if i is None:
            i = self.sel_v
        if 0 <= i < len(self.vclips):
            return self.vclips[i]
        return None

    def _aclip(self, idx: int | None = None) -> ClipInst | None:
        i = self._drag_index if idx is None and self._drag_mode.startswith("audio") else idx
        if i is None:
            i = self.sel_a
        if 0 <= i < len(self.aclips):
            return self.aclips[i]
        return None

    def _x_to_t(self, x: float) -> float:
        left, inner = self._inner(max(1.0, float(self.get_width())))
        span = self._map_span()
        t = ((x - left) / inner) * span
        return max(0.0, min(span, t))

    def _t_to_x(self, t: float, width: float) -> float:
        left, inner = self._inner(width)
        span = self._map_span()
        if span <= 0:
            return left
        return left + (t / span) * inner

    def _in_lane(self, y: float, lane_y: float) -> bool:
        return lane_y <= y <= lane_y + self._LANE_H

    def _hit_edge(self, x: float, y: float, t0: float, t1: float, lane_y: float) -> str:
        if not self._in_lane(y, lane_y) or t1 <= t0:
            return ""
        w = max(1.0, float(self.get_width()))
        x0 = self._t_to_x(t0, w)
        x1 = self._t_to_x(t1, w)
        d0, d1 = abs(x - x0), abs(x - x1)
        if d0 <= self._EDGE and d0 <= d1:
            return "in"
        if d1 <= self._EDGE:
            return "out"
        return ""

    def _hit_clip(self, x: float, y: float, t0: float, t1: float, lane_y: float) -> bool:
        if t1 <= t0 or not self._in_lane(y, lane_y):
            return False
        w = max(1.0, float(self.get_width()))
        x0 = self._t_to_x(t0, w)
        x1 = self._t_to_x(t1, w)
        if x1 < x0:
            x0, x1 = x1, x0
        return (x0 + self._EDGE) < x < (x1 - self._EDGE) or (
            x1 - x0 <= 2 * self._EDGE and x0 - 2 <= x <= x1 + 2
        )

    def _video_times(self, idx: int | None = None) -> tuple[float, float]:
        c = self._vclip(idx)
        if c is None:
            return 0.0, 0.0
        return self._clip_times(c, self._clip_src_dur(c, "v"))

    def _audio_times(self, idx: int | None = None) -> tuple[float, float]:
        c = self._aclip(idx)
        if c is None:
            return 0.0, 0.0
        return self._clip_times(c, self._clip_src_dur(c, "a"))

    def _hit_lane_clip(
        self, x: float, y: float, clips: list[ClipInst], lane: str, lane_y: float
    ) -> tuple[int, str]:
        if not self._in_lane(y, lane_y):
            return -1, ""
        for i in range(len(clips) - 1, -1, -1):
            t0, t1 = self._clip_times(clips[i], self._clip_src_dur(clips[i], lane))
            edge = self._hit_edge(x, y, t0, t1, lane_y)
            if edge:
                return i, edge
            if self._hit_clip(x, y, t0, t1, lane_y):
                return i, "body"
        return -1, ""

    def _hit_video(self, x: float, y: float) -> bool:
        i, part = self._hit_lane_clip(x, y, self.vclips, "v", self._RULER_H)
        return i >= 0 and part == "body"

    def _hit_audio(self, x: float, y: float) -> bool:
        if self.audio_kind == "source":
            return False
        a_y = self._RULER_H + self._LANE_H + self._LANE_GAP
        i, part = self._hit_lane_clip(x, y, self.aclips, "a", a_y)
        return i >= 0 and part == "body"

    def _hit_video_edge(self, x: float, y: float) -> str:
        i, part = self._hit_lane_clip(x, y, self.vclips, "v", self._RULER_H)
        return part if part in ("in", "out") else ""

    def _hit_audio_edge(self, x: float, y: float) -> str:
        if self.audio_kind == "source":
            return ""
        a_y = self._RULER_H + self._LANE_H + self._LANE_GAP
        i, part = self._hit_lane_clip(x, y, self.aclips, "a", a_y)
        return part if part in ("in", "out") else ""

    def _seek_x(self, x: float) -> None:
        t = self._x_to_t(x)
        self.playhead = t
        self.queue_draw()
        if callable(self.on_seek):
            self.on_seek(t)

    def _select_hit(self, x: float, y: float) -> bool:
        a_y = self._RULER_H + self._LANE_H + self._LANE_GAP
        v_y = self._RULER_H
        if self._in_lane(y, a_y):
            ai, _ap = self._hit_lane_clip(x, y, self.aclips, "a", a_y)
            if ai >= 0:
                self.sel_a = ai
                self._mirror_sel()
                if callable(self.on_select):
                    self.on_select("audio", ai)
                return True
        if self._in_lane(y, v_y):
            vi, _vp = self._hit_lane_clip(x, y, self.vclips, "v", v_y)
            if vi >= 0:
                self.sel_v = vi
                self._mirror_sel()
                if callable(self.on_select):
                    self.on_select("video", vi)
                return True
        vi, _vp = self._hit_lane_clip(x, y, self.vclips, "v", v_y)
        if vi >= 0:
            self.sel_v = vi
            self._mirror_sel()
            if callable(self.on_select):
                self.on_select("video", vi)
            return True
        ai, _ap = self._hit_lane_clip(x, y, self.aclips, "a", a_y)
        if ai >= 0:
            self.sel_a = ai
            self._mirror_sel()
            if callable(self.on_select):
                self.on_select("audio", ai)
            return True
        return False

    def _on_pressed(self, _g: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        if self._select_hit(x, y):
            return
        self._seek_x(x)

    def _on_motion(self, _c: Gtk.EventControllerMotion, x: float, y: float) -> None:
        if self._drag_mode in ("video-in", "video-out", "audio-in", "audio-out"):
            self.set_cursor_from_name("ew-resize")
        elif self._drag_mode in ("video", "audio"):
            self.set_cursor_from_name("grabbing")
        elif self._hit_video_edge(x, y) or self._hit_audio_edge(x, y):
            self.set_cursor_from_name("ew-resize")
        elif self._hit_video(x, y) or self._hit_audio(x, y):
            self.set_cursor_from_name("grab")
        else:
            self.set_cursor_from_name("col-resize")

    def _on_drag_begin(self, gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        ok, ox, oy = gesture.get_start_point()
        if not ok:
            return
        _left, inner = self._inner(max(1.0, float(self.get_width())))
        self._drag_inner = inner
        self._drag_span = max(self._map_span(), 0.01)
        vi, vp = self._hit_lane_clip(ox, oy, self.vclips, "v", self._RULER_H)
        a_y = self._RULER_H + self._LANE_H + self._LANE_GAP
        ai, ap = (-1, "")
        if self.audio_kind != "source":
            ai, ap = self._hit_lane_clip(ox, oy, self.aclips, "a", a_y)
        if vp in ("in", "out", "body"):
            self.sel_v = vi
            self._drag_index = vi
            c = self.vclips[vi]
            self._mirror_sel()
            if callable(self.on_select):
                self.on_select("video", vi)
            if vp == "in":
                self._drag_mode = "video-in"
                self._drag_v0 = c.in_s
                self.set_cursor_from_name("ew-resize")
            elif vp == "out":
                self._drag_mode = "video-out"
                self._drag_v0 = c.out_s
                self.set_cursor_from_name("ew-resize")
            else:
                self._drag_mode = "video"
                self._drag_v0 = c.start
                self.set_cursor_from_name("grabbing")
        elif ap in ("in", "out", "body"):
            self.sel_a = ai
            self._drag_index = ai
            c = self.aclips[ai]
            self._mirror_sel()
            if callable(self.on_select):
                self.on_select("audio", ai)
            if ap == "in":
                self._drag_mode = "audio-in"
                self._drag_v0 = c.in_s
                self.set_cursor_from_name("ew-resize")
            elif ap == "out":
                self._drag_mode = "audio-out"
                self._drag_v0 = c.out_s if c.out_s > 0 else self.audio_dur
                self.set_cursor_from_name("ew-resize")
            else:
                self._drag_mode = "audio"
                self._drag_v0 = c.start
                self.set_cursor_from_name("grabbing")
        else:
            self._drag_mode = "seek"
            self._drag_index = -1
            self._seek_x(ox)

    def _dt(self, dx: float) -> float:
        return dx / self._drag_inner * self._drag_span

    def _snap_thresh(self) -> float:
        inner = max(self._drag_inner, 1.0)
        return max(0.04, self._SNAP_PX / inner * self._map_span())

    def _other_edges(self, which: str) -> list[float]:
        skip = self._drag_index
        times: list[float] = []
        for i, c in enumerate(self.vclips):
            if which == "video" and i == skip:
                continue
            t0, t1 = self._clip_times(c, self._clip_src_dur(c, "v"))
            times += [t0, t1]
        if self.audio_kind != "source":
            for i, c in enumerate(self.aclips):
                if which == "audio" and i == skip:
                    continue
                t0, t1 = self._clip_times(c, self._clip_src_dur(c, "a"))
                times += [t0, t1]
        times.append(self.playhead)
        return times

    def _snap_time(self, t: float, targets: list[float]) -> float:
        self._snap_line = None
        if not targets:
            return t
        thresh = self._snap_thresh()
        best: float | None = None
        best_d = thresh
        for tgt in targets:
            d = abs(t - tgt)
            if d <= best_d:
                best = tgt
                best_d = d
        if best is None:
            return t
        self._snap_line = best
        return best

    def _snap_move(
        self, start: float, used_in: float, used_out: float, targets: list[float]
    ) -> float:
        self._snap_line = None
        if not targets:
            return start
        thresh = self._snap_thresh()
        best = start
        best_d = thresh
        line: float | None = None
        for tgt in targets:
            for off in (used_in, used_out):
                cand = tgt - off
                d = abs(start - cand)
                if d <= best_d:
                    best = cand
                    best_d = d
                    line = tgt
        if line is None:
            return start
        self._snap_line = line
        return max(-used_in, best)

    def _on_drag_update(self, gesture: Gtk.GestureDrag, dx: float, _dy: float) -> None:
        dt = self._dt(dx)
        if self._drag_mode == "video-in":
            c = self._vclip()
            if c is None:
                return
            _inn, out = self._clip_used(c, self._clip_src_dur(c, "v"))
            raw = min(max(0.0, self._drag_v0 + dt), out - self._MIN)
            snapped = self._snap_time(c.start + raw, self._other_edges("video"))
            c.in_s = min(max(0.0, snapped - c.start), out - self._MIN)
            self.in_s = c.in_s
            if abs((c.start + c.in_s) - snapped) > 1e-4:
                self._snap_line = None
            if callable(self.on_video_trim):
                self.on_video_trim(self._drag_index, c.in_s, out, False)
            self.queue_draw()
            return
        if self._drag_mode == "video-out":
            c = self._vclip()
            if c is None:
                return
            inn, _out = self._clip_used(c, self._clip_src_dur(c, "v"))
            top = self._clip_src_dur(c, "v") or self._drag_v0 + dt
            raw = max(inn + self._MIN, min(top, self._drag_v0 + dt))
            snapped = self._snap_time(c.start + raw, self._other_edges("video"))
            c.out_s = max(inn + self._MIN, min(top, snapped - c.start))
            self.out_s = c.out_s
            if abs((c.start + c.out_s) - snapped) > 1e-4:
                self._snap_line = None
            if callable(self.on_video_trim):
                self.on_video_trim(self._drag_index, inn, c.out_s, False)
            self.queue_draw()
            return
        if self._drag_mode == "audio-in":
            c = self._aclip()
            if c is None:
                return
            _ain, aout = self._clip_used(c, self._clip_src_dur(c, "a"))
            raw = min(max(0.0, self._drag_v0 + dt), aout - self._MIN)
            snapped = self._snap_time(c.start + raw, self._other_edges("audio"))
            c.in_s = min(max(0.0, snapped - c.start), aout - self._MIN)
            self.audio_in = c.in_s
            if abs((c.start + c.in_s) - snapped) > 1e-4:
                self._snap_line = None
            if callable(self.on_audio_trim):
                self.on_audio_trim(self._drag_index, c.in_s, aout, False)
            self.queue_draw()
            return
        if self._drag_mode == "audio-out":
            c = self._aclip()
            if c is None:
                return
            ain, _aout = self._clip_used(c, self._clip_src_dur(c, "a"))
            top = self._clip_src_dur(c, "a") or self._drag_v0 + dt
            raw = max(ain + self._MIN, min(top, self._drag_v0 + dt))
            snapped = self._snap_time(c.start + raw, self._other_edges("audio"))
            c.out_s = max(ain + self._MIN, min(top, snapped - c.start))
            self.audio_out = c.out_s
            if abs((c.start + c.out_s) - snapped) > 1e-4:
                self._snap_line = None
            if callable(self.on_audio_trim):
                self.on_audio_trim(self._drag_index, ain, c.out_s, False)
            self.queue_draw()
            return
        if self._drag_mode in ("video", "audio"):
            if self._drag_mode == "video":
                c = self._vclip()
                if c is None:
                    return
                inn, out = self._clip_used(c, self._clip_src_dur(c, "v"))
                start = max(-inn, self._drag_v0 + dt)
                start = self._snap_move(start, inn, out, self._other_edges("video"))
                start = max(-inn, start)
                c.start = start
                self.video_start = start
                if callable(self.on_video_move):
                    self.on_video_move(self._drag_index, start, False)
            else:
                c = self._aclip()
                if c is None:
                    return
                inn, out = self._clip_used(c, self._clip_src_dur(c, "a"))
                start = max(-inn, self._drag_v0 + dt)
                start = self._snap_move(start, inn, out, self._other_edges("audio"))
                start = max(-inn, start)
                c.start = start
                self.audio_start = start
                if callable(self.on_audio_move):
                    self.on_audio_move(self._drag_index, start, False)
            self.queue_draw()
            return
        ok, ox, _oy = gesture.get_start_point()
        if ok:
            self._seek_x(ox + dx)

    def _on_drag_end(self, *_args: object) -> None:
        mode = self._drag_mode
        self._drag_mode = ""
        self._snap_line = None
        self._recompute_span()
        idx = self._drag_index
        self._drag_index = -1
        if mode == "video":
            self.set_cursor_from_name("grab")
            c = self._vclip(idx)
            if c is not None and callable(self.on_video_move):
                self.on_video_move(idx, c.start, True)
        elif mode == "audio":
            self.set_cursor_from_name("grab")
            c = self._aclip(idx)
            if c is not None and callable(self.on_audio_move):
                self.on_audio_move(idx, c.start, True)
        elif mode in ("video-in", "video-out"):
            self.set_cursor_from_name("ew-resize")
            c = self._vclip(idx)
            if c is not None and callable(self.on_video_trim):
                inn, out = self._clip_used(c, self._clip_src_dur(c, "v"))
                self.on_video_trim(idx, inn, out, True)
        elif mode in ("audio-in", "audio-out"):
            self.set_cursor_from_name("ew-resize")
            c = self._aclip(idx)
            if c is not None and callable(self.on_audio_trim):
                inn, out = self._clip_used(c, self._clip_src_dur(c, "a"))
                self.on_audio_trim(idx, inn, out, True)
        elif mode == "seek" and callable(self.on_seek):
            self.on_seek(self.playhead)
        self.queue_draw()

    def _parse_bin_payload(self, value: object) -> tuple[str, str]:
        text = str(value or "").strip()
        if ":" in text:
            kind, mid = text.split(":", 1)
            return kind.lower(), mid
        kind = text.lower()
        if kind in ("video", "audio"):
            return kind, ""
        return "", ""

    def _drop_kind(self, target: Gtk.DropTarget) -> str:
        try:
            val = target.get_value()
        except (GLib.Error, ValueError, TypeError):
            return ""
        kind, _mid = self._parse_bin_payload(val)
        return kind

    def _bin_action(self, target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        kind = self._drop_kind(target)
        t = self._x_to_t(x)
        v_y = self._RULER_H
        a_y = self._RULER_H + self._LANE_H + self._LANE_GAP
        hover: tuple[str, float] | None = None
        action = Gdk.DragAction(0)
        if kind == "video" and self._in_lane(y, v_y):
            hover = ("video", t)
            action = Gdk.DragAction.COPY
        elif kind == "audio" and self._in_lane(y, a_y):
            hover = ("audio", t)
            action = Gdk.DragAction.COPY
        if hover != self._drop_hover:
            self._drop_hover = hover
            self.queue_draw()
        return action

    def _on_bin_enter(self, target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        kind = self._drop_kind(target)
        if kind not in ("video", "audio"):
            return Gdk.DragAction(0)
        action = self._bin_action(target, x, y)
        if int(action):
            return action
        return Gdk.DragAction.COPY

    def _on_bin_motion(self, target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        return self._bin_action(target, x, y)

    def _on_bin_leave(self, _t: Gtk.DropTarget) -> None:
        if self._drop_hover is not None:
            self._drop_hover = None
            self.queue_draw()

    def _on_bin_drop(self, _t: Gtk.DropTarget, value: object, x: float, y: float) -> bool:
        kind, mid = self._parse_bin_payload(value)
        t = self._x_to_t(x)
        v_y = self._RULER_H
        a_y = self._RULER_H + self._LANE_H + self._LANE_GAP
        self._drop_hover = None
        self.queue_draw()
        if kind == "video" and self._in_lane(y, v_y):
            if callable(self.on_place):
                self.on_place("video", t, mid)
            return True
        if kind == "audio" and self._in_lane(y, a_y):
            if callable(self.on_place):
                self.on_place("audio", t, mid)
            return True
        return False

    def _draw_clip(
        self,
        cr,  # noqa: ANN001
        x: float,
        y: float,
        w: float,
        h: float,
        color: tuple[float, float, float],
        name: str,
        alpha: float = 1.0,
    ) -> None:
        w = max(3.0, w)
        _round_rect(cr, x, y, w, h, 4)
        cr.set_source_rgba(*color, alpha)
        cr.fill()
        if not name or w < 18:
            return
        cr.save()
        cr.rectangle(x + 4, y, max(0.0, w - 8), h)
        cr.clip()
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.92)
        cr.set_font_size(11)
        ext = cr.text_extents(name)
        cr.move_to(x + 8, y + (h + ext.height) / 2.0)
        cr.show_text(name)
        cr.restore()

    def _draw_handles(
        self,
        cr,  # noqa: ANN001
        x0: float,
        x1: float,
        y: float,
        h: float,
        color: tuple[float, float, float],
    ) -> None:
        cr.set_source_rgb(*color)
        cr.set_line_width(2)
        for x in (x0, x1):
            cr.move_to(x, y)
            cr.line_to(x, y + h)
        cr.stroke()

    def _draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:  # noqa: ANN001
        bg = theme_rgb("dark_background", (0.04, 0.05, 0.08))
        track = theme_rgb("lighter_background", (0.12, 0.14, 0.22))
        sel = theme_rgb("accent", (0.49, 0.51, 0.85))
        fg = theme_rgb("foreground", (1.0, 0.8, 0.68))
        muted = theme_rgb("muted", (0.43, 0.49, 0.71))
        green = theme_rgb("green", (0.22, 0.62, 0.45))
        cr.set_source_rgb(*bg)
        cr.paint()

        left, inner = self._inner(width)
        v_y = self._RULER_H
        a_y = self._RULER_H + self._LANE_H + self._LANE_GAP
        lanes_bottom = a_y + self._LANE_H

        _round_rect(cr, left, v_y, inner, self._LANE_H, 4)
        cr.set_source_rgb(*track)
        cr.fill()
        _round_rect(cr, left, a_y, inner, self._LANE_H, 4)
        cr.set_source_rgb(*track)
        cr.fill()

        cr.set_source_rgb(*muted)
        cr.set_font_size(11)
        cr.move_to(6, v_y + 19)
        cr.show_text("V")
        cr.move_to(6, a_y + 19)
        cr.show_text("A")

        clip_y = 3.0
        clip_h = self._LANE_H - 6
        for i, c in enumerate(self.vclips):
            d = self._clip_src_dur(c, "v")
            inn, out = self._clip_used(c, d)
            gx0 = self._t_to_x(c.start, width)
            gx1 = self._t_to_x(c.start + d, width)
            self._draw_clip(cr, gx0, v_y + clip_y, max(3.0, gx1 - gx0), clip_h, sel, "", 0.28)
            x0 = self._t_to_x(c.start + inn, width)
            x1 = self._t_to_x(c.start + out, width)
            name = self.clip_names.get(c.media_id) or (
                self.video_name if i == self.sel_v or len(self.vclips) == 1 else ""
            )
            self._draw_clip(
                cr, x0, v_y + clip_y, max(3.0, x1 - x0), clip_h, sel, name
            )
            self._draw_handles(cr, x0, x1, v_y + clip_y, clip_h, fg)
            if i == self.sel_v:
                cr.set_source_rgb(*fg)
                cr.set_line_width(1)
                cr.rectangle(x0, v_y + clip_y, max(3.0, x1 - x0), clip_h)
                cr.stroke()

        color = green if self.audio_kind != "source" else muted
        for i, c in enumerate(self.aclips):
            d = self._clip_src_dur(c, "a")
            inn, out = self._clip_used(c, d)
            gx0 = self._t_to_x(c.start, width)
            gx1 = self._t_to_x(c.start + d, width)
            self._draw_clip(cr, gx0, a_y + clip_y, max(3.0, gx1 - gx0), clip_h, color, "", 0.28)
            x0 = self._t_to_x(c.start + inn, width)
            x1 = self._t_to_x(c.start + out, width)
            name = self.clip_names.get(c.media_id) or (
                self.audio_name if i == self.sel_a or len(self.aclips) == 1 else ""
            )
            self._draw_clip(
                cr, x0, a_y + clip_y, max(3.0, x1 - x0), clip_h, color, name
            )
            if self.audio_kind != "source":
                self._draw_handles(cr, x0, x1, a_y + clip_y, clip_h, fg)
            if i == self.sel_a:
                cr.set_source_rgb(*fg)
                cr.set_line_width(1)
                cr.rectangle(x0, a_y + clip_y, max(3.0, x1 - x0), clip_h)
                cr.stroke()

        if self._drop_hover is not None:
            kind, t = self._drop_hover
            if kind == "video" and self.video_dur > 0:
                gx0 = self._t_to_x(t, width)
                gx1 = self._t_to_x(t + self.video_dur, width)
                self._draw_clip(
                    cr, gx0, v_y + clip_y, max(3.0, gx1 - gx0), clip_h, sel, "", 0.5
                )
            elif kind == "audio" and self.audio_dur > 0:
                gx0 = self._t_to_x(t, width)
                gx1 = self._t_to_x(t + self.audio_dur, width)
                self._draw_clip(
                    cr, gx0, a_y + clip_y, max(3.0, gx1 - gx0), clip_h, green, "", 0.5
                )

        if 0 <= self.sel_v < len(self.vclips):
            c = self.vclips[self.sel_v]
            inn, out = self._clip_used(c, self._clip_src_dur(c, "v"))
            if out > inn:
                x_in = self._t_to_x(c.start + inn, width)
                x_out = self._t_to_x(c.start + out, width)
                cr.set_source_rgb(*fg)
                cr.set_line_width(1)
                cr.move_to(x_in, v_y - 2)
                cr.line_to(x_in, lanes_bottom + 2)
                cr.move_to(x_out, v_y - 2)
                cr.line_to(x_out, lanes_bottom + 2)
                cr.stroke()

        cr.set_source_rgb(*muted)
        cr.set_line_width(1)
        cr.set_font_size(10)
        span = self._map_span()
        if span > 0:
            step = span
            for cand in (0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 60.0, 120.0, 300.0):
                if span / cand <= 8:
                    step = cand
                    break
            t = 0.0
            while t <= span + 0.0001:
                tx = self._t_to_x(t, width)
                cr.set_source_rgb(*muted)
                cr.move_to(tx, 2)
                cr.line_to(tx, 8)
                cr.stroke()
                label = f"{t:.0f}s" if step >= 1 else f"{t:.1f}s"
                cr.move_to(tx + 2, 12)
                cr.show_text(label)
                t += step

        if self._snap_line is not None:
            sx = self._t_to_x(self._snap_line, width)
            cr.set_source_rgba(*sel, 0.95)
            cr.set_line_width(1.5)
            cr.move_to(sx, v_y - 2)
            cr.line_to(sx, lanes_bottom + 2)
            cr.stroke()

        px = self._t_to_x(self.playhead, width)
        cr.set_source_rgb(*fg)
        cr.set_line_width(2)
        cr.move_to(px, 2)
        cr.line_to(px, lanes_bottom + 2)
        cr.stroke()
        cr.move_to(px, 2)
        cr.line_to(px - 6, 12)
        cr.line_to(px + 6, 12)
        cr.close_path()
        cr.fill()

        cr.set_source_rgb(*muted)
        cr.set_font_size(11)
        cr.move_to(left, height - 3)
        cr.show_text(f"{self.playhead:.2f}s")
        if span > 0:
            label = f"{span:.2f}s"
            ext = cr.text_extents(label)
            cr.move_to(width - self._PAD_RIGHT - ext.width, height - 3)
            cr.show_text(label)


class MediaCard(Gtk.Box):
    """One video or audio slot in the lower-right media box."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_css_class("card")
        self.set_margin_start(4)
        self.set_margin_end(4)
        self.set_margin_top(2)
        self.set_margin_bottom(2)
        self._kind = "empty"
        self._payload = ""
        drag = Gtk.DragSource()
        drag.set_actions(Gdk.DragAction.COPY)
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("prepare", self._drag_prepare)
        drag.connect("drag-begin", self._drag_begin)
        self.add_controller(drag)
        self._swatch = Gtk.DrawingArea()
        self._swatch.set_content_width(5)
        self._swatch.set_hexpand(False)
        self._swatch.set_draw_func(self._draw_swatch)
        self.append(self._swatch)
        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        self.picture.set_size_request(72, 48)
        self.picture.set_can_shrink(True)
        self.append(self.picture)
        self.icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        self.icon.set_pixel_size(28)
        self.icon.set_valign(Gtk.Align.CENTER)
        self.append(self.icon)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.set_hexpand(True)
        texts.set_valign(Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0)
        self.title.set_wrap(True)
        self.title.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.title.set_max_width_chars(22)
        self.meta = Gtk.Label(xalign=0)
        self.meta.add_css_class("dim-label")
        self.meta.set_wrap(True)
        self.meta.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.meta.set_max_width_chars(22)
        texts.append(self.title)
        texts.append(self.meta)
        self.append(texts)
        self.set_empty("none")

    def _drag_prepare(self, _s: Gtk.DragSource, _x: float, _y: float) -> Gdk.ContentProvider | None:
        if not self._payload:
            return None
        typed = Gdk.ContentProvider.new_for_value(self._payload)
        raw = Gdk.ContentProvider.new_for_bytes(
            "text/plain", GLib.Bytes.new(self._payload.encode("utf-8"))
        )
        return Gdk.ContentProvider.new_union([typed, raw])

    def _drag_begin(self, source: Gtk.DragSource, _drag: Gdk.Drag) -> None:
        source.set_icon(Gtk.WidgetPaintable.new(self), 16, 16)

    def _draw_swatch(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:  # noqa: ANN001
        keys = {
            "video": ("accent", (0.49, 0.51, 0.85)),
            "audio": ("green", (0.22, 0.62, 0.45)),
            "source": ("muted", (0.43, 0.49, 0.71)),
            "empty": ("muted", (0.43, 0.49, 0.71)),
        }
        name, fb = keys.get(self._kind, keys["empty"])
        cr.set_source_rgb(*theme_rgb(name, fb))
        cr.rectangle(0, 0, width, height)
        cr.fill()

    def set_empty(self, text: str) -> None:
        self._kind = "empty"
        self._payload = ""
        self.title.set_text(text)
        self.meta.set_text("")
        self.picture.set_paintable(None)
        self.picture.set_visible(False)
        self.icon.set_visible(False)
        self._swatch.queue_draw()
        self.set_tooltip_text("")
        self.set_cursor_from_name(None)

    def set_item(
        self,
        *,
        kind: str,
        title: str,
        meta: str,
        pixbuf: GdkPixbuf.Pixbuf | None = None,
        tooltip: str = "",
        media_id: str = "",
    ) -> None:
        self._kind = kind
        if kind in ("video", "audio") and media_id:
            self._payload = f"{kind}:{media_id}"
        elif kind in ("video", "audio"):
            self._payload = kind
        else:
            self._payload = ""
        self.title.set_text(title)
        self.meta.set_text(meta)
        if pixbuf is not None:
            if pixbuf.get_width() > 96:
                scale = 96 / pixbuf.get_width()
                pixbuf = pixbuf.scale_simple(
                    96,
                    max(1, int(pixbuf.get_height() * scale)),
                    GdkPixbuf.InterpType.BILINEAR,
                )
            self.picture.set_paintable(Gdk.Texture.new_for_pixbuf(pixbuf))
            self.picture.set_visible(True)
            self.icon.set_visible(False)
        else:
            self.picture.set_paintable(None)
            self.picture.set_visible(False)
            self.icon.set_visible(kind in ("audio", "source"))
        hint = ""
        if kind == "video":
            hint = "Drag onto the V track to place a copy"
        elif kind == "audio":
            hint = "Drag onto the A track to place a copy"
        self.set_tooltip_text("\n".join(p for p in (tooltip, hint) if p))
        self.set_cursor_from_name("grab" if self._payload else None)
        self._swatch.queue_draw()


class EditorWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.set_title("Clip editor")
        self.set_default_size(1100, 760)

        self.video_path: Path | None = None
        self.audio_path: Path | None = None
        self.video_info: dict | None = None
        self.audio_info: dict | None = None
        self.media: list[MediaItem] = []
        self.media_info: dict[str, dict] = {}
        self.media_thumbs: dict[str, GdkPixbuf.Pixbuf] = {}
        self._media_bin_ids: list[str] = []
        self._vmedia_path: Path | None = None
        self.aspect = "9:16"
        self.audio_fit = False
        self.use_video_soundtrack = True
        self.crossfade_s = 0.0
        self.video_start = 0.0
        self.audio_start = 0.0
        self.audio_in = 0.0
        self.audio_out: float | None = None
        self.video_clips: list[ClipInst] = []
        self.audio_clips: list[ClipInst] = []
        self.sel_v = -1
        self.sel_a = -1
        self.sel_kind = ""
        self._clip_playing = False
        self._audio_pending = False
        self.project_path: Path | None = None
        self._loading = False
        self._autosave_src: int = 0
        self.exporting = False
        self.playing = False
        self._vmedia: Gtk.MediaFile | None = None
        self._preview_proc: subprocess.Popen[bytes] | None = None
        self._play_t0 = 0.0
        self._play_mono = 0.0
        self._syncing_scrub = False
        self._seek_audio_src: int = 0
        self._tick: int | None = None
        self._prep_handler: int = 0
        self._space_held = False
        self._history: list[Project] = []
        self._hist_i = -1
        self._ckpt_src: int = 0
        self._applying_history = False
        self._undo_action: Gio.SimpleAction | None = None
        self._redo_action: Gio.SimpleAction | None = None

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        file_menu = Gio.Menu()
        file_menu.append("New", "win.new-project")
        file_menu.append("Open project…", "win.open-project")
        file_menu.append("Save", "win.save")
        file_menu.append("Save As…", "win.save-as")
        edit_menu = Gio.Menu()
        edit_menu.append("Undo", "win.undo")
        edit_menu.append("Redo", "win.redo")
        menu = Gio.Menu()
        menu.append_section(None, file_menu)
        menu.append_section(None, edit_menu)
        mb = Gtk.MenuButton(icon_name="open-menu-symbolic")
        mb.set_menu_model(menu)
        mb.set_tooltip_text("Project")
        header.pack_start(mb)
        toolbar.add_top_bar(header)
        self._install_project_actions()

        root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        root.set_margin_top(16)
        root.set_margin_bottom(16)
        root.set_margin_start(16)
        root.set_margin_end(16)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_hexpand(True)
        self.aspect_frame = Gtk.AspectFrame(ratio=9 / 16, obey_child=False)
        self.aspect_frame.set_hexpand(True)
        self.aspect_frame.set_vexpand(True)
        self.preview = CoverPreview()
        self.preview.on_pan = self._refresh_crop
        self.preview.on_pan_end = self._checkpoint
        self.aspect_frame.set_child(self.preview)
        left.append(self.aspect_frame)

        self.crop_label = self._wrapping_label("")
        self.crop_label.add_css_class("dim-label")
        left.append(self.crop_label)

        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_play = Gtk.Button(label="Play")
        self.btn_play.set_sensitive(False)
        self.btn_play.set_tooltip_text("Play / pause (Space)")
        self.btn_play.connect("clicked", self._on_play)
        transport.append(self.btn_play)
        self.btn_split = Gtk.Button(label="Split")
        self.btn_split.set_tooltip_text("Split the selected clip at the playhead (T)")
        self.btn_split.connect("clicked", lambda *_: self._split_selected_clip())
        transport.append(self.btn_split)
        self.clock = Gtk.Label(label="0.00 / 0.00")
        self.clock.add_css_class("dim-label")
        transport.append(self.clock)
        left.append(transport)
        self.timeline = Timeline()
        self.timeline.on_seek = self._on_timeline_seek
        self.timeline.on_video_move = self._on_video_move
        self.timeline.on_audio_move = self._on_audio_move
        self.timeline.on_video_trim = self._on_video_trim
        self.timeline.on_audio_trim = self._on_audio_trim
        self.timeline.on_place = self._place_clip
        self.timeline.on_select = self._on_clip_select
        self.timeline_scroll = Gtk.ScrolledWindow()
        self.timeline_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.timeline_scroll.set_hexpand(True)
        self.timeline_scroll.set_vexpand(False)
        self.timeline_scroll.set_min_content_height(94)
        self.timeline_scroll.set_child(self.timeline)
        self.timeline_scroll.connect("notify::width", lambda *_: self.timeline._sync_canvas())
        left.append(self.timeline_scroll)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        right.set_margin_end(4)

        right.append(self._section("Video"))
        row = Gtk.Box(spacing=8)
        b = Gtk.Button(label="Open video")
        b.connect("clicked", lambda *_: self._pick("video"))
        row.append(b)
        right.append(row)

        right.append(self._section("Transition"))
        transition = Gtk.Box(spacing=8)
        transition.append(Gtk.Label(label="Cross-fade"))
        self.crossfade_spin = Gtk.SpinButton.new_with_range(0.0, 3.0, 0.1)
        self.crossfade_spin.set_digits(1)
        self.crossfade_spin.set_value(0.0)
        self.crossfade_spin.set_tooltip_text(
            "Dissolve between adjacent touching clips; 0 disables it"
        )
        self.crossfade_spin.connect("value-changed", self._on_crossfade_changed)
        transition.append(self.crossfade_spin)
        transition.append(Gtk.Label(label="seconds"))
        right.append(transition)
        self.video_label = self._wrapping_label("none")
        right.append(self.video_label)

        right.append(self._section("Audio"))
        row = Gtk.Box(spacing=8)
        b = Gtk.Button(label="Open audio")
        b.connect("clicked", lambda *_: self._pick("audio"))
        row.append(b)
        self.btn_fit = Gtk.Button(label="Fit")
        self.btn_fit.set_sensitive(False)
        self.btn_fit.set_tooltip_text("Cut the music to the video length")
        self.btn_fit.connect("clicked", self._on_fit)
        row.append(self.btn_fit)
        self.btn_clear_audio = Gtk.Button(label="Clear")
        self.btn_clear_audio.set_sensitive(False)
        self.btn_clear_audio.connect("clicked", self._on_clear_audio)
        row.append(self.btn_clear_audio)
        right.append(row)
        self.audio_label = self._wrapping_label(
            "none — keeps the video’s audio if it has one"
        )
        right.append(self.audio_label)
        self.follow_in = Gtk.CheckButton(label="Audio follows video in-point")
        self.follow_in.connect("toggled", self._on_follow_in)
        right.append(self.follow_in)

        right.append(self._section("Aspect"))
        aspects = Gtk.Box(spacing=6)
        self.aspect_buttons: dict[str, Gtk.ToggleButton] = {}
        group = None
        for name in ASPECTS:
            tb = Gtk.ToggleButton(label=name)
            if group is None:
                group = tb
            else:
                tb.set_group(group)
            if name == "9:16":
                tb.set_active(True)
            tb.connect("toggled", self._on_aspect, name)
            self.aspect_buttons[name] = tb
            aspects.append(tb)
        right.append(aspects)

        right.append(self._section("Trim"))
        trim = Gtk.Box(spacing=8)
        trim.append(Gtk.Label(label="In"))
        self.in_spin = Gtk.SpinButton.new_with_range(0, 99999, 0.05)
        self.in_spin.set_digits(2)
        self.in_spin.connect("value-changed", lambda *_: self._refresh_fit())
        trim.append(self.in_spin)
        trim.append(Gtk.Label(label="Out"))
        self.out_spin = Gtk.SpinButton.new_with_range(0, 99999, 0.05)
        self.out_spin.set_digits(2)
        self.out_spin.connect("value-changed", lambda *_: self._refresh_fit())
        trim.append(self.out_spin)
        right.append(trim)
        row = Gtk.Box(spacing=8)
        b = Gtk.Button(label="Set in at playhead")
        b.connect("clicked", self._set_in)
        row.append(b)
        b = Gtk.Button(label="Set out at playhead")
        b.connect("clicked", self._set_out)
        row.append(b)
        right.append(row)

        right.append(self._section("Export"))
        self.export_name = self._wrapping_label("name is assigned on export (.mp4)")
        self.export_name.add_css_class("dim-label")
        right.append(self.export_name)
        self.btn_export = Gtk.Button(label="Export")
        self.btn_export.add_css_class("suggested-action")
        self.btn_export.set_sensitive(False)
        self.btn_export.connect("clicked", self._on_export)
        right.append(self.btn_export)
        self.progress = Gtk.ProgressBar()
        right.append(self.progress)
        self.status = self._wrapping_label("")
        self.status.add_css_class("dim-label")
        right.append(self.status)

        right.append(self._section("Media"))
        self.media_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        right.append(self.media_list)
        self._install_drop(self.media_list)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_width(True)
        scroller.set_min_content_width(320)
        scroller.set_hexpand(False)
        scroller.set_hexpand_set(True)
        scroller.set_child(right)

        root.append(left)
        root.append(scroller)
        toolbar.set_content(root)
        self.set_content(toolbar)
        self.connect("close-request", self._on_close)

        # Hyprland+Nautilus prefers MOVE; COPY-only targets reject the drop.
        self._install_drop(self)
        self._install_drop(toolbar)
        self._install_drop(root)
        self._install_drop(self.preview)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key_pressed)
        keys.connect("key-released", self._on_key_released)
        self.add_controller(keys)
        self._open_from_cli = False
        self._refresh_media()
        GLib.idle_add(self._restore_autosave)

    def _install_project_actions(self) -> None:
        for name, handler in (
            ("new-project", self._on_new_project),
            ("open-project", self._on_open_project),
            ("save", self._on_save),
            ("save-as", self._on_save_as),
            ("play-pause", self._on_play),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
        self._undo_action = Gio.SimpleAction.new("undo", None)
        self._undo_action.connect("activate", self._on_undo)
        self._undo_action.set_enabled(False)
        self.add_action(self._undo_action)
        self._redo_action = Gio.SimpleAction.new("redo", None)
        self._redo_action.connect("activate", self._on_redo)
        self._redo_action.set_enabled(False)
        self.add_action(self._redo_action)

    def _on_key_pressed(self, _c: Gtk.EventControllerKey, keyval: int, _code: int, state: int) -> bool:
        mods = state & Gtk.accelerator_get_default_mod_mask()
        ctrl = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(mods & Gdk.ModifierType.SHIFT_MASK)
        extra = mods & ~(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        if extra:
            return False
        if ctrl and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            if shift:
                self._on_redo()
            else:
                self._on_undo()
            return True
        if ctrl and not shift and keyval in (Gdk.KEY_y, Gdk.KEY_Y):
            self._on_redo()
            return True
        if mods:
            return False
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            return self._delete_selected_clip()
        if keyval in (Gdk.KEY_t, Gdk.KEY_T):
            self._split_selected_clip()
            return True
        if keyval not in (Gdk.KEY_space, Gdk.KEY_KP_Space):
            return False
        if self._space_held:
            return True
        self._space_held = True
        self._on_play()
        return True

    def _on_key_released(self, _c: Gtk.EventControllerKey, keyval: int, _code: int, _state: int) -> bool:
        if keyval in (Gdk.KEY_space, Gdk.KEY_KP_Space):
            self._space_held = False
        return False

    def _snapshot_key(self, proj: Project | None = None) -> tuple:
        p = proj if proj is not None else self._current_project()
        out = None if p.out_s is None else round(float(p.out_s), 3)
        return (
            str(p.video) if p.video else "",
            str(p.audio) if p.audio else "",
            p.aspect,
            round(float(p.pan_x), 4),
            round(float(p.pan_y), 4),
            round(float(p.in_s), 3),
            out,
            round(float(p.video_start), 3),
            round(float(p.audio_start), 3),
            round(float(p.audio_in), 3),
            None if p.audio_out is None else round(float(p.audio_out), 3),
            tuple(
                (round(c.start, 3), round(c.in_s, 3), round(c.out_s, 3), c.media_id)
                for c in p.video_clips
            ),
            tuple(
                (round(c.start, 3), round(c.in_s, 3), round(c.out_s, 3), c.media_id)
                for c in p.audio_clips
            ),
            tuple((m.id, m.kind, str(m.path)) for m in p.media),
            bool(p.audio_follows_in),
            bool(p.audio_fit),
            bool(p.use_video_soundtrack),
            round(float(p.crossfade_s), 3),
            str(p.path) if p.path else "",
        )

    def _update_history_actions(self) -> None:
        if self._undo_action is not None:
            self._undo_action.set_enabled(self._hist_i > 0)
        if self._redo_action is not None:
            self._redo_action.set_enabled(
                self._hist_i >= 0 and self._hist_i < len(self._history) - 1
            )

    def _checkpoint(self, *_args: object) -> None:
        if self._loading or self._applying_history:
            return
        if self._ckpt_src:
            GLib.source_remove(self._ckpt_src)
            self._ckpt_src = 0
        snap = self._current_project()
        key = self._snapshot_key(snap)
        if (
            self._history
            and 0 <= self._hist_i < len(self._history)
            and self._snapshot_key(self._history[self._hist_i]) == key
        ):
            self._update_history_actions()
            return
        self._history = self._history[: self._hist_i + 1]
        self._history.append(snap)
        if len(self._history) > HISTORY_LIMIT:
            extra = len(self._history) - HISTORY_LIMIT
            self._history = self._history[extra:]
        self._hist_i = len(self._history) - 1
        self._update_history_actions()

    def _schedule_checkpoint(self) -> None:
        if self._loading or self._applying_history:
            return
        if self._ckpt_src:
            GLib.source_remove(self._ckpt_src)
        self._ckpt_src = GLib.timeout_add(400, self._checkpoint_timeout)

    def _checkpoint_timeout(self) -> bool:
        self._ckpt_src = 0
        self._checkpoint()
        return False

    def _flush_checkpoint(self) -> None:
        if self._ckpt_src:
            GLib.source_remove(self._ckpt_src)
            self._ckpt_src = 0
            self._checkpoint()

    def _on_undo(self, *_args: object) -> None:
        if self.exporting or self._applying_history:
            return
        self._flush_checkpoint()
        if self._hist_i <= 0:
            self._set_status("Nothing to undo")
            return
        self._hist_i -= 1
        self._applying_history = True
        try:
            self._apply_project(self._history[self._hist_i])
        finally:
            self._applying_history = False
        self._update_history_actions()
        self._schedule_autosave()
        self._set_status("Undo")

    def _on_redo(self, *_args: object) -> None:
        if self.exporting or self._applying_history:
            return
        self._flush_checkpoint()
        if self._hist_i < 0 or self._hist_i >= len(self._history) - 1:
            self._set_status("Nothing to redo")
            return
        self._hist_i += 1
        self._applying_history = True
        try:
            self._apply_project(self._history[self._hist_i])
        finally:
            self._applying_history = False
        self._update_history_actions()
        self._schedule_autosave()
        self._set_status("Redo")

    def _current_project(self) -> Project:
        return Project(
            video=self.video_path,
            audio=self.audio_path,
            aspect=self.aspect,
            pan_x=self.preview.pan_x,
            pan_y=self.preview.pan_y,
            in_s=self.in_spin.get_value(),
            out_s=self.out_spin.get_value(),
            video_start=self.video_start,
            audio_start=self.audio_start,
            audio_in=self.audio_in,
            audio_out=self.audio_out,
            media=[m.copy() for m in self.media],
            video_clips=[c.copy() for c in self.video_clips],
            audio_clips=[c.copy() for c in self.audio_clips],
            audio_follows_in=self.follow_in.get_active(),
            audio_fit=self.audio_fit,
            use_video_soundtrack=self.use_video_soundtrack,
            crossfade_s=self.crossfade_s,
            path=self.project_path,
        )

    def _update_title(self) -> None:
        if self.project_path:
            self.set_title(f"Clip editor — {self.project_path.name}")
        else:
            self.set_title("Clip editor")

    def _schedule_autosave(self) -> None:
        if self._loading:
            return
        if self._autosave_src:
            GLib.source_remove(self._autosave_src)
            self._autosave_src = 0
        self._autosave_src = GLib.timeout_add(800, self._autosave_now)

    def _autosave_now(self) -> bool:
        self._autosave_src = 0
        proj = self._current_project()
        if proj.video is None and proj.audio is None:
            return False
        try:
            write_autosave(proj)
            if self.project_path is not None:
                write_project(self.project_path, proj)
        except OSError as exc:
            self._set_status(f"Auto-save failed: {exc}")
        return False

    def _flush_autosave(self) -> None:
        if self._autosave_src:
            GLib.source_remove(self._autosave_src)
            self._autosave_src = 0
        self._autosave_now()

    def _unload_video(self) -> None:
        self._dispose_media()
        self.video_path = None
        self.video_info = None
        self.preview.set_pixbuf(None)
        self.preview.set_media(None)
        self.video_label.set_text("none")
        self.btn_play.set_sensitive(False)
        self.btn_export.set_sensitive(False)
        self.clock.set_text("0.00 / 0.00")
        self.crop_label.set_text("")
        self.export_name.set_text("name is assigned on export (.mp4)")
        self.export_name.set_tooltip_text("")
        self.video_clips = []
        self.sel_v = -1
        if self.sel_kind == "video":
            self.sel_kind = "audio" if self.audio_clips else ""
        self._refresh_media()

    def _unload_audio(self) -> None:
        self.audio_path = None
        self.audio_info = None
        self.audio_fit = False
        self.btn_clear_audio.set_sensitive(False)
        self.btn_fit.set_sensitive(False)
        self.btn_fit.set_label("Fit")
        self.audio_label.set_text("none — keeps the video’s audio if it has one")
        self.audio_start = 0.0
        self.audio_in = 0.0
        self.audio_out = None
        self.audio_clips = []
        self.sel_a = -1
        if self.sel_kind == "audio":
            self.sel_kind = "video" if self.video_clips else ""
        self._refresh_media()

    def _install_media_list(self, items: list[MediaItem]) -> None:
        self.media = []
        self.media_info = {}
        self.media_thumbs = {}
        self._media_bin_ids = []
        for src in items:
            if not src.path.is_file():
                self._set_status(f"Missing {src.path}")
                continue
            try:
                info = probe(src.path)
            except ProbeError:
                self._set_status(f"Could not read {src.path.name}")
                continue
            self.media.append(src.copy())
            self.media_info[src.id] = info
            if src.kind == "video":
                try:
                    self.media_thumbs[src.id] = _load_frame(src.path)
                except (ProbeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    pass

    def _apply_project(self, proj: Project) -> None:
        self._loading = True
        self._stop()
        try:
            self._install_media_list(proj.media)
            if not self.media:
                self._unload_video()
                self._unload_audio()
            if proj.aspect in self.aspect_buttons:
                self.aspect_buttons[proj.aspect].set_active(True)
                self.aspect = proj.aspect
                dw, dh = dest_size(proj.aspect)
                self.aspect_frame.set_ratio(dw / dh)
            self.preview.pan_x = min(1.0, max(0.0, proj.pan_x))
            self.preview.pan_y = min(1.0, max(0.0, proj.pan_y))
            self.preview.queue_draw()
            self.in_spin.set_value(proj.in_s)
            if proj.out_s is not None:
                self.out_spin.set_value(proj.out_s)
            else:
                dur = float((self.video_info or {}).get("duration") or 0)
                self.out_spin.set_value(dur)
            self.follow_in.set_active(proj.audio_follows_in)
            self.audio_fit = proj.audio_fit
            self.use_video_soundtrack = proj.use_video_soundtrack
            self.crossfade_s = max(0.0, float(proj.crossfade_s or 0.0))
            self.crossfade_spin.set_value(self.crossfade_s)
            self.video_start = float(proj.video_start or 0.0)
            self.audio_start = float(proj.audio_start or 0.0)
            self.audio_in = max(0.0, float(proj.audio_in or 0.0))
            self.audio_out = proj.audio_out
            self.video_clips = [c.copy() for c in proj.video_clips]
            self.audio_clips = [c.copy() for c in proj.audio_clips]
            if self.video_path and not self.video_clips:
                dur = float((self.video_info or {}).get("duration") or 0)
                vid = next((m.id for m in self.media if m.kind == "video"), "")
                self.video_clips = [
                    ClipInst(
                        start=self.video_start,
                        in_s=self.in_spin.get_value(),
                        out_s=self.out_spin.get_value() or dur,
                        media_id=vid,
                    )
                ]
            if self.audio_path and not self.audio_clips:
                dur = float((self.audio_info or {}).get("duration") or 0)
                aud = next((m.id for m in self.media if m.kind == "audio"), "")
                self.audio_clips = [
                    ClipInst(
                        start=self.audio_start,
                        in_s=self.audio_in,
                        out_s=self.audio_out or dur,
                        media_id=aud,
                    )
                ]
            self.sel_v = 0 if self.video_clips else -1
            self.sel_a = 0 if self.audio_clips else -1
            if self.video_clips:
                self.sel_kind = "video"
            elif self.audio_clips:
                self.sel_kind = "audio"
            else:
                self.sel_kind = ""
            self.project_path = proj.path
            self._sync_primary_from_selection()
            self._refresh_fit()
            self._update_title()
        finally:
            self._loading = False

    def _load_project_file(self, path: Path) -> None:
        try:
            proj = read_project(path)
        except ProjectError as exc:
            self._set_status(str(exc))
            return
        self._checkpoint()
        self.project_path = path
        self._apply_project(proj)
        self.project_path = path
        self._update_title()
        self._checkpoint()
        self._schedule_autosave()
        self._set_status(f"Opened {path.name}")

    def _restore_autosave(self) -> bool:
        if getattr(self, "_open_from_cli", False):
            return False
        proj = read_autosave()
        if proj is not None and (proj.media or proj.video is not None or proj.audio is not None):
            self._apply_project(proj)
            if self.video_path or self.audio_path:
                self._set_status("Restored last session")
        self._checkpoint()
        return False

    def _project_dialog_filters(self) -> Gio.ListStore:
        filt = Gtk.FileFilter()
        filt.set_name("Clip editor project")
        filt.add_pattern("*.clip.json")
        allf = Gtk.FileFilter()
        allf.set_name("All files")
        allf.add_pattern("*")
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(filt)
        store.append(allf)
        return store

    def _clear_session(self) -> None:
        self._stop()
        self.media = []
        self.media_info = {}
        self.media_thumbs = {}
        self._media_bin_ids = []
        self._unload_video()
        self._unload_audio()
        self.project_path = None
        self.aspect = "9:16"
        dw, dh = dest_size("9:16")
        self.aspect_frame.set_ratio(dw / dh)
        nine = self.aspect_buttons.get("9:16")
        if nine is not None and not nine.get_active():
            nine.set_active(True)
        self.preview.pan_x = 0.5
        self.preview.pan_y = 0.5
        self.preview.queue_draw()
        self.in_spin.set_value(0)
        self.out_spin.set_value(0)
        self.follow_in.set_active(False)
        self.use_video_soundtrack = True
        self.crossfade_s = 0.0
        self.crossfade_spin.set_value(0.0)
        self.video_start = 0.0
        self.audio_start = 0.0
        self.audio_in = 0.0
        self.audio_out = None
        self.video_clips = []
        self.audio_clips = []
        self.sel_v = -1
        self.sel_a = -1
        self.sel_kind = ""
        self.btn_play.set_label("Play")
        self.progress.set_fraction(0)
        self.timeline.set_clips()
        self.timeline.set_playhead(0)
        self.timeline.set_range(0.0, 0.0)
        self.timeline.set_duration(0.0)
        self.preview.set_blank(False)
        self._refresh_media()

    def _on_new_project(self, *_args: object) -> None:
        if self.exporting:
            return
        self._checkpoint()
        self._loading = True
        try:
            if self.project_path is not None:
                self._flush_autosave()
            elif self._autosave_src:
                GLib.source_remove(self._autosave_src)
                self._autosave_src = 0
            self._clear_session()
            clear_autosave()
        finally:
            self._loading = False
        self._update_title()
        self._refresh_fit()
        self._checkpoint()
        self._set_status("New project")

    def _on_open_project(self, *_args: object) -> None:
        dialog = Gtk.FileDialog(title="Open project")
        dialog.set_filters(self._project_dialog_filters())
        filters = dialog.get_filters()
        if filters is not None:
            first = filters.get_item(0)
            if first is not None:
                dialog.set_default_filter(first)

        def done(d: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                f = d.open_finish(result)
            except GLib.Error:
                return
            path = Path(f.get_path() or "")
            if path.is_file():
                self._load_project_file(path)

        dialog.open(self, None, done)

    def _on_save(self, *_args: object) -> None:
        if self.project_path is None:
            self._on_save_as()
            return
        try:
            write_project(self.project_path, self._current_project())
            write_autosave(self._current_project())
            self._update_title()
            self._set_status(f"Saved {self.project_path.name}")
        except OSError as exc:
            self._set_status(f"Save failed: {exc}")

    def _on_save_as(self, *_args: object) -> None:
        dialog = Gtk.FileDialog(title="Save project as")
        dialog.set_filters(self._project_dialog_filters())
        stem = "untitled"
        if self.video_path:
            stem = self.video_path.stem
            dialog.set_initial_folder(Gio.File.new_for_path(str(self.video_path.parent)))
        elif self.project_path:
            stem = self.project_path.name.removesuffix(".clip.json")
            dialog.set_initial_folder(Gio.File.new_for_path(str(self.project_path.parent)))
        dialog.set_initial_name(stem + ".clip.json")

        def done(d: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                f = d.save_finish(result)
            except GLib.Error:
                return
            path = Path(f.get_path() or "")
            if not path.name:
                return
            try:
                written = write_project(path, self._current_project())
            except OSError as exc:
                self._set_status(f"Save failed: {exc}")
                return
            self.project_path = written
            self._update_title()
            self._checkpoint()
            self._schedule_autosave()
            self._set_status(f"Saved {written.name}")

        dialog.save(self, None, done)

    @staticmethod
    def _wrapping_label(text: str) -> Gtk.Label:
        # wrap=True alone only breaks on spaces; paths have none, so the
        # sidebar grows. WORD_CHAR + a small width_chars keeps the panel
        # at 320px and wraps at slashes and underscores.
        lab = Gtk.Label(label=text, xalign=0)
        lab.set_wrap(True)
        lab.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lab.set_hexpand(True)
        lab.set_width_chars(24)
        lab.set_max_width_chars(36)
        lab.set_selectable(True)
        return lab

    def _section(self, title: str) -> Gtk.Label:
        lab = Gtk.Label(label=title, xalign=0)
        lab.add_css_class("heading")
        return lab

    def _media_by_id(self, mid: str) -> MediaItem | None:
        if not mid:
            return None
        for m in self.media:
            if m.id == mid:
                return m
        return None

    def _media_for_path(self, path: Path, kind: str | None = None) -> MediaItem | None:
        for m in self.media:
            if kind is not None and m.kind != kind:
                continue
            if _same_path(m.path, path):
                return m
        return None

    def _media_dur(self, mid: str) -> float:
        info = self.media_info.get(mid) or {}
        return float(info.get("duration") or 0)

    def _src_durs(self) -> dict[str, float]:
        return {m.id: self._media_dur(m.id) for m in self.media}

    def _clip_names(self) -> dict[str, str]:
        return {m.id: m.path.name for m in self.media}

    def _clip_item(self, c: ClipInst, kind: str = "") -> MediaItem | None:
        item = self._media_by_id(c.media_id)
        if item is not None:
            return item
        if kind:
            return next((m for m in self.media if m.kind == kind), None)
        return None

    def _bind_video(self, mid: str) -> None:
        item = self._media_by_id(mid)
        if item is None or item.kind != "video":
            return
        self.video_path = item.path
        self.video_info = self.media_info.get(mid)
        self.preview.set_blank(False)
        thumb = self.media_thumbs.get(mid)
        if thumb is not None:
            self.preview.set_pixbuf(thumb)
        self._load_media(item.path)
        dur = self._media_dur(mid)
        self.video_label.set_text(
            f"{item.path.name}\n"
            f"{(self.video_info or {}).get('width', '?')}×"
            f"{(self.video_info or {}).get('height', '?')} · {dur:.2f}s"
        )
        self.btn_play.set_sensitive(True)

    def _bind_audio(self, mid: str) -> None:
        item = self._media_by_id(mid)
        if item is None or item.kind != "audio":
            return
        self.audio_path = item.path
        self.audio_info = self.media_info.get(mid)
        self.btn_clear_audio.set_sensitive(True)
        dur = self._media_dur(mid)
        self.audio_label.set_text(f"{item.path.name} · {dur:.2f}s")

    def _sync_primary_from_selection(self) -> None:
        bound_v = False
        if 0 <= self.sel_v < len(self.video_clips):
            item = self._clip_item(self.video_clips[self.sel_v], "video")
            if item is not None and item.kind == "video":
                self._bind_video(item.id)
                bound_v = True
        if not bound_v:
            vid = next((m for m in self.media if m.kind == "video"), None)
            if vid is not None:
                self._bind_video(vid.id)
            else:
                self.video_path = None
                self.video_info = None
        bound_a = False
        if 0 <= self.sel_a < len(self.audio_clips):
            item = self._clip_item(self.audio_clips[self.sel_a], "audio")
            if item is not None and item.kind == "audio":
                self._bind_audio(item.id)
                bound_a = True
        if not bound_a:
            aud = next((m for m in self.media if m.kind == "audio"), None)
            if aud is not None:
                self._bind_audio(aud.id)
            else:
                self.audio_path = None
                self.audio_info = None
                self.btn_clear_audio.set_sensitive(False)

    def _set_status(self, text: str) -> None:
        self.status.set_text(text)

    def _edit_dur(self) -> float:
        return max(0.0, self.out_spin.get_value() - self.in_spin.get_value())

    def _audio_start(self) -> float:
        return self.in_spin.get_value() if self.follow_in.get_active() else 0.0

    def _audio_usable(self) -> float:
        if not self.audio_info:
            return 0.0
        return max(0.0, float(self.audio_info["duration"]) - self._audio_start())

    def _program_end(self) -> float:
        ends = [0.05]
        for c in self.video_clips:
            _inn, out = c.used()
            if out <= _inn:
                out = float((self.video_info or {}).get("duration") or 0)
            ends.append(c.start + out)
        for c in self.audio_clips:
            ends.append(c.start + c.out_s)
        return max(ends)

    def _timeline_now(self) -> float:
        if self.playing:
            return self._play_t0 + (time.monotonic() - self._play_mono)
        return self.timeline.playhead

    def _playhead(self) -> float:
        if self.playing and self._clip_playing and self._vmedia is not None and self._vmedia.is_prepared():
            ts = self._vmedia.get_timestamp()
            if ts >= 0:
                return ts / 1_000_000.0
        src = self._timeline_now() - self.video_start
        dur = float((self.video_info or {}).get("duration") or 0)
        if dur > 0:
            return min(max(0.0, src), dur)
        return max(0.0, src)

    def _install_drop(self, widget: Gtk.Widget) -> None:
        actions = Gdk.DragAction.COPY | Gdk.DragAction.MOVE
        dt = Gtk.DropTarget.new(Gdk.FileList, actions)
        dt.set_gtypes([Gdk.FileList, Gio.File])
        dt.set_preload(True)
        dt.connect("enter", self._on_drop_enter)
        dt.connect("drop", self._on_drop)
        widget.add_controller(dt)

    def _on_drop_enter(self, *_args: object) -> Gdk.DragAction:
        return Gdk.DragAction.COPY

    def _on_close(self, *_args: object) -> bool:
        self._stop()
        if self._ckpt_src:
            GLib.source_remove(self._ckpt_src)
            self._ckpt_src = 0
        self._flush_autosave()
        return False

    def _refresh_crop(self) -> None:
        if not self.video_info:
            self.crop_label.set_text("")
            return
        dw, dh = dest_size(self.aspect)
        crop = cover_crop(
            int(self.video_info["width"]),
            int(self.video_info["height"]),
            dw,
            dh,
            self.preview.pan_x,
            self.preview.pan_y,
        )
        self.crop_label.set_text(
            f"crop {crop.w}×{crop.h} at {crop.x},{crop.y}  →  {dw}×{dh}"
        )
        self._refresh_export_name()
        self._schedule_autosave()

    def _refresh_export_name(self) -> None:
        if not self.video_path:
            self.export_name.set_text("name is assigned on export (.mp4)")
            return
        path = default_out_path(self.video_path, self.aspect)
        self.export_name.set_text(f"{path.parent.name}/{path.name}")
        self.export_name.set_tooltip_text(str(path))

    def _sync_timeline_clips(self) -> None:
        vname = self.video_path.name if self.video_path else ""
        vdur = float((self.video_info or {}).get("duration") or 0)
        if self.follow_in.get_active() and self.audio_clips and self.video_clips:
            vs = self.video_clips[self.sel_v] if 0 <= self.sel_v < len(self.video_clips) else self.video_clips[0]
            if 0 <= self.sel_a < len(self.audio_clips):
                self.audio_clips[self.sel_a].start = vs.start
                self.audio_start = vs.start
        source_aclips: list[ClipInst] = []
        if self.audio_clips:
            aname = self.audio_path.name if self.audio_path else "audio"
            adur = float((self.audio_info or {}).get("duration") or 0)
            for c in self.audio_clips:
                d = self._media_dur(c.media_id) or adur
                if c.out_s <= c.in_s and d > 0:
                    c.out_s = d
            kind = "replace"
        elif self.use_video_soundtrack and self.video_info and self.video_info.get("has_audio"):
            aname = "video soundtrack"
            adur = vdur
            kind = "source"
            source_aclips = [c.copy() for c in self.video_clips]
        else:
            aname = ""
            adur = 0.0
            kind = ""
        if 0 <= self.sel_v < len(self.video_clips):
            c = self.video_clips[self.sel_v]
            self.video_start = c.start
        if 0 <= self.sel_a < len(self.audio_clips):
            c = self.audio_clips[self.sel_a]
            self.audio_start = c.start
            self.audio_in = c.in_s
            self.audio_out = c.out_s
        self.timeline.set_clips(
            video_name=vname,
            video_dur=vdur,
            video_start=self.video_start,
            audio_name=aname,
            audio_start=self.audio_start,
            audio_dur=adur,
            audio_in=self.audio_in if kind == "replace" else 0.0,
            audio_out=(
                float(self.audio_out)
                if kind == "replace" and self.audio_out is not None
                else adur
            ),
            audio_kind=kind,
            vclips=self.video_clips,
            aclips=self.audio_clips if kind == "replace" else source_aclips,
            sel_v=self.sel_v,
            sel_a=self.sel_a,
            src_durs=self._src_durs(),
            clip_names=self._clip_names(),
        )
        if 0 <= self.sel_v < len(self.video_clips):
            c = self.video_clips[self.sel_v]
            self.timeline.set_range(c.in_s, c.out_s)
        self.btn_export.set_sensitive(bool(self.video_clips) and not self.exporting)
        self._refresh_media()

    def _refresh_media(self) -> None:
        ids = [m.id for m in self.media]
        if (
            ids
            and ids == self._media_bin_ids
            and self.media_list.get_first_child() is not None
        ):
            return
        self._media_bin_ids = ids
        child = self.media_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.media_list.remove(child)
            child = nxt
        if not self.media:
            lab = Gtk.Label(label="none — drop files or Shift+E from Eagle", xalign=0)
            lab.add_css_class("dim-label")
            lab.set_wrap(True)
            self.media_list.append(lab)
            return
        for m in self.media:
            card = MediaCard()
            info = self.media_info.get(m.id) or {}
            dur = float(info.get("duration") or 0)
            if m.kind == "video":
                meta = f"{info.get('width') or '?'}×{info.get('height') or '?'} · {dur:.2f}s"
                card.set_item(
                    kind="video",
                    title=m.path.name,
                    meta=meta,
                    pixbuf=self.media_thumbs.get(m.id),
                    tooltip=str(m.path),
                    media_id=m.id,
                )
            else:
                card.set_item(
                    kind="audio",
                    title=m.path.name,
                    meta=f"{dur:.2f}s",
                    tooltip=str(m.path),
                    media_id=m.id,
                )
            self.media_list.append(card)

    def _refresh_fit(self) -> None:
        if 0 <= self.sel_v < len(self.video_clips) and not self._loading:
            self.video_clips[self.sel_v].in_s = self.in_spin.get_value()
            self.video_clips[self.sel_v].out_s = self.out_spin.get_value()
        v = self._edit_dur()
        a = self._audio_usable()
        longer = bool(self.audio_path) and a > v + 0.05 and v > 0.04
        self.btn_fit.set_sensitive(bool(self.audio_path))
        self._sync_timeline_clips()
        if not self.audio_path:
            self.btn_fit.set_label("Fit")
            self._schedule_autosave()
            self._schedule_checkpoint()
            return
        name = self.audio_path.name
        dur = float(self.audio_info["duration"]) if self.audio_info else 0.0
        if self.audio_fit and longer:
            self.btn_fit.set_label(f"Fit {v:.2f}s")
            self.audio_label.set_text(f"{name} · {dur:.2f}s cut to {v:.2f}s")
        else:
            self.btn_fit.set_label("Fit")
            self.audio_label.set_text(f"{name} · {dur:.2f}s")
            if self.audio_fit and not longer:
                self.audio_fit = False
        self._refresh_crop()
        self._schedule_autosave()
        self._schedule_checkpoint()

    def _on_crossfade_changed(self, *_args: object) -> None:
        self.crossfade_s = max(0.0, float(self.crossfade_spin.get_value()))
        if not self._loading:
            self._schedule_autosave()
            self._schedule_checkpoint()

    def _pick(self, kind: str) -> None:
        dialog = Gtk.FileDialog(title="Open video" if kind == "video" else "Open audio")
        filt = Gtk.FileFilter()
        if kind == "video":
            filt.set_name("Video")
            for pat in ("*.mp4", "*.mov", "*.webm", "*.mkv", "*.m4v"):
                filt.add_pattern(pat)
        else:
            filt.set_name("Audio")
            for pat in ("*.mp3", "*.wav", "*.m4a", "*.aac", "*.ogg", "*.flac", "*.opus", "*.mp4"):
                filt.add_pattern(pat)
        allf = Gtk.FileFilter()
        allf.set_name("All files")
        allf.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt)
        filters.append(allf)
        dialog.set_filters(filters)
        dialog.set_default_filter(filt)

        def done(d: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                f = d.open_finish(result)
            except GLib.Error:
                return
            path = Path(f.get_path() or "")
            if path.is_file():
                self._open_path(kind, path)

        dialog.open(self, None, done)

    def _paths_from_drop(self, value: object) -> list[Path]:
        files: list[Gio.File] = []
        if isinstance(value, Gdk.FileList):
            files.extend(value.get_files())
        elif isinstance(value, Gio.File):
            files.append(value)
        elif isinstance(value, str):
            for line in value.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("file:"):
                    files.append(Gio.File.new_for_uri(line))
                else:
                    files.append(Gio.File.new_for_path(unquote(line)))
        out: list[Path] = []
        for g in files:
            uri = g.get_uri() or ""
            p = g.get_path()
            if not p and uri.startswith("file:"):
                p = unquote(urlparse(uri).path)
            if p:
                path = Path(p)
                if path.is_file():
                    out.append(path)
        return out

    def _on_drop(self, _t: Gtk.DropTarget, value: object, _x: float, _y: float) -> bool:
        if isinstance(value, str) and value.strip().lower() in ("video", "audio"):
            return False
        dropped = self._paths_from_drop(value)
        if not dropped:
            return False
        video_ext = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
        audio_ext = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus"}
        for p in dropped:
            if p.name.endswith(".clip.json") or self._looks_like_project(p):
                self._load_project_file(p)
                continue
            ext = p.suffix.lower()
            if ext in video_ext:
                self._open_path("video", p)
            elif ext in audio_ext:
                self._open_path("audio", p)
            elif not self.video_path:
                self._open_path("video", p)
            else:
                self._open_path("audio", p)
        return True

    def _looks_like_project(self, path: Path) -> bool:
        if path.suffix.lower() != ".json":
            return False
        try:
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(data, dict) and data.get("format") == "clip-editor-project"

    def _add_media(self, path: Path, *, place: bool | None = None) -> str | None:
        try:
            path = path.expanduser().resolve()
        except OSError:
            path = Path(path).expanduser()
        try:
            info = probe(path)
        except ProbeError as exc:
            self._set_status(str(exc))
            return None
        has_v = bool(info.get("has_video"))
        has_a = bool(info.get("has_audio"))
        if has_v:
            kind = "video"
        elif has_a:
            kind = "audio"
        else:
            self._set_status("that file has no video or audio stream")
            return None
        existing = self._media_for_path(path, kind)
        if existing is not None:
            mid = existing.id
            self.media_info[mid] = info
        else:
            mid = next_media_id(self.media)
            self.media.append(MediaItem(id=mid, path=path, kind=kind))
            self.media_info[mid] = info
            if kind == "video":
                try:
                    self.media_thumbs[mid] = _load_frame(path)
                except (ProbeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    pass
        if place is None:
            place = not self.video_clips if kind == "video" else not self.audio_clips
        if place:
            if kind == "video" and not self.video_clips:
                self.preview.pan_x = 0.5
                self.preview.pan_y = 0.5
            self._place_clip(kind, 0.0, mid)
        else:
            self._refresh_media()
            self._set_status(f"Added {path.name} to media")
            self._sync_timeline_clips()
            self._checkpoint()
            self._schedule_autosave()
        return mid

    def _open_path(self, kind: str, path: Path, *, from_project: bool = False) -> None:
        if from_project:
            self._add_media(path, place=False)
            return
        self._add_media(path)

    def _on_clear_audio(self, *_args: object) -> None:
        self.media = [m for m in self.media if m.kind != "audio"]
        self._media_bin_ids = []
        self.use_video_soundtrack = False
        self._unload_audio()
        self._refresh_fit()
        self._checkpoint()
        self._set_status("Audio cleared")

    def _on_follow_in(self, *_args: object) -> None:
        if self.follow_in.get_active() and self.audio_clips and self.video_clips:
            vs = (
                self.video_clips[self.sel_v]
                if 0 <= self.sel_v < len(self.video_clips)
                else self.video_clips[0]
            )
            if 0 <= self.sel_a < len(self.audio_clips):
                self.audio_clips[self.sel_a].start = vs.start
            self.audio_start = vs.start
        self._refresh_fit()

    def _on_fit(self, *_args: object) -> None:
        if not self.audio_path or not self.audio_clips:
            return
        v, a = self._edit_dur(), self._audio_usable()
        if a <= v + 0.05:
            self._set_status("Audio is already no longer than the video")
            return
        self.audio_fit = True
        idx = self.sel_a if 0 <= self.sel_a < len(self.audio_clips) else 0
        c = self.audio_clips[idx]
        c.out_s = c.in_s + v
        self.audio_in = c.in_s
        self.audio_out = c.out_s
        self._refresh_fit()
        self._checkpoint()
        self._set_status(f"Cut audio to {v:.2f}s (from {a:.2f}s)")

    def _on_aspect(self, btn: Gtk.ToggleButton, name: str) -> None:
        if not btn.get_active():
            return
        self.aspect = name
        w, h = dest_size(name)
        self.aspect_frame.set_ratio(w / h)
        self._refresh_crop()
        self._checkpoint()

    def _set_in(self, *_args: object) -> None:
        self.in_spin.set_value(self._playhead())
        self._refresh_fit()
        self._checkpoint()

    def _set_out(self, *_args: object) -> None:
        self.out_spin.set_value(self._playhead())
        self._refresh_fit()
        self._checkpoint()

    def _on_video_move(self, index: int, start: float, done: bool) -> None:
        if not 0 <= index < len(self.video_clips):
            return
        inn = max(0.0, self.video_clips[index].in_s)
        self.video_clips[index].start = max(-inn, float(start))
        if index == self.sel_v:
            self.video_start = self.video_clips[index].start
        if self.follow_in.get_active() and 0 <= self.sel_a < len(self.audio_clips):
            self.audio_clips[self.sel_a].start = self.video_clips[index].start
            self.audio_start = self.audio_clips[self.sel_a].start
        if not done:
            return
        self._sync_timeline_clips()
        t = self.timeline.playhead
        self.clock.set_text(f"{t:.2f} / {self._program_end():.2f}")
        self._apply_timeline_frame(t, start_media=False)
        self._checkpoint()
        self._schedule_autosave()
        self._set_status(f"Video at {self.video_clips[index].start + inn:.2f}s")

    def _on_audio_move(self, index: int, start: float, done: bool) -> None:
        if not 0 <= index < len(self.audio_clips):
            return
        inn = max(0.0, self.audio_clips[index].in_s)
        self.audio_clips[index].start = max(-inn, float(start))
        if index == self.sel_a:
            self.audio_start = self.audio_clips[index].start
        if not done:
            return
        if self.follow_in.get_active():
            self.follow_in.set_active(False)
        self._sync_timeline_clips()
        self._checkpoint()
        self._schedule_autosave()
        self._set_status(f"Audio at {self.audio_clips[index].start + inn:.2f}s")

    def _on_video_trim(self, index: int, in_s: float, out_s: float, done: bool) -> None:
        if not 0 <= index < len(self.video_clips):
            return
        self.video_clips[index].in_s = in_s
        self.video_clips[index].out_s = out_s
        if not done:
            return
        self.sel_v = index
        self.sel_kind = "video"
        self._loading = True
        try:
            self.in_spin.set_value(in_s)
            self.out_spin.set_value(out_s)
        finally:
            self._loading = False
        self._refresh_fit()
        self._checkpoint()
        self._schedule_autosave()
        self._set_status(f"Video {in_s:.2f}s–{out_s:.2f}s")

    def _on_audio_trim(self, index: int, in_s: float, out_s: float, done: bool) -> None:
        if not 0 <= index < len(self.audio_clips):
            return
        self.audio_clips[index].in_s = max(0.0, in_s)
        self.audio_clips[index].out_s = max(in_s + 0.05, out_s)
        if index == self.sel_a:
            self.audio_in = self.audio_clips[index].in_s
            self.audio_out = self.audio_clips[index].out_s
        if not done:
            return
        self.sel_a = index
        self.sel_kind = "audio"
        if self.follow_in.get_active():
            self.follow_in.set_active(False)
        self._refresh_fit()
        self._checkpoint()
        self._schedule_autosave()
        self._set_status(
            f"Audio {self.audio_clips[index].in_s:.2f}s–{self.audio_clips[index].out_s:.2f}s"
        )

    def _on_clip_select(self, kind: str, index: int) -> None:
        if kind == "video" and 0 <= index < len(self.video_clips):
            self.sel_v = index
            self.sel_kind = "video"
            c = self.video_clips[index]
            self.video_start = c.start
            item = self._clip_item(c, "video")
            if item is not None:
                self._bind_video(item.id)
            self._loading = True
            try:
                self.in_spin.set_value(c.in_s)
                self.out_spin.set_value(c.out_s)
            finally:
                self._loading = False
        elif kind == "audio":
            self.sel_kind = "audio"
            self.sel_a = index
            if 0 <= index < len(self.audio_clips):
                c = self.audio_clips[index]
                self.audio_start = c.start
                self.audio_in = c.in_s
                self.audio_out = c.out_s
                item = self._clip_item(c, "audio")
                if item is not None:
                    self._bind_audio(item.id)

    def _place_clip(self, kind: str, t: float, media_id: str = "") -> None:
        t = max(0.0, float(t))
        if kind == "video":
            item = self._media_by_id(media_id) or next(
                (m for m in self.media if m.kind == "video"), None
            )
            if item is None:
                return
            dur = self._media_dur(item.id)
            if dur <= 0.04:
                return
            self.video_clips.append(
                ClipInst(start=t, in_s=0.0, out_s=dur, media_id=item.id)
            )
            self.sel_v = len(self.video_clips) - 1
            self.sel_kind = "video"
            self._bind_video(item.id)
            self._loading = True
            try:
                self.in_spin.set_value(0)
                self.out_spin.set_value(dur)
            finally:
                self._loading = False
            self.video_start = t
            self._set_status(f"Placed {item.path.name} at {t:.2f}s")
        elif kind == "audio":
            item = self._media_by_id(media_id) or next(
                (m for m in self.media if m.kind == "audio"), None
            )
            if item is None:
                return
            dur = self._media_dur(item.id)
            if dur <= 0.04:
                return
            self.audio_clips.append(
                ClipInst(start=t, in_s=0.0, out_s=dur, media_id=item.id)
            )
            self.use_video_soundtrack = False
            self.sel_a = len(self.audio_clips) - 1
            self.sel_kind = "audio"
            self._bind_audio(item.id)
            self.audio_start = t
            self.audio_in = 0.0
            self.audio_out = dur
            if self.follow_in.get_active():
                self.follow_in.set_active(False)
            self._set_status(f"Placed {item.path.name} at {t:.2f}s")
        else:
            return
        self._sync_timeline_clips()
        self._checkpoint()
        self._schedule_autosave()

    def _selected_clip_kind(self) -> str:
        if self.sel_kind == "audio":
            if 0 <= self.sel_a < len(self.audio_clips):
                return "audio"
            if self.timeline.audio_kind == "source" and 0 <= self.sel_a < len(
                self.timeline.aclips
            ):
                return "audio"
        if self.sel_kind == "video" and 0 <= self.sel_v < len(self.video_clips):
            return "video"
        if 0 <= self.sel_v < len(self.video_clips):
            return "video"
        if 0 <= self.sel_a < len(self.audio_clips):
            return "audio"
        return ""

    def _delete_selected_clip(self) -> bool:
        if self.exporting:
            return False
        kind = self._selected_clip_kind()
        if kind == "video":
            idx = self.sel_v
            if not 0 <= idx < len(self.video_clips):
                return False
            self._stop()
            del self.video_clips[idx]
            if self.video_clips:
                self.sel_v = min(idx, len(self.video_clips) - 1)
                self.sel_kind = "video"
                c = self.video_clips[self.sel_v]
                self.video_start = c.start
                self._loading = True
                try:
                    self.in_spin.set_value(c.in_s)
                    self.out_spin.set_value(c.out_s)
                finally:
                    self._loading = False
            else:
                self.sel_v = -1
                self.video_start = 0.0
                self.sel_kind = "audio" if self.audio_clips else ""
            self._set_status("Removed video clip")
        elif kind == "audio":
            if self.timeline.audio_kind == "source":
                idx = self.sel_a
                srcs = list(self.timeline.aclips)
                if not 0 <= idx < len(srcs):
                    return False
                self._stop()
                kept = [c.copy() for i, c in enumerate(srcs) if i != idx]
                self.audio_clips = kept
                self.use_video_soundtrack = False
                if kept:
                    self.sel_a = min(idx, len(kept) - 1)
                    self.sel_kind = "audio"
                    c = self.audio_clips[self.sel_a]
                    self.audio_start = c.start
                    self.audio_in = c.in_s
                    self.audio_out = c.out_s
                    self._set_status("Removed audio clip")
                else:
                    self.sel_a = -1
                    self.sel_kind = "video" if self.video_clips else ""
                    self._set_status("Audio track empty")
            elif not self.audio_clips:
                return False
            else:
                idx = self.sel_a
                if not 0 <= idx < len(self.audio_clips):
                    return False
                self._stop()
                del self.audio_clips[idx]
                if self.audio_clips:
                    self.sel_a = min(idx, len(self.audio_clips) - 1)
                    self.sel_kind = "audio"
                    c = self.audio_clips[self.sel_a]
                    self.audio_start = c.start
                    self.audio_in = c.in_s
                    self.audio_out = c.out_s
                else:
                    self.sel_a = -1
                    self.sel_kind = "video" if self.video_clips else ""
                    self.use_video_soundtrack = False
                    if self.follow_in.get_active():
                        self.follow_in.set_active(False)
                self._set_status("Removed audio clip")
        else:
            return False
        self._sync_timeline_clips()
        t = self.timeline.playhead
        self.clock.set_text(f"{t:.2f} / {self._program_end():.2f}")
        self._apply_timeline_frame(t, start_media=False)
        self._checkpoint()
        self._schedule_autosave()
        return True

    def _split_selected_clip(self) -> bool:
        if self.exporting:
            return False
        kind = self._selected_clip_kind()
        t = self._timeline_now()
        if kind == "video":
            idx = self.sel_v
            if not 0 <= idx < len(self.video_clips):
                return False
            src_dur = float((self.video_info or {}).get("duration") or 0)
            right = self.video_clips[idx].split_at(t, src_dur)
            if right is None:
                self._set_status("Playhead is not on the selected clip")
                return False
            self.video_clips.insert(idx + 1, right)
            self.sel_v = idx + 1
            self.sel_kind = "video"
            self.video_start = right.start
            self._loading = True
            try:
                self.in_spin.set_value(right.in_s)
                self.out_spin.set_value(right.out_s)
            finally:
                self._loading = False
            self._set_status(f"Split video at {t:.2f}s")
        elif kind == "audio":
            idx = self.sel_a
            if not 0 <= idx < len(self.audio_clips):
                return False
            src_dur = float((self.audio_info or {}).get("duration") or 0)
            right = self.audio_clips[idx].split_at(t, src_dur)
            if right is None:
                self._set_status("Playhead is not on the selected clip")
                return False
            self.audio_clips.insert(idx + 1, right)
            self.sel_a = idx + 1
            self.sel_kind = "audio"
            self.audio_start = right.start
            self.audio_in = right.in_s
            self.audio_out = right.out_s
            if self.follow_in.get_active():
                self.follow_in.set_active(False)
            self._set_status(f"Split audio at {t:.2f}s")
        else:
            self._set_status("Select a clip to split")
            return False
        self._sync_timeline_clips()
        self.clock.set_text(f"{t:.2f} / {self._program_end():.2f}")
        if self.playing:
            self._video_play_clip = None
            self._audio_play_clip = None
            self._apply_timeline_frame(t, start_media=True)
            self._start_preview_audio(t)
        else:
            self._apply_timeline_frame(t, start_media=False)
        self._checkpoint()
        self._schedule_autosave()
        return True

    def _on_timeline_seek(self, value: float) -> None:
        if self._syncing_scrub:
            return
        end = self._program_end()
        self.clock.set_text(f"{value:.2f} / {end:.2f}")
        self._apply_timeline_frame(value, start_media=self.playing)
        if not self.playing:
            return
        self._play_t0 = value
        self._play_mono = time.monotonic()
        if self._seek_audio_src:
            GLib.source_remove(self._seek_audio_src)
        self._seek_audio_src = GLib.timeout_add(80, self._restart_seek_audio, value)

    def _restart_seek_audio(self, value: float) -> bool:
        self._seek_audio_src = 0
        if self.playing:
            self._start_preview_audio(value)
        return False

    def _audio_begins_at(self) -> float | None:
        starts: list[float] = []
        if self.audio_clips:
            dur = float((self.audio_info or {}).get("duration") or 0)
            for c in self.audio_clips:
                t0, _t1 = self._clip_span(c, dur)
                starts.append(t0)
        elif (
            self.use_video_soundtrack
            and self.video_path
            and self.video_info
            and self.video_info.get("has_audio")
        ):
            dur = float(self.video_info.get("duration") or 0)
            for c in self.video_clips:
                t0, _t1 = self._clip_span(c, dur)
                starts.append(t0)
        return min(starts) if starts else None

    def _source_time(self, c: ClipInst, timeline_t: float, src_dur: float) -> float:
        """Source-file time for a clip at a timeline position. Never before the in-point."""
        inn = max(0.0, float(c.in_s))
        out = float(c.out_s) if c.out_s > inn else src_dur
        if src_dur > 0:
            out = min(out, src_dur)
        src = float(timeline_t) - float(c.start)
        src = max(src, inn)
        if out > inn:
            src = min(src, out)
        if src_dur > 0:
            src = min(src, src_dur)
        return max(0.0, src)

    def _preview_audio_spec(self, timeline_t: float) -> tuple[Path, float, float] | None:
        """(path, source_start, remaining) for preview audio at a timeline time."""
        if self.audio_clips:
            ac = self._audio_at(timeline_t)
            if ac is None:
                return None
            item = self._clip_item(ac, "audio")
            if item is None:
                return None
            adur = self._media_dur(item.id)
            start = self._source_time(ac, timeline_t, adur)
            run = self._run_end(self.audio_clips, self._src_durs(), timeline_t)
            remaining = max(0.05, (run if run is not None else timeline_t) - timeline_t)
            return item.path, start, remaining
        if not self.use_video_soundtrack:
            return None
        vc = self._video_at(timeline_t)
        if vc is None:
            return None
        item = self._clip_item(vc, "video")
        info = self.media_info.get(item.id) if item is not None else self.video_info
        if not info or not info.get("has_audio"):
            return None
        path = item.path if item is not None else self.video_path
        if path is None:
            return None
        vdur = float(info.get("duration") or 0)
        start = self._source_time(vc, timeline_t, vdur)
        run = self._run_end(self.video_clips, self._src_durs(), timeline_t)
        remaining = max(0.05, (run if run is not None else timeline_t) - timeline_t)
        return path, start, remaining

    def _stop_preview_audio(self) -> None:
        proc = self._preview_proc
        self._preview_proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=0.4)
        except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    def _clip_span(self, c: ClipInst, src_dur: float | dict[str, float] = 0.0) -> tuple[float, float]:
        dur = self._media_dur(c.media_id) if c.media_id else 0.0
        if dur <= 0:
            if isinstance(src_dur, dict):
                dur = float(src_dur.get(c.media_id) or 0)
            else:
                dur = float(src_dur or 0)
        inn = max(0.0, c.in_s)
        out = c.out_s if c.out_s > inn else dur
        if dur > 0:
            out = min(out, dur)
        return c.start + inn, c.start + out

    def _continuous_with(self, prev: ClipInst | None, nxt: ClipInst | None, src_dur: float) -> bool:
        """True if nxt is the same source file playing on from prev (a split, not moved)."""
        if prev is None or nxt is None:
            return False
        if prev is nxt:
            return True
        if (prev.media_id or "") != (nxt.media_id or ""):
            return False
        if abs(float(prev.start) - float(nxt.start)) > JOIN_EPS:
            return False
        _p0, p1 = self._clip_span(prev, src_dur)
        n0, _n1 = self._clip_span(nxt, src_dur)
        return abs(p1 - n0) <= JOIN_EPS

    def _run_end(self, clips: list[ClipInst], src_dur: float, t: float) -> float | None:
        """Timeline end of the contiguous same-source run covering t."""
        covering = None
        for c in clips:
            t0, t1 = self._clip_span(c, src_dur)
            if t0 - 0.02 <= t < t1:
                covering = c
        if covering is None:
            return None
        _t0, end = self._clip_span(covering, src_dur)
        rest: list[tuple[float, float, ClipInst]] = []
        for c in clips:
            if c is covering:
                continue
            c0, c1 = self._clip_span(c, src_dur)
            if c1 > c0:
                rest.append((c0, c1, c))
        rest.sort(key=lambda row: row[0])
        start0 = covering.start
        for c0, c1, c in rest:
            if abs(float(c.start) - float(start0)) > JOIN_EPS:
                continue
            if abs(c0 - end) <= JOIN_EPS:
                end = max(end, c1)
        return end

    def _video_at(self, t: float) -> ClipInst | None:
        hit = None
        dur = float((self.video_info or {}).get("duration") or 0)
        for c in self.video_clips:
            t0, t1 = self._clip_span(c, dur)
            if t0 - 0.02 <= t < t1:
                hit = c
        return hit

    def _audio_at(self, t: float) -> ClipInst | None:
        hit = None
        dur = float((self.audio_info or {}).get("duration") or 0)
        for c in self.audio_clips:
            t0, t1 = self._clip_span(c, dur)
            if t0 - 0.02 <= t < t1:
                hit = c
        return hit

    def _apply_timeline_frame(self, timeline_t: float, *, start_media: bool) -> None:
        clip = self._video_at(timeline_t)
        if clip is None or self._vmedia is None:
            self.preview.set_blank(True)
            if self._vmedia is not None:
                self._vmedia.pause()
            self._clip_playing = False
            return
        item = self._clip_item(clip, "video")
        if item is not None:
            self._load_media(item.path)
            info = self.media_info.get(item.id) or self.video_info or {}
        else:
            info = self.video_info or {}
        self.preview.set_blank(False)
        dur = float(info.get("duration") or 0)
        source = self._source_time(clip, timeline_t, dur)
        self.preview.set_media(self._vmedia)
        if start_media:
            self._play_media_at(source)
            self._clip_playing = True
        else:
            try:
                self._vmedia.seek(int(source * 1_000_000))
            except GLib.Error:
                pass
            self.preview.queue_draw()

    def _start_preview_audio(self, timeline_t: float) -> None:
        self._stop_preview_audio()
        begins = self._audio_begins_at()
        spec = self._preview_audio_spec(timeline_t)
        if spec is None:
            if begins is not None and timeline_t < begins - 0.02:
                self._audio_pending = True
                return
            if self.audio_path and not shutil.which("ffplay") and not shutil.which("mpv"):
                self._set_status("Need ffplay or mpv to hear preview audio")
            return
        path, start, remaining = spec
        self._audio_pending = False
        log = Path.home() / ".cache" / "clip-editor" / "preview-audio.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        logf = log.open("w", encoding="utf-8")
        # mpv first: --hr-seek hits the playhead. ffplay -ss is keyframe-only
        # and often restarts at the clip origin.
        if shutil.which("mpv"):
            cmd = [
                "mpv",
                "--no-video",
                "--force-window=no",
                "--no-terminal",
                "--audio-display=no",
                "--no-resume-playback",
                "--hr-seek=always",
                f"--start={start:.3f}",
                f"--length={remaining:.3f}",
                str(path),
            ]
        elif shutil.which("ffplay"):
            cmd = [
                "ffplay",
                "-vn",
                "-nodisp",
                "-autoexit",
                "-loglevel",
                "error",
                "-nostats",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{remaining:.3f}",
                str(path),
            ]
        else:
            self._set_status("Need ffplay or mpv to hear preview audio")
            logf.close()
            return
        self._preview_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=logf,
        )
        src = self.audio_path.name if self.audio_path else "video soundtrack"
        self._set_status(f"Playing {src}")
        GLib.timeout_add(400, self._check_preview_audio, log)

    def _check_preview_audio(self, log: Path) -> bool:
        proc = self._preview_proc
        if proc is None or self.playing is False:
            return False
        code = proc.poll()
        if code is None:
            return False
        err = ""
        try:
            err = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            err = ""
        if code != 0:
            self._set_status(
                f"Audio preview failed ({code}): {err.strip()[:300] or 'no output'}"
            )
        return False

    def _dispose_media(self) -> None:
        if self._vmedia is not None and self._prep_handler:
            try:
                self._vmedia.disconnect(self._prep_handler)
            except (TypeError, RuntimeError):
                pass
            self._prep_handler = 0
        if self._vmedia is not None:
            self._vmedia.pause()
        self._vmedia = None
        self._vmedia_path = None
        self.preview.set_media(None)

    def _load_media(self, path: Path) -> None:
        if self._vmedia is not None and _same_path(self._vmedia_path, path):
            return
        self._dispose_media()
        media = Gtk.MediaFile.new_for_filename(str(path))
        media.set_loop(False)
        self._vmedia = media
        self._vmedia_path = path

    def _play_media_at(self, t: float) -> None:
        m = self._vmedia
        if m is None:
            return
        def go(*_a: object) -> bool:
            # Gtk.Picture is video-only; sound goes through ffplay/mpv.
            m.set_muted(True)
            m.set_volume(0.0)
            try:
                m.seek(int(max(0.0, t) * 1_000_000))
            except GLib.Error:
                pass
            m.play()
            return False

        if m.is_prepared():
            go()
            return
        if self._prep_handler:
            try:
                m.disconnect(self._prep_handler)
            except (TypeError, RuntimeError):
                pass
        self._prep_handler = m.connect("notify::prepared", go)
        m.play()

    def _stop(self) -> None:
        self.playing = False
        self._clip_playing = False
        self._audio_pending = False
        self.btn_play.set_label("Play")
        self._stop_preview_audio()
        if self._seek_audio_src:
            GLib.source_remove(self._seek_audio_src)
            self._seek_audio_src = 0
        if self._vmedia is not None:
            self._vmedia.pause()
        t = self.timeline.playhead
        clip = self._video_at(t)
        if clip is None or self._vmedia is None:
            self.preview.set_blank(True)
        else:
            # Keep the paused MediaFile on screen. Clearing it falls back
            # to the opening still (first frame).
            self.preview.set_blank(False)
            self.preview.set_media(self._vmedia)
            self.preview.queue_draw()
        if self._tick is not None:
            GLib.source_remove(self._tick)
            self._tick = None

    def _on_play(self, *_args: object) -> None:
        if self.exporting:
            return
        if not self.video_path and not self.audio_path:
            return
        if self.playing:
            self._stop()
            return
        end = self._program_end()
        t = self.timeline.playhead
        if t >= end - 0.04:
            t = 0.0
        self._play_t0 = t
        self._play_mono = time.monotonic()
        self._clip_playing = False
        self._audio_pending = False
        self._video_play_clip = None
        self._audio_play_clip = None
        self._syncing_scrub = True
        self.timeline.set_playhead(t)
        self._syncing_scrub = False
        self.clock.set_text(f"{t:.2f} / {end:.2f}")
        self._apply_timeline_frame(t, start_media=True)
        self._start_preview_audio(t)
        if self.audio_clips:
            self._audio_play_clip = self._audio_at(t)
        elif self.use_video_soundtrack and self.video_info and self.video_info.get("has_audio"):
            self._audio_play_clip = self._video_at(t)
        if not self.audio_clips and not (
            self.use_video_soundtrack
            and self.video_info
            and self.video_info.get("has_audio")
        ):
            self._set_status("Playing · this clip is silent — add an audio track")
        self.playing = True
        self.btn_play.set_label("Pause")
        self._tick = GLib.timeout_add(50, self._on_tick)

    def _on_tick(self) -> bool:
        t = self._timeline_now()
        end = self._program_end()
        self._syncing_scrub = True
        self.timeline.set_playhead(t)
        self._syncing_scrub = False
        self.clock.set_text(f"{t:.2f} / {end:.2f}")
        vdur = float((self.video_info or {}).get("duration") or 0)
        vclip = self._video_at(t)
        prev_v = getattr(self, "_video_play_clip", None)
        if vclip is None:
            self._video_play_clip = None
            self._apply_timeline_frame(t, start_media=False)
        elif not self._clip_playing:
            self._video_play_clip = vclip
            self._apply_timeline_frame(t, start_media=True)
        elif vclip is not prev_v:
            if self._continuous_with(prev_v, vclip, vdur):
                self._video_play_clip = vclip
                self.preview.queue_draw()
            else:
                self._video_play_clip = vclip
                self._apply_timeline_frame(t, start_media=True)
        else:
            self.preview.queue_draw()
        if self.audio_clips:
            adur = float((self.audio_info or {}).get("duration") or 0)
            aclip = self._audio_at(t)
            prev = getattr(self, "_audio_play_clip", None)
            if aclip is not prev:
                if aclip is not None and self._continuous_with(prev, aclip, adur):
                    self._audio_play_clip = aclip
                else:
                    self._audio_play_clip = aclip
                    if aclip is not None:
                        self._start_preview_audio(t)
                    else:
                        self._stop_preview_audio()
                        self._audio_pending = True
        elif self.use_video_soundtrack and self.video_info and self.video_info.get("has_audio"):
            prev = getattr(self, "_audio_play_clip", None)
            if vclip is not prev:
                if vclip is not None and self._continuous_with(prev, vclip, vdur):
                    self._audio_play_clip = vclip
                else:
                    self._audio_play_clip = vclip
                    if vclip is not None:
                        self._start_preview_audio(t)
                    else:
                        self._stop_preview_audio()
                        self._audio_pending = True
        elif self._audio_pending and self._preview_proc is None:
            begins = self._audio_begins_at()
            if begins is not None and t >= begins - 0.02:
                self._start_preview_audio(t)
        if t >= end - 0.02:
            self._stop()
            return False
        return True

    def _on_export(self, *_args: object) -> None:
        if not self.video_path or self.exporting:
            return
        if not self.video_clips:
            self._set_status("No video on the timeline")
            return
        self._stop()
        self.exporting = True
        self.btn_export.set_sensitive(False)
        self.progress.set_fraction(0)
        self._set_status("Starting export…")
        video = self.video_path
        audio = self.audio_path
        aspect = self.aspect
        pan_x, pan_y = self.preview.pan_x, self.preview.pan_y
        in_s = self.in_spin.get_value()
        out_s = self.out_spin.get_value()
        follows = self.follow_in.get_active()
        use_soundtrack = self.use_video_soundtrack
        crossfade_s = self.crossfade_s
        v_start = self.video_start
        a_start = self.audio_start
        a_in = self.audio_in
        a_out = self.audio_out
        v_clips = [c.copy() for c in self.video_clips]
        a_clips = [c.copy() for c in self.audio_clips]
        media = [m.copy() for m in self.media]
        if a_clips:
            item = self._clip_item(a_clips[0], "audio")
            audio = item.path if item is not None else audio
        else:
            audio = None
        out = default_out_path(video, aspect)

        def progress(pct: float, _state: str) -> None:
            GLib.idle_add(self.progress.set_fraction, pct)

        def work() -> None:
            try:
                result = run_export(
                    video,
                    out,
                    audio=audio,
                    aspect=aspect,
                    pan_x=pan_x,
                    pan_y=pan_y,
                    in_s=in_s,
                    out_s=out_s,
                    audio_follows_in=follows,
                    video_start=v_start,
                    audio_start=a_start,
                    audio_in=a_in,
                    audio_out=a_out,
                    video_clips=v_clips or None,
                    audio_clips=a_clips or None,
                    media=media or None,
                    use_video_soundtrack=use_soundtrack,
                    crossfade_s=crossfade_s,
                    progress=progress,
                )
                GLib.idle_add(self._export_done, result, None)
            except (ExportError, ProbeError, OSError) as exc:
                GLib.idle_add(self._export_done, None, exc)

        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, result: dict | None, err: BaseException | None) -> bool:
        self.exporting = False
        self.btn_export.set_sensitive(bool(self.video_clips))
        if err is not None:
            self.progress.set_fraction(0)
            self._set_status(str(err))
            return False
        assert result is not None
        g = result.get("gate") or {}
        self.progress.set_fraction(1)
        self._set_status(
            f"Wrote {result['out']}\n"
            f"{g.get('vcodec')} {g.get('width')}×{g.get('height')} "
            f"audio={g.get('acodec') or 'none'} {float(g.get('duration') or 0):.2f}s"
        )
        self._refresh_export_name()
        return False


class EditorApp(Adw.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.connect("activate", self._activate)
        self.connect("command-line", self._on_command_line)

    def _install_accels(self) -> None:
        self.set_accels_for_action("win.new-project", ["<Control>n"])
        self.set_accels_for_action("win.save", ["<Control>s"])
        self.set_accels_for_action("win.save-as", ["<Control><Shift>s"])
        self.set_accels_for_action("win.open-project", ["<Control>o"])
        self.set_accels_for_action("win.undo", ["<Control>z"])
        self.set_accels_for_action("win.redo", ["<Control><Shift>z", "<Control>y"])

    def _ensure_window(self) -> EditorWindow:
        apply_omarchy_theme()
        win = self.props.active_window
        if win is None:
            win = EditorWindow(application=self)
            self._install_accels()
        return win  # type: ignore[return-value]

    def _activate(self, _app: Adw.Application) -> None:
        self._ensure_window().present()

    def _on_command_line(
        self, _app: Adw.Application, cmdline: Gio.ApplicationCommandLine
    ) -> int:
        args = list(cmdline.get_arguments())
        new_project = "--new" in args
        video = _cli_flag_path(args, "--video")
        audio = _cli_flag_path(args, "--audio")
        self.activate()
        win = self.props.active_window
        if isinstance(win, EditorWindow):
            if new_project:
                # Skip autosave restore on first launch so --new is a blank project.
                win._open_from_cli = True
            if new_project or video is not None or audio is not None:
                _idle_open_cli(
                    win, video=video, audio=audio, new_project=new_project
                )
        if win is not None:
            win.present()
        return 0


def _cli_flag_path(args: list[str], flag: str) -> Path | None:
    if flag not in args:
        return None
    i = args.index(flag)
    if i + 1 >= len(args):
        return None
    return Path(args[i + 1]).expanduser()


def _idle_open_cli(
    win: EditorWindow,
    *,
    video: Path | None,
    audio: Path | None,
    new_project: bool,
) -> None:
    def go(
        _w: EditorWindow = win,
        _video: Path | None = video,
        _audio: Path | None = audio,
        _new: bool = new_project,
    ) -> bool:
        if _new:
            if _w.exporting:
                _w._set_status("export in progress")
                return False
            _w._on_new_project()
        if _video is not None:
            if _video.is_file():
                _w._open_path("video", _video)
            else:
                _w._set_status(f"missing {_video}")
        if _audio is not None:
            if _audio.is_file():
                _w._open_path("audio", _audio)
            else:
                _w._set_status(f"missing {_audio}")
        return False

    GLib.idle_add(go)


def run(
    *,
    open_video: str | Path | None = None,
    open_audio: str | Path | None = None,
    new_project: bool = False,
) -> int:
    Adw.init()
    apply_omarchy_theme()
    argv = ["clip-editor"]
    if new_project:
        argv.append("--new")
    if open_video:
        argv += ["--video", str(open_video)]
    if open_audio:
        argv += ["--audio", str(open_audio)]
    return int(EditorApp().run(argv))
