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

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Graphene, Gtk, Pango  # noqa: E402

from clip_editor.aspects import ASPECTS, cover_crop, dest_size
from clip_editor.eagle import apply_omarchy_theme, theme_rgb
from clip_editor.export import ExportError, default_out_path, run_export
from clip_editor.probe import ProbeError, probe, which_ffmpeg
from clip_editor.project import (
    Project,
    ProjectError,
    clear_autosave,
    read_autosave,
    read_project,
    write_autosave,
    write_project,
)

APP_ID = "local.clip.Editor"
HISTORY_LIMIT = 80


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
        self.playhead = 0.0
        self.on_seek = None
        self.on_video_move = None
        self.on_audio_move = None
        self.on_video_trim = None
        self.on_audio_trim = None
        self._drag_mode = ""
        self._drag_v0 = 0.0
        self._drag_span = 0.0
        self._drag_inner = 1.0
        self._snap_line: float | None = None
        self.set_hexpand(True)
        self.set_vexpand(False)
        self.set_content_width(200)
        self.set_content_height(94)
        self.set_size_request(-1, 94)
        self.set_draw_func(self._draw)
        self.set_sensitive(False)
        self.set_cursor_from_name("col-resize")
        self.set_tooltip_text(
            "Drag a clip to slide it. Drag either edge to trim. "
            "Drag the ruler or playhead to seek."
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

    def _recompute_span(self) -> None:
        self.duration = max(
            self.video_start + self.video_dur,
            self.audio_start + self.audio_dur,
            self.video_start + self.out_s,
            0.0,
        )
        self.set_sensitive(self.duration > 0.04)

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
    ) -> None:
        self.video_name = video_name
        self.video_dur = max(0.0, float(video_dur))
        self.video_start = max(0.0, float(video_start))
        self.audio_name = audio_name
        self.audio_start = max(0.0, float(audio_start))
        self.audio_dur = max(0.0, float(audio_dur))
        self.audio_in = max(0.0, float(audio_in))
        aout = float(audio_out) if audio_out else 0.0
        self.audio_out = aout if aout > self.audio_in else self.audio_dur
        self.audio_kind = audio_kind
        self._recompute_span()
        self.queue_draw()

    def set_range(self, in_s: float, out_s: float) -> None:
        self.in_s = max(0.0, float(in_s))
        self.out_s = max(self.in_s, float(out_s))
        self._recompute_span()
        if self.duration > 0:
            self.out_s = min(self.out_s, self.duration)
        self.queue_draw()

    def set_playhead(self, t: float) -> None:
        if self._drag_mode:
            return
        self.playhead = max(0.0, float(t))
        if self.duration > 0:
            self.playhead = min(self.playhead, self.duration)
        self.queue_draw()

    def _inner(self, width: float) -> tuple[float, float]:
        left = self._GUTTER
        inner = max(1.0, float(width) - left - self._PAD_RIGHT)
        return left, inner

    def _map_span(self) -> float:
        if self._drag_mode and self._drag_mode != "seek" and self._drag_span > 0:
            return self._drag_span
        return self.duration

    def _video_used(self) -> tuple[float, float]:
        inn = max(0.0, self.in_s)
        out = self.out_s if self.out_s > inn else self.video_dur
        if self.video_dur > 0:
            out = min(out, self.video_dur)
        return inn, max(inn, out)

    def _audio_used(self) -> tuple[float, float]:
        inn = max(0.0, self.audio_in)
        out = self.audio_out if self.audio_out > inn else self.audio_dur
        if self.audio_dur > 0:
            out = min(out, self.audio_dur)
        return inn, max(inn, out)

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

    def _video_times(self) -> tuple[float, float]:
        inn, out = self._video_used()
        return self.video_start + inn, self.video_start + out

    def _audio_times(self) -> tuple[float, float]:
        inn, out = self._audio_used()
        return self.audio_start + inn, self.audio_start + out

    def _hit_video(self, x: float, y: float) -> bool:
        t0, t1 = self._video_times()
        return self._hit_clip(x, y, t0, t1, self._RULER_H)

    def _hit_audio(self, x: float, y: float) -> bool:
        if self.audio_kind == "source":
            return False
        a_y = self._RULER_H + self._LANE_H + self._LANE_GAP
        t0, t1 = self._audio_times()
        return self._hit_clip(x, y, t0, t1, a_y)

    def _hit_video_edge(self, x: float, y: float) -> str:
        t0, t1 = self._video_times()
        return self._hit_edge(x, y, t0, t1, self._RULER_H)

    def _hit_audio_edge(self, x: float, y: float) -> str:
        if self.audio_kind == "source":
            return ""
        a_y = self._RULER_H + self._LANE_H + self._LANE_GAP
        t0, t1 = self._audio_times()
        return self._hit_edge(x, y, t0, t1, a_y)

    def _seek_x(self, x: float) -> None:
        t = self._x_to_t(x)
        self.playhead = t
        self.queue_draw()
        if callable(self.on_seek):
            self.on_seek(t)

    def _on_pressed(self, _g: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        if (
            self._hit_video_edge(x, y)
            or self._hit_audio_edge(x, y)
            or self._hit_video(x, y)
            or self._hit_audio(x, y)
        ):
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
        self._drag_span = max(self.duration, 0.01)
        ved = self._hit_video_edge(ox, oy)
        aed = self._hit_audio_edge(ox, oy)
        if ved == "in":
            self._drag_mode = "video-in"
            self._drag_v0 = self.in_s
            self.set_cursor_from_name("ew-resize")
        elif ved == "out":
            self._drag_mode = "video-out"
            self._drag_v0 = self.out_s
            self.set_cursor_from_name("ew-resize")
        elif aed == "in":
            self._drag_mode = "audio-in"
            self._drag_v0 = self.audio_in
            self.set_cursor_from_name("ew-resize")
        elif aed == "out":
            self._drag_mode = "audio-out"
            self._drag_v0 = self.audio_out if self.audio_out > 0 else self.audio_dur
            self.set_cursor_from_name("ew-resize")
        elif self._hit_video(ox, oy):
            self._drag_mode = "video"
            self._drag_v0 = self.video_start
            self.set_cursor_from_name("grabbing")
        elif self._hit_audio(ox, oy):
            self._drag_mode = "audio"
            self._drag_v0 = self.audio_start
            self.set_cursor_from_name("grabbing")
        else:
            self._drag_mode = "seek"
            self._seek_x(ox)

    def _dt(self, dx: float) -> float:
        return dx / self._drag_inner * self._drag_span

    def _snap_thresh(self) -> float:
        inner = max(self._drag_inner, 1.0)
        return max(0.04, self._SNAP_PX / inner * self._map_span())

    def _other_edges(self, which: str) -> list[float]:
        if which == "video":
            if self.audio_dur <= 0 or self.audio_kind == "source":
                return []
            t0, t1 = self._audio_times()
        else:
            if self.video_dur <= 0:
                return []
            t0, t1 = self._video_times()
        return [t0, t1]

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
        return max(0.0, best)

    def _on_drag_update(self, gesture: Gtk.GestureDrag, dx: float, _dy: float) -> None:
        dt = self._dt(dx)
        if self._drag_mode == "video-in":
            _inn, out = self._video_used()
            raw = min(max(0.0, self._drag_v0 + dt), out - self._MIN)
            snapped = self._snap_time(self.video_start + raw, self._other_edges("video"))
            self.in_s = min(max(0.0, snapped - self.video_start), out - self._MIN)
            if abs((self.video_start + self.in_s) - snapped) > 1e-4:
                self._snap_line = None
            if callable(self.on_video_trim):
                self.on_video_trim(self.in_s, out, False)
            self.queue_draw()
            return
        if self._drag_mode == "video-out":
            inn, _out = self._video_used()
            top = self.video_dur if self.video_dur > 0 else self._drag_v0 + dt
            raw = max(inn + self._MIN, min(top, self._drag_v0 + dt))
            snapped = self._snap_time(self.video_start + raw, self._other_edges("video"))
            self.out_s = max(inn + self._MIN, min(top, snapped - self.video_start))
            if abs((self.video_start + self.out_s) - snapped) > 1e-4:
                self._snap_line = None
            if callable(self.on_video_trim):
                self.on_video_trim(inn, self.out_s, False)
            self.queue_draw()
            return
        if self._drag_mode == "audio-in":
            _ain, aout = self._audio_used()
            raw = min(max(0.0, self._drag_v0 + dt), aout - self._MIN)
            snapped = self._snap_time(self.audio_start + raw, self._other_edges("audio"))
            self.audio_in = min(max(0.0, snapped - self.audio_start), aout - self._MIN)
            if abs((self.audio_start + self.audio_in) - snapped) > 1e-4:
                self._snap_line = None
            if callable(self.on_audio_trim):
                self.on_audio_trim(self.audio_in, aout, False)
            self.queue_draw()
            return
        if self._drag_mode == "audio-out":
            ain, _aout = self._audio_used()
            top = self.audio_dur if self.audio_dur > 0 else self._drag_v0 + dt
            raw = max(ain + self._MIN, min(top, self._drag_v0 + dt))
            snapped = self._snap_time(self.audio_start + raw, self._other_edges("audio"))
            self.audio_out = max(ain + self._MIN, min(top, snapped - self.audio_start))
            if abs((self.audio_start + self.audio_out) - snapped) > 1e-4:
                self._snap_line = None
            if callable(self.on_audio_trim):
                self.on_audio_trim(ain, self.audio_out, False)
            self.queue_draw()
            return
        if self._drag_mode in ("video", "audio"):
            start = max(0.0, self._drag_v0 + dt)
            if self._drag_mode == "video":
                inn, out = self._video_used()
                start = self._snap_move(start, inn, out, self._other_edges("video"))
                self.video_start = start
                if callable(self.on_video_move):
                    self.on_video_move(start, False)
            else:
                inn, out = self._audio_used()
                start = self._snap_move(start, inn, out, self._other_edges("audio"))
                self.audio_start = start
                if callable(self.on_audio_move):
                    self.on_audio_move(start, False)
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
        if mode == "video":
            self.set_cursor_from_name("grab")
            if callable(self.on_video_move):
                self.on_video_move(self.video_start, True)
        elif mode == "audio":
            self.set_cursor_from_name("grab")
            if callable(self.on_audio_move):
                self.on_audio_move(self.audio_start, True)
        elif mode in ("video-in", "video-out"):
            self.set_cursor_from_name("ew-resize")
            inn, out = self._video_used()
            if callable(self.on_video_trim):
                self.on_video_trim(inn, out, True)
        elif mode in ("audio-in", "audio-out"):
            self.set_cursor_from_name("ew-resize")
            inn, out = self._audio_used()
            if callable(self.on_audio_trim):
                self.on_audio_trim(inn, out, True)
        elif mode == "seek" and callable(self.on_seek):
            self.on_seek(self.playhead)
        self.queue_draw()

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
        if self.video_dur > 0:
            gx0 = self._t_to_x(self.video_start, width)
            gx1 = self._t_to_x(self.video_start + self.video_dur, width)
            self._draw_clip(cr, gx0, v_y + clip_y, max(3.0, gx1 - gx0), clip_h, sel, "", 0.28)
            inn, out = self._video_used()
            x0 = self._t_to_x(self.video_start + inn, width)
            x1 = self._t_to_x(self.video_start + out, width)
            self._draw_clip(
                cr, x0, v_y + clip_y, max(3.0, x1 - x0), clip_h, sel, self.video_name
            )
            self._draw_handles(cr, x0, x1, v_y + clip_y, clip_h, fg)

        if self.audio_dur > 0:
            color = green if self.audio_kind != "source" else muted
            gx0 = self._t_to_x(self.audio_start, width)
            gx1 = self._t_to_x(self.audio_start + self.audio_dur, width)
            self._draw_clip(cr, gx0, a_y + clip_y, max(3.0, gx1 - gx0), clip_h, color, "", 0.28)
            inn, out = self._audio_used()
            x0 = self._t_to_x(self.audio_start + inn, width)
            x1 = self._t_to_x(self.audio_start + out, width)
            self._draw_clip(
                cr, x0, a_y + clip_y, max(3.0, x1 - x0), clip_h, color, self.audio_name
            )
            if self.audio_kind != "source":
                self._draw_handles(cr, x0, x1, a_y + clip_y, clip_h, fg)

        if self.duration > 0 and self.out_s > self.in_s:
            x_in = self._t_to_x(self.video_start + self.in_s, width)
            x_out = self._t_to_x(self.video_start + self.out_s, width)
            cr.set_source_rgba(*bg, 0.55)
            if x_in > left:
                cr.rectangle(left, v_y, x_in - left, lanes_bottom - v_y)
                cr.fill()
            right_end = left + inner
            if x_out < right_end:
                cr.rectangle(x_out, v_y, right_end - x_out, lanes_bottom - v_y)
                cr.fill()
            cr.set_source_rgb(*fg)
            cr.set_line_width(2)
            cr.move_to(x_in, v_y - 2)
            cr.line_to(x_in, lanes_bottom + 2)
            cr.move_to(x_out, v_y - 2)
            cr.line_to(x_out, lanes_bottom + 2)
            cr.stroke()

        cr.set_source_rgb(*muted)
        cr.set_line_width(1)
        cr.set_font_size(10)
        if self.duration > 0:
            step = self.duration
            for cand in (0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 60.0, 120.0, 300.0):
                if self.duration / cand <= 7:
                    step = cand
                    break
            t = 0.0
            while t <= self.duration + 0.0001:
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
        if self.duration > 0:
            label = f"{self.duration:.2f}s"
            ext = cr.text_extents(label)
            cr.move_to(width - self._PAD_RIGHT - ext.width, height - 3)
            cr.show_text(label)


class EditorWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.set_title("Clip editor")
        self.set_default_size(1100, 760)

        self.video_path: Path | None = None
        self.audio_path: Path | None = None
        self.video_info: dict | None = None
        self.audio_info: dict | None = None
        self.aspect = "9:16"
        self.audio_fit = False
        self.video_start = 0.0
        self.audio_start = 0.0
        self.audio_in = 0.0
        self.audio_out: float | None = None
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
        left.append(self.timeline)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        right.set_size_request(320, -1)
        right.set_hexpand(False)
        right.set_hexpand_set(True)

        right.append(self._section("Video"))
        row = Gtk.Box(spacing=8)
        b = Gtk.Button(label="Open video")
        b.connect("clicked", lambda *_: self._pick("video"))
        row.append(b)
        right.append(row)
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

        root.append(left)
        root.append(right)
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
            bool(p.audio_follows_in),
            bool(p.audio_fit),
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
            audio_follows_in=self.follow_in.get_active(),
            audio_fit=self.audio_fit,
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

    def _apply_project(self, proj: Project) -> None:
        self._loading = True
        self._stop()
        try:
            if proj.video is not None and proj.video.is_file():
                if not _same_path(self.video_path, proj.video):
                    self._open_path("video", proj.video, from_project=True)
            else:
                if proj.video is not None:
                    self._set_status(f"Video missing: {proj.video}")
                self._unload_video()
            if proj.audio is not None and proj.audio.is_file():
                if not _same_path(self.audio_path, proj.audio):
                    self._open_path("audio", proj.audio, from_project=True)
            else:
                if proj.audio is not None:
                    self._set_status(f"Audio missing: {proj.audio}")
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
            self.video_start = max(0.0, float(proj.video_start or 0.0))
            self.audio_start = max(0.0, float(proj.audio_start or 0.0))
            self.audio_in = max(0.0, float(proj.audio_in or 0.0))
            self.audio_out = proj.audio_out
            self.project_path = proj.path
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
        if proj is not None and (proj.video is not None or proj.audio is not None):
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
        self.video_start = 0.0
        self.audio_start = 0.0
        self.audio_in = 0.0
        self.audio_out = None
        self.btn_play.set_label("Play")
        self.progress.set_fraction(0)
        self.timeline.set_clips()
        self.timeline.set_playhead(0)
        self.timeline.set_range(0.0, 0.0)
        self.timeline.set_duration(0.0)
        self.preview.set_blank(False)

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
        return max(self.video_start + self.out_spin.get_value(), 0.05)

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
        dt.set_gtypes([Gdk.FileList, Gio.File, str])
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
        if self.follow_in.get_active() and self.audio_path:
            self.audio_start = self.video_start
        if self.audio_path and self.audio_info:
            aname = self.audio_path.name
            adur = float(self.audio_info.get("duration") or 0)
            astart = self.audio_start
            if self.audio_out is None or self.audio_out <= self.audio_in:
                self.audio_out = adur
            kind = "replace"
        elif self.video_info and self.video_info.get("has_audio"):
            aname = "video soundtrack"
            adur = vdur
            astart = self.video_start
            kind = "source"
        else:
            aname = ""
            adur = 0.0
            astart = 0.0
            kind = ""
        self.timeline.set_clips(
            video_name=vname,
            video_dur=vdur,
            video_start=self.video_start,
            audio_name=aname,
            audio_start=astart,
            audio_dur=adur,
            audio_in=self.audio_in if kind == "replace" else 0.0,
            audio_out=(
                float(self.audio_out)
                if kind == "replace" and self.audio_out is not None
                else adur
            ),
            audio_kind=kind,
        )
        self.timeline.set_range(self.in_spin.get_value(), self.out_spin.get_value())

    def _refresh_fit(self) -> None:
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

    def _open_path(self, kind: str, path: Path, *, from_project: bool = False) -> None:
        try:
            info = probe(path)
        except ProbeError as exc:
            self._set_status(str(exc))
            return
        if kind == "video":
            if not info.get("has_video"):
                self._set_status("that file has no video stream")
                return
            self._stop()
            self.preview.set_blank(False)
            self.video_path = path
            self.video_info = info
            dur = float(info["duration"] or 0)
            if not from_project:
                self.in_spin.set_value(0)
                self.out_spin.set_value(dur)
                self.video_start = 0.0
            self.timeline.set_duration(dur)
            self.timeline.set_playhead(self.video_start)
            self.timeline.set_range(
                self.in_spin.get_value(),
                self.out_spin.get_value() if from_project else dur,
            )
            self.btn_play.set_sensitive(True)
            self.btn_export.set_sensitive(True)
            self.video_label.set_text(
                f"{path.name}\n{info['width']}×{info['height']} · {dur:.2f}s"
            )
            try:
                self.preview.set_pixbuf(_load_frame(path))
            except (ProbeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                self._set_status(f"opened {path.name}, no preview frame ({exc})")
            if not from_project:
                self.preview.pan_x = 0.5
                self.preview.pan_y = 0.5
                self.in_spin.set_value(0)
                self.out_spin.set_value(dur)
            self._load_media(path)
            extra = ""
            if not info.get("has_audio"):
                extra = " · this clip has no sound — add an audio track"
            self._set_status(f"Opened {path.name}{extra}")
        else:
            if not info.get("has_audio"):
                self._set_status("that file has no audio stream")
                return
            self.audio_path = path
            self.audio_info = info
            if not from_project:
                self.audio_fit = False
                self.audio_start = (
                    self.video_start if self.follow_in.get_active() else self.in_spin.get_value()
                )
                self.audio_in = 0.0
                self.audio_out = float(info.get("duration") or 0)
            self.btn_clear_audio.set_sensitive(True)
            self._set_status(f"Opened {path.name}")
            if self._audio_usable() > self._edit_dur() + 0.05:
                self._set_status(
                    f"Music is {self._audio_usable():.2f}s; video is {self._edit_dur():.2f}s. Fit cuts the extra."
                )
        self._refresh_fit()
        self._schedule_autosave()
        if not from_project:
            self._checkpoint()

    def _on_clear_audio(self, *_args: object) -> None:
        self._unload_audio()
        self._refresh_fit()
        self._checkpoint()
        self._set_status("Audio cleared")

    def _on_follow_in(self, *_args: object) -> None:
        if self.follow_in.get_active() and self.audio_path:
            self.audio_start = self.video_start
        self._refresh_fit()

    def _on_fit(self, *_args: object) -> None:
        if not self.audio_path:
            return
        v, a = self._edit_dur(), self._audio_usable()
        if a <= v + 0.05:
            self._set_status("Audio is already no longer than the video")
            return
        self.audio_fit = True
        self.audio_out = self.audio_in + v
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

    def _on_video_move(self, start: float, done: bool) -> None:
        self.video_start = max(0.0, float(start))
        if self.follow_in.get_active() and self.audio_path:
            self.audio_start = self.video_start
            self.timeline.audio_start = self.audio_start
        elif not self.audio_path:
            self.timeline.audio_start = self.video_start
        if not done:
            self.timeline.queue_draw()
            return
        self._sync_timeline_clips()
        t = self.timeline.playhead
        self.clock.set_text(f"{t:.2f} / {self._program_end():.2f}")
        self._apply_timeline_frame(t, start_media=False)
        self._checkpoint()
        self._schedule_autosave()
        self._set_status(f"Video starts at {self.video_start:.2f}s")

    def _on_audio_move(self, start: float, done: bool) -> None:
        self.audio_start = max(0.0, float(start))
        if not done:
            return
        if self.follow_in.get_active():
            self.follow_in.set_active(False)
        self._sync_timeline_clips()
        self._checkpoint()
        self._schedule_autosave()
        self._set_status(f"Audio starts at {self.audio_start:.2f}s")

    def _on_video_trim(self, in_s: float, out_s: float, done: bool) -> None:
        if not done:
            return
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

    def _on_audio_trim(self, in_s: float, out_s: float, done: bool) -> None:
        self.audio_in = max(0.0, in_s)
        self.audio_out = max(self.audio_in + 0.05, out_s)
        if not done:
            return
        if self.follow_in.get_active():
            self.follow_in.set_active(False)
        self._refresh_fit()
        self._checkpoint()
        self._schedule_autosave()
        self._set_status(f"Audio {self.audio_in:.2f}s–{self.audio_out:.2f}s")

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
        if self.audio_path:
            return self.audio_start + self.audio_in
        if self.video_path and self.video_info and self.video_info.get("has_audio"):
            return self.video_start + self.in_spin.get_value()
        return None

    def _preview_cmd(self, timeline_t: float) -> list[str] | None:
        end = self._program_end()
        if self.audio_path and self.audio_out is not None:
            end = min(end, self.audio_start + self.audio_out)
        remaining = max(0.05, end - timeline_t)
        begins = self._audio_begins_at()
        if begins is None:
            return None
        if timeline_t < begins - 0.02:
            return None
        if self.audio_path:
            path = self.audio_path
            start = timeline_t - self.audio_start
        elif self.video_path and self.video_info and self.video_info.get("has_audio"):
            path = self.video_path
            start = timeline_t - self.video_start
        else:
            return None
        start = max(0.0, start)
        # Same players Eagle Browse uses for audio preview. ffplay first —
        # Gtk.MediaFile/GStreamer is mute or abort here.
        if shutil.which("ffplay"):
            return [
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
        if shutil.which("mpv"):
            return [
                "mpv",
                "--no-video",
                "--force-window=no",
                "--no-terminal",
                "--audio-display=no",
                "--no-resume-playback",
                f"--start={start:.3f}",
                f"--length={remaining:.3f}",
                str(path),
            ]
        return None

    def _stop_preview_audio(self) -> None:
        proc = self._preview_proc
        self._preview_proc = None
        if proc is None:
            return
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=0.4)
        except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    def _apply_timeline_frame(self, timeline_t: float, *, start_media: bool) -> None:
        v0 = self.video_start + self.in_spin.get_value()
        if timeline_t < v0 - 0.02 or self._vmedia is None:
            self.preview.set_blank(True)
            if self._vmedia is not None:
                self._vmedia.pause()
            self._clip_playing = False
            return
        self.preview.set_blank(False)
        source = max(0.0, timeline_t - self.video_start)
        dur = float((self.video_info or {}).get("duration") or 0)
        if dur > 0:
            source = min(source, dur)
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
        cmd = self._preview_cmd(timeline_t)
        if cmd is None:
            if begins is not None and timeline_t < begins - 0.02:
                self._audio_pending = True
                return
            if self.audio_path and not shutil.which("ffplay") and not shutil.which("mpv"):
                self._set_status("Need ffplay or mpv to hear preview audio")
            return
        self._audio_pending = False
        log = Path.home() / ".cache" / "clip-editor" / "preview-audio.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        logf = log.open("w", encoding="utf-8")
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
        self.preview.set_media(None)

    def _load_media(self, path: Path) -> None:
        self._dispose_media()
        media = Gtk.MediaFile.new_for_filename(str(path))
        media.set_loop(False)
        self._vmedia = media

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
        self.preview.set_media(None)
        t = self.timeline.playhead
        self.preview.set_blank(t < self.video_start + self.in_spin.get_value() - 0.02)
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
        self._play_t0 = 0.0
        self._play_mono = time.monotonic()
        self._clip_playing = False
        self._audio_pending = False
        self._syncing_scrub = True
        self.timeline.set_playhead(0.0)
        self._syncing_scrub = False
        self.clock.set_text(f"0.00 / {self._program_end():.2f}")
        self._apply_timeline_frame(0.0, start_media=True)
        self._start_preview_audio(0.0)
        if not self.audio_path and not (
            self.video_info and self.video_info.get("has_audio")
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
        if t < self.video_start + self.in_spin.get_value() - 0.02:
            self._apply_timeline_frame(t, start_media=False)
        elif not self._clip_playing:
            self._apply_timeline_frame(t, start_media=True)
        else:
            self.preview.queue_draw()
        if self._audio_pending and self._preview_proc is None:
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
        v_start = self.video_start
        a_start = self.audio_start
        a_in = self.audio_in
        a_out = self.audio_out
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
                    progress=progress,
                )
                GLib.idle_add(self._export_done, result, None)
            except (ExportError, ProbeError, OSError) as exc:
                GLib.idle_add(self._export_done, None, exc)

        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, result: dict | None, err: BaseException | None) -> bool:
        self.exporting = False
        self.btn_export.set_sensitive(self.video_path is not None)
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
        video = _cli_flag_path(args, "--video")
        audio = _cli_flag_path(args, "--audio")
        self.activate()
        win = self.props.active_window
        if isinstance(win, EditorWindow):
            if video is not None:
                # Replace the session with this video; skip autosave restore.
                win._open_from_cli = True
                _idle_open_media(win, "video", video)
            if audio is not None:
                # Same as dropping audio on the window: keep the current
                # (or restored) project and attach this track.
                _idle_open_media(win, "audio", audio)
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


def _idle_open_media(win: EditorWindow, kind: str, path: Path) -> None:
    def go(_w: EditorWindow = win, k: str = kind, p: Path = path) -> bool:
        if p.is_file():
            _w._open_path(k, p)
        else:
            _w._set_status(f"missing {p}")
        return False

    GLib.idle_add(go)


def run(
    *,
    open_video: str | Path | None = None,
    open_audio: str | Path | None = None,
) -> int:
    Adw.init()
    apply_omarchy_theme()
    argv = ["clip-editor"]
    if open_video:
        argv += ["--video", str(open_video)]
    if open_audio:
        argv += ["--audio", str(open_audio)]
    return int(EditorApp().run(argv))
