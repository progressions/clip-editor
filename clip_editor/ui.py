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

from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from clip_editor.aspects import ASPECTS, cover_crop, dest_size
from clip_editor.eagle import apply_omarchy_theme, theme_rgb
from clip_editor.export import ExportError, default_out_path, run_export
from clip_editor.probe import ProbeError, probe, which_ffmpeg

APP_ID = "local.clip.Editor"


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


class CoverPreview(Gtk.DrawingArea):
    def __init__(self) -> None:
        super().__init__()
        self.pixbuf: GdkPixbuf.Pixbuf | None = None
        self.pan_x = 0.5
        self.pan_y = 0.5
        self.on_pan = None
        self._drag_pan = (0.5, 0.5)
        self.set_draw_func(self._draw)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_cursor_from_name("grab")
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", lambda *_: self.set_cursor_from_name("grab"))
        self.add_controller(drag)

    def set_pixbuf(self, pb: GdkPixbuf.Pixbuf | None) -> None:
        self.pixbuf = pb
        self.queue_draw()

    def _draw(self, _area: Gtk.DrawingArea, cr, width: int, height: int) -> None:  # noqa: ANN001
        cr.set_source_rgb(*theme_rgb("dark_background", (0.0, 0.0, 0.0)))
        cr.paint()
        pb = self.pixbuf
        if pb is None or width <= 0 or height <= 0:
            return
        pw, ph = pb.get_width(), pb.get_height()
        src_a = pw / ph
        dst_a = width / height
        if src_a > dst_a:
            scale = height / ph
            overflow = pw * scale - width
            x = -overflow * self.pan_x
            y = 0.0
        else:
            scale = width / pw
            overflow = ph * scale - height
            x = 0.0
            y = -overflow * self.pan_y
        cr.save()
        cr.translate(x, y)
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()
        cr.restore()

    def _overflow(self) -> tuple[float, float]:
        pb = self.pixbuf
        w, h = self.get_width(), self.get_height()
        if pb is None or w <= 0 or h <= 0:
            return 0.0, 0.0
        pw, ph = pb.get_width(), pb.get_height()
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
        self.exporting = False
        self.playing = False
        self._vmedia: Gtk.MediaFile | None = None
        self._preview_proc: subprocess.Popen[bytes] | None = None
        self._play_t0 = 0.0
        self._play_mono = 0.0
        self._syncing_scrub = False
        self._tick: int | None = None
        self._prep_handler: int = 0

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

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
        self.preview_stack = Gtk.Stack()
        self.preview_stack.set_hexpand(True)
        self.preview_stack.set_vexpand(True)
        self.preview = CoverPreview()
        self.preview.on_pan = self._refresh_crop
        self.live = Gtk.Picture()
        self.live.set_content_fit(Gtk.ContentFit.COVER)
        self.live.set_hexpand(True)
        self.live.set_vexpand(True)
        self.preview_stack.add_named(self.preview, "still")
        self.preview_stack.add_named(self.live, "live")
        self.aspect_frame.set_child(self.preview_stack)
        left.append(self.aspect_frame)

        self.crop_label = Gtk.Label(xalign=0)
        self.crop_label.add_css_class("dim-label")
        left.append(self.crop_label)

        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.btn_play = Gtk.Button(label="Play")
        self.btn_play.set_sensitive(False)
        self.btn_play.connect("clicked", self._on_play)
        transport.append(self.btn_play)
        self.clock = Gtk.Label(label="0.00 / 0.00")
        self.clock.add_css_class("dim-label")
        transport.append(self.clock)
        self.scrub = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
        self.scrub.set_hexpand(True)
        self.scrub.set_draw_value(False)
        self.scrub.set_sensitive(False)
        self.scrub.connect("change-value", self._on_scrub)
        transport.append(self.scrub)
        left.append(transport)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        right.set_size_request(320, -1)

        right.append(self._section("Video"))
        row = Gtk.Box(spacing=8)
        b = Gtk.Button(label="Open video")
        b.connect("clicked", lambda *_: self._pick("video"))
        row.append(b)
        right.append(row)
        self.video_label = Gtk.Label(label="none", xalign=0, wrap=True)
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
        self.audio_label = Gtk.Label(
            label="none — keeps the video’s audio if it has one",
            xalign=0,
            wrap=True,
        )
        right.append(self.audio_label)
        self.follow_in = Gtk.CheckButton(label="Audio follows video in-point")
        self.follow_in.connect("toggled", lambda *_: self._refresh_fit())
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
        self.export_name = Gtk.Label(
            label="name is assigned on export (.mp4)",
            xalign=0,
            wrap=True,
        )
        self.export_name.add_css_class("dim-label")
        right.append(self.export_name)
        self.btn_export = Gtk.Button(label="Export")
        self.btn_export.add_css_class("suggested-action")
        self.btn_export.set_sensitive(False)
        self.btn_export.connect("clicked", self._on_export)
        right.append(self.btn_export)
        self.progress = Gtk.ProgressBar()
        right.append(self.progress)
        self.status = Gtk.Label(xalign=0, wrap=True)
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
        self._install_drop(self.live)

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

    def _playhead(self) -> float:
        if self.playing and self._vmedia is not None and self._vmedia.is_prepared():
            ts = self._vmedia.get_timestamp()
            if ts >= 0:
                return ts / 1_000_000.0
        if self.playing:
            return self._play_t0 + (time.monotonic() - self._play_mono)
        return self.scrub.get_value()

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

    def _refresh_export_name(self) -> None:
        if not self.video_path:
            self.export_name.set_text("name is assigned on export (.mp4)")
            return
        path = default_out_path(self.video_path, self.aspect)
        self.export_name.set_text(f"{path.parent.name}/{path.name}")
        self.export_name.set_tooltip_text(str(path))

    def _refresh_fit(self) -> None:
        v = self._edit_dur()
        a = self._audio_usable()
        longer = bool(self.audio_path) and a > v + 0.05 and v > 0.04
        self.btn_fit.set_sensitive(bool(self.audio_path))
        if not self.audio_path:
            self.btn_fit.set_label("Fit")
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

    def _open_path(self, kind: str, path: Path) -> None:
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
            self.video_path = path
            self.video_info = info
            dur = float(info["duration"] or 0)
            self.in_spin.set_value(0)
            self.out_spin.set_value(dur)
            self.scrub.set_range(0, max(dur, 0.01))
            self.scrub.set_value(0)
            self.scrub.set_sensitive(True)
            self.btn_play.set_sensitive(True)
            self.btn_export.set_sensitive(True)
            self.video_label.set_text(
                f"{path.name}\n{info['width']}×{info['height']} · {dur:.2f}s"
            )
            try:
                self.preview.set_pixbuf(_load_frame(path))
            except (ProbeError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                self._set_status(f"opened {path.name}, no preview frame ({exc})")
            self.preview.pan_x = 0.5
            self.preview.pan_y = 0.5
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
            self.audio_fit = False
            self.btn_clear_audio.set_sensitive(True)
            self._set_status(f"Opened {path.name}")
            if self._audio_usable() > self._edit_dur() + 0.05:
                self._set_status(
                    f"Music is {self._audio_usable():.2f}s; video is {self._edit_dur():.2f}s. Fit cuts the extra."
                )
        self._refresh_fit()

    def _on_clear_audio(self, *_args: object) -> None:
        self.audio_path = None
        self.audio_info = None
        self.audio_fit = False
        self.btn_clear_audio.set_sensitive(False)
        self.audio_label.set_text("none — keeps the video’s audio if it has one")
        self._refresh_fit()
        self._set_status("Audio cleared")

    def _on_fit(self, *_args: object) -> None:
        if not self.audio_path:
            return
        v, a = self._edit_dur(), self._audio_usable()
        if a <= v + 0.05:
            self._set_status("Audio is already no longer than the video")
            return
        self.audio_fit = True
        self._refresh_fit()
        self._set_status(f"Cut audio to {v:.2f}s (from {a:.2f}s)")

    def _on_aspect(self, btn: Gtk.ToggleButton, name: str) -> None:
        if not btn.get_active():
            return
        self.aspect = name
        w, h = dest_size(name)
        self.aspect_frame.set_ratio(w / h)
        self._refresh_crop()

    def _set_in(self, *_args: object) -> None:
        self.in_spin.set_value(self._playhead())
        self._refresh_fit()

    def _set_out(self, *_args: object) -> None:
        self.out_spin.set_value(self._playhead())
        self._refresh_fit()

    def _on_scrub(self, _s: Gtk.Scale, _t: Gtk.ScrollType, value: float) -> bool:
        if self._syncing_scrub:
            return False
        dur = float((self.video_info or {}).get("duration") or 0)
        self.clock.set_text(f"{value:.2f} / {dur:.2f}")
        if self.playing:
            self._play_t0 = value
            self._play_mono = time.monotonic()
            if self._vmedia is not None:
                try:
                    self._vmedia.seek(int(max(0.0, value) * 1_000_000))
                except GLib.Error:
                    pass
            if self.audio_path:
                self._start_preview_audio(value)
        return False

    def _preview_cmd(self, at_s: float) -> list[str] | None:
        if not self.audio_path:
            return None
        remaining = max(0.05, self.out_spin.get_value() - at_s)
        path = self.audio_path
        start = self._audio_start() + (at_s - self.in_spin.get_value())
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

    def _start_preview_audio(self, at_s: float) -> None:
        self._stop_preview_audio()
        cmd = self._preview_cmd(at_s)
        if cmd is None:
            if self.audio_path:
                self._set_status("Need ffplay or mpv to hear preview audio")
            return
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
        self.live.set_paintable(None)

    def _load_media(self, path: Path) -> None:
        self._dispose_media()
        media = Gtk.MediaFile.new_for_filename(str(path))
        media.set_loop(False)
        self._vmedia = media
        self.live.set_paintable(media)

    def _play_media_at(self, t: float) -> None:
        m = self._vmedia
        if m is None:
            return
        mute = self.audio_path is not None

        def go(*_a: object) -> bool:
            m.set_muted(mute)
            m.set_volume(0.0 if mute else 1.0)
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
        self.btn_play.set_label("Play")
        self._stop_preview_audio()
        if self._vmedia is not None:
            self._vmedia.pause()
        self.preview_stack.set_visible_child_name("still")
        if self._tick is not None:
            GLib.source_remove(self._tick)
            self._tick = None

    def _on_play(self, *_args: object) -> None:
        if not self.video_path and not self.audio_path:
            return
        if self.playing:
            self._stop()
            return
        t = self._playhead()
        inn, out = self.in_spin.get_value(), self.out_spin.get_value()
        if t < inn - 0.02 or t >= out - 0.02:
            t = inn
        self._play_t0 = t
        self._play_mono = time.monotonic()
        self.preview_stack.set_visible_child_name("live")
        self._play_media_at(t)
        if self.audio_path:
            self._start_preview_audio(t)
        elif self.video_info and not self.video_info.get("has_audio"):
            self._set_status("Playing · this clip is silent — add an audio track")
        else:
            self._set_status("Playing")
        self.playing = True
        self.btn_play.set_label("Pause")
        self._tick = GLib.timeout_add(50, self._on_tick)

    def _on_tick(self) -> bool:
        t = self._playhead()
        out = self.out_spin.get_value()
        dur = float((self.video_info or {}).get("duration") or 0)
        self._syncing_scrub = True
        self.scrub.set_value(t)
        self._syncing_scrub = False
        self.clock.set_text(f"{t:.2f} / {dur:.2f}")
        if t >= out - 0.02:
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
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect("activate", self._activate)

    def _activate(self, _app: Adw.Application) -> None:
        apply_omarchy_theme()
        win = self.props.active_window
        if win is None:
            win = EditorWindow(application=self)
        win.present()


def run() -> int:
    Adw.init()
    apply_omarchy_theme()
    return int(EditorApp().run(["clip-editor"]))
