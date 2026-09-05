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

from clip_editor.cli_paths import cli_flag_paths
from clip_editor.aspects import (
    ASPECTS,
    DEFAULT_RESOLUTION,
    RESOLUTIONS,
    cover_crop,
    dest_size,
)
from clip_editor.commands import parse_command
from clip_editor.keyboard_edits import move_clips
from clip_editor.eagle import apply_omarchy_theme, theme_rgb
from clip_editor.export import ExportCancelled, ExportError, default_out_path, run_export
from clip_editor.preview import (
    PREVIEW_PROFILE,
    TimelineSegment,
    assert_preview_path_safe,
    build_timeline_segments,
    cleanup_preview_cache,
    compiled_allows_action,
    compiled_playhead_seconds,
    has_touching_follower,
    mark_segments_green,
    playback_source,
    preview_out_path,
    rebase_clips_for_window,
    render_fingerprint,
    segment_at,
    selected_cut_window,
)
from clip_editor.probe import ProbeError, probe, which_ffmpeg
from clip_editor.ripple import (
    follower_indices,
    resolve_edge_hits,
    ripple_starts,
)
from clip_editor.project import (
    DEFAULT_SPEED,
    DEFAULT_TRANSITION_S,
    MAX_SPEED,
    MIN_SPEED,
    TRANSITION_DISSOLVE,
    TRANSITION_NONE,
    TRANSITION_WHITE_FLASH,
    ClipInst,
    MediaItem,
    Project,
    ProjectError,
    clear_autosave,
    media_load_errors,
    next_media_id,
    normalize_speed,
    normalize_transition,
    read_autosave,
    read_project,
    write_autosave,
    write_project,
)
from clip_editor.selection import (
    group_moved_starts,
    move_timeline_track,
    move_track_selection,
    nearest_clip,
    next_video_selection,
    prune_video_selection,
)

def application_id() -> str:
    """GApplication id. Override with CLIP_EDITOR_APP_ID to run beside production."""
    raw = os.environ.get("CLIP_EDITOR_APP_ID", "").strip()
    return raw or "local.clip.Editor"


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
        self.transform_x = 0.0
        self.transform_y = 0.0
        self.transform_scale = 1.0
        self.output_width = 1080
        self.output_height = 1920
        self.on_pan = None
        self.on_pan_end = None
        self._drag_pan = (0.5, 0.5)
        self._texture: Gdk.Texture | None = None
        self._media: Gtk.MediaFile | None = None
        self._inv_id = 0
        self.blank = False
        self.read_only = False
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

    def set_transform(
        self, x: float, y: float, scale: float, output_width: int, output_height: int
    ) -> None:
        self.transform_x = float(x)
        self.transform_y = float(y)
        self.transform_scale = max(0.05, float(scale))
        self.output_width = max(1, int(output_width))
        self.output_height = max(1, int(output_height))
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
        tx = self.transform_x * w / self.output_width
        ty = self.transform_y * h / self.output_height
        # Draw the full source as a layer and clip it to the project frame.
        # X/Y therefore pan the source under the viewport, rather than moving
        # a pre-cropped project-shaped raster and exposing the background.
        cover = max(w / iw, h / ih)
        factor = cover * max(1.0, self.transform_scale)
        sw, sh = iw * factor, ih * factor
        x = min(0.0, max(w - sw, (w - sw) * self.pan_x + tx))
        y = min(0.0, max(h - sh, (h - sh) * self.pan_y + ty))
        snapshot.push_clip(Graphene.Rect().init(0, 0, w, h))
        snapshot.translate(Graphene.Point().init(x, y))
        p.snapshot(snapshot, sw, sh)
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
        scale = max(1.0, self.transform_scale)
        if src_a > dst_a:
            return pw * (h / ph) * scale - w, 0.0
        return 0.0, ph * (w / pw) * scale - h

    def _drag_begin(self, *_args: object) -> None:
        if self.read_only:
            return
        self._drag_pan = (self.pan_x, self.pan_y)
        self.set_cursor_from_name("grabbing")

    def _drag_update(self, _g: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self.read_only:
            return
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
        if self.read_only:
            self.set_cursor_from_name("default")
            return
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
    _CACHE_BAR_H = 8.0
    _LANE_H = 28.0
    _LANE_GAP = 6.0
    _BOTTOM = 16.0
    _EDGE = 8.0
    _MIN = 0.05
    _SNAP_PX = 10.0
    _TRAIL_PX = 160.0
    _TRAIL_MIN_S = 8.0
    _MIN_PPS = 8.0
    _HEIGHT = 170

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
        self.sel_vs: set[int] = set()
        self.sel_as: set[int] = set()
        self.nav_kind = "video"
        self.nav_track = 1
        self.playhead = 0.0
        self.read_only = False
        # (t0, t1, green) spans for the Premiere-style cache bar (#532)
        self.cache_spans: list[tuple[float, float, bool]] = []
        self.on_seek = None
        self.on_video_move = None
        self.on_audio_move = None
        self.on_video_trim = None
        self.on_audio_trim = None
        self.on_track_change = None
        self.on_navigation_track_change = None
        self.on_place = None
        self.on_select = None
        self._drag_mode = ""
        self._drag_index = -1
        self._drag_v0 = 0.0
        self._drag_y0 = 0.0
        self._drag_span = 0.0
        self._drag_inner = 1.0
        self._drag_group_starts: dict[int, float] = {}
        self._ripple_t1_0 = 0.0
        self._ripple_starts: dict[int, float] = {}
        self._drag_moved = False
        self._snap_line: float | None = None
        self._drop_hover: tuple[str, float, int] | None = None
        self.set_hexpand(False)
        self.set_vexpand(False)
        self.set_content_width(200)
        self.set_content_height(self._HEIGHT)
        self.set_size_request(200, self._HEIGHT)
        self.set_focusable(True)
        self.set_draw_func(self._draw)
        self.connect("resize", self._on_resize)
        self.set_sensitive(False)
        self.set_cursor_from_name("col-resize")
        self.set_tooltip_text(
            "Drag a clip to slide it. Drag either edge to trim. "
            "Shift+click adds a clip to the selection; drag moves the group. "
            "With the timeline focused, H/L move between clips on the active track; Shift+H/L "
            "extends the selection. "
            "J/K move the track cursor down/up. "
            "Drag the ruler or playhead to seek. T splits at the playhead. "
            "Del removes the selected clip (A-track too). Esc clears multi-select."
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
            _t0, t1 = self._clip_times(c, self._clip_src_dur(c, "v"))
            ends.append(t1)
            # Full-media ghost footprint at this speed (in=0..src_dur).
            d = self._clip_src_dur(c, "v")
            if d > 0:
                ends.append(c.start + d / c.playback_speed())
        for c in self.aclips:
            _t0, t1 = self._clip_times(c, self._clip_src_dur(c, "a"))
            ends.append(t1)
            d = self._clip_src_dur(c, "a")
            if d > 0:
                ends.append(c.start + d / c.playback_speed())
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
        sel_vs: set[int] | None = None,
        sel_as: set[int] | None = None,
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
            # Same ClipInst objects as the editor so ripple/trim persist on sync.
            self.vclips = list(vclips)
        elif video_dur > 0:
            self.vclips = [
                ClipInst(start=float(video_start), in_s=self.in_s, out_s=self.out_s or video_dur)
            ]
        else:
            self.vclips = []
        if aclips is not None:
            self.aclips = list(aclips)
        elif audio_dur > 0:
            aout = float(audio_out) if audio_out else audio_dur
            self.aclips = [
                ClipInst(start=float(audio_start), in_s=max(0.0, float(audio_in)), out_s=aout)
            ]
        else:
            self.aclips = []
        if self.vclips:
            seed = set(sel_vs) if sel_vs is not None else ({sel_v} if sel_v >= 0 else set())
            self.sel_v, self.sel_vs = prune_video_selection(seed, sel_v, len(self.vclips))
        else:
            self.sel_v, self.sel_vs = -1, set()
        self.sel_a = sel_a if self.aclips else -1
        self.sel_a, self.sel_as = prune_video_selection(
            set(sel_as or ()), self.sel_a, len(self.aclips)
        )
        self._mirror_sel()
        self._recompute_span()
        self.queue_draw()

    def set_cache_spans(self, spans: list[tuple[float, float, bool]]) -> None:
        self.cache_spans = list(spans)
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
            self.set_size_request(w, self._HEIGHT)
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
        speed = c.playback_speed()
        t0 = c.start + inn
        return t0, t0 + (out - inn) / speed

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

    def _lane_y(self, kind: str, track: int) -> float:
        # Overlay video sits above the base picture; audio tracks sit below it.
        slot = {("video", 2): 0, ("video", 1): 1, ("audio", 1): 2, ("audio", 2): 3}[
            (kind, max(1, min(2, int(track))))
        ]
        return (
            self._RULER_H
            + self._CACHE_BAR_H
            + slot * (self._LANE_H + self._LANE_GAP)
        )

    def _track_at(self, kind: str, y: float) -> int | None:
        for track in (1, 2):
            if self._in_lane(y, self._lane_y(kind, track)):
                return track
        return None

    def _hit_kind(self, x: float, y: float, kind: str) -> tuple[int, str]:
        clips = self.vclips if kind == "video" else self.aclips
        lane = "v" if kind == "video" else "a"
        for track in (1, 2):
            lane_y = self._lane_y(kind, track)
            candidates = [i for i, c in enumerate(clips) if int(c.track) == track]
            edge_hits: list[tuple[int, str, float, float]] = []
            for i in candidates:
                t0, t1 = self._clip_times(clips[i], self._clip_src_dur(clips[i], lane))
                edge = self._hit_edge(x, y, t0, t1, lane_y)
                if edge:
                    edge_hits.append((i, edge, t0, t1))
            resolved = resolve_edge_hits(edge_hits)
            if resolved is not None:
                return resolved
            for i in reversed(candidates):
                t0, t1 = self._clip_times(clips[i], self._clip_src_dur(clips[i], lane))
                if self._hit_clip(x, y, t0, t1, lane_y):
                    return i, "body"
        return -1, ""

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
        edge_hits: list[tuple[int, str, float, float]] = []
        for i in range(len(clips)):
            t0, t1 = self._clip_times(clips[i], self._clip_src_dur(clips[i], lane))
            edge = self._hit_edge(x, y, t0, t1, lane_y)
            if edge:
                edge_hits.append((i, edge, t0, t1))
        resolved = resolve_edge_hits(edge_hits)
        if resolved is not None:
            return resolved
        for i in range(len(clips) - 1, -1, -1):
            t0, t1 = self._clip_times(clips[i], self._clip_src_dur(clips[i], lane))
            if self._hit_clip(x, y, t0, t1, lane_y):
                return i, "body"
        return -1, ""

    def _hit_video(self, x: float, y: float) -> bool:
        i, part = self._hit_kind(x, y, "video")
        return i >= 0 and part == "body"

    def _hit_audio(self, x: float, y: float) -> bool:
        if self.audio_kind == "source":
            return False
        i, part = self._hit_kind(x, y, "audio")
        return i >= 0 and part == "body"

    def _hit_video_edge(self, x: float, y: float) -> str:
        i, part = self._hit_kind(x, y, "video")
        return part if part in ("in", "out") else ""

    def _hit_audio_edge(self, x: float, y: float) -> str:
        if self.audio_kind == "source":
            return ""
        i, part = self._hit_kind(x, y, "audio")
        return part if part in ("in", "out") else ""

    def _seek_x(self, x: float) -> None:
        t = self._x_to_t(x)
        self.playhead = t
        self.queue_draw()
        if callable(self.on_seek):
            self.on_seek(t)

    def _shift_held(self, gesture: Gtk.GestureClick) -> bool:
        """True when Shift is down at click time.

        Prefer the seat keyboard's live modifier state — under Hyprland/Wayland,
        GestureClick's event state often omits Shift.
        """
        display = Gdk.Display.get_default()
        if display is not None:
            seat = display.get_default_seat()
            keyboard = seat.get_keyboard() if seat is not None else None
            if keyboard is not None:
                mods = (
                    keyboard.get_modifier_state()
                    & Gtk.accelerator_get_default_mod_mask()
                )
                if mods & Gdk.ModifierType.SHIFT_MASK:
                    return True
        state = gesture.get_current_event_state()
        mods = state & Gtk.accelerator_get_default_mod_mask()
        return bool(mods & Gdk.ModifierType.SHIFT_MASK)

    def _select_hit(self, x: float, y: float, *, shift: bool = False) -> bool:
        vi, _vp = self._hit_kind(x, y, "video")
        if vi >= 0:
            self.sel_v, self.sel_vs = next_video_selection(
                clicked=vi,
                primary=self.sel_v,
                selected=self.sel_vs,
                shift=shift,
                n_clips=len(self.vclips),
            )
            self._mirror_sel()
            if callable(self.on_select):
                self.on_select("video", self.sel_v, frozenset(self.sel_vs))
            self.queue_draw()
            return True
        ai, _ap = self._hit_kind(x, y, "audio")
        if ai >= 0:
            # Audio stays single-select for now; clear video multi-select.
            self.sel_vs = set()
            self.sel_a = ai
            self._mirror_sel()
            if callable(self.on_select):
                self.on_select("audio", ai, frozenset())
            self.queue_draw()
            return True
        return False

    def _navigation_clips(self) -> list[tuple[int, float, float]]:
        clips = self.vclips if self.nav_kind == "video" else self.aclips
        return sorted(
            [(i, *self._clip_times(c, self._clip_src_dur(c, self.nav_kind[0])))
             for i, c in enumerate(clips) if c.track == self.nav_track],
            key=lambda row: (row[1], row[0]),
        )

    def _select_navigation_clip(self, primary: int, selected: set[int]) -> None:
        if self.nav_kind == "video":
            self.sel_v, self.sel_vs = primary, selected
            self.sel_a, self.sel_as = -1, set()
        else:
            self.sel_a, self.sel_as = primary, selected
            self.sel_v, self.sel_vs = -1, set()
        self._mirror_sel()
        if callable(self.on_select):
            self.on_select(self.nav_kind, primary, frozenset(selected))
        self._ensure_clip_visible(self.nav_kind, primary)
        self.queue_draw()

    def clear_selection(self) -> None:
        self._select_navigation_clip(-1, set())

    def move_clip_selection(self, delta: int, *, extend: bool) -> bool:
        if self.read_only:
            return False
        video = self.nav_kind == "video"
        old_primary = self.sel_v if video else self.sel_a
        old_selected = self.sel_vs if video else self.sel_as
        primary, selected = move_track_selection(
            order=[row[0] for row in self._navigation_clips()],
            primary=old_primary, selected=old_selected, delta=delta, extend=extend,
        )
        if primary == old_primary and selected == old_selected:
            return False
        self._select_navigation_clip(primary, selected)
        return True

    def move_navigation_track(self, delta: int) -> bool:
        """Move the visible keyboard cursor through V2, V1, A1, and A2."""
        if self.read_only:
            return False
        kind, track = move_timeline_track(self.nav_kind, self.nav_track, delta)
        if (kind, track) == (self.nav_kind, self.nav_track):
            return False
        self.nav_kind, self.nav_track = kind, track
        primary = nearest_clip(self._navigation_clips(), self.playhead)
        self._select_navigation_clip(primary, {primary} if primary >= 0 else set())
        if callable(self.on_navigation_track_change):
            self.on_navigation_track_change(kind, track)
        self.queue_draw()
        return True

    def _ensure_clip_visible(self, kind: str, index: int) -> None:
        """Scroll the horizontal timeline just enough to reveal a clip."""
        clips = self.vclips if kind == "video" else self.aclips
        if not 0 <= index < len(clips):
            return
        parent = self.get_parent()
        scroll: Gtk.ScrolledWindow | None = None
        for _ in range(4):
            if isinstance(parent, Gtk.ScrolledWindow):
                scroll = parent
                break
            parent = parent.get_parent() if parent is not None else None
        if scroll is None:
            return
        clip = clips[index]
        t0, t1 = self._clip_times(clip, self._clip_src_dur(clip, kind[0]))
        width = max(1.0, float(self.get_width()))
        x0 = self._t_to_x(t0, width)
        x1 = self._t_to_x(t1, width)
        adjustment = scroll.get_hadjustment()
        visible_start = adjustment.get_value()
        visible_width = adjustment.get_page_size()
        if visible_width <= 0:
            return
        visible_end = visible_start + visible_width
        target = visible_start
        if x0 < visible_start:
            target = x0
        elif x1 > visible_end:
            target = x1 - visible_width
        else:
            return
        max_value = max(adjustment.get_lower(), adjustment.get_upper() - visible_width)
        adjustment.set_value(max(adjustment.get_lower(), min(target, max_value)))

    def set_read_only(self, locked: bool) -> None:
        self.read_only = bool(locked)
        if locked:
            self._drag_mode = ""
            self._drag_index = -1
            self._drag_group_starts = {}
            self._drop_hover = None
            self.set_cursor_from_name("col-resize")
            self.set_tooltip_text(
                "Rendered preview — seek only. Editing is locked. Use Back to edit."
            )
        else:
            self.set_tooltip_text(
                "Drag a clip to slide it. Drag the right edge to trim and "
                "ripple later clips on that track. Drag the left edge to trim in. "
                "Shift+click adds a clip to the selection; drag moves the group. "
                "With the timeline focused, H/L move between clips on the active track; Shift+H/L "
                "extends the selection. Drag the ruler or playhead to seek. "
                "J/K move the track cursor down/up. "
                "T splits at the playhead. "
                "Del removes the selected clip (A-track too). Esc clears multi-select."
            )
        self.queue_draw()

    def _on_pressed(self, gesture: Gtk.GestureClick, _n: int, x: float, y: float) -> None:
        self.grab_focus()
        if self.read_only:
            self._seek_x(x)
            return
        shift = self._shift_held(gesture)
        vi, _vp = self._hit_kind(x, y, "video")
        if vi >= 0:
            # Plain clip presses are owned by drag-begin so a group drag does not
            # get collapsed by a later GestureClick. Shift+click still adds here.
            if shift:
                self._select_hit(x, y, shift=True)
            return
        if self.audio_kind != "source":
            ai, _ap = self._hit_kind(x, y, "audio")
            if ai >= 0:
                # Same race as video: let drag-begin own plain audio selection.
                return
        # Empty click: clear multi-select, then seek.
        self.clear_selection()
        self._seek_x(x)

    def _on_motion(self, _c: Gtk.EventControllerMotion, x: float, y: float) -> None:
        if self.read_only:
            self.set_cursor_from_name("col-resize")
            return
        if self._drag_mode in ("video-in", "video-out", "audio-in", "audio-out"):
            self.set_cursor_from_name("ew-resize")
        elif self._drag_mode in ("video", "video-group", "audio"):
            self.set_cursor_from_name("grabbing")
        elif self._hit_video_edge(x, y) == "out" or self._hit_audio_edge(x, y) == "out":
            self.set_cursor_from_name("col-resize")
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
        self._drag_y0 = oy
        self._drag_group_starts = {}
        self._drag_moved = False
        if self.read_only:
            self._drag_mode = "seek"
            self._drag_index = -1
            self._seek_x(ox)
            return
        vi, vp = self._hit_kind(ox, oy, "video")
        ai, ap = (-1, "")
        if self.audio_kind != "source":
            ai, ap = self._hit_kind(ox, oy, "audio")
        if vp in ("in", "out", "body"):
            # Selection lives here (not in GestureClick) to avoid races and to
            # read Shift from the seat keyboard under Hyprland.
            shift = False
            display = Gdk.Display.get_default()
            if display is not None:
                seat = display.get_default_seat()
                keyboard = seat.get_keyboard() if seat is not None else None
                if keyboard is not None:
                    mods = (
                        keyboard.get_modifier_state()
                        & Gtk.accelerator_get_default_mod_mask()
                    )
                    shift = bool(mods & Gdk.ModifierType.SHIFT_MASK)
            # Do not collapse a multi-select already applied by _on_pressed.
            # Only change membership here when Shift is held (add) or the hit
            # clip is outside the current set (plain replace).
            if shift:
                self.sel_v, self.sel_vs = next_video_selection(
                    clicked=vi,
                    primary=self.sel_v,
                    selected=self.sel_vs,
                    shift=True,
                    n_clips=len(self.vclips),
                )
            elif vi not in self.sel_vs:
                self.sel_v = vi
                self.sel_vs = {vi}
            else:
                self.sel_v = vi
            self._drag_index = vi
            c = self.vclips[vi]
            self._mirror_sel()
            if callable(self.on_select):
                self.on_select("video", self.sel_v, frozenset(self.sel_vs))
            if vp == "in":
                self._drag_mode = "video-in"
                self._drag_v0 = c.in_s
                self.set_cursor_from_name("ew-resize")
            elif vp == "out":
                self._drag_mode = "video-out"
                self._drag_v0 = c.out_s
                self._begin_ripple("video", vi)
                self.set_cursor_from_name("col-resize")
            else:
                if len(self.sel_vs) > 1 and vi in self.sel_vs:
                    self._drag_mode = "video-group"
                    self._drag_group_starts = {
                        i: float(self.vclips[i].start)
                        for i in sorted(self.sel_vs)
                        if 0 <= i < len(self.vclips)
                    }
                    self._drag_v0 = float(self._drag_group_starts[vi])
                else:
                    self._drag_mode = "video"
                    self._drag_v0 = c.start
                self.set_cursor_from_name("grabbing")
        elif ap in ("in", "out", "body"):
            self.sel_a = ai
            self._drag_index = ai
            c = self.aclips[ai]
            self._mirror_sel()
            self.sel_vs = set()
            if callable(self.on_select):
                self.on_select("audio", ai, frozenset())
            if ap == "in":
                self._drag_mode = "audio-in"
                self._drag_v0 = c.in_s
                self.set_cursor_from_name("ew-resize")
            elif ap == "out":
                self._drag_mode = "audio-out"
                self._drag_v0 = c.out_s if c.out_s > 0 else self.audio_dur
                self._begin_ripple("audio", ai)
                self.set_cursor_from_name("col-resize")
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

    def _clip_times_list(self, clips: list[ClipInst], lane: str) -> list[tuple[float, float]]:
        return [
            self._clip_times(c, self._clip_src_dur(c, lane)) for c in clips
        ]

    def _begin_ripple(self, kind: str, index: int) -> None:
        clips = self.vclips if kind == "video" else self.aclips
        lane = "v" if kind == "video" else "a"
        times = self._clip_times_list(clips, lane)
        tracks = [int(c.track) for c in clips]
        self._ripple_t1_0 = times[index][1] if 0 <= index < len(times) else 0.0
        follow = follower_indices(tracks, times, index)
        self._ripple_starts = {i: float(clips[i].start) for i in follow}

    def _apply_ripple(self, kind: str, index: int) -> None:
        clips = self.vclips if kind == "video" else self.aclips
        lane = "v" if kind == "video" else "a"
        if not 0 <= index < len(clips) or not self._ripple_starts:
            return
        _t0, t1 = self._clip_times(clips[index], self._clip_src_dur(clips[index], lane))
        delta = t1 - self._ripple_t1_0
        for i, start in ripple_starts(self._ripple_starts, delta).items():
            if 0 <= i < len(clips):
                clips[i].start = start

    def _snap_thresh(self) -> float:
        inner = max(self._drag_inner, 1.0)
        return max(0.04, self._SNAP_PX / inner * self._map_span())

    def _other_edges(self, which: str) -> list[float]:
        skip = {self._drag_index}
        if which == "video" and self._drag_mode == "video-group":
            skip |= set(self._drag_group_starts)
        if self._drag_mode in ("video-out", "audio-out"):
            skip |= set(self._ripple_starts)
        times: list[float] = []
        for i, c in enumerate(self.vclips):
            if which == "video" and i in skip:
                continue
            t0, t1 = self._clip_times(c, self._clip_src_dur(c, "v"))
            times += [t0, t1]
        if self.audio_kind != "source":
            for i, c in enumerate(self.aclips):
                if which == "audio" and i in skip:
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

    def _on_drag_update(self, gesture: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if abs(dx) >= 1.0 or abs(dy) >= 1.0:
            self._drag_moved = True
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
            top = self._clip_src_dur(c, "v") or max(inn + self._MIN, self._drag_v0)
            speed = c.playback_speed()
            # _drag_v0 is source out; map dx through timeline then back to source.
            t0 = c.start + inn
            timeline_end = t0 + max(self._MIN, (self._drag_v0 - inn) / speed) + dt
            snapped = self._snap_time(timeline_end, self._other_edges("video"))
            source_len = max(self._MIN, (snapped - t0) * speed)
            c.out_s = max(inn + self._MIN, min(top, inn + source_len))
            self.out_s = c.out_s
            if abs((t0 + (c.out_s - inn) / speed) - snapped) > 1e-4:
                self._snap_line = None
            if callable(self.on_video_trim):
                self.on_video_trim(self._drag_index, inn, c.out_s, False)
            self._apply_ripple("video", self._drag_index)
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
            top = self._clip_src_dur(c, "a") or max(ain + self._MIN, self._drag_v0)
            speed = c.playback_speed()
            t0 = c.start + ain
            timeline_end = t0 + max(self._MIN, (self._drag_v0 - ain) / speed) + dt
            snapped = self._snap_time(timeline_end, self._other_edges("audio"))
            source_len = max(self._MIN, (snapped - t0) * speed)
            c.out_s = max(ain + self._MIN, min(top, ain + source_len))
            self.audio_out = c.out_s
            if abs((t0 + (c.out_s - ain) / speed) - snapped) > 1e-4:
                self._snap_line = None
            if callable(self.on_audio_trim):
                self.on_audio_trim(self._drag_index, ain, c.out_s, False)
            self._apply_ripple("audio", self._drag_index)
            self.queue_draw()
            return
        if self._drag_mode == "video-group":
            anchor = self._vclip()
            if anchor is None or self._drag_index not in self._drag_group_starts:
                return
            inn, out = self._clip_used(anchor, self._clip_src_dur(anchor, "v"))
            right = inn + (out - inn) / anchor.playback_speed()
            start = max(-inn, self._drag_v0 + dt)
            start = self._snap_move(start, inn, right, self._other_edges("video"))
            start = max(-inn, start)
            moved = group_moved_starts(
                self._drag_group_starts,
                anchor=self._drag_index,
                new_anchor_start=start,
            )
            for i, new_start in moved.items():
                if not 0 <= i < len(self.vclips):
                    continue
                c = self.vclips[i]
                used_in, _used_out = self._clip_used(c, self._clip_src_dur(c, "v"))
                c.start = max(-used_in, float(new_start))
                if callable(self.on_video_move):
                    self.on_video_move(i, c.start, False)
            self.video_start = self.vclips[self._drag_index].start
            self.queue_draw()
            return
        if self._drag_mode in ("video", "audio"):
            if self._drag_mode == "video":
                c = self._vclip()
                if c is None:
                    return
                inn, out = self._clip_used(c, self._clip_src_dur(c, "v"))
                right = inn + (out - inn) / c.playback_speed()
                start = max(-inn, self._drag_v0 + dt)
                start = self._snap_move(start, inn, right, self._other_edges("video"))
                start = max(-inn, start)
                c.start = start
                track = self._track_at("video", self._drag_y0 + dy)
                if track is not None and track != c.track:
                    c.track = track
                    if callable(self.on_track_change):
                        self.on_track_change("video", self._drag_index, track)
                self.video_start = start
                if callable(self.on_video_move):
                    self.on_video_move(self._drag_index, start, False)
            else:
                c = self._aclip()
                if c is None:
                    return
                inn, out = self._clip_used(c, self._clip_src_dur(c, "a"))
                right = inn + (out - inn) / c.playback_speed()
                start = max(-inn, self._drag_v0 + dt)
                start = self._snap_move(start, inn, right, self._other_edges("audio"))
                start = max(-inn, start)
                c.start = start
                track = self._track_at("audio", self._drag_y0 + dy)
                if track is not None and track != c.track:
                    c.track = track
                    if callable(self.on_track_change):
                        self.on_track_change("audio", self._drag_index, track)
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
        group_starts = dict(self._drag_group_starts)
        idx = self._drag_index
        if self._drag_moved and mode == "video-out":
            self._apply_ripple("video", idx)
        elif self._drag_moved and mode == "audio-out":
            self._apply_ripple("audio", idx)
        self._drag_mode = ""
        self._drag_group_starts = {}
        self._ripple_starts = {}
        self._snap_line = None
        self._recompute_span()
        self._drag_index = -1
        if mode == "video-group":
            self.set_cursor_from_name("grab")
            if callable(self.on_video_move):
                ordered = [i for i in sorted(group_starts) if self._vclip(i) is not None]
                for n, i in enumerate(ordered):
                    c = self._vclip(i)
                    assert c is not None
                    # One undo checkpoint for the whole group (done only on last).
                    self.on_video_move(i, c.start, n == len(ordered) - 1)
        elif mode == "video":
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
            self.set_cursor_from_name("col-resize" if mode == "video-out" else "ew-resize")
            c = self._vclip(idx)
            if c is not None and callable(self.on_video_trim) and self._drag_moved:
                inn, out = self._clip_used(c, self._clip_src_dur(c, "v"))
                self.on_video_trim(idx, inn, out, True)
        elif mode in ("audio-in", "audio-out"):
            self.set_cursor_from_name("ew-resize")
            c = self._aclip(idx)
            if c is not None and callable(self.on_audio_trim) and self._drag_moved:
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
        if self.read_only:
            if self._drop_hover is not None:
                self._drop_hover = None
                self.queue_draw()
            return Gdk.DragAction(0)
        kind = self._drop_kind(target)
        t = self._x_to_t(x)
        hover: tuple[str, float, int] | None = None
        action = Gdk.DragAction(0)
        for track in (1, 2):
            if kind in ("video", "audio") and self._in_lane(y, self._lane_y(kind, track)):
                hover = (kind, t, track)
                action = Gdk.DragAction.COPY
                break
        if hover != self._drop_hover:
            self._drop_hover = hover
            self.queue_draw()
        return action

    def _on_bin_enter(self, target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        if self.read_only:
            return Gdk.DragAction(0)
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
        if self.read_only:
            self._drop_hover = None
            self.queue_draw()
            return False
        kind, mid = self._parse_bin_payload(value)
        t = self._x_to_t(x)
        self._drop_hover = None
        self.queue_draw()
        for track in (1, 2):
            if kind in ("video", "audio") and self._in_lane(y, self._lane_y(kind, track)):
                if callable(self.on_place):
                    self.on_place(kind, t, mid, track)
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
        v_y = self._lane_y("video", 1)
        lanes_bottom = self._lane_y("audio", 2) + self._LANE_H

        # Render-cache bar: gray empty, red dirty, green baked (#532).
        bar_y = self._RULER_H + 1.0
        bar_h = max(3.0, self._CACHE_BAR_H - 3.0)
        _round_rect(cr, left, bar_y, inner, bar_h, 2)
        cr.set_source_rgb(*muted)
        cr.fill()
        red = theme_rgb("red", (0.75, 0.28, 0.32))
        span = self._map_span()
        for t0, t1, green_ok in self.cache_spans:
            if span <= 0 or t1 <= t0:
                continue
            x0 = self._t_to_x(t0, width)
            x1 = self._t_to_x(t1, width)
            cr.set_source_rgb(*(green if green_ok else red))
            cr.rectangle(x0, bar_y, max(1.0, x1 - x0), bar_h)
            cr.fill()

        for kind, track_no in (("video", 2), ("video", 1), ("audio", 1), ("audio", 2)):
            lane_y = self._lane_y(kind, track_no)
            _round_rect(cr, left, lane_y, inner, self._LANE_H, 4)
            cr.set_source_rgb(*track)
            cr.fill()
            if (kind, track_no) == (self.nav_kind, self.nav_track):
                cr.set_source_rgb(*sel)
                cr.set_line_width(2)
                _round_rect(cr, left + 1, lane_y + 1, inner - 2, self._LANE_H - 2, 3)
                cr.stroke()

        cr.set_source_rgb(*muted)
        cr.set_font_size(11)
        for label, kind, track_no in (
            ("V2", "video", 2), ("V1", "video", 1),
            ("A1", "audio", 1), ("A2", "audio", 2),
        ):
            cr.move_to(3, self._lane_y(kind, track_no) + 19)
            cr.show_text(label)

        clip_y = 3.0
        clip_h = self._LANE_H - 6
        for i, c in enumerate(self.vclips):
            clip_lane_y = self._lane_y("video", c.track)
            d = self._clip_src_dur(c, "v")
            speed = c.playback_speed()
            t0, t1 = self._clip_times(c, d)
            # Ghost = full source placed at this speed (shrinks/grows with rate).
            gx0 = self._t_to_x(c.start, width)
            gx1 = self._t_to_x(c.start + (d / speed if d > 0 else 0.0), width)
            self._draw_clip(cr, gx0, clip_lane_y + clip_y, max(3.0, gx1 - gx0), clip_h, sel, "", 0.28)
            x0 = self._t_to_x(t0, width)
            x1 = self._t_to_x(t1, width)
            name = self.clip_names.get(c.media_id) or (
                self.video_name if i == self.sel_v or len(self.vclips) == 1 else ""
            )
            self._draw_clip(
                cr, x0, clip_lane_y + clip_y, max(3.0, x1 - x0), clip_h, sel, name
            )
            self._draw_handles(cr, x0, x1, clip_lane_y + clip_y, clip_h, fg)
            if i in self.sel_vs or i == self.sel_v:
                # Accent outline so multi-select stays obvious vs muted neighbors.
                cr.set_source_rgb(*sel)
                cr.set_line_width(3.0 if i == self.sel_v else 2.0)
                cr.rectangle(
                    x0 - 1.0,
                    clip_lane_y + clip_y - 1.0,
                    max(3.0, x1 - x0) + 2.0,
                    clip_h + 2.0,
                )
                cr.stroke()
            if getattr(c, "transition", TRANSITION_NONE) not in ("", TRANSITION_NONE):
                # Compact marker at the outgoing cut of a configured transition.
                mx = x1
                mid_y = clip_lane_y + clip_y + clip_h / 2.0
                cr.set_source_rgb(*fg)
                cr.move_to(mx - 4, mid_y - 5)
                cr.line_to(mx + 2, mid_y)
                cr.line_to(mx - 4, mid_y + 5)
                cr.close_path()
                cr.fill()

        color = green if self.audio_kind != "source" else muted
        for i, c in enumerate(self.aclips):
            clip_lane_y = self._lane_y("audio", c.track)
            d = self._clip_src_dur(c, "a")
            speed = c.playback_speed()
            t0, t1 = self._clip_times(c, d)
            gx0 = self._t_to_x(c.start, width)
            gx1 = self._t_to_x(c.start + (d / speed if d > 0 else 0.0), width)
            self._draw_clip(cr, gx0, clip_lane_y + clip_y, max(3.0, gx1 - gx0), clip_h, color, "", 0.28)
            x0 = self._t_to_x(t0, width)
            x1 = self._t_to_x(t1, width)
            name = self.clip_names.get(c.media_id) or (
                self.audio_name if i == self.sel_a or len(self.aclips) == 1 else ""
            )
            self._draw_clip(
                cr, x0, clip_lane_y + clip_y, max(3.0, x1 - x0), clip_h, color, name
            )
            if self.audio_kind != "source":
                self._draw_handles(cr, x0, x1, clip_lane_y + clip_y, clip_h, fg)
            if i in self.sel_as or i == self.sel_a:
                cr.set_source_rgb(*fg)
                cr.set_line_width(3.0 if i == self.sel_a else 2.0)
                cr.rectangle(x0, clip_lane_y + clip_y, max(3.0, x1 - x0), clip_h)
                cr.stroke()

        if self._drop_hover is not None:
            kind, t, track_no = self._drop_hover
            hover_y = self._lane_y(kind, track_no)
            if kind == "video" and self.video_dur > 0:
                gx0 = self._t_to_x(t, width)
                gx1 = self._t_to_x(t + self.video_dur, width)
                self._draw_clip(
                    cr, gx0, hover_y + clip_y, max(3.0, gx1 - gx0), clip_h, sel, "", 0.5
                )
            elif kind == "audio" and self.audio_dur > 0:
                gx0 = self._t_to_x(t, width)
                gx1 = self._t_to_x(t + self.audio_dur, width)
                self._draw_clip(
                    cr, gx0, hover_y + clip_y, max(3.0, gx1 - gx0), clip_h, green, "", 0.5
                )

        if 0 <= self.sel_v < len(self.vclips):
            c = self.vclips[self.sel_v]
            t0, t1 = self._clip_times(c, self._clip_src_dur(c, "v"))
            if t1 > t0:
                x_in = self._t_to_x(t0, width)
                x_out = self._t_to_x(t1, width)
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
        self.resolution = DEFAULT_RESOLUTION
        self.audio_fit = False
        self.use_video_soundtrack = True
        self.video_start = 0.0
        self.audio_start = 0.0
        self.audio_in = 0.0
        self.audio_out: float | None = None
        self.video_clips: list[ClipInst] = []
        self.audio_clips: list[ClipInst] = []
        self.sel_v = -1
        self.sel_a = -1
        self.sel_vs: set[int] = set()
        self.sel_as: set[int] = set()
        self.sel_kind = ""
        self.keyboard_mode = ""
        self.keyboard_increment = 1.0
        self._clip_playing = False
        self._audio_pending = False
        self.project_path: Path | None = None
        self.set_title(self._title_text())
        self._loading = False
        self._autosave_src: int = 0
        self.exporting = False
        self._preview_rendering = False
        self._preview_cancel = threading.Event()
        self._preview_generation = 0
        self._project_warnings: list[str] = []
        self._compiled_mode = False
        self._compiled_stale = False
        self._compiled_path: Path | None = None
        self._compiled_hash: str | None = None
        self._compiled_kind: str = ""
        self._compiled_duration = 0.0
        self._compiled_window: tuple[float, float] | None = None
        self._cache_segments: list[TimelineSegment] = []
        self._playthrough_path: Path | None = None
        self._playthrough_hash: str | None = None
        self._playthrough_playing = False
        self._play_after_render: float | None = None
        self._edit_vmedia_path: Path | None = None
        self.playing = False
        self._vmedia: Gtk.MediaFile | None = None
        self._preview_proc: subprocess.Popen[bytes] | None = None
        self._preview_mix_proc: subprocess.Popen[bytes] | None = None
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
        self.preview.on_pan_end = self._on_preview_pan_end
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
        self.btn_render_preview = Gtk.Button(label="Render Preview")
        self.btn_render_preview.set_tooltip_text(
            "Render the timeline preview without playing it (:rp)"
        )
        self.btn_render_preview.connect("clicked", self._on_render_preview)
        transport.append(self.btn_render_preview)
        self.btn_split = Gtk.Button(label="Split")
        self.btn_split.set_tooltip_text("Split the selected clip at the playhead (T)")
        self.btn_split.connect("clicked", lambda *_: self._split_selected_clip())
        transport.append(self.btn_split)
        self.clock = Gtk.Label(label="0.00 / 0.00")
        self.clock.add_css_class("dim-label")
        transport.append(self.clock)
        left.append(transport)
        self.keyboard_hint = Gtk.Label(xalign=0)
        self.keyboard_hint.set_wrap(True)
        left.append(self.keyboard_hint)
        self.command_revealer = Gtk.Revealer()
        self.command_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        command_box = Gtk.Box(spacing=8)
        command_box.append(Gtk.Label(label=":"))
        self.command_entry = Gtk.Entry()
        self.command_entry.set_hexpand(True)
        self.command_entry.set_placeholder_text("command (r916, r43, rp)")
        self.command_entry.connect("activate", self._on_command_activate)
        command_keys = Gtk.EventControllerKey()
        command_keys.connect("key-pressed", self._on_command_key_pressed)
        self.command_entry.add_controller(command_keys)
        command_box.append(self.command_entry)
        self.command_revealer.set_child(command_box)
        left.append(self.command_revealer)
        self.timeline = Timeline()
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._exit_keyboard_mode())
        self.timeline.add_controller(focus)
        self.timeline.on_seek = self._on_timeline_seek
        self.timeline.on_video_move = self._on_video_move
        self.timeline.on_audio_move = self._on_audio_move
        self.timeline.on_video_trim = self._on_video_trim
        self.timeline.on_audio_trim = self._on_audio_trim
        self.timeline.on_track_change = self._on_track_change
        self.timeline.on_navigation_track_change = self._on_navigation_track_change
        self.timeline.on_place = self._place_clip
        self.timeline.on_select = self._on_clip_select
        self.timeline_scroll = Gtk.ScrolledWindow()
        self.timeline_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.timeline_scroll.set_hexpand(True)
        self.timeline_scroll.set_vexpand(False)
        self.timeline_scroll.set_min_content_height(Timeline._HEIGHT)
        self.timeline_scroll.set_child(self.timeline)
        self.timeline_scroll.connect("notify::width", lambda *_: self.timeline._sync_canvas())
        left.append(self.timeline_scroll)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        right.set_margin_end(4)

        right.append(self._section("Video"))
        row = Gtk.Box(spacing=8)
        self.btn_open_video = Gtk.Button(label="Open video")
        self.btn_open_video.connect("clicked", lambda *_: self._pick("video"))
        row.append(self.btn_open_video)
        right.append(row)

        right.append(self._section("Transition after this clip"))
        self.transition_type = Gtk.DropDown.new_from_strings(
            ["None", "Dissolve", "White flash"]
        )
        self.transition_type.set_tooltip_text(
            "Applies into the next touching video segment; gaps stay hard cuts"
        )
        self.transition_type.connect("notify::selected", self._on_transition_changed)
        right.append(self.transition_type)
        transition_dur = Gtk.Box(spacing=8)
        transition_dur.append(Gtk.Label(label="Duration"))
        self.transition_spin = Gtk.SpinButton.new_with_range(0.1, 3.0, 0.05)
        self.transition_spin.set_digits(2)
        self.transition_spin.set_value(0.5)
        self.transition_spin.set_tooltip_text("0.1–3.0 seconds")
        self.transition_spin.connect("value-changed", self._on_transition_changed)
        transition_dur.append(self.transition_spin)
        transition_dur.append(Gtk.Label(label="seconds"))
        right.append(transition_dur)
        self.transition_hint = self._wrapping_label("")
        right.append(self.transition_hint)
        self.cache_hint = self._wrapping_label(
            "Green = rendered preview; red = raw timeline. Play (Space) starts immediately. "
            "Render Preview (:rp) bakes transitions."
        )
        self.cache_hint.add_css_class("dim-label")
        right.append(self.cache_hint)
        self.btn_preview_cancel = Gtk.Button(label="Cancel render")
        self.btn_preview_cancel.set_sensitive(False)
        self.btn_preview_cancel.connect("clicked", self._on_cancel_preview_render)
        right.append(self.btn_preview_cancel)
        self.compiled_preview_label = self._wrapping_label("")
        self.compiled_preview_label.add_css_class("dim-label")
        right.append(self.compiled_preview_label)
        self.video_label = self._wrapping_label("none")
        right.append(self.video_label)

        right.append(self._section("Position / scale selected clip"))
        transform = Gtk.Grid(column_spacing=8, row_spacing=6)
        self.transform_x_spin = Gtk.SpinButton.new_with_range(-4096, 4096, 1)
        self.transform_y_spin = Gtk.SpinButton.new_with_range(-4096, 4096, 1)
        self.transform_scale_spin = Gtk.SpinButton.new_with_range(1.0, 4.0, 0.05)
        self.transform_x_spin.set_digits(0)
        self.transform_y_spin.set_digits(0)
        self.transform_scale_spin.set_digits(2)
        self.transform_scale_spin.set_value(1.0)
        for row_i, (label, spin) in enumerate(
            (
                ("X", self.transform_x_spin),
                ("Y", self.transform_y_spin),
                ("Scale", self.transform_scale_spin),
            )
        ):
            transform.attach(Gtk.Label(label=label, xalign=0), 0, row_i, 1, 1)
            transform.attach(spin, 1, row_i, 1, 1)
            spin.connect("value-changed", self._on_transform_changed)
        self.transform_x_spin.set_tooltip_text(
            "Move the source clip left or right within the project frame."
        )
        self.transform_y_spin.set_tooltip_text(
            "Move the source clip up or down within the project frame."
        )
        self.transform_scale_spin.set_tooltip_text(
            "Zoom the source clip. 1.00 fills the project frame."
        )
        self.btn_transform_reset = Gtk.Button(label="Reset")
        self.btn_transform_reset.connect("clicked", self._on_transform_reset)
        transform.attach(self.btn_transform_reset, 2, 0, 1, 3)
        right.append(transform)

        right.append(self._section("Speed"))
        speed_row = Gtk.Box(spacing=8)
        speed_row.append(Gtk.Label(label="Rate", xalign=0))
        self.speed_spin = Gtk.SpinButton.new_with_range(MIN_SPEED, MAX_SPEED, 0.05)
        self.speed_spin.set_digits(2)
        self.speed_spin.set_value(DEFAULT_SPEED)
        self.speed_spin.set_tooltip_text(
            "Playback rate for the selected clip(s). "
            "Faster shortens the timeline; slower lengthens it. "
            "Export audio pitch follows rate (atempo)."
        )
        self.speed_spin.connect("value-changed", self._on_speed_changed)
        speed_row.append(self.speed_spin)
        speed_row.append(Gtk.Label(label="×"))
        self.btn_speed_reset = Gtk.Button(label="1×")
        self.btn_speed_reset.set_tooltip_text("Reset selected clip(s) to 1×")
        self.btn_speed_reset.connect("clicked", self._on_speed_reset)
        speed_row.append(self.btn_speed_reset)
        right.append(speed_row)
        self.speed_hint = self._wrapping_label("")
        self.speed_hint.add_css_class("dim-label")
        right.append(self.speed_hint)

        right.append(self._section("Audio"))
        row = Gtk.Box(spacing=8)
        self.btn_open_audio = Gtk.Button(label="Open audio")
        self.btn_open_audio.connect("clicked", lambda *_: self._pick("audio"))
        row.append(self.btn_open_audio)
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

        right.append(self._section("Resolution"))
        resolutions = Gtk.Box(spacing=6)
        self.resolution_buttons: dict[str, Gtk.ToggleButton] = {}
        res_group = None
        for name in RESOLUTIONS:
            label = name.capitalize()
            tb = Gtk.ToggleButton(label=label)
            if res_group is None:
                res_group = tb
            else:
                tb.set_group(res_group)
            if name == DEFAULT_RESOLUTION:
                tb.set_active(True)
            tb.connect("toggled", self._on_resolution, name)
            self.resolution_buttons[name] = tb
            resolutions.append(tb)
        right.append(resolutions)
        self.resolution_size_label = self._wrapping_label("")
        right.append(self.resolution_size_label)
        self._refresh_resolution_label()

        right.append(self._section("Trim"))
        trim = Gtk.Box(spacing=8)
        trim.append(Gtk.Label(label="In"))
        self.in_spin = Gtk.SpinButton.new_with_range(0, 99999, 0.05)
        self.in_spin.set_digits(2)
        self.in_spin.connect("value-changed", self._on_trim_spin_changed)
        trim.append(self.in_spin)
        trim.append(Gtk.Label(label="Out"))
        self.out_spin = Gtk.SpinButton.new_with_range(0, 99999, 0.05)
        self.out_spin.set_digits(2)
        self.out_spin.connect("value-changed", self._on_trim_spin_changed)
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
        self._sync_transform_controls()
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

    def _editing_locked(self) -> bool:
        return bool(self._compiled_mode)

    def _guard_edit(self, action: str = "edit") -> bool:
        """Return True if the edit action may proceed."""
        if compiled_allows_action(action, compiled_mode=self._editing_locked()):
            return True
        self._set_status("Rendered preview — editing locked")
        return False

    def _show_command_line(self) -> None:
        self.command_entry.set_text("")
        self.command_revealer.set_reveal_child(True)
        self.command_entry.grab_focus()

    def _hide_command_line(self) -> None:
        self.command_revealer.set_reveal_child(False)
        self.timeline.grab_focus()

    def _on_command_key_pressed(
        self, _controller: Gtk.EventControllerKey, keyval: int, _code: int, _state: int
    ) -> bool:
        if keyval == Gdk.KEY_Escape:
            self._hide_command_line()
            return True
        return False

    def _on_command_activate(self, *_args: object) -> None:
        command = self.command_entry.get_text().strip().lower().lstrip(":")
        self._hide_command_line()
        parsed = parse_command(command)
        if parsed is None:
            if command:
                self._set_status(f"Unknown command: :{command}")
            return
        if parsed.name == "render_preview":
            self._on_render_preview()
            return
        if parsed.name == "aspect" and parsed.value:
            if not self._guard_edit("aspect"):
                return
            button = self.aspect_buttons[parsed.value]
            if button.get_active():
                self._set_status(f"Aspect already {parsed.value}")
            else:
                button.set_active(True)

    def _on_render_preview(self, *_args: object) -> None:
        """Bake the current timeline preview without changing playback state."""
        if self._busy_rendering() or not self.video_path or not self.video_clips:
            return
        self._play_after_render = None
        self._start_playthrough_render()

    def _on_key_pressed(self, _c: Gtk.EventControllerKey, keyval: int, _code: int, state: int) -> bool:
        # Timeline is a leaf widget: exact ownership also excludes inspector
        # entries, buttons, popovers, dialogs, and the colon command entry.
        if self.get_focus() is not self.timeline:
            return False
        mods = state & Gtk.accelerator_get_default_mod_mask()
        ctrl = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(mods & Gdk.ModifierType.SHIFT_MASK)
        extra = mods & ~(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        if extra:
            return False
        if not mods and keyval == Gdk.KEY_m:
            self._enter_keyboard_mode("move")
            return True
        if self.keyboard_mode and not mods and keyval == Gdk.KEY_Escape:
            self._exit_keyboard_mode()
            return True
        if self.keyboard_mode and not mods and keyval in (Gdk.KEY_h, Gdk.KEY_l):
            self._nudge_keyboard(-1 if keyval == Gdk.KEY_h else 1)
            return True
        if self.keyboard_mode and not ctrl and keyval in (Gdk.KEY_H, Gdk.KEY_L):
            return True
        if ctrl and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            if shift:
                self._on_redo()
            else:
                self._on_undo()
            return True
        if ctrl and not shift and keyval in (Gdk.KEY_y, Gdk.KEY_Y):
            self._on_redo()
            return True
        if keyval == Gdk.KEY_colon and not ctrl and not extra:
            self._show_command_line()
            return True
        if (
            not ctrl
            and not extra
            and keyval in (Gdk.KEY_h, Gdk.KEY_H, Gdk.KEY_l, Gdk.KEY_L)
        ):
            delta = -1 if keyval in (Gdk.KEY_h, Gdk.KEY_H) else 1
            self.timeline.move_clip_selection(delta, extend=shift)
            return True
        if (
            not ctrl
            and not extra
            and keyval in (Gdk.KEY_j, Gdk.KEY_J, Gdk.KEY_k, Gdk.KEY_K)
        ):
            delta = 1 if keyval in (Gdk.KEY_j, Gdk.KEY_J) else -1
            self._exit_keyboard_mode()
            self.timeline.move_navigation_track(delta)
            return True
        if mods:
            return False
        if keyval in (Gdk.KEY_Escape,):
            if self.sel_v >= 0 or self.sel_a >= 0:
                self.timeline.clear_selection()
                self._set_status("Selection cleared")
                return True
            return False
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            if not self._guard_edit("delete"):
                return True
            return self._delete_selected_clip()
        if keyval in (Gdk.KEY_t, Gdk.KEY_T):
            if not self._guard_edit("split"):
                return True
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

    def _keyboard_target(self) -> tuple[str, list[ClipInst], int, set[int]]:
        kind = self._selected_clip_kind()
        clips = self.video_clips if kind == "video" else (
            self.timeline.aclips if self.timeline.audio_kind == "source" else self.audio_clips
        )
        primary = self.sel_v if kind == "video" else self.sel_a
        selected = self.sel_vs if kind == "video" else self.sel_as
        if (not kind or not 0 <= primary < len(clips)
                or kind != self.timeline.nav_kind
                or clips[primary].track != self.timeline.nav_track):
            return "", [], -1, set()
        return kind, clips, primary, {i for i in selected or {primary} if 0 <= i < len(clips)}

    def _enter_keyboard_mode(self, mode: str) -> None:
        if self.exporting or not self._guard_edit("move"):
            return
        if not self._keyboard_target()[0]:
            self._set_status("Select a clip on the active track")
            return
        self._stop()
        self.keyboard_mode = mode
        self._show_keyboard_mode()

    def _exit_keyboard_mode(self) -> None:
        self.keyboard_mode = ""
        self.keyboard_hint.set_text("")

    def _show_keyboard_mode(self) -> None:
        kind, _clips, _primary, selected = self._keyboard_target()
        self.keyboard_hint.set_text(
            f"{self.keyboard_mode.upper()} · {kind[:1].upper()}{self.timeline.nav_track} · "
            f"{len(selected)} selected · {self.keyboard_increment:g}s · h/l earlier/later · Esc exits"
        )

    def _apply_keyboard_clips(self, kind: str, clips: list[ClipInst]) -> None:
        self._flush_checkpoint()
        self._checkpoint()
        self._stop()
        if kind == "video":
            self.video_clips = clips
            clip = clips[self.sel_v]
            self._loading = True
            try:
                self.in_spin.set_value(clip.in_s)
                self.out_spin.set_value(clip.out_s)
            finally:
                self._loading = False
        else:
            # Detach mirrored soundtrack only when an edit actually changes it.
            self.audio_clips = clips
            self.use_video_soundtrack = False
            self._loading = True
            try:
                self.follow_in.set_active(False)
            finally:
                self._loading = False
            self._bind_audio(clips[self.sel_a].media_id)
        # The editor now owns a replacement list, so refresh the Timeline's
        # shallow list and switch source soundtrack mode to independent audio.
        self._sync_timeline_clips()
        self._refresh_fit()
        self._checkpoint()
        self._schedule_autosave()
        self.timeline._ensure_clip_visible(kind, self.sel_v if kind == "video" else self.sel_a)
        self._apply_timeline_frame(self.timeline.playhead, start_media=False)

    def _nudge_keyboard(self, direction: int) -> None:
        if self.exporting or not self._guard_edit("move"):
            return
        kind, clips, _primary, selected = self._keyboard_target()
        if not kind:
            self._exit_keyboard_mode()
            self._set_status("Select a clip on the active track")
            return
        changed = move_clips(clips, selected, direction * self.keyboard_increment)
        if changed == clips:
            self._set_status("Timeline boundary")
            return
        self._apply_keyboard_clips(kind, changed)
        self._show_keyboard_mode()

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
                (
                    round(c.start, 3),
                    round(c.in_s, 3),
                    round(c.out_s, 3),
                    c.media_id,
                    round(c.transform_x, 3),
                    round(c.transform_y, 3),
                    round(c.scale, 4),
                    int(c.track),
                    c.transition,
                    round(float(c.transition_s), 3),
                )
                for c in p.video_clips
            ),
            tuple(
                (
                    round(c.start, 3), round(c.in_s, 3), round(c.out_s, 3),
                    c.media_id, int(c.track),
                )
                for c in p.audio_clips
            ),
            tuple((m.id, m.kind, str(m.path)) for m in p.media),
            bool(p.audio_follows_in),
            bool(p.audio_fit),
            bool(p.use_video_soundtrack),
            str(p.path) if p.path else "",
        )

    def _update_history_actions(self) -> None:
        locked = self._editing_locked()
        if self._undo_action is not None:
            self._undo_action.set_enabled(not locked and self._hist_i > 0)
        if self._redo_action is not None:
            self._redo_action.set_enabled(
                not locked
                and self._hist_i >= 0
                and self._hist_i < len(self._history) - 1
            )

    def _on_preview_pan_end(self, *_args: object) -> None:
        self._checkpoint()
        self._refresh_cache_bar()

    def _checkpoint(self, *_args: object) -> None:
        if self._loading or self._applying_history or self._editing_locked():
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
        if self._loading or self._applying_history or self._editing_locked():
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
        if not self._guard_edit("undo"):
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
        if not self._guard_edit("redo"):
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
            resolution=self.resolution,
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
            crossfade_s=0.0,
            path=self.project_path,
        )

    def _title_text(self, project_name: str | None = None) -> str:
        base = "Clip editor"
        if application_id() != "local.clip.Editor":
            base = "Clip editor (dev)"
        name = project_name
        if name is None and self.project_path:
            name = self.project_path.name
        if name:
            return f"{base} — {name}"
        return base

    def _update_title(self) -> None:
        self.set_title(self._title_text())

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
            # Keep offline rows in the project/bin so their ids and paths can
            # be repaired later instead of being lost on the next autosave.
            self.media.append(src.copy())
            if not src.path.is_file():
                continue
            try:
                info = probe(src.path)
            except ProbeError:
                continue
            self.media_info[src.id] = info
            if src.kind == "video":
                try:
                    self.media_thumbs[src.id] = _load_frame(src.path)
                except (ProbeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    pass

    def _apply_project(self, proj: Project) -> None:
        self._exit_keyboard_mode()
        was_loading = self._loading
        self._loading = True
        self._abandon_preview_render()
        if self._compiled_mode:
            self._reset_compiled_preview_flags()
        self._stop()
        try:
            self._install_media_list(proj.media)
            if not self.media:
                self._unload_video()
                self._unload_audio()
            if proj.aspect in self.aspect_buttons:
                self.aspect_buttons[proj.aspect].set_active(True)
                self.aspect = proj.aspect
                dw, dh = dest_size(proj.aspect, proj.resolution)
                self.aspect_frame.set_ratio(dw / dh)
            if getattr(proj, "resolution", DEFAULT_RESOLUTION) in self.resolution_buttons:
                self.resolution = proj.resolution
                self.resolution_buttons[proj.resolution].set_active(True)
                self._refresh_resolution_label()
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
            self.video_start = float(proj.video_start or 0.0)
            self.audio_start = float(proj.audio_start or 0.0)
            self.audio_in = max(0.0, float(proj.audio_in or 0.0))
            self.audio_out = proj.audio_out
            self.video_clips = [c.copy() for c in proj.video_clips]
            self.audio_clips = [c.copy() for c in proj.audio_clips]
            # Modern projects own explicit clip lists, including empty lists.
            # Never recreate deleted/detached clips from a stale bound path.
            if not proj.media and proj.video and self.video_path and not self.video_clips:
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
            if not proj.media and proj.audio and self.audio_path and not self.audio_clips:
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
            self.sel_vs = {0} if self.video_clips else set()
            self.sel_a = 0 if self.audio_clips else -1
            self.sel_as = {self.sel_a} if self.sel_a >= 0 else set()
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
            self._loading = was_loading

    def _probe_project_media(self, proj: Project) -> None:
        """Collect recoverable media warnings without rejecting the project."""
        warnings = media_load_errors(proj)
        for m in proj.media:
            if not m.path.is_file():
                continue
            try:
                info = probe(m.path)
            except ProbeError:
                warnings.append(f"could not read media {m.id}: {m.path.name}")
                continue
            if m.kind == "video" and not info.get("has_video"):
                warnings.append(f"video media {m.id} has no video stream: {m.path.name}")
            if m.kind == "audio" and not info.get("has_audio"):
                warnings.append(f"audio media {m.id} has no audio stream: {m.path.name}")
        self._project_warnings = warnings

    def _reset_history_to_current(self) -> None:
        self._history = [self._current_project()]
        self._hist_i = 0
        if self._ckpt_src:
            GLib.source_remove(self._ckpt_src)
            self._ckpt_src = 0
        self._update_history_actions()

    def _load_project_file(self, path: Path) -> None:
        try:
            proj = read_project(path)
            self._probe_project_media(proj)
        except ProjectError as exc:
            self._set_status(str(exc))
            return
        # Cancel a pending autosave so a half-applied project cannot flush.
        if self._autosave_src:
            GLib.source_remove(self._autosave_src)
            self._autosave_src = 0
        if self._ckpt_src:
            GLib.source_remove(self._ckpt_src)
            self._ckpt_src = 0
        self._loading = True
        try:
            self._apply_project(proj)
            self.project_path = path
            self._update_title()
        finally:
            self._loading = False
        # Undo must not reach back into the previously open project.
        self._reset_history_to_current()
        self._schedule_autosave()
        if self._project_warnings:
            first = self._project_warnings[0]
            more = len(self._project_warnings) - 1
            suffix = f" (+{more} more)" if more else ""
            self._set_status(f"Opened {path.name} with media warnings: {first}{suffix}")
        else:
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
        self._abandon_preview_render()
        self._stop()
        self._reset_compiled_preview_flags()
        self.media = []
        self.media_info = {}
        self.media_thumbs = {}
        self._media_bin_ids = []
        self._unload_video()
        self._unload_audio()
        self.project_path = None
        self.aspect = "9:16"
        self.resolution = DEFAULT_RESOLUTION
        dw, dh = dest_size("9:16", self.resolution)
        self.aspect_frame.set_ratio(dw / dh)
        nine = self.aspect_buttons.get("9:16")
        med = self.resolution_buttons.get(DEFAULT_RESOLUTION)
        if med is not None:
            med.set_active(True)
        self._refresh_resolution_label()
        if nine is not None and not nine.get_active():
            nine.set_active(True)
        self.preview.pan_x = 0.5
        self.preview.pan_y = 0.5
        self.preview.read_only = False
        self.preview.set_cursor_from_name("grab")
        self.preview.queue_draw()
        self.in_spin.set_value(0)
        self.out_spin.set_value(0)
        self.follow_in.set_active(False)
        self.use_video_soundtrack = True
        self.video_start = 0.0
        self.audio_start = 0.0
        self.audio_in = 0.0
        self.audio_out = None
        self.video_clips = []
        self.audio_clips = []
        self.sel_v = -1
        self.sel_a = -1
        self.sel_vs: set[int] = set()
        self.sel_as: set[int] = set()
        self.sel_kind = ""
        self.btn_play.set_label("Play")
        self.progress.set_fraction(0)
        self.timeline.set_clips()
        self.timeline.set_playhead(0)
        self.timeline.set_range(0.0, 0.0)
        self.timeline.set_duration(0.0)
        self.timeline.set_read_only(False)
        self.preview.set_blank(False)
        self._sync_transition_controls()
        self._sync_compiled_preview_controls()
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
        # Only legacy empty ids may use a compatible fallback. Preserve an
        # explicit unknown id as offline instead of displaying another file.
        if kind and not c.media_id:
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
        if item is None or (item.kind != "audio" and not
                            (self.media_info.get(mid) or {}).get("has_audio")):
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
            if item is not None and (item.kind == "audio" or
                                    (self.media_info.get(item.id) or {}).get("has_audio")):
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
        if self._compiled_mode:
            return max(0.05, float(self._compiled_duration))
        ends = [0.05]
        for c in self.video_clips:
            dur = self._media_dur(c.media_id) or float(
                (self.video_info or {}).get("duration") or 0
            )
            _t0, t1 = c.used_times(dur)
            ends.append(t1)
        for c in self.audio_clips:
            dur = self._media_dur(c.media_id) or float(
                (self.audio_info or {}).get("duration") or 0
            )
            _t0, t1 = c.used_times(dur)
            ends.append(t1)
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
        if self._editing_locked():
            return Gdk.DragAction(0)
        return Gdk.DragAction.COPY

    def _on_close(self, *_args: object) -> bool:
        self._abandon_preview_render()
        self._stop()
        self._reset_compiled_preview_flags()
        if self._ckpt_src:
            GLib.source_remove(self._ckpt_src)
            self._ckpt_src = 0
        self._flush_autosave()
        return False

    def _refresh_crop(self) -> None:
        if not self.video_info:
            self.crop_label.set_text("")
            return
        dw, dh = dest_size(self.aspect, self.resolution)
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

    def _selected_video_clip(self) -> ClipInst | None:
        if self.sel_kind == "video" and 0 <= self.sel_v < len(self.video_clips):
            return self.video_clips[self.sel_v]
        return None

    def _sync_transform_controls(self) -> None:
        clip = self._selected_video_clip()
        enabled = clip is not None and not self._editing_locked()
        controls = (
            self.transform_x_spin,
            self.transform_y_spin,
            self.transform_scale_spin,
            self.btn_transform_reset,
        )
        for widget in controls:
            widget.set_sensitive(enabled and not self.exporting)
        was_loading = self._loading
        self._loading = True
        try:
            self.transform_x_spin.set_value(clip.transform_x if clip else 0.0)
            self.transform_y_spin.set_value(clip.transform_y if clip else 0.0)
            self.transform_scale_spin.set_value(clip.scale if clip else 1.0)
        finally:
            self._loading = was_loading
        self._sync_speed_controls()

    def _on_transform_changed(self, *_args: object) -> None:
        if self._loading:
            return
        if not self._guard_edit("transform"):
            self._sync_transform_controls()
            return
        clip = self._selected_video_clip()
        if clip is None:
            return
        clip.transform_x = self.transform_x_spin.get_value()
        clip.transform_y = self.transform_y_spin.get_value()
        clip.scale = max(1.0, self.transform_scale_spin.get_value())
        self._apply_timeline_frame(self.timeline.playhead, start_media=False)
        self._schedule_checkpoint()
        self._schedule_autosave()

    def _on_transform_reset(self, *_args: object) -> None:
        if not self._guard_edit("transform"):
            return
        if self._selected_video_clip() is None:
            return
        self.transform_x_spin.set_value(0.0)
        self.transform_y_spin.set_value(0.0)
        self.transform_scale_spin.set_value(1.0)
        self._checkpoint()

    def _speed_target_clips(self) -> list[ClipInst]:
        """Clips that should receive a speed change (video multi-select, else audio)."""
        indices = self._selected_video_indices()
        if indices:
            return [self.video_clips[i] for i in indices]
        if self.sel_kind == "audio" and 0 <= self.sel_a < len(self.audio_clips):
            return [self.audio_clips[i] for i in sorted(self.sel_as or {self.sel_a})
                    if 0 <= i < len(self.audio_clips)]
        return []

    def _sync_speed_controls(self) -> None:
        clips = self._speed_target_clips()
        enabled = bool(clips) and not self.exporting and not self._editing_locked()
        was_loading = self._loading
        self._loading = True
        try:
            if not clips:
                self.speed_spin.set_value(DEFAULT_SPEED)
                self.speed_hint.set_text("")
            else:
                speeds = [c.playback_speed() for c in clips]
                self.speed_spin.set_value(speeds[0])
                if len(speeds) > 1 and max(speeds) - min(speeds) > 0.001:
                    self.speed_hint.set_text(
                        f"{len(clips)} selected — spin sets all to the same rate"
                    )
                else:
                    src = clips[0].source_len(
                        self._media_dur(clips[0].media_id)
                        if clips[0].media_id
                        else 0.0
                    )
                    tl = src / speeds[0] if speeds[0] > 0 else 0.0
                    self.speed_hint.set_text(
                        f"Source {src:.2f}s → timeline {tl:.2f}s at {speeds[0]:.2f}×"
                    )
            self.speed_spin.set_sensitive(enabled)
            self.btn_speed_reset.set_sensitive(enabled)
        finally:
            self._loading = was_loading

    def _on_speed_changed(self, *_args: object) -> None:
        if self._loading:
            return
        if not self._guard_edit("speed"):
            self._sync_speed_controls()
            return
        clips = self._speed_target_clips()
        if not clips:
            return
        speed = normalize_speed(self.speed_spin.get_value())
        for clip in clips:
            clip.speed = speed
        self._sync_timeline_clips()
        self._sync_speed_controls()
        self._apply_timeline_frame(self.timeline.playhead, start_media=False)
        self._schedule_checkpoint()
        self._schedule_autosave()

    def _on_speed_reset(self, *_args: object) -> None:
        if not self._guard_edit("speed"):
            return
        if not self._speed_target_clips():
            return
        self.speed_spin.set_value(DEFAULT_SPEED)

    def _refresh_export_name(self) -> None:
        if not self.video_path:
            self.export_name.set_text("name is assigned on export (.mp4)")
            return
        path = default_out_path(self.video_path, self.aspect)
        self.export_name.set_text(f"{path.parent.name}/{path.name}")
        self.export_name.set_tooltip_text(str(path))

    def _sync_timeline_clips(self) -> None:
        if self._compiled_mode:
            self._invalidate_compiled_preview_if_stale()
            if not self._compiled_mode:
                return
            self._sync_transform_controls()
            self._sync_transition_controls()
            self._sync_compiled_preview_controls()
            return
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
        self.sel_v, self.sel_vs = prune_video_selection(
            self.sel_vs, self.sel_v, len(self.video_clips)
        )
        self.sel_a, self.sel_as = prune_video_selection(
            self.sel_as, self.sel_a,
            len(self.audio_clips if kind == "replace" else source_aclips),
        )
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
            sel_vs=self.sel_vs,
            sel_as=self.sel_as,
            src_durs=self._src_durs(),
            clip_names=self._clip_names(),
        )
        self._refresh_cache_bar()
        if 0 <= self.sel_v < len(self.video_clips):
            c = self.video_clips[self.sel_v]
            self.timeline.set_range(c.in_s, c.out_s)
        self.btn_export.set_sensitive(
            bool(self.video_clips) and not self.exporting and not self._preview_rendering
        )
        self._sync_transform_controls()
        self._sync_transition_controls()
        self._sync_compiled_preview_controls()
        self._mark_compiled_stale_if_needed()
        self._refresh_media()

    def _refresh_cache_bar(self) -> None:
        if not self.video_clips or not self.video_path:
            self._cache_segments = []
            self.timeline.set_cache_spans([])
            return
        self._cache_segments = build_timeline_segments(
            video_clips=self.video_clips,
            audio_clips=self.audio_clips,
            src_durs=self._src_durs(),
            aspect=self.aspect,
            pan_x=self.preview.pan_x,
            pan_y=self.preview.pan_y,
            audio_follows_in=self.follow_in.get_active(),
            use_video_soundtrack=self.use_video_soundtrack,
            audio_offset=0.0,
            media=self.media,
        )
        self.timeline.set_cache_spans(
            [(s.t0, s.t1, s.is_green()) for s in self._cache_segments]
        )
        self._sync_compiled_preview_controls()

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

    def _on_trim_spin_changed(self, *_args: object) -> None:
        """Inspector In/Out only; do not run this on timeline click/trim."""
        if self._loading:
            return
        if 0 <= self.sel_v < len(self.video_clips):
            self.video_clips[self.sel_v].in_s = self.in_spin.get_value()
            self.video_clips[self.sel_v].out_s = self.out_spin.get_value()
        self._refresh_fit()

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

    def _clip_used_end(self, clip: ClipInst, *, kind: str = "video") -> float:
        if kind == "video":
            fallback = float((self.video_info or {}).get("duration") or 0)
        else:
            fallback = float((self.audio_info or {}).get("duration") or 0)
        dur = self._media_dur(clip.media_id) or fallback
        inn = max(0.0, float(clip.in_s))
        out = float(clip.out_s) if clip.out_s > inn else (dur if dur > inn else inn)
        if dur > 0:
            out = min(out, dur)
        return float(clip.start) + out

    def _clip_used_start(self, clip: ClipInst, *, kind: str = "video") -> float:
        if kind == "video":
            fallback = float((self.video_info or {}).get("duration") or 0)
        else:
            fallback = float((self.audio_info or {}).get("duration") or 0)
        dur = self._media_dur(clip.media_id) or fallback
        inn = max(0.0, float(clip.in_s))
        out = float(clip.out_s) if clip.out_s > inn else (dur if dur > inn else inn)
        if dur > 0:
            out = min(out, dur)
        if out <= inn:
            return float(clip.start)
        return float(clip.start) + inn

    def _has_touching_video_follower(self, index: int) -> bool:
        if not 0 <= index < len(self.video_clips):
            return False
        end = self._clip_used_end(self.video_clips[index])
        for j, other in enumerate(self.video_clips):
            if j == index:
                continue
            if abs(self._clip_used_start(other) - end) <= JOIN_EPS:
                return True
        return False

    def _transition_type_index(self, transition: str) -> int:
        mapping = {
            TRANSITION_NONE: 0,
            TRANSITION_DISSOLVE: 1,
            TRANSITION_WHITE_FLASH: 2,
        }
        return mapping.get(transition, 0)

    def _transition_from_index(self, index: int) -> str:
        return (
            TRANSITION_NONE,
            TRANSITION_DISSOLVE,
            TRANSITION_WHITE_FLASH,
        )[max(0, min(2, int(index)))]

    def _selected_video_indices(self) -> list[int]:
        if self.sel_kind != "video":
            return []
        if self.sel_vs:
            return sorted(i for i in self.sel_vs if 0 <= i < len(self.video_clips))
        if 0 <= self.sel_v < len(self.video_clips):
            return [self.sel_v]
        return []

    def _sync_transition_controls(self) -> None:
        indices = self._selected_video_indices()
        clips = [self.video_clips[i] for i in indices]
        clip = clips[0] if clips else None
        any_follower = any(self._has_touching_video_follower(i) for i in indices)
        enabled = (
            bool(clips)
            and not self.exporting
            and not self._editing_locked()
        )
        was_loading = self._loading
        self._loading = True
        try:
            if clip is None:
                self.transition_type.set_selected(0)
                self.transition_spin.set_value(0.5)
            else:
                ttype, tdur = normalize_transition(clip.transition, clip.transition_s)
                self.transition_type.set_selected(self._transition_type_index(ttype))
                if ttype == TRANSITION_NONE:
                    self.transition_spin.set_value(
                        DEFAULT_TRANSITION_S.get(TRANSITION_DISSOLVE, 0.5)
                    )
                else:
                    self.transition_spin.set_value(tdur)
            self.transition_type.set_sensitive(enabled)
            self.transition_spin.set_sensitive(
                enabled and self.transition_type.get_selected() > 0
            )
            if self._editing_locked():
                self.transition_hint.set_text("Rendered preview — editing locked")
            elif not clips:
                self.transition_hint.set_text("Select a video clip")
            elif len(clips) > 1:
                note = f"{len(clips)} clips selected"
                if not any_follower:
                    note += " · some have no following cut (still applied)"
                self.transition_hint.set_text(note)
            elif not any_follower:
                self.transition_hint.set_text(
                    "No touching video segment follows this clip (value still saved)"
                )
            else:
                self.transition_hint.set_text("")
        finally:
            self._loading = was_loading

    def _on_transition_changed(self, *_args: object) -> None:
        if self._loading:
            return
        if not self._guard_edit("transition"):
            self._sync_transition_controls()
            return
        indices = self._selected_video_indices()
        if not indices:
            return
        ttype = self._transition_from_index(self.transition_type.get_selected())
        if ttype == TRANSITION_NONE:
            applied_type, applied_dur = TRANSITION_NONE, 0.0
        else:
            raw_dur = float(self.transition_spin.get_value())
            if raw_dur <= 0.0:
                raw_dur = DEFAULT_TRANSITION_S.get(ttype, 0.5)
                was_loading = self._loading
                self._loading = True
                try:
                    self.transition_spin.set_value(raw_dur)
                finally:
                    self._loading = was_loading
            applied_type, applied_dur = normalize_transition(ttype, raw_dur)
        for idx in indices:
            clip = self.video_clips[idx]
            clip.transition = applied_type
            clip.transition_s = applied_dur
        self.transition_spin.set_sensitive(
            not self.exporting and self.transition_type.get_selected() > 0
        )
        n = len(indices)
        if n > 1:
            label = {
                TRANSITION_NONE: "None",
                TRANSITION_DISSOLVE: "Dissolve",
                TRANSITION_WHITE_FLASH: "White flash",
            }.get(applied_type, applied_type)
            self._set_status(f"Transition → {label} on {n} clips")
        self._sync_timeline_clips()
        self._schedule_autosave()
        self._schedule_checkpoint()

    def _pick(self, kind: str) -> None:
        if not self._guard_edit("drop_media"):
            return
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
        if self._editing_locked():
            # Opening a project file is allowed (exits compiled mode via apply).
            projects = [
                p
                for p in dropped
                if p.name.endswith(".clip.json") or self._looks_like_project(p)
            ]
            if not projects:
                self._set_status("Rendered preview — editing locked")
                return True
            for p in projects:
                self._load_project_file(p)
            return True
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
        if not self._guard_edit("drop_media"):
            return None
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
        if not self._guard_edit("clear_audio"):
            return
        self.media = [m for m in self.media if m.kind != "audio"]
        self._media_bin_ids = []
        self.use_video_soundtrack = False
        self._unload_audio()
        self._refresh_fit()
        self._checkpoint()
        self._set_status("Audio cleared")

    def _on_follow_in(self, *_args: object) -> None:
        if self._loading:
            return
        if not self._guard_edit("follow_in"):
            return
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
        if not self._guard_edit("fit"):
            return
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
        if not self._guard_edit("aspect"):
            return
        self._set_aspect(name)

    def _set_aspect(self, name: str) -> None:
        self.aspect = name
        w, h = dest_size(name, self.resolution)
        self.aspect_frame.set_ratio(w / h)
        self._refresh_resolution_label()
        clip = self._video_at(self.timeline.playhead)
        if clip is not None:
            self.preview.set_transform(
                clip.transform_x, clip.transform_y, clip.scale, w, h
            )
        self._refresh_crop()
        self._checkpoint()

    def _on_resolution(self, btn: Gtk.ToggleButton, name: str) -> None:
        if not btn.get_active():
            return
        if not self._guard_edit("resolution"):
            return
        self.resolution = name
        w, h = dest_size(self.aspect, self.resolution)
        self._refresh_resolution_label()
        clip = self._video_at(self.timeline.playhead)
        if clip is not None:
            self.preview.set_transform(
                clip.transform_x, clip.transform_y, clip.scale, w, h
            )
        self._refresh_crop()
        self._checkpoint()

    def _refresh_resolution_label(self) -> None:
        if not hasattr(self, "resolution_size_label"):
            return
        try:
            w, h = dest_size(self.aspect, self.resolution)
            self.resolution_size_label.set_text(f"{w}×{h}")
        except ValueError:
            self.resolution_size_label.set_text("")

    def _set_in(self, *_args: object) -> None:
        if not self._guard_edit("set_in_out"):
            return
        self.in_spin.set_value(self._playhead())
        self._refresh_fit()
        self._checkpoint()

    def _set_out(self, *_args: object) -> None:
        if not self._guard_edit("set_in_out"):
            return
        self.out_spin.set_value(self._playhead())
        self._refresh_fit()
        self._checkpoint()

    def _on_video_move(self, index: int, start: float, done: bool) -> None:
        if not self._guard_edit("move"):
            return
        if not 0 <= index < len(self.video_clips):
            return
        inn = max(0.0, self.video_clips[index].in_s)
        self.video_clips[index].start = max(-inn, float(start))
        if index == self.sel_v:
            self.video_start = self.video_clips[index].start
            # Follow-in tracks the primary clip only (not every group member).
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
        n = len(self.sel_vs) if len(self.sel_vs) > 1 else 1
        if n > 1:
            self._set_status(f"Moved {n} video clips")
        else:
            self._set_status(f"Video at {self.video_clips[index].start + inn:.2f}s")

    def _on_audio_move(self, index: int, start: float, done: bool) -> None:
        if not self._guard_edit("move"):
            return
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

    def _on_track_change(self, kind: str, index: int, track: int) -> None:
        if not self._guard_edit("track"):
            return
        clips = self.video_clips if kind == "video" else self.audio_clips
        if not 0 <= index < len(clips):
            return
        clips[index].track = max(1, min(2, int(track)))
        self._set_status(f"Moved clip to {kind[0].upper()}{clips[index].track}")

    def _on_navigation_track_change(self, kind: str, track: int) -> None:
        """Report the keyboard lane cursor without changing clip selection."""
        self._set_status(f"Keyboard track: {kind[0].upper()}{track}")

    def _on_video_trim(self, index: int, in_s: float, out_s: float, done: bool) -> None:
        if not self._guard_edit("trim"):
            return
        if not 0 <= index < len(self.video_clips):
            return
        self.video_clips[index].in_s = in_s
        self.video_clips[index].out_s = out_s
        # Timeline may have rippled follower starts on the same objects.
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
        if not self._guard_edit("trim"):
            return
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

    def _on_clip_select(
        self, kind: str, index: int, selected: frozenset[int] | None = None
    ) -> None:
        if not self._guard_edit("select_rebind"):
            return
        self._exit_keyboard_mode()
        if index < 0:
            self.sel_v, self.sel_vs = -1, set()
            self.sel_a, self.sel_as = -1, set()
            self.sel_kind = ""
            self._sync_timeline_clips()
            return
        if kind == "video":
            self.sel_a, self.sel_as = -1, set()
            self.timeline.sel_a, self.timeline.sel_as = -1, set()
        else:
            self.sel_v, self.sel_vs = -1, set()
            self.timeline.sel_v, self.timeline.sel_vs = -1, set()
        clips = self.video_clips if kind == "video" else self.timeline.aclips
        if 0 <= index < len(clips):
            self.timeline.nav_kind, self.timeline.nav_track = kind, clips[index].track
        if kind == "video":
            if 0 <= index < len(self.video_clips):
                self.sel_v = index
                self.sel_vs = (
                    set(selected)
                    if selected is not None
                    else {index}
                )
                self.sel_v, self.sel_vs = prune_video_selection(
                    self.sel_vs, self.sel_v, len(self.video_clips)
                )
                self.sel_kind = "video"
                c = self.video_clips[self.sel_v]
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
                n = len(self.sel_vs)
                if n > 1:
                    self._set_status(f"{n} video clips selected")
        elif kind == "audio":
            self.sel_vs = set()
            self.sel_kind = "audio"
            self.sel_a = index
            self.sel_as = set(selected) if selected else {index}
            self.timeline.sel_as = set(self.sel_as)
            if 0 <= index < len(self.audio_clips):
                c = self.audio_clips[index]
                self.audio_start = c.start
                self.audio_in = c.in_s
                self.audio_out = c.out_s
                item = self._clip_item(c, "audio")
                if item is not None:
                    self._bind_audio(item.id)
        self._sync_transform_controls()
        self._sync_transition_controls()

    def _place_clip(self, kind: str, t: float, media_id: str = "", track: int = 1) -> None:
        if not self._guard_edit("media_place"):
            return
        t = max(0.0, float(t))
        track = max(1, min(2, int(track)))
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
                ClipInst(start=t, in_s=0.0, out_s=dur, media_id=item.id, track=track)
            )
            self.sel_v = len(self.video_clips) - 1
            self.sel_vs = {self.sel_v}
            self.sel_kind = "video"
            self._bind_video(item.id)
            self._loading = True
            try:
                self.in_spin.set_value(0)
                self.out_spin.set_value(dur)
            finally:
                self._loading = False
            self.video_start = t
            self._set_status(f"Placed {item.path.name} on V{track} at {t:.2f}s")
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
                ClipInst(start=t, in_s=0.0, out_s=dur, media_id=item.id, track=track)
            )
            self.use_video_soundtrack = False
            self.sel_a = len(self.audio_clips) - 1
            self.sel_vs = set()
            self.sel_kind = "audio"
            self._bind_audio(item.id)
            self.audio_start = t
            self.audio_in = 0.0
            self.audio_out = dur
            if self.follow_in.get_active():
                self.follow_in.set_active(False)
            self._set_status(f"Placed {item.path.name} on A{track} at {t:.2f}s")
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
        return ""

    def _delete_selected_clip(self) -> bool:
        if self.exporting:
            return False
        if not self._guard_edit("delete"):
            return True
        kind = self._selected_clip_kind()
        if kind == "video":
            idx = self.sel_v
            if not 0 <= idx < len(self.video_clips):
                return False
            self._stop()
            del self.video_clips[idx]
            # Remap multi-select indices after the deletion.
            remapped = {
                (i - 1 if i > idx else i)
                for i in self.sel_vs
                if i != idx
            }
            if self.video_clips:
                self.sel_v, self.sel_vs = prune_video_selection(
                    remapped or {min(idx, len(self.video_clips) - 1)},
                    min(idx, len(self.video_clips) - 1),
                    len(self.video_clips),
                )
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
                self.sel_vs = set()
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
        if not self._guard_edit("split"):
            return True
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
            remapped = {
                (i + 1 if i > idx else i) for i in self.sel_vs if i != idx
            }
            remapped.add(idx + 1)
            self.sel_v = idx + 1
            self.sel_vs = remapped
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
        value = min(max(0.0, float(value)), end)
        self.clock.set_text(f"{value:.2f} / {end:.2f}")
        if self._compiled_mode:
            # Compiled A/V come from one MediaFile; keep edit ffplay/mpv silent.
            self._stop_preview_audio()
            if self._vmedia is not None:
                self.preview.set_blank(False)
                self.preview.set_media(self._vmedia)
                self._vmedia.set_muted(False)
                self._vmedia.set_volume(1.0)
                try:
                    self._vmedia.seek(int(value * 1_000_000))
                except GLib.Error:
                    pass
                if self.playing:
                    self._vmedia.play()
                else:
                    self._vmedia.pause()
            if self.playing:
                self._play_t0 = value
                self._play_mono = time.monotonic()
            self.timeline.set_playhead(value)
            return
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
        speed = c.playback_speed()
        t0 = float(c.start) + inn
        # Map timeline progress through the sped clip back onto source inn..out.
        src = inn + (float(timeline_t) - t0) * speed
        src = max(src, inn)
        if out > inn:
            src = min(src, out)
        if src_dur > 0:
            src = min(src, src_dur)
        return max(0.0, src)

    def _audio_tracks_at(self, timeline_t: float) -> tuple[ClipInst | None, ClipInst | None]:
        hits: list[ClipInst | None] = [None, None]
        dur = float((self.audio_info or {}).get("duration") or 0)
        for c in self.audio_clips:
            t0, t1 = self._clip_span(c, dur)
            if t0 - 0.02 <= timeline_t < t1:
                hits[max(1, min(2, int(c.track))) - 1] = c
        return hits[0], hits[1]

    def _preview_audio_specs(self, timeline_t: float) -> list[tuple[Path, float, float]]:
        """Active (path, source start, remaining) entries for preview audio."""
        if self.audio_clips:
            specs: list[tuple[Path, float, float]] = []
            for ac in self._audio_tracks_at(timeline_t):
                if ac is None:
                    continue
                item = self._clip_item(ac, "audio")
                if item is None:
                    continue
                adur = self._media_dur(item.id)
                start = self._source_time(ac, timeline_t, adur)
                _t0, end = self._clip_span(ac, adur)
                specs.append((item.path, start, max(0.05, end - timeline_t)))
            return specs
        if not self.use_video_soundtrack:
            return []
        vc = self._video_at(timeline_t)
        if vc is None:
            return []
        item = self._clip_item(vc, "video")
        info = self.media_info.get(item.id) if item is not None else self.video_info
        if not info or not info.get("has_audio"):
            return []
        path = item.path if item is not None else self.video_path
        if path is None:
            return []
        vdur = float(info.get("duration") or 0)
        start = self._source_time(vc, timeline_t, vdur)
        run = self._run_end(self.video_clips, self._src_durs(), timeline_t)
        remaining = max(0.05, (run if run is not None else timeline_t) - timeline_t)
        return [(path, start, remaining)]

    def _stop_preview_audio(self) -> None:
        proc = self._preview_proc
        self._preview_proc = None
        mixer = self._preview_mix_proc
        self._preview_mix_proc = None
        for child in (proc, mixer):
            if child is None or child.poll() is not None:
                continue
            try:
                child.terminate()
                child.wait(timeout=0.4)
            except (ProcessLookupError, PermissionError, OSError, subprocess.TimeoutExpired):
                try:
                    child.kill()
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
        speed = c.playback_speed()
        t0 = c.start + inn
        return t0, t0 + (out - inn) / speed

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

    def _audio_state_continuous(
        self,
        prev: tuple[ClipInst | None, ...] | None,
        nxt: tuple[ClipInst | None, ...],
        src_dur: float,
    ) -> bool:
        """True if every track carried on into a split of the same source."""
        if prev is None or len(prev) != len(nxt):
            return False
        if not any(c is not None for c in nxt):
            return False
        for was, now in zip(prev, nxt):
            if was is now:
                continue
            if not self._continuous_with(was, now, src_dur):
                return False
        return True

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
        for c in sorted(self.video_clips, key=lambda clip: int(clip.track)):
            t0, t1 = self._clip_span(c, dur)
            if t0 - 0.02 <= t < t1:
                hit = c
        return hit

    def _cached_playback_available(self, timeline_t: float) -> bool:
        """Whether this playhead position can safely use the baked proxy."""
        path = self._playthrough_path
        if path is None:
            return False
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return False
        except OSError:
            return False
        return playback_source(timeline_t, self._cache_segments) == "cache"

    def _show_playthrough(self, timeline_t: float, *, start_media: bool) -> bool:
        """Play the baked timeline proxy at 1:1. Same Play control; no lock."""
        path = self._playthrough_path
        if path is None:
            return False
        self._stop_preview_audio()
        self._load_media(path)
        if self._vmedia is None:
            return False
        dw, dh = dest_size(self.aspect, self.resolution)
        self.preview.set_transform(0.0, 0.0, 1.0, dw, dh)
        self.preview.set_blank(False)
        self.preview.set_media(self._vmedia)
        m = self._vmedia
        # Gtk.Picture is video-only; playthrough audio is ffplay/mpv on this file.
        m.set_muted(True)
        m.set_volume(0.0)
        offset = max(0.0, float(timeline_t))
        self._playthrough_playing = True

        def go(*_a: object) -> bool:
            try:
                m.seek(int(offset * 1_000_000))
            except GLib.Error:
                pass
            if start_media:
                m.play()
            else:
                m.pause()
            self.preview.queue_draw()
            return False

        if start_media:
            self._clip_playing = True
        if m.is_prepared():
            go()
        else:
            if self._prep_handler:
                try:
                    m.disconnect(self._prep_handler)
                except (TypeError, RuntimeError):
                    pass
            self._prep_handler = m.connect("notify::prepared", go)
            if start_media:
                m.play()
        return True

    def _apply_timeline_frame(self, timeline_t: float, *, start_media: bool) -> None:
        if not self._compiled_mode and self._cached_playback_available(timeline_t):
            if self._show_playthrough(timeline_t, start_media=start_media):
                return
        self._playthrough_playing = False
        clip = self._video_at(timeline_t)
        if clip is None or self._vmedia is None:
            dw, dh = dest_size(self.aspect, self.resolution)
            self.preview.set_transform(0.0, 0.0, 1.0, dw, dh)
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
        dw, dh = dest_size(self.aspect, self.resolution)
        self.preview.set_transform(
            clip.transform_x, clip.transform_y, clip.scale, dw, dh
        )
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
        if self._compiled_mode:
            self._stop_preview_audio()
            return
        self._stop_preview_audio()
        begins = self._audio_begins_at()
        if self._playthrough_playing and self._playthrough_path is not None:
            start = max(0.0, float(timeline_t))
            remaining = max(0.05, self._program_end() - start)
            specs: list[tuple[Path, float, float]] = [
                (self._playthrough_path, start, remaining)
            ]
        else:
            specs = self._preview_audio_specs(timeline_t)
        # Gain and routing follow the specs playing at timeline_t, not every clip
        # in the project: a clip on A1 at 0-5s and one on A2 at 10-15s never
        # overlap, so halving both would preview quieter than the export, which
        # mixes only genuinely overlapping segments.
        overlapping = len(specs) > 1
        preview_gain = 1.0 / len(specs) if overlapping else 1.0
        if not specs:
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
        mix_proc: subprocess.Popen[bytes] | None = None
        # FFmpeg mixes overlapping A1/A2 clips, then pipes PCM to ffplay.
        if overlapping and shutil.which("ffplay"):
            mix_cmd = [which_ffmpeg(), "-hide_banner", "-loglevel", "error"]
            for path, start, remaining in specs:
                mix_cmd += ["-ss", f"{start:.3f}", "-t", f"{remaining:.3f}", "-i", str(path)]
            pads = "".join(f"[{i}:a]" for i in range(len(specs)))
            if len(specs) > 1:
                audio_filter = (
                    pads
                    + f"amix=inputs={len(specs)}:duration=shortest:normalize=0,"
                    + f"volume={preview_gain:.6f}[a]"
                )
            else:
                audio_filter = f"[0:a]volume={preview_gain:.6f}[a]"
            mix_cmd += [
                "-filter_complex",
                audio_filter,
                "-map",
                "[a]",
                "-f", "wav", "-ar", "48000", "-ac", "2", "pipe:1",
            ]
            mix_proc = subprocess.Popen(
                mix_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=logf,
            )
            cmd = [
                "ffplay", "-vn", "-nodisp", "-autoexit", "-loglevel", "error",
                "-nostats", "-i", "pipe:0",
            ]
        elif overlapping:
            self._set_status("Need ffplay to preview overlapping audio tracks")
            logf.close()
            return
        else:
            path, start, remaining = specs[0]
            # mpv first: --hr-seek hits the playhead. ffplay -ss is keyframe-only.
            if shutil.which("mpv"):
                cmd = [
                    "mpv", "--no-video", "--force-window=no", "--no-terminal",
                    "--audio-display=no", "--no-resume-playback", "--hr-seek=always",
                    f"--volume={preview_gain * 100.0:.1f}",
                    f"--start={start:.3f}", f"--length={remaining:.3f}", str(path),
                ]
            elif shutil.which("ffplay"):
                cmd = [
                    "ffplay", "-vn", "-nodisp", "-autoexit", "-loglevel", "error",
                    "-nostats", "-ss", f"{start:.3f}", "-t", f"{remaining:.3f}",
                    "-af", f"volume={preview_gain:.6f}", str(path),
                ]
            else:
                self._set_status("Need ffplay or mpv to hear preview audio")
                logf.close()
                return
        # Adopt the mixer before the player can raise, or a failed spawn leaves
        # ffmpeg running on a pipe nobody reads and _stop_preview_audio blind.
        self._preview_mix_proc = mix_proc
        try:
            self._preview_proc = subprocess.Popen(
                cmd,
                stdin=mix_proc.stdout if mix_proc is not None else subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=logf,
            )
        except OSError:
            self._stop_preview_audio()
            logf.close()
            self._set_status("Could not start preview audio")
            return
        if mix_proc is not None and mix_proc.stdout is not None:
            mix_proc.stdout.close()
        if self._playthrough_playing:
            src = "preview"
        elif len(specs) > 1:
            src = "A1 + A2"
        else:
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
        if self._compiled_mode:
            if self._vmedia is not None:
                self.preview.set_blank(False)
                self.preview.set_media(self._vmedia)
                self.preview.queue_draw()
        else:
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

    def _compiled_media_timestamp_us(self) -> int | None:
        m = self._vmedia
        if m is None or not m.is_prepared():
            return None
        ts = m.get_timestamp()
        if ts < 0:
            return None
        return int(ts)

    def _compiled_timeline_t(self) -> float:
        return compiled_playhead_seconds(
            playing=self.playing,
            duration=self._compiled_duration,
            media_timestamp_us=self._compiled_media_timestamp_us(),
            play_t0=self._play_t0,
            play_mono=self._play_mono,
            now_mono=time.monotonic(),
            paused_playhead=self.timeline.playhead,
        )

    def _on_play(self, *_args: object) -> None:
        if self._busy_rendering():
            return
        if self._compiled_mode:
            if self._vmedia is None:
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
            # Never mix edit-preview ffplay/mpv with compiled MediaFile audio.
            self._stop_preview_audio()
            self._audio_pending = False
            self.preview.set_blank(False)
            self.preview.set_media(self._vmedia)
            m = self._vmedia
            m.set_muted(False)
            m.set_volume(1.0)
            try:
                m.seek(int(max(0.0, t) * 1_000_000))
            except GLib.Error:
                pass
            m.play()
            self.playing = True
            self._clip_playing = True
            self.btn_play.set_label("Pause")
            self._syncing_scrub = True
            self.timeline.set_playhead(t)
            self._syncing_scrub = False
            self.clock.set_text(f"{t:.2f} / {end:.2f}")
            self._tick = GLib.timeout_add(50, self._on_tick)
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
        self._refresh_cache_bar()
        self._begin_timeline_play(t)

    def _on_tick(self) -> bool:
        if self._compiled_mode:
            t = self._compiled_timeline_t()
            end = self._program_end()
            self._syncing_scrub = True
            self.timeline.set_playhead(t)
            self._syncing_scrub = False
            self.clock.set_text(f"{t:.2f} / {end:.2f}")
            if self._vmedia is not None:
                self.preview.queue_draw()
            if t >= end - 0.02:
                self._stop()
                self._syncing_scrub = True
                self.timeline.set_playhead(end)
                self._syncing_scrub = False
                return False
            return True
        if self._playthrough_playing:
            ts = self._compiled_media_timestamp_us()
            end = self._program_end()
            if ts is not None:
                t = ts / 1_000_000.0
            else:
                t = self._timeline_now()
            t = min(max(0.0, t), end)
            self._syncing_scrub = True
            self.timeline.set_playhead(t)
            self._syncing_scrub = False
            self.clock.set_text(f"{t:.2f} / {end:.2f}")
            if self._vmedia is not None:
                self.preview.queue_draw()
            if not self._cached_playback_available(t):
                self._apply_timeline_frame(t, start_media=True)
                self._start_preview_audio(t)
                return True
            if t >= end - 0.02:
                self._stop()
                self._syncing_scrub = True
                self.timeline.set_playhead(end)
                self._syncing_scrub = False
                return False
            return True
        t = self._timeline_now()
        end = self._program_end()
        self._syncing_scrub = True
        self.timeline.set_playhead(t)
        self._syncing_scrub = False
        self.clock.set_text(f"{t:.2f} / {end:.2f}")
        if self._cached_playback_available(t):
            self._apply_timeline_frame(t, start_media=True)
            self._start_preview_audio(t)
            return True
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
            astate = self._audio_tracks_at(t)
            prev = getattr(self, "_audio_play_clip", None)
            if astate != prev:
                self._audio_play_clip = astate
                if self._audio_state_continuous(prev, astate, adur):
                    # Crossing a split in the same source on every track: the
                    # player is already playing that audio, so let it run rather
                    # than respawn and drop out at the cut.
                    pass
                elif any(c is not None for c in astate):
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

    def _busy_rendering(self) -> bool:
        return bool(self.exporting or self._preview_rendering)

    def _current_render_fingerprint(
        self, *, kind: str, window: tuple[float, float] | None = None
    ) -> str:
        return render_fingerprint(
            aspect=self.aspect,
            pan_x=self.preview.pan_x,
            pan_y=self.preview.pan_y,
            audio_follows_in=self.follow_in.get_active(),
            use_video_soundtrack=self.use_video_soundtrack,
            audio_offset=0.0,
            video_clips=self.video_clips,
            audio_clips=self.audio_clips,
            media=self.media,
            kind=kind,
            window=window,
        )

    def _sync_compiled_preview_controls(self) -> None:
        busy = self._busy_rendering()
        locked = self._editing_locked()
        has_video = bool(self.video_clips) and bool(self.video_path)
        can_cut = False
        if has_video and self.sel_kind == "video" and 0 <= self.sel_v < len(self.video_clips):
            can_cut = has_touching_follower(
                self.video_clips, self.sel_v, self._src_durs()
            )
        if hasattr(self, "btn_preview_cut"):
            self.btn_preview_cut.set_sensitive(
                has_video and can_cut and not busy and not locked
            )
            self.btn_preview_full.set_sensitive(has_video and not busy and not locked)
        if hasattr(self, "btn_preview_cancel"):
            self.btn_preview_cancel.set_sensitive(self._preview_rendering)
        if hasattr(self, "btn_render_preview"):
            self.btn_render_preview.set_sensitive(has_video and not busy and not locked)
        if hasattr(self, "btn_back_edit_preview"):
            self.btn_back_edit_preview.set_sensitive(
                self._compiled_mode and not self._preview_rendering
            )
        if hasattr(self, "btn_export"):
            self.btn_export.set_sensitive(has_video and not busy)
        if hasattr(self, "btn_open_video"):
            self.btn_open_video.set_sensitive(not locked and not busy)
            self.btn_open_audio.set_sensitive(not locked and not busy)
        if hasattr(self, "btn_clear_audio"):
            self.btn_clear_audio.set_sensitive(bool(self.audio_path) and not locked)
        if hasattr(self, "btn_fit"):
            self.btn_fit.set_sensitive(bool(self.audio_path) and not locked)
        if hasattr(self, "follow_in"):
            self.follow_in.set_sensitive(not locked)
        if hasattr(self, "in_spin"):
            self.in_spin.set_sensitive(not locked)
            self.out_spin.set_sensitive(not locked)
        if hasattr(self, "aspect_buttons"):
            for btn in self.aspect_buttons.values():
                btn.set_sensitive(not locked)
        if hasattr(self, "resolution_buttons"):
            for btn in self.resolution_buttons.values():
                btn.set_sensitive(not locked)
        self._update_history_actions()
        self._update_compiled_preview_label()

    def _update_compiled_preview_label(self) -> None:
        if not hasattr(self, "compiled_preview_label"):
            return
        if self._preview_rendering:
            self.compiled_preview_label.set_text("Rendering preview…")
            return
        if not self._compiled_mode:
            self.compiled_preview_label.set_text("")
            return
        kind = "cut" if self._compiled_kind == "cut" else "full timeline"
        self.compiled_preview_label.set_text(
            f"Rendered preview — editing locked ({kind})"
        )

    def _invalidate_compiled_preview_if_stale(self) -> None:
        if not self._compiled_mode or self._compiled_hash is None:
            return
        window = None
        if self._compiled_kind == "cut" and getattr(self, "_compiled_window", None):
            window = self._compiled_window
        current = self._current_render_fingerprint(
            kind=self._compiled_kind or "full", window=window
        )
        if current != self._compiled_hash:
            self._exit_compiled_preview(
                status="Rendered preview ended — project changed"
            )

    def _mark_compiled_stale_if_needed(self) -> None:
        # Kept for callers; unexpected edits exit rather than leave a stale preview.
        self._invalidate_compiled_preview_if_stale()

    def _on_cancel_preview_render(self, *_args: object) -> None:
        if self._preview_rendering:
            self._preview_cancel.set()
            self._set_status("Cancelling preview render…")

    def _abandon_preview_render(self) -> None:
        """Cancel preview work and make any queued completion callback stale."""
        if self._preview_rendering:
            self._preview_cancel.set()
        self._preview_generation += 1
        self._preview_rendering = False

    def _on_back_edit_preview(self, *_args: object) -> None:
        self._exit_compiled_preview()

    def _reset_compiled_preview_flags(self) -> None:
        self._compiled_mode = False
        self._compiled_stale = False
        self._compiled_path = None
        self._compiled_hash = None
        self._compiled_kind = ""
        self._compiled_duration = 0.0
        self._compiled_window = None
        self._edit_vmedia_path = None
        if hasattr(self, "timeline"):
            self.timeline.set_read_only(False)
        if hasattr(self, "preview"):
            self.preview.read_only = False
            self.preview.set_cursor_from_name("grab")

    def _exit_compiled_preview(self, *, status: str = "Back to edit") -> None:
        was = self._compiled_mode
        playhead = self.timeline.playhead if was else 0.0
        restore = self._edit_vmedia_path
        self._stop()
        self._reset_compiled_preview_flags()
        if not was:
            self._sync_compiled_preview_controls()
            return
        if restore is not None and restore.is_file():
            self._load_media(restore)
            self.preview.set_blank(False)
            self.preview.set_media(self._vmedia)
        else:
            self._dispose_media()
            if 0 <= self.sel_v < len(self.video_clips):
                item = self._clip_item(self.video_clips[self.sel_v], "video")
                if item is not None:
                    self._bind_video(item.id)
        self._sync_timeline_clips()
        end = self._program_end()
        t = min(max(0.0, playhead), end)
        self._syncing_scrub = True
        self.timeline.set_playhead(t)
        self._syncing_scrub = False
        self.clock.set_text(f"{t:.2f} / {end:.2f}")
        self._apply_timeline_frame(t, start_media=False)
        self._sync_transform_controls()
        self._sync_transition_controls()
        self._sync_compiled_preview_controls()
        self._set_status(status)

    def _enter_compiled_preview(
        self, path: Path, *, kind: str, fingerprint: str, duration: float,
        window: tuple[float, float] | None = None,
    ) -> None:
        # Stop edit-preview audio/video before compiled MediaFile playback.
        self._stop()
        if not self._compiled_mode:
            self._edit_vmedia_path = self._vmedia_path
        self._compiled_mode = True
        self._compiled_stale = False
        self._compiled_path = path
        self._compiled_hash = fingerprint
        self._compiled_kind = kind
        self._compiled_duration = max(0.05, float(duration))
        self._compiled_window = window
        self.timeline.set_read_only(True)
        self.preview.read_only = True
        self.preview.set_cursor_from_name("default")
        self._load_media(path)
        if self._vmedia is not None:
            self._vmedia.set_muted(False)
            self._vmedia.set_volume(1.0)
            self._vmedia.pause()
        self.preview.set_blank(False)
        self.preview.set_media(self._vmedia)
        self.preview.set_transform(
            0.0, 0.0, 1.0, *dest_size(self.aspect, self.resolution)
        )
        self.timeline.set_duration(self._compiled_duration)
        self.timeline.set_playhead(0.0)
        self.timeline.set_range(0.0, self._compiled_duration)
        self.clock.set_text(f"0.00 / {self._compiled_duration:.2f}")
        self._sync_transform_controls()
        self._sync_transition_controls()
        self._sync_compiled_preview_controls()
        self._set_status("Rendered preview — editing locked")

    def _start_playthrough_render(self) -> None:
        """Bake a 1:1 timeline proxy and mark its cache spans green."""
        if self._busy_rendering() or not self.video_path or not self.video_clips:
            self._play_after_render = None
            return
        fingerprint = self._current_render_fingerprint(kind="play")
        out = preview_out_path(fingerprint, "play")
        try:
            assert_preview_path_safe(out)
        except ValueError as exc:
            self._set_status(str(exc))
            self._play_after_render = None
            return
        self._stop()
        self._preview_rendering = True
        self._preview_cancel = threading.Event()
        self._preview_generation += 1
        generation = self._preview_generation
        self.progress.set_fraction(0)
        self._set_status("Rendering preview…")
        self._sync_compiled_preview_controls()
        video = self.video_path
        audio = self.audio_path
        aspect = self.aspect
        pan_x, pan_y = self.preview.pan_x, self.preview.pan_y
        follows = self.follow_in.get_active()
        use_soundtrack = self.use_video_soundtrack
        v_clips = [c.copy() for c in self.video_clips]
        a_clips = [c.copy() for c in self.audio_clips]
        media = [m.copy() for m in self.media]
        if a_clips:
            item = self._clip_item(a_clips[0], "audio")
            audio = item.path if item is not None else audio
        else:
            audio = None
        prim = next((m for m in media if m.kind == "video"), None)
        if prim is not None:
            video = prim.path
        cancel_event = self._preview_cancel
        segs = list(self._cache_segments)

        def progress(pct: float, _state: str) -> None:
            GLib.idle_add(self.progress.set_fraction, pct)

        def work() -> None:
            err: BaseException | None = None
            result: dict | None = None
            try:
                result = run_export(
                    video,
                    out,
                    audio=audio,
                    aspect=aspect,
                    pan_x=pan_x,
                    pan_y=pan_y,
                    in_s=0.0,
                    out_s=None,
                    audio_follows_in=follows,
                    video_clips=v_clips,
                    audio_clips=a_clips or None,
                    media=media or None,
                    use_video_soundtrack=use_soundtrack,
                    profile=PREVIEW_PROFILE,
                    progress=progress,
                    cancel_event=cancel_event,
                )
            except (ExportCancelled, ExportError, ProbeError, OSError) as exc:
                err = exc

            def done() -> bool:
                if generation != self._preview_generation:
                    return False
                self._preview_rendering = False
                self._sync_compiled_preview_controls()
                play_at = self._play_after_render
                self._play_after_render = None
                if err is not None:
                    self.progress.set_fraction(0)
                    if isinstance(err, ExportCancelled):
                        self._set_status("Preview render cancelled")
                    else:
                        self._set_status(str(err))
                    return False
                mark_segments_green(segs)
                self._playthrough_path = out
                self._playthrough_hash = fingerprint
                self._refresh_cache_bar()
                cleanup_preview_cache()
                self.progress.set_fraction(1)
                self._set_status("Preview ready")
                if play_at is not None:
                    self._begin_timeline_play(play_at)
                else:
                    self._apply_timeline_frame(
                        self.timeline.playhead, start_media=False
                    )
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _begin_timeline_play(self, t: float) -> None:
        end = self._program_end()
        t = min(max(0.0, t), end)
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
        self.playing = True
        self.btn_play.set_label("Pause")
        if self._tick is not None:
            GLib.source_remove(self._tick)
        self._tick = GLib.timeout_add(50, self._on_tick)

    def _on_compiled_preview(self, kind: str) -> None:
        if self._busy_rendering() or not self.video_path or not self.video_clips:
            return
        src_durs = self._src_durs()
        window: tuple[float, float] | None = None
        v_clips = [c.copy() for c in self.video_clips]
        a_clips = [c.copy() for c in self.audio_clips]
        if kind == "cut":
            if self.sel_kind != "video" or not 0 <= self.sel_v < len(self.video_clips):
                self._set_status("Select a video clip with a following cut")
                return
            try:
                t0, t1, _cut = selected_cut_window(
                    self.video_clips,
                    self.sel_v,
                    src_durs,
                    audio_clips=self.audio_clips,
                )
            except ValueError as exc:
                self._set_status(str(exc))
                return
            window = (t0, t1)
            v_clips = rebase_clips_for_window(v_clips, t0, t1, src_durs)
            a_clips = rebase_clips_for_window(a_clips, t0, t1, src_durs)
            if not v_clips:
                self._set_status("Nothing to preview in that cut window")
                return
        fingerprint = self._current_render_fingerprint(kind=kind, window=window)
        out = preview_out_path(fingerprint, kind)
        try:
            assert_preview_path_safe(out)
        except ValueError as exc:
            self._set_status(str(exc))
            return
        if out.is_file() and out.stat().st_size > 0:
            try:
                info = probe(out)
                dur = float(info.get("duration") or 0.0)
            except ProbeError:
                dur = 0.0
            if dur > 0.04:
                cleanup_preview_cache()
                self._enter_compiled_preview(
                    out, kind=kind, fingerprint=fingerprint, duration=dur, window=window
                )
                return

        self._stop()
        self._preview_rendering = True
        self._preview_cancel = threading.Event()
        self._preview_generation += 1
        generation = self._preview_generation
        self.progress.set_fraction(0)
        self._set_status(
            "Rendering cut preview…" if kind == "cut" else "Rendering full preview…"
        )
        self._sync_compiled_preview_controls()

        video = self.video_path
        audio = self.audio_path
        aspect = self.aspect
        pan_x, pan_y = self.preview.pan_x, self.preview.pan_y
        follows = self.follow_in.get_active()
        use_soundtrack = self.use_video_soundtrack
        media = [m.copy() for m in self.media]
        if a_clips:
            item = self._clip_item(a_clips[0], "audio")
            audio = item.path if item is not None else audio
        else:
            audio = None
            # Source soundtrack follows rebased video clips.
        cancel_event = self._preview_cancel
        # Primary video for build_cmd when clips carry media_ids.
        prim = next((m for m in media if m.kind == "video"), None)
        if prim is not None:
            video = prim.path

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
                    in_s=0.0,
                    out_s=None,
                    audio_follows_in=follows,
                    video_clips=v_clips,
                    audio_clips=a_clips or None,
                    media=media or None,
                    use_video_soundtrack=use_soundtrack,
                    profile=PREVIEW_PROFILE,
                    progress=progress,
                    cancel_event=cancel_event,
                )
                GLib.idle_add(
                    self._compiled_preview_done,
                    result,
                    None,
                    kind,
                    fingerprint,
                    window,
                    generation,
                )
            except (ExportCancelled, ExportError, ProbeError, OSError) as exc:
                GLib.idle_add(
                    self._compiled_preview_done,
                    None,
                    exc,
                    kind,
                    fingerprint,
                    window,
                    generation,
                )

        threading.Thread(target=work, daemon=True).start()

    def _compiled_preview_done(
        self,
        result: dict | None,
        err: BaseException | None,
        kind: str,
        fingerprint: str,
        window: tuple[float, float] | None,
        generation: int,
    ) -> bool:
        if generation != self._preview_generation:
            return False
        self._preview_rendering = False
        self._sync_compiled_preview_controls()
        if err is not None:
            self.progress.set_fraction(0)
            if isinstance(err, ExportCancelled):
                self._set_status("Preview render cancelled")
            else:
                self._set_status(str(err))
            return False
        assert result is not None
        path = Path(result["out"])
        dur = float((result.get("meta") or {}).get("duration") or 0.0)
        cleanup_preview_cache()
        self.progress.set_fraction(1)
        self._enter_compiled_preview(
            path, kind=kind, fingerprint=fingerprint, duration=dur, window=window
        )
        return False

    def _on_export(self, *_args: object) -> None:
        if not self.video_path or self._busy_rendering():
            return
        if not self.video_clips:
            self._set_status("No video on the timeline")
            return
        self._stop()
        self.exporting = True
        self.btn_export.set_sensitive(False)
        self._sync_compiled_preview_controls()
        self.progress.set_fraction(0)
        self._set_status("Starting export…")
        video = self.video_path
        audio = self.audio_path
        aspect = self.aspect
        resolution = self.resolution
        pan_x, pan_y = self.preview.pan_x, self.preview.pan_y
        in_s = self.in_spin.get_value()
        out_s = self.out_spin.get_value()
        follows = self.follow_in.get_active()
        use_soundtrack = self.use_video_soundtrack
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
                    resolution=resolution,
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
                    progress=progress,
                )
                GLib.idle_add(self._export_done, result, None)
            except (ExportError, ProbeError, OSError) as exc:
                GLib.idle_add(self._export_done, None, exc)

        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, result: dict | None, err: BaseException | None) -> bool:
        self.exporting = False
        self._sync_compiled_preview_controls()
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
            application_id=application_id(),
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.connect("activate", self._activate)
        self.connect("command-line", self._on_command_line)

    def _install_accels(self) -> None:
        self.set_accels_for_action("win.new-project", ["<Control>n"])
        self.set_accels_for_action("win.save", ["<Control>s"])
        self.set_accels_for_action("win.save-as", ["<Control><Shift>s"])
        self.set_accels_for_action("win.open-project", ["<Control>o"])
        # Timeline undo/redo is dispatched by its focus-scoped key handler.
        # Application accelerators would bypass it and steal native text undo.

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
        videos = cli_flag_paths(args, "--video")
        audios = cli_flag_paths(args, "--audio")
        self.activate()
        win = self.props.active_window
        if isinstance(win, EditorWindow):
            if new_project:
                # Skip autosave restore on first launch so --new is a blank project.
                win._open_from_cli = True
            if new_project or videos or audios:
                _idle_open_cli(
                    win,
                    videos=videos,
                    audios=audios,
                    new_project=new_project,
                )
        if win is not None:
            win.present()
        return 0


def _idle_open_cli(
    win: EditorWindow,
    *,
    videos: list[Path] | None = None,
    audios: list[Path] | None = None,
    new_project: bool,
) -> None:
    def go(
        _w: EditorWindow = win,
        _videos: list[Path] = list(videos or []),
        _audios: list[Path] = list(audios or []),
        _new: bool = new_project,
    ) -> bool:
        if _new:
            if _w.exporting:
                _w._set_status("export in progress")
                return False
            _w._on_new_project()
        missing: list[str] = []
        added = 0
        for path in _videos:
            if path.is_file():
                _w._open_path("video", path)
                added += 1
            else:
                missing.append(str(path))
        for path in _audios:
            if path.is_file():
                _w._open_path("audio", path)
                added += 1
            else:
                missing.append(str(path))
        if missing:
            _w._set_status("missing " + ", ".join(missing))
        elif added > 1:
            _w._set_status(f"Added {added} clips")
        return False

    GLib.idle_add(go)


def run(
    *,
    open_video: str | Path | None = None,
    open_audio: str | Path | None = None,
    open_videos: list[str | Path] | None = None,
    open_audios: list[str | Path] | None = None,
    new_project: bool = False,
) -> int:
    Adw.init()
    apply_omarchy_theme()
    argv = ["clip-editor"]
    if new_project:
        argv.append("--new")
    videos = list(open_videos or [])
    if open_video:
        videos.append(open_video)
    audios = list(open_audios or [])
    if open_audio:
        audios.append(open_audio)
    for path in videos:
        argv += ["--video", str(path)]
    for path in audios:
        argv += ["--audio", str(path)]
    return int(EditorApp().run(argv))
