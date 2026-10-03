"""Native GTK4 clip editor. Not a browser window."""

from __future__ import annotations

import array
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import cairo
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
    cover_source_placement,
    resize_source_from_corner,
    dest_size,
)
from clip_editor.commands import parse_command
from clip_editor.keyboard_edits import (
    MOVE_INCREMENTS, SEEK_INCREMENTS, boundary_delta, move_clips, reorder_clips,
    seek_frame, trim_clip,
)
from clip_editor.eagle import apply_omarchy_theme, theme_rgb
from clip_editor.theme import on_theme_change, off_theme_change
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
    DEFAULT_FADE_S,
    DEFAULT_TRANSITION_S,
    MAX_FADE_S,
    MIN_FADE_S,
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
    clamp_clip_fades,
    media_load_errors,
    next_media_id,
    normalize_fade_s,
    normalize_speed,
    normalize_volume,
    normalize_track_volumes,
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

# 9:16 areas that TikTok, Reels and Shorts cover with their own interface,
# as fractions of the frame. Rounded up from each app's 1080×1920 overlay so
# one shading covers all three; these are working margins, not platform specs.
SAFE_TOP = 0.08
SAFE_BOTTOM = 0.22
SAFE_RIGHT = 0.13
SAFE_RIGHT_RAIL = (0.35, 0.78)
SAFE_ZONE_ASPECTS = ("9:16",)

APP_CSS = """
/* Omarchy-style shell. Colors are --clip-* roles from theme.py, derived from
   the active Omarchy theme; these defaults only apply without one. */
:root {
  --clip-key: @accent_color; --clip-line: alpha(currentColor, 0.2);
  --clip-hover: alpha(@accent_color, 0.16); --clip-muted: alpha(currentColor, 0.55);
  --clip-surface: alpha(currentColor, 0.06); --clip-accent: @accent_color;
  --clip-on-accent: @accent_fg_color; --clip-bg: @window_bg_color; --clip-fg: @window_fg_color;
}
window.clip-editor { font-family: monospace; font-size: 10pt; background-color: var(--clip-bg); }
window.clip-editor popover { font-family: monospace; font-size: 10pt; }

/* Panes: bordered like Hyprland windows, title set into the top border. */
frame.pane { border: 2px solid var(--clip-line); border-radius: 8px; }
frame.pane.focused { border: 3px solid var(--clip-accent); }
frame.pane > .pane-title {
  background-color: var(--clip-bg); margin-left: 10px; padding: 0 4px;
}
.pane-name { font-weight: bold; color: var(--clip-fg); margin-right: 6px; }
.key { color: var(--clip-key); }
.dim { color: var(--clip-muted); }

/* Key hints: text that is also a button. */
button.hint {
  background: none; border: none; box-shadow: none; outline: none;
  min-height: 0; min-width: 0; padding: 1px 5px; border-radius: 4px;
  font-weight: normal; color: var(--clip-fg);
}
button.hint:hover { background-color: var(--clip-hover); }
button.hint:focus-visible { outline: 1px solid var(--clip-accent); }
button.hint:disabled { opacity: 0.4; }
button.hint.choice { color: var(--clip-muted); }
button.hint.choice:checked { color: var(--clip-fg); font-weight: bold; background: none; }
button.hint:checked { background: none; }

.clock { font-size: 15pt; font-weight: bold; }

/* Media strip */
.media-row { padding: 4px 8px 4px 4px; border-radius: 6px; border: 1px solid var(--clip-line); }
.media-row:hover { background-color: var(--clip-hover); }

/* Statusline, lualine-style */
.statusline { background-color: var(--clip-surface); border-radius: 6px; min-height: 24px; }
.statusline > label, .statusline > progressbar { padding: 0 10px; }
.statusline .mode {
  background-color: var(--clip-accent); color: var(--clip-on-accent);
  font-weight: 800; border-radius: 6px 0 0 6px;
}
.statusline .seg { border-right: 1px solid var(--clip-line); }
.commandline entry { background-color: var(--clip-hover); border: none; box-shadow: none; }
progressbar.thin > trough { min-height: 3px; background-color: var(--clip-line); border: none; }
progressbar.thin > trough > progress { min-height: 3px; background-color: var(--clip-accent); border: none; }

/* Popovers: Walker-style box. */
popover.clip-pop > contents {
  background-color: var(--clip-bg); border: 3px solid var(--clip-accent);
  border-radius: 8px; padding: 10px 12px; box-shadow: none;
}
popover.clip-pop spinbutton, popover.clip-pop dropdown > button {
  background-color: var(--clip-hover); border: none; border-radius: 6px; box-shadow: none;
}
"""
_app_css: Gtk.CssProvider | None = None


def install_app_css() -> None:
    global _app_css
    display = Gdk.Display.get_default()
    if _app_css is not None or display is None:
        return
    _app_css = Gtk.CssProvider()
    _app_css.load_from_data(APP_CSS.encode())
    # Below the Omarchy provider (USER) so theme colors still win.
    Gtk.StyleContext.add_provider_for_display(
        display, _app_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


def _same_path(a: Path | None, b: Path | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b


FILMSTRIP_FRAMES = 24
FILMSTRIP_HEIGHT = 72
WAVE_RATE = 100.0  # peaks per second of source audio


def _load_filmstrip(path: Path, duration: float) -> tuple[GdkPixbuf.Pixbuf, int] | None:
    """Evenly spaced frames from `path`, tiled left to right in one image."""
    if duration <= 0:
        return None
    frames = max(2, min(FILMSTRIP_FRAMES, int(duration * 2)))
    raw = subprocess.check_output(
        [
            which_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-vf", f"fps={frames}/{duration:.3f},scale=-2:{FILMSTRIP_HEIGHT},tile={frames}x1",
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        ],
        timeout=120,
    )
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(raw)
    loader.close()
    pb = loader.get_pixbuf()
    return (pb, frames) if pb is not None else None


def _load_waveform(path: Path) -> tuple[list[float], float] | None:
    """Peak amplitude (0–1) per 1/WAVE_RATE second of `path`'s first audio stream."""
    sample_rate = 8000
    raw = subprocess.check_output(
        [
            which_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-map", "0:a:0", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "pipe:1",
        ],
        timeout=120,
    )
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % 2])
    per = int(sample_rate / WAVE_RATE)
    peaks = [
        max(max(chunk), -min(chunk)) / 32768.0
        for chunk in (samples[i : i + per] for i in range(0, len(samples), per))
        if len(chunk)
    ]
    return peaks, WAVE_RATE


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
        self.layers: list[tuple[ClipInst, Gdk.Paintable, int, int]] = []
        self.selected_clip: ClipInst | None = None
        self.on_pan_begin = None
        self.on_pan = None
        self.on_pan_end = None
        self.on_pan_cancel = None
        self.on_scale = None
        self._drag_active = False
        self._texture: Gdk.Texture | None = None
        self._media: Gtk.MediaFile | None = None
        self._inv_id = 0
        self.blank = False
        self.read_only = False
        self.safe_zones = False
        self.set_layout_manager(Gtk.BinLayout())
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_cursor_from_name("grab")
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        drag.connect("cancel", self._drag_cancel)
        self.add_controller(drag)
        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.VERTICAL)
        scroll.connect("scroll", self._on_scroll)
        self.add_controller(scroll)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_transform_motion)
        self.add_controller(motion)

    def _on_scroll(self, _c: Gtk.EventControllerScroll, _dx: float, dy: float) -> bool:
        if self.read_only or not callable(self.on_scale):
            return False
        self.on_scale(dy)
        return True

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
        r, g, b = 0.0, 0.0, 0.0
        bg = Gdk.RGBA()
        bg.red, bg.green, bg.blue, bg.alpha = r, g, b, 1.0
        snapshot.append_color(bg, Graphene.Rect().init(0, 0, w, h))
        layers = [] if self.blank else self.layers
        if layers:
            for clip, paintable, iw, ih in layers:
                self._snapshot_layer(snapshot, paintable, iw, ih, clip.transform_x,
                                     clip.transform_y, clip.scale, w, h)
        else:
            p = self._paintable()
            if p is not None:
                self._snapshot_layer(snapshot, p, int(p.get_intrinsic_width() or 0),
                                     int(p.get_intrinsic_height() or 0), self.transform_x,
                                     self.transform_y, self.transform_scale, w, h)
        if self.safe_zones:
            self._snapshot_safe_zones(snapshot, w, h)
        self._snapshot_selection(snapshot)

    def _snapshot_layer(self, snapshot, paintable, iw, ih, tx, ty, scale, w, h) -> None:
        if min(iw, ih) <= 0:
            return
        box = cover_source_placement(iw, ih, self.output_width, self.output_height,
                                     self.pan_x, self.pan_y, tx, ty, scale)
        ux, uy = w / self.output_width, h / self.output_height
        snapshot.save()
        snapshot.push_clip(Graphene.Rect().init(0, 0, w, h))
        snapshot.translate(Graphene.Point().init(box.x * ux, box.y * uy))
        paintable.snapshot(snapshot, box.w * ux, box.h * uy)
        snapshot.pop()
        snapshot.restore()

    def _selection_box(self) -> tuple[float, float, float, float] | None:
        if self.read_only or self.blank or self.selected_clip is None:
            return None
        layers = self.layers or [(self.selected_clip, None, *getattr(self, "selected_source_size", (0, 0)))]
        for clip, _paintable, iw, ih in layers:
            if min(iw, ih) <= 0 or clip is not self.selected_clip:
                continue
            box = cover_source_placement(iw, ih, self.output_width, self.output_height,
                                         self.pan_x, self.pan_y, clip.transform_x,
                                         clip.transform_y, clip.scale)
            w, h = self.get_width(), self.get_height()
            ux, uy = w / self.output_width, h / self.output_height
            left, top = max(6, box.x * ux), max(6, box.y * uy)
            right, bottom = min(w - 6, (box.x + box.w) * ux), min(h - 6, (box.y + box.h) * uy)
            if right > left and bottom > top:
                return left, top, right, bottom
        return None

    def _hit_transform(self, x: float, y: float) -> str:
        box = self._selection_box()
        if box is None:
            return ""
        left, top, right, bottom = box
        for corner, hx, hy in (("nw", left, top), ("ne", right, top),
                               ("sw", left, bottom), ("se", right, bottom)):
            if abs(x - hx) <= 9 and abs(y - hy) <= 9:
                return corner
        return "move" if left <= x <= right and top <= y <= bottom else ""

    def _on_transform_motion(self, _controller, x: float, y: float) -> None:
        if self._drag_active:
            return
        hit = self._hit_transform(x, y)
        cursor = {"nw": "nwse-resize", "se": "nwse-resize", "ne": "nesw-resize",
                  "sw": "nesw-resize", "move": "grab"}.get(hit, "default")
        self.set_cursor_from_name(cursor)

    def _snapshot_selection(self, snapshot) -> None:
        box = self._selection_box()
        if box is None:
            return
        left, top, right, bottom = box
        color = Gdk.RGBA()
        color.red, color.green, color.blue = theme_rgb("accent", (1., 1., 1.))
        color.alpha = 1
        for x, y, w, h in ((left, top, right-left, 2), (left, bottom-2, right-left, 2),
                            (left, top, 2, bottom-top), (right-2, top, 2, bottom-top)):
            snapshot.append_color(color, Graphene.Rect().init(x, y, w, h))
        for x, y in ((left, top), (right, top), (left, bottom), (right, bottom)):
            snapshot.append_color(color, Graphene.Rect().init(x-5, y-5, 10, 10))

    def set_safe_zones(self, on: bool) -> None:
        if self.safe_zones == on:
            return
        self.safe_zones = on
        self.queue_draw()

    def _snapshot_safe_zones(self, snapshot: Gtk.Snapshot, w: int, h: int) -> None:
        shade = Gdk.RGBA()
        shade.red = shade.green = shade.blue = 0.0
        shade.alpha = 0.5
        top = h * SAFE_TOP
        bottom = h * (1.0 - SAFE_BOTTOM)
        right = w * (1.0 - SAFE_RIGHT)
        rail_top = h * SAFE_RIGHT_RAIL[0]
        rail_bottom = h * SAFE_RIGHT_RAIL[1]
        for rx, ry, rw, rh in (
            (0, 0, w, top),
            (0, bottom, w, h - bottom),
            (right, max(top, rail_top), w - right, min(bottom, rail_bottom) - max(top, rail_top)),
        ):
            if rw > 0 and rh > 0:
                snapshot.append_color(shade, Graphene.Rect().init(rx, ry, rw, rh))
        r, g, b = theme_rgb("accent", (1.0, 1.0, 1.0))
        edge = Gdk.RGBA()
        edge.red, edge.green, edge.blue, edge.alpha = r, g, b, 0.9
        for rx, ry, rw, rh in ((0, top, w, 1), (0, bottom - 1, w, 1)):
            snapshot.append_color(edge, Graphene.Rect().init(rx, ry, rw, rh))

    def _drag_begin(self, _gesture=None, x: float | None = None, y: float | None = None) -> None:
        mode = self._hit_transform(x, y) if x is not None and y is not None else "move"
        self._drag_active = (
            bool(mode) and not self.read_only
            and callable(self.on_pan_begin)
            and bool(self.on_pan_begin(mode))
        )
        if self._drag_active:
            self.set_cursor_from_name("grabbing")

    def _drag_update(self, _g: Gtk.GestureDrag, dx: float, dy: float) -> None:
        if self._drag_active and not self.read_only and callable(self.on_pan):
            self.on_pan(dx, dy)

    def _drag_end(self, *_args: object) -> None:
        active = self._drag_active
        self._drag_active = False
        self.set_cursor_from_name("default" if self.read_only else "grab")
        if active and callable(self.on_pan_end):
            self.on_pan_end()

    def _drag_cancel(self, *_args: object) -> None:
        active = self._drag_active
        self._drag_active = False
        self.set_cursor_from_name("default")
        if active and callable(self.on_pan_cancel):
            self.on_pan_cancel()



def _rgb_luminance(color: tuple[float, float, float]) -> float:
    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(color[0]) + 0.7152 * lin(color[1]) + 0.0722 * lin(color[2])


def _label_rgb_on(
    color: tuple[float, float, float], alpha: float = 1.0
) -> tuple[float, float, float]:
    """Theme background or foreground, whichever contrasts more with `color`."""
    fg = theme_rgb("foreground", (1.0, 1.0, 1.0))
    if alpha < 0.6:
        return fg
    bg = theme_rgb("background", (0.0, 0.0, 0.0))
    lum = _rgb_luminance(color)

    def contrast(other: tuple[float, float, float]) -> float:
        hi, lo = sorted((lum, _rgb_luminance(other)), reverse=True)
        return (hi + 0.05) / (lo + 0.05)

    return bg if contrast(bg) > contrast(fg) else fg


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
    _LANE_H = 40.0
    _CLIP_PAD = 3.0
    _KNOB_R = 5.0
    _KNOB_INSET = 8.0
    _LANE_GAP = 6.0
    _BOTTOM = 6.0
    _EDGE = 8.0
    _MIN = 0.05
    _SNAP_PX = 10.0
    _TRAIL_PX = 160.0
    _TRAIL_MIN_S = 8.0
    _MIN_PPS = 8.0
    _HEIGHT = 208

    def __init__(self) -> None:
        super().__init__()
        self.duration = 0.0
        # None preserves the initial automatic scale; 1.0 fits the whole timeline.
        self._view_zoom: float | None = None
        self._zoom_anchor: tuple[float, Gtk.Adjustment] | None = None
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
        # Direct edits: on_fade(kind, i, in_s, out_s, final),
        # on_volume(kind, i, volume, final), on_transition(i), on_activate(kind, i).
        self.on_fade = None
        self.on_volume = None
        self.on_transition = None
        self.on_activate = None
        # media_id -> (tiled frames pixbuf, frame count) / (peaks, peaks per second)
        self.filmstrips: dict[str, tuple[GdkPixbuf.Pixbuf, int]] = {}
        self.waves: dict[str, tuple[list[float], float]] = {}
        self._drag_kind = ""
        self._drag_fades = (0.0, 0.0)
        self._handle_dirty = False
        self._handle_offsets = (0.0, 0.0)
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
        self._seek_scroll_source = 0
        self._seek_pointer_x = 0.0
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
        drag.connect("cancel", self._on_drag_cancel)
        self.connect("unmap", lambda *_: self._stop_seek_scroll())
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
        # Timeline bounds follow the edit, not the full-source ghost outlines.
        # _clip_times accounts for source in/out points, placement, and speed.
        ends = [0.0]
        for clips, lane in ((self.vclips, "v"), (self.aclips, "a")):
            for clip in clips:
                _t0, t1 = self._clip_times(clip, self._clip_src_dur(clip, lane))
                ends.append(t1)
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
        pps = (max(self._MIN_PPS, fit_pps) if self._view_zoom is None
               else fit_pps * self._view_zoom)
        width = int(self._GUTTER + content * pps + trail_px + self._PAD_RIGHT)
        return max(width, int(vp))

    def _scroll_adjustment(self) -> Gtk.Adjustment | None:
        parent = self.get_parent()
        for _ in range(4):
            if isinstance(parent, Gtk.ScrolledWindow):
                return parent.get_hadjustment()
            parent = parent.get_parent() if parent is not None else None
        return None

    def zoom_view(self, factor: float) -> None:
        """Change only timeline display scale, preserving the visible center."""
        if self._drag_mode or self.duration <= 0.04:
            return
        adjustment = self._scroll_adjustment()
        if adjustment is not None:
            center = adjustment.get_value() + adjustment.get_page_size() / 2
            self._zoom_anchor = (self._x_to_t(center), adjustment)
        fit_px = max(1.0, self._viewport_width() - self._GUTTER
                     - self._PAD_RIGHT - self._TRAIL_PX)
        current_px = max(1.0, self._desired_width() - self._GUTTER
                         - self._PAD_RIGHT - self._TRAIL_PX)
        current_zoom = (current_px / fit_px if self._view_zoom is None
                        else self._view_zoom)
        previous_width = self._desired_width()
        self._view_zoom = max(1 / 256, min(max(256.0, current_zoom),
                                         current_zoom * factor))
        if self._desired_width() == previous_width:
            self._zoom_anchor = None
        self._sync_canvas()
        self.queue_draw()

    def fit_view(self) -> None:
        """Fit all video/audio clips, including gaps, into the available width."""
        if self._drag_mode:
            return
        self._view_zoom = 1.0
        self._zoom_anchor = None
        adjustment = self._scroll_adjustment()
        if adjustment is not None:
            adjustment.set_value(0)
        self._sync_canvas()
        self.queue_draw()

    def _sync_canvas(self) -> None:
        w = self._desired_width()
        cur = int(self.get_size_request()[0] or 0)
        if cur != w:
            self.set_size_request(w, self._HEIGHT)
            self.set_content_width(w)

    def _on_resize(self, _area: Gtk.DrawingArea, _width: int, _height: int) -> None:
        self._sync_canvas()
        if self._zoom_anchor is not None:
            time, adjustment = self._zoom_anchor
            self._zoom_anchor = None
            target = self._t_to_x(time, float(_width)) - adjustment.get_page_size() / 2
            adjustment.set_value(max(0.0, target))

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
        # The canvas stays at least viewport-wide. Below Fit, extend its time
        # range instead of stretching the clips back to fill that width.
        zoom = min(1.0, self._view_zoom) if self._view_zoom is not None else 1.0
        return content * inner / content_px / zoom

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

    def _stop_seek_scroll(self) -> None:
        if self._seek_scroll_source:
            GLib.source_remove(self._seek_scroll_source)
            self._seek_scroll_source = 0

    def _drag_seek_x(self, x: float) -> None:
        self._seek_x(x)
        adjustment = self._scroll_adjustment()
        if adjustment is None:
            return
        # Keep the pointer relative to the viewport while the canvas scrolls.
        self._seek_pointer_x = x - adjustment.get_value()
        if not self._seek_scroll_source:
            self._seek_scroll_source = GLib.timeout_add(33, self._scroll_seek_edge)

    def _scroll_seek_edge(self) -> bool:
        adjustment = self._scroll_adjustment()
        if self._drag_mode != "seek" or adjustment is None:
            self._seek_scroll_source = 0
            return False
        page = adjustment.get_page_size()
        edge = min(40.0, page / 4)
        if edge <= 0:
            return True
        pointer = self._seek_pointer_x
        direction = 0.0
        if pointer < edge:
            direction = max(-1.0, (pointer - edge) / edge)
        elif pointer > page - edge:
            direction = min(1.0, (pointer - (page - edge)) / edge)
        if not direction:
            return True
        old = adjustment.get_value()
        lower = adjustment.get_lower()
        upper = max(lower, adjustment.get_upper() - page)
        adjustment.set_value(max(lower, min(upper, old + direction * 24)))
        position = adjustment.get_value()
        if position != old:
            x = position + max(0.0, min(page, pointer))
            if direction < 0 and position == lower:
                x = 0.0
            self._seek_x(x)
        return True

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

    def _on_pressed(self, gesture: Gtk.GestureClick, n: int, x: float, y: float) -> None:
        self.grab_focus()
        if self.read_only:
            self._seek_x(x)
            return
        if self._hit_handle(x, y)[0]:
            return
        if n == 2 and callable(self.on_activate):
            for kind in ("video", "audio"):
                i, _part = self._hit_kind(x, y, kind)
                if i >= 0 and (kind == "video" or self.audio_kind != "source"):
                    self.on_activate(kind, i)
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
        part = self._drag_mode or self._hit_handle(x, y)[0]
        if part in ("fade-in", "fade-out"):
            self.set_cursor_from_name("ew-resize")
        elif part == "volume":
            self.set_cursor_from_name("ns-resize")
        elif part == "transition":
            self.set_cursor_from_name("pointer")
        elif self._drag_mode in ("video-in", "video-out", "audio-in", "audio-out"):
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
        self._stop_seek_scroll()
        self._handle_dirty = False
        self._handle_offsets = (0.0, 0.0)
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
            self._drag_seek_x(ox)
            return
        part, kind, index = self._hit_handle(ox, oy)
        if part:
            self._drag_mode = part
            self._drag_kind = kind
            self._drag_index = index
            clips = self.vclips if kind == "video" else self.aclips
            c = clips[index]
            self._drag_fades = (float(c.fade_in_s), float(c.fade_out_s))
            self._drag_v0 = float(c.volume)
            if kind == "video":
                self.sel_v, self.sel_vs = index, {index}
            else:
                self.sel_a, self.sel_as = index, {index}
            self._mirror_sel()
            if callable(self.on_select):
                self.on_select(kind, index, frozenset({index}) if kind == "video" else frozenset())
            self.queue_draw()
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
            self._drag_seek_x(ox)

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
        if self._drag_mode in ("fade-in", "fade-out", "volume"):
            self._handle_offsets = (dt, dy)
            self._drag_handle(dt, dy, final=False)
            return
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
            self._drag_seek_x(ox + dx)

    def _drag_handle(self, dt: float, dy: float, *, final: bool) -> None:
        kind, idx = self._drag_kind, self._drag_index
        clips = self.vclips if kind == "video" else self.aclips
        if not 0 <= idx < len(clips):
            return
        c = clips[idx]
        if self._drag_mode == "volume":
            box = self._clip_box("audio", idx)
            h = box[3] if box else self._LANE_H
            vol = max(0.0, min(2.0, self._drag_v0 - dy / h * 2.0))
            if abs(vol - 1.0) < 0.04:
                vol = 1.0
            self._handle_dirty |= c.volume != vol
            c.volume = vol
            if callable(self.on_volume):
                self.on_volume(kind, idx, vol, final)
        else:
            lane = "v" if kind == "video" else "a"
            t0, t1 = self._clip_times(c, self._clip_src_dur(c, lane))
            fi, fo = self._drag_fades
            if self._drag_mode == "fade-in":
                fi = max(0.0, min(t1 - t0, fi + dt))
                fi = 0.0 if fi < 0.05 else fi
            else:
                fo = max(0.0, min(t1 - t0, fo - dt))
                fo = 0.0 if fo < 0.05 else fo
            fades = clamp_clip_fades(fi, fo, t1 - t0)
            self._handle_dirty |= fades != (c.fade_in_s, c.fade_out_s)
            c.fade_in_s, c.fade_out_s = fades
            if callable(self.on_fade):
                self.on_fade(kind, idx, c.fade_in_s, c.fade_out_s, final)
        self.queue_draw()

    def _on_drag_cancel(self, *_args: object) -> None:
        self._stop_seek_scroll()
        if self._drag_mode not in ("fade-in", "fade-out", "volume", "transition"):
            return
        clips = self.vclips if self._drag_kind == "video" else self.aclips
        if self._handle_dirty and 0 <= self._drag_index < len(clips):
            clip = clips[self._drag_index]
            if self._drag_mode == "volume":
                clip.volume = self._drag_v0
                if callable(self.on_volume):
                    self.on_volume(self._drag_kind, self._drag_index, clip.volume, True)
            else:
                clip.fade_in_s, clip.fade_out_s = self._drag_fades
                if callable(self.on_fade):
                    self.on_fade(self._drag_kind, self._drag_index, *self._drag_fades, True)
        self._drag_mode = self._drag_kind = ""
        self._drag_index = -1
        self._handle_dirty = False
        self.queue_draw()

    def _on_drag_end(self, gesture: Gtk.GestureDrag | None = None, *_args: object) -> None:
        self._stop_seek_scroll()
        mode = self._drag_mode
        if mode in ("fade-in", "fade-out", "volume", "transition"):
            if mode == "transition":
                if not self._drag_moved and callable(self.on_transition):
                    self.on_transition(self._drag_index)
            elif self._handle_dirty:
                self._drag_handle(*self._handle_offsets, final=True)
            self._drag_mode = ""
            self._drag_kind = ""
            self._drag_index = -1
            self.queue_draw()
            return
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

    # ── Direct-edit handles: fade knobs, volume line, cut diamonds ───────

    def _clip_box(self, kind: str, index: int) -> tuple[float, float, float, float] | None:
        """(x0, y, x1, h) of a clip's drawn body, in widget coordinates."""
        clips = self.vclips if kind == "video" else self.aclips
        if not 0 <= index < len(clips):
            return None
        c = clips[index]
        lane = "v" if kind == "video" else "a"
        t0, t1 = self._clip_times(c, self._clip_src_dur(c, lane))
        if t1 <= t0:
            return None
        width = max(1.0, float(self.get_width()))
        y = self._lane_y(kind, c.track) + self._CLIP_PAD
        return self._t_to_x(t0, width), y, self._t_to_x(t1, width), self._LANE_H - 2 * self._CLIP_PAD

    def clip_rect(self, kind: str, index: int) -> Gdk.Rectangle | None:
        box = self._clip_box(kind, index)
        if box is None:
            return None
        x0, y, x1, h = box
        rect = Gdk.Rectangle()
        rect.x, rect.y = int(x0), int(y)
        rect.width, rect.height = max(1, int(x1 - x0)), int(h)
        return rect

    def _cuts(self) -> list[tuple[int, float]]:
        """(outgoing clip index, cut time) for video clips with a touching follower."""
        times = [self._clip_times(c, self._clip_src_dur(c, "v")) for c in self.vclips]
        cuts = []
        for i, c in enumerate(self.vclips):
            t1 = times[i][1]
            for j, d in enumerate(self.vclips):
                if j != i and d.track == c.track and abs(times[j][0] - t1) <= JOIN_EPS:
                    cuts.append((i, t1))
                    break
        return cuts

    def cut_rect(self, index: int) -> Gdk.Rectangle | None:
        box = self._clip_box("video", index)
        if box is None:
            return None
        _x0, y, x1, h = box
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x1 - 6), int(y + h - 6), 12, 12
        return rect

    def _fade_knob_x(self, kind: str, index: int, which: str) -> float | None:
        box = self._clip_box(kind, index)
        if box is None:
            return None
        x0, _y, x1, _h = box
        clips = self.vclips if kind == "video" else self.aclips
        c = clips[index]
        lane = "v" if kind == "video" else "a"
        t0, t1 = self._clip_times(c, self._clip_src_dur(c, lane))
        fi, fo = clamp_clip_fades(c.fade_in_s, c.fade_out_s, t1 - t0)
        width = max(1.0, float(self.get_width()))
        if which == "in":
            return min(x1, self._t_to_x(t0 + fi, width)) if fi > 0 else x0 + self._KNOB_INSET
        return max(x0, self._t_to_x(t1 - fo, width)) if fo > 0 else x1 - self._KNOB_INSET

    def _volume_y(self, index: int) -> float | None:
        box = self._clip_box("audio", index)
        if box is None:
            return None
        _x0, y, _x1, h = box
        vol = max(0.0, min(2.0, float(self.aclips[index].volume)))
        return y + h * (1.0 - vol / 2.0)

    def _knob_visible(self, kind: str, index: int, which: str) -> bool:
        primary = self.sel_v if kind == "video" else self.sel_a
        if index == primary:
            return True
        clips = self.vclips if kind == "video" else self.aclips
        c = clips[index]
        return (c.fade_in_s if which == "in" else c.fade_out_s) > 0

    def _hit_handle(self, x: float, y: float) -> tuple[str, str, int]:
        """Return (part, kind, index) for a fade knob, volume line or cut diamond."""
        if self.read_only:
            return "", "", -1
        kinds = ["video"] + (["audio"] if self.audio_kind != "source" else [])
        for kind in kinds:
            clips = self.vclips if kind == "video" else self.aclips
            for i in range(len(clips) - 1, -1, -1):
                box = self._clip_box(kind, i)
                if box is None:
                    continue
                x0, cy, x1, h = box
                if abs(y - cy) <= self._KNOB_R + 3:
                    for which in ("in", "out"):
                        kx = self._fade_knob_x(kind, i, which)
                        if (kx is not None and abs(x - kx) <= self._KNOB_R + 3
                                and self._knob_visible(kind, i, which)):
                            return f"fade-{which}", kind, i
        for i, _t in self._cuts():
            box = self._clip_box("video", i)
            if box is None:
                continue
            _x0, cy, x1, h = box
            if abs(x - x1) <= 7 and abs(y - (cy + h)) <= 7:
                return "transition", "video", i
        if self.audio_kind != "source":
            for i in range(len(self.aclips) - 1, -1, -1):
                box = self._clip_box("audio", i)
                vy = self._volume_y(i)
                if box is None or vy is None:
                    continue
                x0, _cy, x1, _h = box
                if x0 + self._EDGE < x < x1 - self._EDGE and abs(y - vy) <= 4:
                    return "volume", "audio", i
        return "", "", -1

    def _draw_filmstrip(
        self, cr, c: ClipInst, x0: float, x1: float, y: float, h: float  # noqa: ANN001
    ) -> bool:
        strip = self.filmstrips.get(c.media_id)
        src = self._clip_src_dur(c, "v")
        if strip is None or src <= 0 or x1 - x0 < 2:
            return False
        pixbuf, frames = strip
        fw = pixbuf.get_width() / frames
        fh = pixbuf.get_height()
        scale = h / fh
        slot = fw * scale
        inn, out = self._clip_used(c, src)
        cr.save()
        _round_rect(cr, x0, y, x1 - x0, h, 4)
        cr.clip()
        x = x0
        while x < x1:
            frac = (x + slot / 2 - x0) / max(1.0, x1 - x0)
            src_t = inn + min(1.0, max(0.0, frac)) * (out - inn)
            idx = min(frames - 1, max(0, int(src_t / src * frames)))
            cr.save()
            cr.rectangle(x, y, slot, h)
            cr.clip()
            cr.translate(x - idx * slot, y)
            cr.scale(scale, scale)
            Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
            cr.paint()
            cr.restore()
            x += slot
        cr.restore()
        return True

    def _draw_waveform(
        self, cr, c: ClipInst, x0: float, x1: float, y: float, h: float,  # noqa: ANN001
        color: tuple[float, float, float],
    ) -> None:
        wave = self.waves.get(c.media_id)
        src = self._clip_src_dur(c, "a")
        if wave is None or src <= 0 or x1 - x0 < 2:
            return
        peaks, rate = wave
        if not peaks:
            return
        inn, out = self._clip_used(c, src)
        top = max(peaks) or 1.0
        mid = y + h / 2.0
        cr.save()
        cr.rectangle(x0, y, x1 - x0, h)
        cr.clip()
        cr.set_source_rgba(*color, 0.75)
        cr.set_line_width(1.0)
        span = max(1.0, x1 - x0)
        step = 2.0
        x = x0 + 1.0
        per_px = (out - inn) / span
        while x < x1:
            a = inn + (x - x0) * per_px
            i0 = int(a * rate)
            i1 = max(i0 + 1, int((a + per_px * step) * rate))
            chunk = peaks[i0:i1]
            amp = (max(chunk) if chunk else 0.0) / top
            half = max(0.5, amp * (h / 2.0 - 2.0))
            cr.move_to(x, mid - half)
            cr.line_to(x, mid + half)
            x += step
        cr.stroke()
        cr.restore()

    def _draw_fades(
        self, cr, c: ClipInst, t0: float, t1: float,  # noqa: ANN001
        x0: float, x1: float, y: float, h: float, width: float,
    ) -> None:
        fi, fo = clamp_clip_fades(c.fade_in_s, c.fade_out_s, t1 - t0)
        if fi > 0:
            fx = min(x1, self._t_to_x(t0 + fi, width))
            grad = cairo.LinearGradient(x0, 0, fx, 0)
            grad.add_color_stop_rgba(0, 0, 0, 0, 0.8)
            grad.add_color_stop_rgba(1, 0, 0, 0, 0.0)
            cr.rectangle(x0, y, fx - x0, h)
            cr.set_source(grad)
            cr.fill()
        if fo > 0:
            fx = max(x0, self._t_to_x(t1 - fo, width))
            grad = cairo.LinearGradient(fx, 0, x1, 0)
            grad.add_color_stop_rgba(0, 0, 0, 0, 0.0)
            grad.add_color_stop_rgba(1, 0, 0, 0, 0.8)
            cr.rectangle(fx, y, x1 - fx, h)
            cr.set_source(grad)
            cr.fill()

    def _draw_knob(self, cr, x: float, y: float, fg, bg) -> None:  # noqa: ANN001
        cr.new_path()
        cr.arc(x, y, self._KNOB_R, 0, 6.2832)
        cr.set_source_rgb(*fg)
        cr.fill_preserve()
        cr.set_source_rgb(*bg)
        cr.set_line_width(2)
        cr.stroke()

    def _draw_tab(self, cr, x0: float, x1: float, y: float, name: str,  # noqa: ANN001
                  fill, ink) -> None:
        if not name or x1 - x0 < 24:
            return
        cr.save()
        cr.rectangle(x0, y, x1 - x0, self._LANE_H)
        cr.clip()
        cr.set_font_size(10)
        ext = cr.text_extents(name)
        tw = min(ext.x_advance + 10, x1 - x0)
        cr.rectangle(x0, y, tw, 15)
        cr.set_source_rgb(*fill)
        cr.fill()
        cr.rectangle(x0 + 5, y, tw - 7, 15)
        cr.clip()
        cr.set_source_rgb(*ink)
        cr.move_to(x0 + 5, y + 11)
        cr.show_text(name)
        cr.restore()

    def _draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:  # noqa: ANN001
        bg = theme_rgb("background", (0.07, 0.07, 0.07))
        lane_bg = theme_rgb("lighter_background", (0.13, 0.12, 0.13))
        line = theme_rgb("line", (0.22, 0.19, 0.21))
        accent = theme_rgb("accent", (0.84, 0.67, 0.80))
        fg = theme_rgb("foreground", (0.84, 0.67, 0.80))
        muted = theme_rgb("muted", (0.49, 0.40, 0.47))
        green = theme_rgb("green", (0.54, 0.86, 0.54))
        red = theme_rgb("red", (1.0, 0.30, 0.65))
        audio = theme_rgb("blue", (0.49, 0.65, 0.79))
        on_accent = _label_rgb_on(accent)
        cr.select_font_face("monospace")
        cr.set_source_rgb(*bg)
        cr.paint()

        left, inner = self._inner(width)
        v_y = self._lane_y("video", 2)
        lanes_bottom = self._lane_y("audio", 2) + self._LANE_H
        span = self._map_span()

        # Ruler
        cr.set_font_size(10)
        cr.set_line_width(1)
        if span > 0:
            step = span
            for cand in (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 30.0, 60.0, 120.0, 300.0):
                if span / cand <= max(1.0, (width - self._GUTTER - self._PAD_RIGHT) / 90):
                    step = cand
                    break
            t = 0.0
            while t <= span + 0.0001:
                tx = int(self._t_to_x(t, width)) + 0.5
                cr.set_source_rgb(*line)
                cr.move_to(tx, 1)
                cr.line_to(tx, self._RULER_H - 2)
                cr.stroke()
                cr.set_source_rgb(*muted)
                cr.move_to(tx + 3, 11)
                cr.show_text(f"{t:.0f}s" if step >= 1 else f"{t:.1f}s")
                t += step

        # Render-cache bar: line = nothing rendered, red = stale, green = current.
        bar_y = self._RULER_H + 1.0
        cr.rectangle(left, bar_y, inner, 3)
        cr.set_source_rgb(*line)
        cr.fill()
        for t0, t1, ok in self.cache_spans:
            if span <= 0 or t1 <= t0:
                continue
            x0 = self._t_to_x(t0, width)
            x1 = self._t_to_x(t1, width)
            cr.set_source_rgb(*(green if ok else red))
            cr.rectangle(x0, bar_y, max(1.0, x1 - x0), 3)
            cr.fill()

        # Lanes and their names
        for label, kind, track_no in (
            ("V2", "video", 2), ("V1", "video", 1), ("A1", "audio", 1), ("A2", "audio", 2),
        ):
            lane_y = self._lane_y(kind, track_no)
            _round_rect(cr, left, lane_y, inner, self._LANE_H, 4)
            cr.set_source_rgb(*lane_bg)
            cr.fill()
            active = (kind, track_no) == (self.nav_kind, self.nav_track)
            cr.set_font_size(10)
            cr.set_source_rgb(*(fg if active else muted))
            cr.move_to(2, lane_y + self._LANE_H / 2.0 + 4.0)
            cr.show_text(label)
            if active:
                cr.rectangle(0, lane_y + 4, 2, self._LANE_H - 8)
                cr.fill()

        ch = self._LANE_H - 2 * self._CLIP_PAD

        # Video clips: filmstrip, accent outline, name tab, fades
        for i, c in enumerate(self.vclips):
            d = self._clip_src_dur(c, "v")
            t0, t1 = self._clip_times(c, d)
            cy = self._lane_y("video", c.track) + self._CLIP_PAD
            gx0 = self._t_to_x(c.start, width)
            gx1 = self._t_to_x(c.start + (d / c.playback_speed() if d > 0 else 0.0), width)
            _round_rect(cr, gx0, cy, max(3.0, gx1 - gx0), ch, 4)
            cr.set_source_rgba(*accent, 0.1)
            cr.fill()
            x0 = self._t_to_x(t0, width)
            x1 = max(x0 + 3.0, self._t_to_x(t1, width))
            if not self._draw_filmstrip(cr, c, x0, x1, cy, ch):
                _round_rect(cr, x0, cy, x1 - x0, ch, 4)
                cr.set_source_rgba(*accent, 0.35)
                cr.fill()
            self._draw_fades(cr, c, t0, t1, x0, x1, cy, ch, width)
            primary = i == self.sel_v
            selected = primary or i in self.sel_vs
            _round_rect(cr, x0 + 1, cy + 1, x1 - x0 - 2, ch - 2, 4)
            cr.set_source_rgb(*(accent if selected else muted))
            cr.set_line_width(3.0 if primary else 2.0 if selected else 1.5)
            cr.stroke()
            name = self.clip_names.get(c.media_id) or self.video_name
            self._draw_tab(cr, x0, x1, cy, Path(name).stem if name else "",
                           accent if selected else muted, on_accent if selected else bg)

        # Audio clips: tinted body, waveform, volume line
        source = self.audio_kind == "source"
        acolor = muted if source else audio
        for i, c in enumerate(self.aclips):
            d = self._clip_src_dur(c, "a")
            t0, t1 = self._clip_times(c, d)
            cy = self._lane_y("audio", c.track) + self._CLIP_PAD
            gx0 = self._t_to_x(c.start, width)
            gx1 = self._t_to_x(c.start + (d / c.playback_speed() if d > 0 else 0.0), width)
            _round_rect(cr, gx0, cy, max(3.0, gx1 - gx0), ch, 4)
            cr.set_source_rgba(*acolor, 0.07)
            cr.fill()
            x0 = self._t_to_x(t0, width)
            x1 = max(x0 + 3.0, self._t_to_x(t1, width))
            _round_rect(cr, x0, cy, x1 - x0, ch, 4)
            cr.set_source_rgba(*acolor, 0.16)
            cr.fill()
            self._draw_waveform(cr, c, x0, x1, cy, ch, acolor)
            self._draw_fades(cr, c, t0, t1, x0, x1, cy, ch, width)
            primary = i == self.sel_a
            selected = primary or i in self.sel_as
            _round_rect(cr, x0 + 1, cy + 1, x1 - x0 - 2, ch - 2, 4)
            cr.set_source_rgb(*(fg if selected else acolor))
            cr.set_line_width(3.0 if primary else 2.0 if selected else 1.5)
            cr.stroke()
            if not source:
                vy = self._volume_y(i)
                if vy is not None:
                    cr.set_source_rgb(*fg)
                    cr.set_line_width(1.5)
                    cr.move_to(x0 + 2, vy)
                    cr.line_to(x1 - 2, vy)
                    cr.stroke()
                    if selected or abs(c.volume - 1.0) > 0.005:
                        label = f"{c.volume * 100:.0f}%"
                        cr.set_font_size(10)
                        ext = cr.text_extents(label)
                        lx = x1 - ext.x_advance - 6
                        ly = vy - 4 if vy - cy > 14 else vy + 12
                        if lx > x0 + 4:
                            cr.move_to(lx, ly)
                            cr.show_text(label)
            name = self.clip_names.get(c.media_id) or self.audio_name
            if name:
                self._draw_tab(cr, x0, x1, cy, Path(name).stem,
                               fg if selected else acolor, bg)

        # Fade knobs (selected clip, or wherever a fade is set)
        for kind in ("video", "audio"):
            clips = self.vclips if kind == "video" else self.aclips
            if kind == "audio" and source:
                continue
            for i in range(len(clips)):
                box = self._clip_box(kind, i)
                if box is None:
                    continue
                for which in ("in", "out"):
                    if self._knob_visible(kind, i, which):
                        kx = self._fade_knob_x(kind, i, which)
                        if kx is not None:
                            self._draw_knob(cr, kx, box[1], fg, bg)

        # Cut diamonds: hollow = hard cut, filled = transition set
        for i, t in self._cuts():
            box = self._clip_box("video", i)
            if box is None:
                continue
            _x0, cy, x1, h = box
            dy = cy + h
            cr.move_to(x1, dy - 6)
            cr.line_to(x1 + 6, dy)
            cr.line_to(x1, dy + 6)
            cr.line_to(x1 - 6, dy)
            cr.close_path()
            set_ = self.vclips[i].transition not in ("", TRANSITION_NONE)
            cr.set_source_rgb(*(fg if set_ else bg))
            cr.fill_preserve()
            cr.set_source_rgb(*fg)
            cr.set_line_width(2)
            cr.stroke()

        if self._drop_hover is not None:
            kind, t, track_no = self._drop_hover
            hover_y = self._lane_y(kind, track_no) + self._CLIP_PAD
            dur = self.video_dur if kind == "video" else self.audio_dur
            if dur > 0:
                gx0 = self._t_to_x(t, width)
                gx1 = self._t_to_x(t + dur, width)
                _round_rect(cr, gx0, hover_y, max(3.0, gx1 - gx0), ch, 4)
                cr.set_source_rgba(*(accent if kind == "video" else audio), 0.45)
                cr.fill()

        if self._snap_line is not None:
            sx = self._t_to_x(self._snap_line, width)
            cr.set_source_rgba(*accent, 0.95)
            cr.set_line_width(1.5)
            cr.move_to(sx, v_y - 2)
            cr.line_to(sx, lanes_bottom + 2)
            cr.stroke()

        px = int(self._t_to_x(self.playhead, width)) + 0.5
        cr.set_source_rgb(*fg)
        cr.set_line_width(2)
        cr.move_to(px, 4)
        cr.line_to(px, lanes_bottom + 2)
        cr.stroke()
        cr.move_to(px - 6, 0)
        cr.line_to(px + 6, 0)
        cr.line_to(px, 8)
        cr.close_path()
        cr.fill()


class MediaCard(Gtk.Box):
    """One video or audio slot in the lower-right media box."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.add_css_class("media-row")
        self.set_size_request(230, -1)
        self._kind = "empty"
        self._payload = ""
        drag = Gtk.DragSource()
        drag.set_actions(Gdk.DragAction.COPY)
        drag.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag.connect("prepare", self._drag_prepare)
        drag.connect("drag-begin", self._drag_begin)
        self.add_controller(drag)
        self._swatch = Gtk.DrawingArea()
        self._swatch.set_content_width(3)
        self._swatch.set_hexpand(False)
        self._swatch.set_draw_func(self._draw_swatch)
        self.append(self._swatch)
        self.picture = Gtk.Picture()
        self.picture.set_content_fit(Gtk.ContentFit.COVER)
        self.picture.set_valign(Gtk.Align.CENTER)
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
        self.title.set_lines(2)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.meta = Gtk.Label(xalign=0)
        self.meta.add_css_class("dim-label")
        self.meta.set_wrap(True)
        self.meta.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.meta.set_max_width_chars(18)
        self.meta.add_css_class("tnum")
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
            "audio": ("blue", (0.49, 0.65, 0.79)),
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
            scale = min(44 / pixbuf.get_width(), 64 / pixbuf.get_height())
            if scale < 1:
                pixbuf = pixbuf.scale_simple(
                    max(1, int(pixbuf.get_width() * scale)),
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
        self.audio_track_volumes = {1: 1.0, 2: 1.0}
        self.sel_v = -1
        self.sel_a = -1
        self.sel_vs: set[int] = set()
        self.sel_as: set[int] = set()
        self.sel_kind = ""
        self.keyboard_mode = ""
        self.keyboard_increment = 1.0
        self.keyboard_increments = {"move": 1.0, "trim-in": .1, "trim-out": .1,
                                    "ripple": "clip", "seek": .1}
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
        self._preview_drag_clip: ClipInst | None = None
        self._layer_media: dict[int, tuple[Gtk.MediaFile, int, int]] = {}
        self._raw_layer_ids: tuple[int, ...] = ()
        self._history: list[Project] = []
        self._hist_i = -1
        self._ckpt_src: int = 0
        self._applying_history = False
        self._undo_action: Gio.SimpleAction | None = None
        self._redo_action: Gio.SimpleAction | None = None

        # No header bar: like other Omarchy apps, Hyprland owns the window
        # frame. Panes are bordered like Hyprland windows; the focused one gets
        # the accent border. Actions are key hints, also clickable.
        self.add_css_class("clip-editor")
        toolbar = Adw.ToolbarView()
        self._install_project_actions()
        self.btn_export = self._hint("e", "export", self._on_export)
        self.btn_export.set_sensitive(False)

        self._panes: list[Gtk.Frame] = []
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_top(10)
        root.set_margin_bottom(4)
        root.set_margin_start(4)
        root.set_margin_end(4)
        root.append(self._build_preview_pane())
        root.append(self._build_timeline_pane())
        root.append(self._build_media_pane())
        root.append(self._build_statusline())
        self._build_popovers()
        self.connect("notify::focus-widget", lambda *_: self._sync_pane_focus())
        toolbar.set_content(root)
        self.set_content(toolbar)
        self.keyboard_help = self._build_keyboard_help()
        self.keyboard_help.set_parent(self.timeline)
        self._closed = False
        on_theme_change(self._on_theme_change)
        self._shutdown_handler = 0
        self.connect("close-request", self._on_close)
        application = self.get_application()
        if application is not None:
            self._shutdown_handler = application.connect("shutdown", self._on_close)

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

    def _build_keyboard_help(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        popover.add_css_class("clip-pop")
        popover.set_autohide(False)
        popover.set_focusable(False)
        popover.set_has_arrow(False)
        popover.set_position(Gtk.PositionType.TOP)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(18)
        content.set_margin_end(18)

        title = Gtk.Label(label="Keyboard shortcuts", xalign=0)
        title.add_css_class("title-3")
        content.append(title)

        body = Gtk.Label(xalign=0)
        body.set_wrap(True)
        body.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        body.set_markup(
            "<b>Editor-wide</b>\n"
            "Space   play / pause from any editor control\n"
            "Ctrl+N / Ctrl+O   new / open project\n"
            "Ctrl+S / Ctrl+Shift+S   save / save as\n\n"
            "<b>Timeline navigation</b>\n"
            "Click the timeline to use the shortcuts below.\n"
            "h / l   select previous / next clip\n"
            "Shift+h / Shift+l   extend selection\n"
            "j / k   move between video and audio tracks\n"
            "= / +   zoom in     -   zoom out     f   fit timeline\n"
            "Enter   open selected clip settings\n"
            "z   toggle safe zones     e   export\n"
            ":   command line (r916, r43, rp)\n\n"
            "<b>Keyboard editing</b>\n"
            "m   move selected clips\n"
            "r   ripple-reorder a clip or contiguous group\n"
            "[ / ]   trim the selected clip's left / right edge\n"
            "s   seek the playhead\n"
            "h / l   apply the active edit or seek increment\n"
            "Up / Down   change the active increment\n"
            "t   split the selected clip at the playhead\n"
            "Delete   remove the selected clip\n"
            "Ctrl+Z   undo     Ctrl+Shift+Z / Ctrl+Y   redo\n\n"
            "<b>Help and modes</b>\n"
            "?   show / hide this help\n"
            "Esc   close help first; then exit mode or clear selection\n\n"
            "Edits target the active video or audio track.\n"
            "Opening help leaves the current mode and selection unchanged."
        )
        body.set_selectable(False)
        self.keyboard_help_body = body
        content.append(body)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_focusable(False)
        scroll.set_min_content_width(320)
        scroll.set_min_content_height(240)
        scroll.set_child(content)
        self.keyboard_help_scroll = scroll
        popover.set_child(scroll)
        return popover

    def _toggle_keyboard_help(self) -> None:
        if self.keyboard_help.get_visible():
            self.keyboard_help.popdown()
        else:
            # Keep the reference scrollable inside smaller editor windows.
            self.keyboard_help_scroll.set_min_content_height(
                min(440, max(160, self.get_height() - 120))
            )
            self.keyboard_help.popup()

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
        # Playback belongs to the whole editor, including focused controls.
        mods = state & Gtk.accelerator_get_default_mod_mask()
        if not mods and keyval in (Gdk.KEY_space, Gdk.KEY_KP_Space):
            if not self._space_held:
                self._space_held = True
                self._on_play()
            return True
        keyboard_help = getattr(self, "keyboard_help", None)
        if keyboard_help is not None and keyboard_help.get_visible() and not mods and keyval == Gdk.KEY_Escape:
            self._toggle_keyboard_help()
            return True
        # Timeline is a leaf widget: exact ownership also excludes inspector
        # entries, buttons, popovers, dialogs, and the colon command entry.
        if self.get_focus() is not self.timeline:
            return False
        mods = state & Gtk.accelerator_get_default_mod_mask()
        ctrl = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        shift = bool(mods & Gdk.ModifierType.SHIFT_MASK)
        extra = mods & ~(Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)
        if not ctrl and not extra and keyval == Gdk.KEY_question:
            self._toggle_keyboard_help()
            return True
        if extra:
            return False
        if not mods and keyval == Gdk.KEY_m:
            self._enter_keyboard_mode("move")
            return True
        if not mods and keyval == Gdk.KEY_r:
            self._enter_keyboard_mode("ripple")
            return True
        if not mods and keyval == Gdk.KEY_s:
            self._enter_keyboard_mode("seek")
            return True
        if not mods and keyval in (Gdk.KEY_bracketleft, Gdk.KEY_bracketright):
            self._enter_keyboard_mode("trim-in" if keyval == Gdk.KEY_bracketleft else "trim-out")
            return True
        if self.keyboard_mode and not mods and keyval == Gdk.KEY_Escape:
            self._exit_keyboard_mode()
            return True
        if self.keyboard_mode and not mods and keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            self._change_keyboard_increment(-1 if keyval == Gdk.KEY_Up else 1)
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
        if keyval == Gdk.KEY_plus and not ctrl:
            # "+" is Shift+= on most layouts, so it cannot wait for the no-modifier block.
            self.timeline.zoom_view(1.5)
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
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self._open_clip_popover()
            return True
        if keyval == Gdk.KEY_z:
            if self.btn_safe_zones.get_sensitive():
                self.btn_safe_zones.set_active(not self.btn_safe_zones.get_active())
            return True
        if keyval == Gdk.KEY_e:
            if self.btn_export.get_sensitive():
                self._on_export()
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
            self.timeline.zoom_view(1 / 1.5)
            return True
        if keyval in (Gdk.KEY_equal, Gdk.KEY_KP_Add):
            self.timeline.zoom_view(1.5)
            return True
        if keyval == Gdk.KEY_f:
            self.timeline.fit_view()
            return True
        return False

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
        if self.exporting or not self._guard_edit("seek" if mode == "seek" else "move"):
            return
        if mode != "seek" and not self._keyboard_target()[0]:
            self._set_status("Select a clip on the active track")
            return
        self._stop()
        self._exit_keyboard_mode()
        self.keyboard_mode = mode
        self.keyboard_increment = self.keyboard_increments.get(mode, 1.0)
        self._show_keyboard_mode()

    def _exit_keyboard_mode(self) -> None:
        if self.keyboard_mode:
            self.keyboard_increments[self.keyboard_mode] = self.keyboard_increment
        self.keyboard_mode = ""
        self.keyboard_hint.set_text("")
        self.keyboard_hint.set_visible(False)
        self.mode_label.set_text("NORMAL")

    def _show_keyboard_mode(self) -> None:
        kind, clips, primary, selected = self._keyboard_target()
        increment = (str(self.keyboard_increment) if isinstance(self.keyboard_increment, str)
                     else f"{self.keyboard_increment:g}s")
        target = f"{len(selected)} selected"
        if self.keyboard_mode == "ripple":
            target += " · h inserts before previous / l inserts after next"
        if self.keyboard_mode.startswith("trim") and primary >= 0:
            clip = clips[primary]
            edge = "left" if self.keyboard_mode == "trim-in" else "right (ripple)"
            target = f"primary clip {edge} edge · duration {clip.timeline_len():.2f}s"
        if self.keyboard_mode == "seek":
            kind = self.timeline.nav_kind
            target = f"playhead {self.timeline.playhead:.3f}s"
            if self.keyboard_increment == "frame":
                increment = f"1 frame ({self._keyboard_fps():g} fps grid)"
            elif self.keyboard_increment == "clip":
                increment = "clip edges"
        self.mode_label.set_text(self.keyboard_mode.upper())
        self.keyboard_hint.set_visible(True)
        self.keyboard_hint.set_text(
            f"{kind[:1].upper()}{self.timeline.nav_track} · "
            f"{target} · {increment} · h/l earlier/later · ↑/↓ increment · esc exits")

    def _change_keyboard_increment(self, direction: int) -> None:
        ladder = MOVE_INCREMENTS[:3] if self.keyboard_mode.startswith("trim") else MOVE_INCREMENTS
        if self.keyboard_mode == "ripple":
            ladder = ("clip",)
        elif self.keyboard_mode == "seek":
            ladder = SEEK_INCREMENTS
        index = ladder.index(self.keyboard_increment)
        self.keyboard_increment = ladder[
            max(0, min(len(ladder) - 1, index + direction))
        ]
        self._show_keyboard_mode()

    def _apply_keyboard_clips(self, kind: str, clips: list[ClipInst], *,
                              primary: int | None = None, selected: set[int] | None = None) -> None:
        self._flush_checkpoint()
        self._checkpoint()
        self._stop()
        if primary is not None:
            if kind == "video":
                self.sel_v, self.sel_vs = primary, set(selected or {primary})
            else:
                self.sel_a, self.sel_as = primary, set(selected or {primary})
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
        self._sync_timeline_clips()
        self._refresh_fit()
        self._checkpoint()
        self._schedule_autosave()
        self.timeline._ensure_clip_visible(kind, self.sel_v if kind == "video" else self.sel_a)
        self._apply_timeline_frame(self.timeline.playhead, start_media=False)

    def _nudge_keyboard(self, direction: int) -> None:
        if self.keyboard_mode == "seek":
            self._seek_keyboard(direction)
            return
        if self.exporting or not self._guard_edit("move"):
            return
        kind, clips, primary, selected = self._keyboard_target()
        if not kind:
            self._exit_keyboard_mode()
            self._set_status("Select a clip on the active track")
            return
        delta = (boundary_delta(clips, selected, primary, direction)
                 if self.keyboard_increment == "clip" else direction * self.keyboard_increment)
        if self.keyboard_mode == "ripple":
            try:
                changed = reorder_clips(clips, selected, primary, direction)
            except ValueError as error:
                self._set_status(str(error))
                return
        elif self.keyboard_mode.startswith("trim"):
            clip = clips[primary]
            duration = self._media_dur(clip.media_id) or clip.out_s
            changed = trim_clip(clips, primary, self.keyboard_mode.removeprefix("trim-"),
                                delta, duration)
        else:
            changed = move_clips(clips, selected, delta)
        if changed == clips:
            self._set_status("Clip or timeline boundary")
            return
        self._apply_keyboard_clips(kind, changed)
        self._show_keyboard_mode()

    def _keyboard_fps(self) -> float:
        # A timeline grid, not source-frame stepping: stable across selection
        # and applicable to audio-only tracks as well.
        info = ((self.media_info.get(self.video_clips[0].media_id) or {})
                if self.video_clips else {})
        fps = float(info.get("fps") or 30)
        return fps if 1 <= fps <= 1000 else 30.0

    def _seek_keyboard(self, direction: int) -> None:
        time = self._timeline_now()
        if self.keyboard_increment == "frame":
            target = seek_frame(time, direction, self._keyboard_fps())
        elif self.keyboard_increment == "clip":
            edges = [edge for _, start, end in self.timeline._navigation_clips()
                     for edge in (start, end) if (edge - time) * direction > 1e-8]
            target = (min(edges) if direction > 0 else max(edges)) if edges else time
        else:
            target = time + direction * self.keyboard_increment
        target = max(0, min(self._program_end(), target))
        self.timeline.set_playhead(target)
        self._on_timeline_seek(target)
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
                    round(float(c.fade_in_s), 3),
                    round(float(c.fade_out_s), 3),
                    round(float(c.volume), 4),
                )
                for c in p.video_clips
            ),
            tuple(
                (
                    round(c.start, 3),
                    round(c.in_s, 3),
                    round(c.out_s, 3),
                    c.media_id,
                    int(c.track),
                    round(float(c.fade_in_s), 3),
                    round(float(c.fade_out_s), 3),
                    round(float(c.volume), 4),
                )
                for c in p.audio_clips
            ),
            tuple((m.id, m.kind, str(m.path)) for m in p.media),
            tuple(sorted(p.audio_track_volumes.items())),
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

    def _on_preview_pan_begin(self, mode: str = "move") -> bool:
        """Start a per-clip translation, using the selected source, not a proxy."""
        self._preview_drag_clip = None
        clip = self._selected_video_clip()
        if clip is None or self._busy_rendering() or not self._guard_edit("transform"):
            return False
        info = self.media_info.get(clip.media_id) or self.video_info or {}
        sw, sh = int(info.get("width") or 0), int(info.get("height") or 0)
        vw, vh = self.preview.get_width(), self.preview.get_height()
        if min(sw, sh, vw, vh) <= 0:
            return False
        dw, dh = dest_size(self.aspect, self.resolution)
        self._preview_drag_origin = (clip.transform_x, clip.transform_y, clip.scale)
        self._preview_drag_mode = mode
        self._preview_drag_source = (sw, sh, dw, dh)
        self._preview_drag_units = (dw / vw, dh / vh)
        self._stop()
        self._preview_drag_clip = clip
        self._apply_timeline_frame(self.timeline.playhead, start_media=False, edit_clip=clip)
        return True

    def _on_preview_pan(self, dx: float, dy: float) -> None:
        clip = self._preview_drag_clip
        if (clip is None or clip is not self._selected_video_clip()
                or self._busy_rendering() or self._editing_locked()):
            return
        x, y, scale = self._preview_drag_origin
        ux, uy = self._preview_drag_units
        if dx == 0 and dy == 0:
            clip.transform_x, clip.transform_y, clip.scale = x, y, scale
        elif self._preview_drag_mode == "move":
            clip.transform_x = min(4096, max(-4096, x + dx * ux))
            clip.transform_y = min(4096, max(-4096, y + dy * uy))
        else:
            sw, sh, dw, dh = self._preview_drag_source
            clip.transform_x, clip.transform_y, clip.scale = resize_source_from_corner(
                sw, sh, dw, dh, self.preview.pan_x, self.preview.pan_y, x, y, scale,
                self._preview_drag_mode, dx * ux, dy * uy,
            )
        clip.transform_x = min(4096, max(-4096, clip.transform_x))
        clip.transform_y = min(4096, max(-4096, clip.transform_y))
        self._sync_transform_controls()
        dw, dh = dest_size(self.aspect, self.resolution)
        self.preview.set_transform(clip.transform_x, clip.transform_y, clip.scale, dw, dh)

    def _on_preview_pan_cancel(self) -> None:
        clip = self._preview_drag_clip
        if clip is None:
            return
        clip.transform_x, clip.transform_y, clip.scale = self._preview_drag_origin
        self._preview_drag_clip = None
        self._sync_transform_controls()
        self.preview.queue_draw()

    def _on_preview_pan_end(self, *_args: object) -> None:
        if self._preview_drag_clip is None:
            return
        self._preview_drag_clip = None
        self._checkpoint()
        self._refresh_cache_bar()
        self._schedule_autosave()

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
            audio_track_volumes=dict(self.audio_track_volumes),
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
        self.video_label.set_text("no clip selected")
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
        self.audio_label.set_text("no music")
        self.audio_start = 0.0
        self.audio_in = 0.0
        self.audio_out = None
        self.audio_clips = []
        self.sel_a = -1
        if self.sel_kind == "audio":
            self.sel_kind = "video" if self.video_clips else ""
        self._refresh_media()

    def _install_media_list(self, items: list[MediaItem]) -> None:
        self._clear_visuals()
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
            self._queue_visuals(src.id)
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
            self.audio_track_volumes = normalize_track_volumes(proj.audio_track_volumes)
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
        self._clear_visuals()
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
        self.audio_track_volumes = {1: 1.0, 2: 1.0}
        self.sel_v = -1
        self.sel_a = -1
        self.sel_vs: set[int] = set()
        self.sel_as: set[int] = set()
        self.sel_kind = ""
        self.btn_play.text_label.set_text("play")
        self.progress.set_fraction(0)
        self.timeline.set_clips()
        self.timeline.set_playhead(0)
        self.timeline.set_range(0.0, 0.0)
        self.timeline.set_duration(0.0)
        self.timeline.set_read_only(False)
        self.preview.set_blank(False)
        self._sync_transition_controls()
        self._sync_audio_volume_controls()
        self._sync_fade_controls()
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

    # ── Building blocks ──────────────────────────────────────────────────

    def _hint(
        self,
        key: str,
        text: str,
        on_click=None,  # noqa: ANN001
        *,
        toggle: bool = False,
        tooltip: str = "",
    ) -> Gtk.Button:
        """A key hint (`key` in the key color, then `text`) that is also a button."""
        btn: Gtk.Button = Gtk.ToggleButton() if toggle else Gtk.Button()
        btn.add_css_class("hint")
        box = Gtk.Box(spacing=6)
        if key:
            k = Gtk.Label(label=key)
            k.add_css_class("key")
            box.append(k)
        t = Gtk.Label(label=text)
        box.append(t)
        btn.set_child(box)
        btn.text_label = t  # type: ignore[attr-defined]
        if tooltip:
            btn.set_tooltip_text(tooltip)
        if on_click is not None:
            btn.connect("toggled" if toggle else "clicked", lambda *_: on_click())
        return btn

    def _pane(self, name: str, *title_parts: Gtk.Widget) -> tuple[Gtk.Frame, Gtk.Box]:
        """Bordered pane with `name` and key hints set into the top border."""
        title = Gtk.Box(spacing=4)
        title.add_css_class("pane-title")
        lab = Gtk.Label(label=name)
        lab.add_css_class("pane-name")
        title.append(lab)
        for part in title_parts:
            title.append(part)
        frame = Gtk.Frame()
        frame.add_css_class("pane")
        frame.set_label_widget(title)
        frame.set_label_align(0.0)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.set_margin_top(4)
        body.set_margin_bottom(8)
        body.set_margin_start(10)
        body.set_margin_end(10)
        frame.set_child(body)
        self._panes.append(frame)
        return frame, body

    def _sync_pane_focus(self) -> None:
        focus = self.get_focus()
        for pane in self._panes:
            inside = focus is not None and (focus is pane or focus.is_ancestor(pane))
            if inside:
                pane.add_css_class("focused")
            else:
                pane.remove_css_class("focused")

    @staticmethod
    def _dim(text: str) -> Gtk.Label:
        lab = Gtk.Label(label=text, xalign=0)
        lab.add_css_class("dim")
        return lab

    # ── Panes ────────────────────────────────────────────────────────────

    def _build_preview_pane(self) -> Gtk.Widget:
        self.aspect_buttons: dict[str, Gtk.ToggleButton] = {}
        aspects = Gtk.Box()
        group = None
        for name in ASPECTS:
            tb = Gtk.ToggleButton(label=name)
            tb.add_css_class("hint")
            tb.add_css_class("choice")
            if group is None:
                group = tb
            else:
                tb.set_group(group)
            if name == "9:16":
                tb.set_active(True)
            tb.connect("toggled", self._on_aspect, name)
            self.aspect_buttons[name] = tb
            aspects.append(tb)
        self.resolution_buttons: dict[str, Gtk.ToggleButton] = {}
        resolutions = Gtk.Box()
        res_group = None
        for name in RESOLUTIONS:
            tb = Gtk.ToggleButton(label=name)
            tb.add_css_class("hint")
            tb.add_css_class("choice")
            if res_group is None:
                res_group = tb
            else:
                tb.set_group(res_group)
            if name == DEFAULT_RESOLUTION:
                tb.set_active(True)
            tb.connect("toggled", self._on_resolution, name)
            self.resolution_buttons[name] = tb
            resolutions.append(tb)
        self.btn_safe_zones = self._hint(
            "z", "safe zones off", lambda: self._on_safe_zones(self.btn_safe_zones),
            toggle=True,
            tooltip="Shade where TikTok, Reels and Shorts draw captions and buttons (9:16 only)",
        )
        frame, body = self._pane(
            "preview", aspects, self._dim("·"), resolutions, self._dim("·"), self.btn_safe_zones
        )
        frame.set_vexpand(True)

        self.aspect_frame = Gtk.AspectFrame(ratio=9 / 16, obey_child=False)
        self.aspect_frame.set_hexpand(True)
        self.aspect_frame.set_vexpand(True)
        self.preview = CoverPreview()
        self.preview.on_pan_begin = self._on_preview_pan_begin
        self.preview.on_pan = self._on_preview_pan
        self.preview.on_pan_end = self._on_preview_pan_end
        self.preview.on_pan_cancel = self._on_preview_pan_cancel
        self.preview.on_scale = self._on_preview_scale
        self.aspect_frame.set_child(self.preview)
        body.append(self.aspect_frame)

        transport = Gtk.Box(spacing=6)
        self.clock = Gtk.Label(label="0.00 / 0.00", xalign=0)
        self.clock.add_css_class("clock")
        transport.append(self.clock)
        self.compiled_preview_label = Gtk.Label(xalign=0)
        self.compiled_preview_label.add_css_class("dim")
        self.compiled_preview_label.set_hexpand(True)
        self.compiled_preview_label.set_ellipsize(Pango.EllipsizeMode.END)
        transport.append(self.compiled_preview_label)
        self.btn_play = self._hint("␣", "play", self._on_play, tooltip="Play / pause (Space)")
        self.btn_play.set_sensitive(False)
        transport.append(self.btn_play)
        self.btn_split = self._hint(
            "t", "split", self._split_selected_clip,
            tooltip="Split the selected clip at the playhead",
        )
        transport.append(self.btn_split)
        self.btn_render_preview = self._hint(
            ":rp", "render", self._on_render_preview,
            tooltip="Render the timeline with transitions baked in. The bar above the "
            "tracks turns green where the render is current; red parts play from "
            "the raw clips.",
        )
        transport.append(self.btn_render_preview)
        self.btn_preview_cancel = self._hint("", "cancel render", self._on_cancel_preview_render)
        self.btn_preview_cancel.set_sensitive(False)
        self.btn_preview_cancel.set_visible(False)
        transport.append(self.btn_preview_cancel)
        self.crop_label = Gtk.Label(xalign=1)
        self.crop_label.add_css_class("dim")
        self.crop_label.set_ellipsize(Pango.EllipsizeMode.START)
        transport.append(self.crop_label)
        body.append(transport)
        return frame

    def _build_timeline_pane(self) -> Gtk.Widget:
        self.btn_clip_settings = self._hint(
            "⏎", "clip", lambda: self._open_clip_popover(),
            tooltip="Exact values for the selected clip (Enter or double-click)",
        )
        self.btn_timeline_zoom_out = self._hint(
            "-", "", lambda: self.timeline.zoom_view(1 / 1.5), tooltip="Zoom out"
        )
        self.btn_timeline_zoom_in = self._hint(
            "=", "zoom", lambda: self.timeline.zoom_view(1.5), tooltip="Zoom in (= or +)"
        )
        self.btn_timeline_fit = self._hint(
            "f", "fit", lambda: self.timeline.fit_view(), tooltip="Fit the whole timeline (F)"
        )
        # h/l and j/k only work from the keyboard, so their hints are labels.
        clip_nav = self._hint("h/l", "clip")
        track_nav = self._hint("j/k", "track")
        for hint in (clip_nav, track_nav):
            hint.set_can_target(False)
            hint.set_focusable(False)
        frame, body = self._pane(
            "timeline",
            clip_nav,
            track_nav,
            self.btn_clip_settings,
            self._dim("·"),
            self.btn_timeline_zoom_out,
            self.btn_timeline_zoom_in,
            self.btn_timeline_fit,
        )

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
        self.timeline.on_fade = self._on_timeline_fade
        self.timeline.on_volume = self._on_timeline_volume
        self.timeline.on_transition = self._on_timeline_transition
        self.timeline.on_activate = self._on_timeline_activate
        self.timeline_scroll = Gtk.ScrolledWindow()
        self.timeline_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self.timeline_scroll.set_hexpand(True)
        self.timeline_scroll.set_vexpand(False)
        self.timeline_scroll.set_min_content_height(Timeline._HEIGHT)
        self.timeline_scroll.set_child(self.timeline)
        self.timeline_scroll.connect("notify::width", lambda *_: self.timeline._sync_canvas())
        body.append(self.timeline_scroll)
        return frame

    def _build_media_pane(self) -> Gtk.Widget:
        self.btn_open_video = self._hint("+", "video", lambda: self._pick("video"))
        self.btn_open_audio = self._hint("+", "audio", lambda: self._pick("audio"))
        frame, body = self._pane(
            "media", self.btn_open_video, self.btn_open_audio,
            self._dim("· drag onto a track"),
        )
        row = Gtk.Box(spacing=10)
        self.media_list = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        media_scroll = Gtk.ScrolledWindow()
        media_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        media_scroll.set_hexpand(True)
        media_scroll.set_propagate_natural_height(True)
        media_scroll.set_child(self.media_list)
        row.append(media_scroll)
        self._install_drop(self.media_list)

        music = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        music.set_valign(Gtk.Align.CENTER)
        self.audio_label = Gtk.Label(label="no music", xalign=0)
        self.audio_label.add_css_class("dim")
        self.audio_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.audio_label.set_max_width_chars(28)
        music.append(self.audio_label)
        buttons = Gtk.Box()
        self.btn_fit = Gtk.Button(label="fit")
        self.btn_fit.add_css_class("hint")
        self.btn_fit.set_sensitive(False)
        self.btn_fit.set_tooltip_text("Cut the music to the video length")
        self.btn_fit.connect("clicked", self._on_fit)
        buttons.append(self.btn_fit)
        self.btn_clear_audio = Gtk.Button(label="clear")
        self.btn_clear_audio.add_css_class("hint")
        self.btn_clear_audio.set_sensitive(False)
        self.btn_clear_audio.connect("clicked", self._on_clear_audio)
        buttons.append(self.btn_clear_audio)
        self.follow_in = Gtk.CheckButton(label="follow in-point")
        self.follow_in.set_tooltip_text("Start the music at the video's in-point")
        self.follow_in.connect("toggled", self._on_follow_in)
        buttons.append(self.follow_in)
        music.append(buttons)
        row.append(music)
        body.append(row)
        return frame

    def _build_statusline(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.command_revealer = Gtk.Revealer()
        self.command_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        command_box = Gtk.Box(spacing=6)
        command_box.add_css_class("commandline")
        colon = Gtk.Label(label=":")
        colon.add_css_class("key")
        command_box.append(colon)
        self.command_entry = Gtk.Entry()
        self.command_entry.set_hexpand(True)
        self.command_entry.set_placeholder_text("r916  r43  rp")
        self.command_entry.connect("activate", self._on_command_activate)
        command_keys = Gtk.EventControllerKey()
        command_keys.connect("key-pressed", self._on_command_key_pressed)
        self.command_entry.add_controller(command_keys)
        command_box.append(self.command_entry)
        self.command_revealer.set_child(command_box)
        outer.append(self.command_revealer)

        line = Gtk.Box()
        line.add_css_class("statusline")
        self.mode_label = Gtk.Label(label="NORMAL")
        self.mode_label.add_css_class("mode")
        line.append(self.mode_label)
        self.keyboard_hint = Gtk.Label(xalign=0)
        self.keyboard_hint.add_css_class("seg")
        self.keyboard_hint.set_ellipsize(Pango.EllipsizeMode.END)
        self.keyboard_hint.set_visible(False)
        line.append(self.keyboard_hint)
        self.status = Gtk.Label(xalign=0)
        self.status.add_css_class("seg")
        self.status.set_hexpand(True)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        line.append(self.status)
        self.progress = Gtk.ProgressBar()
        self.progress.add_css_class("thin")
        self.progress.set_valign(Gtk.Align.CENTER)
        self.progress.set_size_request(80, -1)
        line.append(self.progress)
        self.export_name = Gtk.Label(label="name is assigned on export", xalign=0)
        self.export_name.add_css_class("dim")
        self.export_name.set_ellipsize(Pango.EllipsizeMode.START)
        self.export_name.set_max_width_chars(36)
        line.append(self.export_name)
        line.append(self.btn_export)
        menu = Gio.Menu()
        file_menu = Gio.Menu()
        file_menu.append("New", "win.new-project")
        file_menu.append("Open project…", "win.open-project")
        file_menu.append("Save", "win.save")
        file_menu.append("Save As…", "win.save-as")
        edit_menu = Gio.Menu()
        edit_menu.append("Undo", "win.undo")
        edit_menu.append("Redo", "win.redo")
        menu.append_section(None, file_menu)
        menu.append_section(None, edit_menu)
        mb = Gtk.MenuButton(label="project")
        mb.add_css_class("hint")
        mb.set_menu_model(menu)
        mb.set_tooltip_text("New, open, save (Ctrl+N / Ctrl+O / Ctrl+S)")
        line.append(mb)
        self.btn_keyboard_help = self._hint(
            "?", "help", self._toggle_keyboard_help,
            tooltip="Keyboard shortcuts (? with the timeline focused)",
        )
        # Clicking the reference must not leave the active keyboard edit mode.
        self.btn_keyboard_help.set_focusable(False)
        self.btn_keyboard_help.set_focus_on_click(False)
        line.append(self.btn_keyboard_help)
        outer.append(line)
        return outer

    # ── Popovers: exact values ───────────────────────────────────────────

    def _popover_grid(self) -> tuple[Gtk.Popover, Gtk.Grid]:
        pop = Gtk.Popover()
        pop.add_css_class("clip-pop")
        pop.set_has_arrow(False)
        pop.set_autohide(True)
        pop.set_parent(self.timeline)
        pop.connect("closed", self._on_popover_closed)
        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        pop.set_child(grid)
        return pop, grid

    def _on_popover_closed(self, _popover) -> None:
        if not any(p.get_visible() for p in (self.clip_popover, self.transition_popover)):
            self.timeline.grab_focus()

    def _build_popovers(self) -> None:
        self.clip_popover, grid = self._popover_grid()
        rows = iter(range(1000))

        def field(label: str | Gtk.Widget, *widgets: Gtk.Widget) -> None:
            row = next(rows)
            if isinstance(label, str):
                label = self._dim(label)
            grid.attach(label, 0, row, 1, 1)
            box = Gtk.Box(spacing=6)
            for w in widgets:
                box.append(w)
            grid.attach(box, 1, row, 1, 1)

        def note(widget: Gtk.Widget) -> None:
            grid.attach(widget, 0, next(rows), 2, 1)

        def wrap_dim(text: str = "") -> Gtk.Label:
            lab = self._wrapping_label(text)
            lab.add_css_class("dim")
            lab.set_selectable(False)
            return lab

        self.video_label = self._wrapping_label("no clip selected")
        self.video_label.add_css_class("pane-name")
        self.video_label.set_selectable(False)
        self.clip_title = self._wrapping_label("no clip selected")
        self.clip_title.add_css_class("pane-name")
        self.clip_title.set_selectable(False)
        note(self.clip_title)

        self.in_spin = Gtk.SpinButton.new_with_range(0, 99999, 0.05)
        self.in_spin.set_digits(2)
        self.in_spin.connect("value-changed", self._on_trim_spin_changed)
        self.out_spin = Gtk.SpinButton.new_with_range(0, 99999, 0.05)
        self.out_spin.set_digits(2)
        self.out_spin.connect("value-changed", self._on_trim_spin_changed)
        self.clip_in_spin = Gtk.SpinButton.new_with_range(0, 99999, 0.05)
        self.clip_out_spin = Gtk.SpinButton.new_with_range(0, 99999, 0.05)
        for spin in (self.clip_in_spin, self.clip_out_spin):
            spin.set_digits(2)
            spin.connect("value-changed", self._on_clip_trim_changed)
        self.clip_set_in = self._hint("", "← playhead", lambda: self._set_clip_bound(True),
                                      tooltip="Set this clip's in-point to the playhead")
        self.clip_set_out = self._hint("", "← playhead", lambda: self._set_clip_bound(False),
                                       tooltip="Set this clip's out-point to the playhead")
        field("in", self.clip_in_spin, self.clip_set_in)
        field("out", self.clip_out_spin, self.clip_set_out)

        self.speed_spin = Gtk.SpinButton.new_with_range(MIN_SPEED, MAX_SPEED, 0.05)
        self.speed_spin.set_digits(2)
        self.speed_spin.set_value(DEFAULT_SPEED)
        self.speed_spin.set_tooltip_text(
            "Playback rate. Faster shortens the clip on the timeline; slower lengthens it."
        )
        self.speed_spin.connect("value-changed", self._on_speed_changed)
        self.btn_speed_reset = self._hint("", "1×", self._on_speed_reset,
                                          tooltip="Reset to normal speed")
        field("speed", self.speed_spin, self._dim("×"), self.btn_speed_reset)
        self.speed_hint = wrap_dim()
        note(self.speed_hint)

        self.transform_x_spin = Gtk.SpinButton.new_with_range(-4096, 4096, 1)
        self.transform_y_spin = Gtk.SpinButton.new_with_range(-4096, 4096, 1)
        self.transform_scale_spin = Gtk.SpinButton.new_with_range(0.05, 4.0, 0.05)
        self.transform_scale_spin.set_digits(2)
        self.transform_scale_spin.set_value(1.0)
        self.transform_scale_spin.set_tooltip_text(
            "1.00 fills the frame; smaller values reveal lower tracks. Drag a preview corner to resize."
        )
        for spin in (self.transform_x_spin, self.transform_y_spin, self.transform_scale_spin):
            spin.connect("value-changed", self._on_transform_changed)
        self.btn_transform_reset = self._hint("", "reset", self._on_transform_reset)
        field("x / y", self.transform_x_spin, self.transform_y_spin)
        field("scale", self.transform_scale_spin, self._dim("×"), self.btn_transform_reset)

        self.fade_in_check = Gtk.CheckButton(label="in")
        self.fade_in_check.connect("toggled", self._on_fade_changed)
        self.fade_in_spin = Gtk.SpinButton.new_with_range(MIN_FADE_S, MAX_FADE_S, 0.05)
        self.fade_in_spin.set_digits(2)
        self.fade_in_spin.set_value(DEFAULT_FADE_S)
        self.fade_in_spin.connect("value-changed", self._on_fade_changed)
        self.fade_out_check = Gtk.CheckButton(label="out")
        self.fade_out_check.connect("toggled", self._on_fade_changed)
        self.fade_out_spin = Gtk.SpinButton.new_with_range(MIN_FADE_S, MAX_FADE_S, 0.05)
        self.fade_out_spin.set_digits(2)
        self.fade_out_spin.set_value(DEFAULT_FADE_S)
        self.fade_out_spin.connect("value-changed", self._on_fade_changed)
        field("fade", self.fade_in_check, self.fade_in_spin)
        field("", self.fade_out_check, self.fade_out_spin)
        self.fade_hint = wrap_dim()
        note(self.fade_hint)

        self.audio_volume_spins = {}
        for key, label in (("clip", "volume"), (1, "A1 track"), (2, "A2 track")):
            spin = Gtk.SpinButton.new_with_range(0, 200, 1)
            spin.set_value(100)
            spin.set_tooltip_text("0 mutes, 100 is the original level. Clip and track volumes multiply.")
            spin.connect("value-changed", self._on_audio_volume_changed, key)
            self.audio_volume_spins[key] = spin
            field(label, spin, self._dim("%"))
        note(self._dim("esc closes"))

        self.transition_popover, tgrid = self._popover_grid()
        self.transition_type = Gtk.DropDown.new_from_strings(["None", "Dissolve", "White flash"])
        self.transition_type.connect("notify::selected", self._on_transition_changed)
        self.transition_spin = Gtk.SpinButton.new_with_range(0.1, 3.0, 0.05)
        self.transition_spin.set_digits(2)
        self.transition_spin.set_value(0.5)
        self.transition_spin.connect("value-changed", self._on_transition_changed)
        heading = Gtk.Label(label="transition to next clip", xalign=0)
        heading.add_css_class("pane-name")
        tgrid.attach(heading, 0, 0, 2, 1)
        tgrid.attach(self._dim("type"), 0, 1, 1, 1)
        tgrid.attach(self.transition_type, 1, 1, 1, 1)
        tgrid.attach(self._dim("length"), 0, 2, 1, 1)
        length = Gtk.Box(spacing=6)
        length.append(self.transition_spin)
        length.append(self._dim("s"))
        tgrid.attach(length, 1, 2, 1, 1)
        self.transition_hint = wrap_dim()
        tgrid.attach(self.transition_hint, 0, 3, 2, 1)

    def _trim_target(self) -> tuple[str, int, ClipInst | None]:
        kind = self._selected_clip_kind()
        index = self.sel_v if kind == "video" else self.sel_a
        clips = self.video_clips if kind == "video" else self.audio_clips
        if kind and 0 <= index < len(clips):
            return kind, index, clips[index]
        return kind, index, None

    def _sync_clip_trim_controls(self) -> None:
        if not hasattr(self, "clip_in_spin"):
            return
        kind, index, clip = self._trim_target()
        linked = kind == "audio" and self.timeline.audio_kind == "source"
        if linked and 0 <= index < len(self.timeline.aclips):
            clip = self.timeline.aclips[index]
        loading = self._loading
        self._loading = True
        try:
            enabled = clip is not None and not linked and not self._busy_rendering() and not self._editing_locked()
            duration = self._media_dur(clip.media_id) if clip is not None else 0.0
            self.clip_in_spin.set_value(clip.in_s if clip else 0)
            self.clip_out_spin.set_value((clip.out_s or duration) if clip else 0)
            item = self._clip_item(clip, kind) if clip is not None else None
            self.clip_title.set_text(item.path.name if item else "no clip selected")
            hint = "Linked soundtrack: trim the video clip, or detach audio using a timeline edit." if linked else None
            for control in (self.clip_in_spin, self.clip_out_spin, self.clip_set_in, self.clip_set_out):
                control.set_sensitive(enabled)
                control.set_tooltip_text(hint)
        finally:
            self._loading = loading

    def _on_clip_trim_changed(self, spin) -> None:
        if self._loading:
            return
        kind, index, clip = self._trim_target()
        if clip is None or self._busy_rendering() or not self._guard_edit("trim"):
            self._sync_clip_trim_controls()
            return
        duration = self._media_dur(clip.media_id) or max(clip.out_s, clip.in_s + .05)
        if duration < .05:
            return
        inn = min(self.clip_in_spin.get_value(), duration - .05)
        out = min(duration, max(inn + .05, self.clip_out_spin.get_value()))
        if spin is self.clip_out_spin and out <= clip.in_s:
            out = min(duration, clip.in_s + .05)
            inn = clip.in_s
        self._flush_checkpoint()
        self._checkpoint()
        self._stop()
        if kind == "audio":
            self._on_audio_trim(index, inn, out, True)
        else:
            self._on_video_trim(index, inn, out, True)
        self._sync_clip_trim_controls()

    def _set_clip_bound(self, is_in: bool) -> None:
        kind, _index, clip = self._trim_target()
        if clip is None or self._busy_rendering() or not self._guard_edit("trim"):
            return
        source_time = self._source_time(clip, self._timeline_now(), self._media_dur(clip.media_id))
        (self.clip_in_spin if is_in else self.clip_out_spin).set_value(source_time)

    def _open_clip_popover(self, kind: str | None = None, index: int | None = None) -> None:
        kind = kind or self._selected_clip_kind()
        if not kind:
            self._set_status("Select a clip first")
            return
        if index is None:
            index = self.sel_v if kind == "video" else self.sel_a
        rect = self.timeline.clip_rect(kind, index)
        if rect is None:
            return
        self._sync_clip_trim_controls()
        self.transition_popover.popdown()
        self.clip_popover.set_pointing_to(rect)
        self.clip_popover.set_position(Gtk.PositionType.TOP)
        self.clip_popover.popup()

    def _open_transition_popover(self, index: int) -> None:
        rect = self.timeline.cut_rect(index)
        if rect is None:
            return
        self.clip_popover.popdown()
        self.transition_popover.set_pointing_to(rect)
        self.transition_popover.set_position(Gtk.PositionType.TOP)
        self.transition_popover.popup()

    # ── Direct edits from the timeline and preview ───────────────────────

    def _on_timeline_activate(self, kind: str, index: int) -> None:
        self._open_clip_popover(kind, index)

    def _on_timeline_transition(self, index: int) -> None:
        self._open_transition_popover(index)

    def _on_timeline_fade(
        self, kind: str, index: int, fade_in: float, fade_out: float, final: bool
    ) -> None:
        if not self._guard_edit("fade"):
            return
        clips = self.video_clips if kind == "video" else self.audio_clips
        if not 0 <= index < len(clips):
            return
        clip = clips[index]
        src = float(self._src_durs().get(clip.media_id) or 0.0)
        clip.fade_in_s, clip.fade_out_s = clamp_clip_fades(
            normalize_fade_s(fade_in) if fade_in > 0 else 0.0,
            normalize_fade_s(fade_out) if fade_out > 0 else 0.0,
            clip.timeline_len(src),
        )
        self._sync_fade_controls()
        if final:
            self._sync_timeline_clips()
            self._schedule_autosave()
            self._checkpoint()
        else:
            self.timeline.queue_draw()

    def _on_timeline_volume(self, kind: str, index: int, volume: float, final: bool) -> None:
        if not self._guard_edit("audio_volume"):
            return
        clips = self.audio_clips if kind == "audio" else self.video_clips
        if not 0 <= index < len(clips):
            return
        clips[index].volume = normalize_volume(volume)
        if not final:
            self.timeline.queue_draw()
            return
        was_playing = self.playing
        playhead = self._timeline_now() if was_playing else self.timeline.playhead
        self._stop()
        self._sync_audio_volume_controls()
        self._sync_timeline_clips()
        self._checkpoint()
        self._schedule_autosave()
        if was_playing:
            self._begin_timeline_play(playhead)

    def _on_preview_scale(self, steps: float) -> None:
        """Scroll on the preview scales the selected clip (0.05–4.00)."""
        if self._selected_video_clip() is None:
            return
        value = self.transform_scale_spin.get_value() * (1.05 ** -steps)
        self.transform_scale_spin.set_value(max(0.05, min(4.0, value)))

    def _on_safe_zones(self, btn: Gtk.ToggleButton) -> None:
        btn.text_label.set_text("safe zones on" if btn.get_active() else "safe zones off")
        self.preview.set_safe_zones(btn.get_active() and self.aspect in SAFE_ZONE_ASPECTS)

    def _sync_safe_zones(self) -> None:
        if not hasattr(self, "btn_safe_zones"):
            return
        supported = self.aspect in SAFE_ZONE_ASPECTS
        self.btn_safe_zones.set_sensitive(supported)
        self.preview.set_safe_zones(self.btn_safe_zones.get_active() and supported)

    @staticmethod
    def _visual_key(path: Path) -> tuple[str, int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return str(path), stat.st_mtime_ns, stat.st_size

    def _clear_visuals(self) -> None:
        self.timeline.filmstrips.clear()
        self.timeline.waves.clear()
        running = self.__dict__.get("_visual_running", set())
        pending = self.__dict__.get("_visual_pending", {})
        for key in list(pending):
            if key not in running:
                pending.pop(key)

    def _queue_visuals(self, mid: str) -> None:
        """Deduplicate file jobs and run at most two FFmpeg workers per window."""
        if self._closed:
            return
        item = self._media_by_id(mid)
        info = self.media_info.get(mid) or {}
        if item is None or (key := self._visual_key(item.path)) is None:
            return
        cache = self.__dict__.setdefault("_visual_cache", {})
        if key in cache:
            strip, wave = cache[key]
            if strip is not None:
                self.timeline.filmstrips[mid] = strip
            if wave is not None:
                self.timeline.waves[mid] = wave
            self.timeline.queue_draw()
            return
        pending = self.__dict__.setdefault("_visual_pending", {})
        pending.setdefault(key, (item.path, bool(info.get("has_video")),
                                 bool(info.get("has_audio")), float(info.get("duration") or 0)))
        self._start_visual_jobs()

    def _start_visual_jobs(self) -> None:
        running = self.__dict__.setdefault("_visual_running", set())
        pending = self.__dict__.setdefault("_visual_pending", {})
        if self._closed:
            return
        for key, task in list(pending.items()):
            if len(running) >= 2:
                break
            if key in running:
                continue
            running.add(key)
            threading.Thread(target=self._build_visuals, args=(key, task), daemon=True).start()

    def _build_visuals(self, key, task) -> None:
        path, want_strip, want_wave, duration = task
        strip = wave = None
        try:
            if want_strip:
                strip = _load_filmstrip(path, duration)
        except (OSError, ProbeError, subprocess.SubprocessError, GLib.Error):
            pass
        try:
            if want_wave:
                wave = _load_waveform(path)
        except (OSError, ProbeError, subprocess.SubprocessError, GLib.Error):
            pass
        GLib.idle_add(self._visuals_ready, key, strip, wave)

    def _visuals_ready(self, key, strip, wave) -> bool:
        self._visual_running.discard(key)
        self._visual_pending.pop(key, None)
        if self._closed:
            return False
        # A worker may finish after New/Open/Undo reused m1 for a different file.
        # Match current file identity, never the media id captured at submission.
        cache = self.__dict__.setdefault("_visual_cache", {})
        cache[key] = (strip, wave)
        while len(cache) > 16:
            cache.pop(next(iter(cache)))
        for item in self.media:
            if self._visual_key(item.path) != key:
                continue
            if strip is not None:
                self.timeline.filmstrips[item.id] = strip
            if wave is not None:
                self.timeline.waves[item.id] = wave
        self.timeline.queue_draw()
        self._start_visual_jobs()
        return False

    def _on_theme_change(self) -> None:
        """Cairo/snapshot drawing reads the palette at draw time; repaint it."""
        if self._closed:
            return
        self.timeline.queue_draw()
        self.preview.queue_draw()
        child = self.media_list.get_first_child()
        while child is not None:
            if isinstance(child, MediaCard):
                child._swatch.queue_draw()
            child = child.get_next_sibling()

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
        self.status.set_tooltip_text(text or None)

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
        if self._closed:
            return False
        self._closed = True
        off_theme_change(self._on_theme_change)
        self._clear_visuals()
        self.__dict__.get("_visual_cache", {}).clear()
        application = self.get_application()
        if application is not None and self._shutdown_handler:
            application.disconnect(self._shutdown_handler)
            self._shutdown_handler = 0
        self._abandon_preview_render()
        self._stop()
        self._reset_compiled_preview_flags()
        if self._ckpt_src:
            GLib.source_remove(self._ckpt_src)
            self._ckpt_src = 0
        self._flush_autosave()
        self._dispose_media()
        # Popovers are parented to the timeline, which does not unparent them.
        for name in ("clip_popover", "transition_popover", "keyboard_help"):
            pop = getattr(self, name, None)
            if pop is not None and pop.get_parent() is not None:
                pop.unparent()
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
        self.preview.selected_clip = clip
        info = (self.media_info.get(clip.media_id) or self.video_info or {}) if clip else {}
        self.preview.selected_source_size = (int(info.get("width") or 0), int(info.get("height") or 0))
        self.preview.queue_draw()
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
        clip.scale = max(0.05, self.transform_scale_spin.get_value())
        self._stop()
        self._refresh_cache_bar()
        self._apply_timeline_frame(self.timeline.playhead, start_media=False, edit_clip=clip)
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

    def _volume_target_clips(self) -> list[ClipInst]:
        if self.sel_kind == "audio" and not self.audio_clips and self.use_video_soundtrack:
            return [self.video_clips[i] for i in sorted(self.sel_as or {self.sel_a})
                    if 0 <= i < len(self.video_clips)]
        return self._speed_target_clips()

    def _sync_audio_volume_controls(self) -> None:
        self._sync_clip_trim_controls()
        if not hasattr(self, "audio_volume_spins"):
            return
        loading = self._loading
        self._loading = True
        try:
            clips = self._volume_target_clips()
            locked = self._busy_rendering() or self._editing_locked()
            self.audio_volume_spins["clip"].set_value(clips[0].volume * 100 if clips else 100)
            self.audio_volume_spins["clip"].set_sensitive(bool(clips) and not locked)
            for track in (1, 2):
                self.audio_volume_spins[track].set_value(self.audio_track_volumes[track] * 100)
                self.audio_volume_spins[track].set_sensitive(not locked)
        finally:
            self._loading = loading

    def _on_audio_volume_changed(self, spin, key) -> None:
        if self._loading:
            return
        if self._busy_rendering() or not self._guard_edit("audio_volume"):
            self._sync_audio_volume_controls()
            return
        volume = normalize_volume(spin.get_value() / 100)
        if key == "clip":
            for clip in self._volume_target_clips():
                clip.volume = volume
        else:
            self.audio_track_volumes[key] = volume
        was_playing = self.playing
        playhead = self._timeline_now() if was_playing else self.timeline.playhead
        self._stop()
        self._sync_timeline_clips()
        self._checkpoint()
        self._schedule_autosave()
        if was_playing:
            self._begin_timeline_play(playhead)

    def _render_clips(self, kind: str) -> list[ClipInst]:
        # Bake gains into copies at the rendering/cache boundary, never into
        # editable clips. The implicit video soundtrack uses A1's gain.
        clips = self.audio_clips if kind == "audio" else self.video_clips
        result = [c.copy() for c in clips]
        for clip in result:
            track = clip.track if kind == "audio" else 1
            clip.volume *= self.audio_track_volumes[track]
        return result

    def _preserve_video_soundtrack(self) -> None:
        if self.audio_clips or not self.use_video_soundtrack:
            return
        for source in sorted(self.video_clips, key=lambda c: c.track):
            item = self._clip_item(source, "video")
            if item is None or not (self.media_info.get(item.id) or {}).get("has_audio"):
                continue
            audio = self._media_for_path(item.path, "audio")
            if audio is None:
                audio = MediaItem(next_media_id(self.media), item.path, "audio")
                self.media.append(audio)
                self.media_info[audio.id] = dict(self.media_info[item.id])
            clip = source.copy()
            clip.media_id = audio.id
            clip.track = 1
            self.audio_clips.append(clip)

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
            self._sync_audio_volume_controls()
            self._sync_fade_controls()
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
        self._sync_audio_volume_controls()
        self._sync_fade_controls()
        self._sync_compiled_preview_controls()
        self._mark_compiled_stale_if_needed()
        self._refresh_media()

    def _refresh_cache_bar(self) -> None:
        if not self.video_clips or not self.video_path:
            self._cache_segments = []
            self.timeline.set_cache_spans([])
            return
        self._cache_segments = build_timeline_segments(
            video_clips=self._render_clips("video"),
            audio_clips=self._render_clips("audio"),
            src_durs=self._src_durs(),
            aspect=self.aspect,
            resolution=self.resolution,
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
            lab = Gtk.Label(
                label="Drop video or audio here, or press Shift+E in Eagle Browse.", xalign=0
            )
            lab.set_valign(Gtk.Align.CENTER)
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
            self.btn_fit.set_label("fit")
            self._schedule_autosave()
            self._schedule_checkpoint()
            return
        name = self.audio_path.name
        dur = float(self.audio_info["duration"]) if self.audio_info else 0.0
        if self.audio_fit and longer:
            self.btn_fit.set_label(f"fit {v:.2f}s")
            self.audio_label.set_text(f"{name} · {dur:.2f}s cut to {v:.2f}s")
        else:
            self.btn_fit.set_label("fit")
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
            self._sync_audio_volume_controls()
            self._sync_fade_controls()
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

    def _selected_fade_clips(self) -> list[ClipInst]:
        """Clips that fade controls edit: multi video selection, else primary video or audio."""
        v_indices = self._selected_video_indices()
        if v_indices:
            return [self.video_clips[i] for i in v_indices]
        if 0 <= self.sel_a < len(self.audio_clips):
            return [self.audio_clips[self.sel_a]]
        return []

    def _sync_fade_controls(self) -> None:
        clips = self._selected_fade_clips()
        clip = clips[0] if clips else None
        enabled = bool(clips) and not self.exporting and not self._editing_locked()
        was_loading = self._loading
        self._loading = True
        try:
            if clip is None:
                self.fade_in_check.set_active(False)
                self.fade_out_check.set_active(False)
                self.fade_in_spin.set_value(DEFAULT_FADE_S)
                self.fade_out_spin.set_value(DEFAULT_FADE_S)
                self.fade_hint.set_text("Select a clip")
            else:
                fi = normalize_fade_s(clip.fade_in_s)
                fo = normalize_fade_s(clip.fade_out_s)
                self.fade_in_check.set_active(fi > 0.0)
                self.fade_out_check.set_active(fo > 0.0)
                self.fade_in_spin.set_value(fi if fi > 0.0 else DEFAULT_FADE_S)
                self.fade_out_spin.set_value(fo if fo > 0.0 else DEFAULT_FADE_S)
                durs = self._src_durs()
                src = float(durs.get(clip.media_id) or 0.0)
                dur = clip.timeline_len(src)
                efi, efo = clamp_clip_fades(fi, fo, dur)
                if self._editing_locked():
                    self.fade_hint.set_text("Rendered preview — editing locked")
                elif len(clips) > 1:
                    self.fade_hint.set_text(f"{len(clips)} clips selected")
                elif (fi > 0 and abs(efi - fi) > 0.01) or (fo > 0 and abs(efo - fo) > 0.01):
                    parts = []
                    if efi > 0:
                        parts.append(f"in {efi:.2f}s")
                    if efo > 0:
                        parts.append(f"out {efo:.2f}s")
                    self.fade_hint.set_text(
                        "Clamped to " + " / ".join(parts) if parts else "Clamped off (clip too short)"
                    )
                else:
                    self.fade_hint.set_text("")
            self.fade_in_check.set_sensitive(enabled)
            self.fade_out_check.set_sensitive(enabled)
            self.fade_in_spin.set_sensitive(enabled and self.fade_in_check.get_active())
            self.fade_out_spin.set_sensitive(enabled and self.fade_out_check.get_active())
        finally:
            self._loading = was_loading

    def _on_fade_changed(self, *_args: object) -> None:
        if self._loading:
            return
        if not self._guard_edit("fade"):
            self._sync_audio_volume_controls()
            self._sync_fade_controls()
            return
        clips = self._selected_fade_clips()
        if not clips:
            return
        want_in = bool(self.fade_in_check.get_active())
        want_out = bool(self.fade_out_check.get_active())
        raw_in = float(self.fade_in_spin.get_value()) if want_in else 0.0
        raw_out = float(self.fade_out_spin.get_value()) if want_out else 0.0
        if want_in and raw_in <= 0.0:
            raw_in = DEFAULT_FADE_S
        if want_out and raw_out <= 0.0:
            raw_out = DEFAULT_FADE_S
        durs = self._src_durs()
        for clip in clips:
            src = float(durs.get(clip.media_id) or 0.0)
            dur = clip.timeline_len(src)
            fi, fo = clamp_clip_fades(
                normalize_fade_s(raw_in) if want_in else 0.0,
                normalize_fade_s(raw_out) if want_out else 0.0,
                dur,
            )
            clip.fade_in_s = fi
            clip.fade_out_s = fo
        was_loading = self._loading
        self._loading = True
        try:
            self.fade_in_spin.set_sensitive(want_in and not self.exporting)
            self.fade_out_spin.set_sensitive(want_out and not self.exporting)
            # Reflect clamps back into the spins when a single clip is selected.
            if len(clips) == 1:
                clip = clips[0]
                if clip.fade_in_s > 0:
                    self.fade_in_spin.set_value(clip.fade_in_s)
                if clip.fade_out_s > 0:
                    self.fade_out_spin.set_value(clip.fade_out_s)
                self.fade_in_check.set_active(clip.fade_in_s > 0)
                self.fade_out_check.set_active(clip.fade_out_s > 0)
        finally:
            self._loading = was_loading
        self._sync_audio_volume_controls()
        self._sync_fade_controls()
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
            self._queue_visuals(mid)
        else:
            mid = next_media_id(self.media)
            self.media.append(MediaItem(id=mid, path=path, kind=kind))
            self.media_info[mid] = info
            self._queue_visuals(mid)
            if kind == "video":
                try:
                    self.media_thumbs[mid] = _load_frame(path)
                except (ProbeError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                    pass
        if place is None:
            place = not self.video_clips if kind == "video" else True
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
        self._sync_safe_zones()
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
        clip = self._video_at(self.timeline.playhead)
        if clip is not None:
            self.preview.set_transform(
                clip.transform_x, clip.transform_y, clip.scale, w, h
            )
        self._refresh_crop()
        self._refresh_cache_bar()
        self._apply_timeline_frame(self.timeline.playhead, start_media=False)
        self._checkpoint()

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
        self._sync_audio_volume_controls()
        self._sync_fade_controls()
        clip = self._selected_video_clip()
        if clip is not None and not self.playing:
            self._apply_timeline_frame(self.timeline.playhead, start_media=False, edit_clip=clip)

    def _place_clip(self, kind: str, t: float, media_id: str = "", track: int | None = None) -> None:
        if not self._guard_edit("media_place"):
            return
        t = max(0.0, float(t))
        if kind == "audio":
            self._preserve_video_soundtrack()
            if track is None:
                used = {c.track for c in self.audio_clips}
                track = next((n for n in (1, 2) if n not in used), None)
                if track is None:
                    self._refresh_media()
                    self._checkpoint()
                    self._schedule_autosave()
                    self._set_status("Both audio layers are occupied. Added to media; drag it to the desired lane and time.")
                    return
        track = max(1, min(2, int(track or 1)))
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
            self.sel_as = {self.sel_a}
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
        self._exit_keyboard_mode()
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
        self._exit_keyboard_mode()
        if self.exporting:
            return False
        if not self._guard_edit("split"):
            return True
        kind, originals, idx, selected = self._keyboard_target()
        t = self._timeline_now()
        if not kind:
            self._set_status("Select a clip on the active track to split")
            return False
        clips = [c.copy() for c in originals]
        duration = self._media_dur(clips[idx].media_id) or clips[idx].out_s
        right = clips[idx].split_at(t, duration)
        if right is None:
            self._set_status("Playhead must be inside the selected clip, away from its edges")
            return False
        clips.insert(idx + 1, right)
        remapped = {(i + 1 if i > idx else i) for i in selected if i != idx}
        remapped.add(idx + 1)
        was_playing = self.playing
        self._apply_keyboard_clips(kind, clips, primary=idx + 1, selected=remapped)
        self._set_status(f"Split {kind} at {t:.3f}s")
        self.clock.set_text(f"{t:.2f} / {self._program_end():.2f}")
        if was_playing:
            self._on_play()
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

    def _preview_audio_specs(self, timeline_t: float) -> list[tuple[Path, float, float, float]]:
        """Active (path, source start, remaining, effective gain) entries."""
        if self.audio_clips:
            specs: list[tuple[Path, float, float, float]] = []
            for ac in self._audio_tracks_at(timeline_t):
                if ac is None:
                    continue
                item = self._clip_item(ac, "audio")
                if item is None:
                    continue
                adur = self._media_dur(item.id)
                start = self._source_time(ac, timeline_t, adur)
                _t0, end = self._clip_span(ac, adur)
                specs.append((item.path, start, max(0.05, end - timeline_t),
                              ac.volume * self.audio_track_volumes[ac.track]))
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
        return [(path, start, remaining, vc.volume * self.audio_track_volumes[1])]

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
        if abs(prev.volume - nxt.volume) > 0.0001:
            return False
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

    def _videos_at(self, t: float) -> list[ClipInst]:
        visible: dict[int, ClipInst] = {}
        for clip in self.video_clips:
            dur = self._media_dur(clip.media_id) or float((self.video_info or {}).get("duration") or 0)
            t0, t1 = self._clip_span(clip, dur)
            if t0 <= t < t1:
                visible[int(clip.track)] = clip
        return [visible[track] for track in sorted(visible)]

    def _video_at(self, t: float) -> ClipInst | None:
        clips = self._videos_at(t)
        return clips[-1] if clips else None

    def _clear_preview_layers(self) -> None:
        self.preview.layers = []
        self._raw_layer_ids = ()
        for media, invalidate, prepared in self._layer_media.values():
            media.disconnect(invalidate)
            if prepared:
                media.disconnect(prepared)
            media.pause()
            media.clear()
        self._layer_media.clear()

    def _sync_preview_layers(self, t: float, *, start_media: bool) -> None:
        clips = self._videos_at(t)
        wanted = {id(clip) for clip in clips[:-1]}
        for key in set(self._layer_media) - wanted:
            media, invalidate, prepared = self._layer_media.pop(key)
            media.disconnect(invalidate)
            if prepared:
                media.disconnect(prepared)
            media.pause()
            media.clear()
        layers = []
        for clip in clips:
            info = self.media_info.get(clip.media_id) or self.video_info or {}
            iw, ih = int(info.get("width") or 0), int(info.get("height") or 0)
            if min(iw, ih) <= 0:
                continue
            if clip is clips[-1]:
                media = self._vmedia
            else:
                key = id(clip)
                item = self._clip_item(clip, "video")
                if item is None:
                    continue
                if key not in self._layer_media:
                    media = Gtk.MediaFile.new_for_filename(str(item.path))
                    media.set_loop(False)
                    media.set_muted(True)
                    invalidate = media.connect("invalidate-contents", lambda *_: self.preview.queue_draw())
                    self._layer_media[key] = (media, invalidate, 0)
                media, invalidate, prepared = self._layer_media[key]
                if prepared:
                    media.disconnect(prepared)
                source = self._source_time(clip, t, float(info.get("duration") or 0))
                def seek(*_args, player=media, offset=source, play=start_media):
                    if self._closed or not player.is_prepared():
                        return
                    player.seek(int(offset * 1_000_000))
                    player.play() if play and self.playing else player.pause()
                prepared = 0
                if media.is_prepared():
                    seek()
                else:
                    prepared = media.connect("notify::prepared", seek)
                self._layer_media[key] = (media, invalidate, prepared)
            if media is not None:
                layers.append((clip, media, iw, ih))
        self.preview.layers = layers
        self._raw_layer_ids = tuple(id(clip) for clip in clips)
        self.preview.queue_draw()

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
        self._clear_preview_layers()
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

    def _apply_timeline_frame(
        self, timeline_t: float, *, start_media: bool, edit_clip: ClipInst | None = None
    ) -> None:
        # Seek inside the selection before transforming, including at a cut.
        # A baked frame cannot show live changes to individual layers.
        if edit_clip is not None:
            duration = self._media_dur(edit_clip.media_id) or float((self.video_info or {}).get("duration") or 0)
            t0, t1 = self._clip_span(edit_clip, duration)
            if not t0 <= timeline_t < t1:
                timeline_t = max(0, t0)
                self.timeline.set_playhead(timeline_t)
        if edit_clip is None and not self._compiled_mode and self._cached_playback_available(timeline_t):
            if self._show_playthrough(timeline_t, start_media=start_media):
                return
        self._playthrough_playing = False
        clip = self._video_at(timeline_t)
        if clip is None or self._vmedia is None:
            self._clear_preview_layers()
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
        self._sync_preview_layers(timeline_t, start_media=start_media)
        if start_media:
            self._play_media_at(source)
            self._clip_playing = True
        else:
            self._play_media_at(source, start_media=False)
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
            specs: list[tuple[Path, float, float, float]] = [
                (self._playthrough_path, start, remaining, 1.0)
            ]
        else:
            specs = self._preview_audio_specs(timeline_t)
        overlapping = len(specs) > 1
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
            for path, start, remaining, gain in specs:
                mix_cmd += ["-ss", f"{start:.3f}", "-t", f"{remaining:.3f}", "-i", str(path)]
            chains = [f"[{i}:a]volume={gain:.6f}[mix{i}]"
                      for i, (_path, _start, _remaining, gain) in enumerate(specs)]
            pads = "".join(f"[mix{i}]" for i in range(len(specs)))
            audio_filter = ";".join(chains + [
                pads + f"amix=inputs={len(specs)}:duration=longest:normalize=0[a]"
            ])
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
            path, start, remaining, preview_gain = specs[0]
            # mpv first: --hr-seek hits the playhead. ffplay -ss is keyframe-only.
            if shutil.which("mpv"):
                cmd = [
                    "mpv", "--no-video", "--force-window=no", "--no-terminal",
                    "--audio-display=no", "--no-resume-playback", "--hr-seek=always",
                    "--volume=100", f"--af=volume={preview_gain:.6f}",
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
        logf.close()
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
        self._clear_preview_layers()
        if self._vmedia is not None and self._prep_handler:
            try:
                self._vmedia.disconnect(self._prep_handler)
            except (TypeError, RuntimeError):
                pass
            self._prep_handler = 0
        media = self._vmedia
        self._vmedia = None
        self._vmedia_path = None
        self.preview.set_media(None)
        if media is not None:
            media.pause()
            # Pause leaves decoding/preroll workers alive. Close the source before
            # replacing it or allowing application/GObject teardown to begin.
            media.clear()

    def _load_media(self, path: Path) -> None:
        if self._vmedia is not None and _same_path(self._vmedia_path, path):
            return
        self._dispose_media()
        media = Gtk.MediaFile.new_for_filename(str(path))
        media.set_loop(False)
        self._vmedia = media
        self._vmedia_path = path

    def _play_media_at(self, t: float, *, start_media: bool = True) -> None:
        m = self._vmedia
        if m is None:
            return
        def go(*_a: object) -> bool:
            if self._closed or not m.is_prepared():
                return False
            # The preview is video-only; sound goes through ffplay/mpv.
            m.set_muted(True)
            m.set_volume(0.0)
            try:
                m.seek(int(max(0.0, t) * 1_000_000))
            except GLib.Error:
                pass
            m.play() if start_media and self.playing else m.pause()
            return False

        if self._prep_handler:
            try:
                m.disconnect(self._prep_handler)
            except (TypeError, RuntimeError):
                pass
        self._prep_handler = 0
        if m.is_prepared():
            go()
        else:
            self._prep_handler = m.connect("notify::prepared", go)
            if start_media:
                m.play()

    def _stop(self) -> None:
        for media, _invalidate, _prepared in self._layer_media.values():
            media.pause()
        self.playing = False
        self._clip_playing = False
        self._audio_pending = False
        self.btn_play.text_label.set_text("play")
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
            self.btn_play.text_label.set_text("pause")
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
        elif tuple(id(c) for c in self._videos_at(t)) != self._raw_layer_ids:
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
            resolution=self.resolution,
            pan_x=self.preview.pan_x,
            pan_y=self.preview.pan_y,
            audio_follows_in=self.follow_in.get_active(),
            use_video_soundtrack=self.use_video_soundtrack,
            audio_offset=0.0,
            video_clips=self._render_clips("video"),
            audio_clips=self._render_clips("audio"),
            media=self.media,
            kind=kind,
            window=window,
        )

    def _sync_compiled_preview_controls(self) -> None:
        self._sync_audio_volume_controls()
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
            self.btn_preview_cancel.set_visible(self._preview_rendering)
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
        self._sync_audio_volume_controls()
        self._sync_fade_controls()
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
        self._sync_audio_volume_controls()
        self._sync_fade_controls()
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
        resolution = self.resolution
        pan_x, pan_y = self.preview.pan_x, self.preview.pan_y
        follows = self.follow_in.get_active()
        use_soundtrack = self.use_video_soundtrack
        v_clips = self._render_clips("video")
        a_clips = self._render_clips("audio")
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
                    resolution=resolution,
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
        self.playing = True
        self._apply_timeline_frame(t, start_media=True)
        self._start_preview_audio(t)
        self.btn_play.text_label.set_text("pause")
        if self._tick is not None:
            GLib.source_remove(self._tick)
        self._tick = GLib.timeout_add(50, self._on_tick)

    def _on_compiled_preview(self, kind: str) -> None:
        if self._busy_rendering() or not self.video_path or not self.video_clips:
            return
        src_durs = self._src_durs()
        window: tuple[float, float] | None = None
        v_clips = self._render_clips("video")
        a_clips = self._render_clips("audio")
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
        resolution = self.resolution
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
                    resolution=resolution,
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
        v_clips = self._render_clips("video")
        a_clips = self._render_clips("audio")
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
        install_app_css()
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
