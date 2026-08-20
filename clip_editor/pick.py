"""Native GTK file picker as a subprocess so it has its own main loop.

Prints the chosen path to stdout. Exit 0 on pick, 1 on cancel, 2 on error.
"""

from __future__ import annotations

import sys


def _pick(kind: str) -> str | None:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, GLib, Gtk

    class Picker(Gtk.Application):
        def __init__(self) -> None:
            super().__init__(
                application_id="local.clipeditor.picker",
                flags=Gio.ApplicationFlags.NON_UNIQUE,
            )
            self.chosen: str | None = None
            self.kind = kind

        def do_activate(self) -> None:  # noqa: N802
            dialog = Gtk.FileDialog()
            dialog.set_title("Open video" if self.kind == "video" else "Open audio")
            filt = Gtk.FileFilter()
            if self.kind == "video":
                filt.set_name("Video")
                for pat in ("*.mp4", "*.mov", "*.webm", "*.mkv", "*.m4v"):
                    filt.add_pattern(pat)
            else:
                filt.set_name("Audio")
                for pat in (
                    "*.mp3",
                    "*.wav",
                    "*.m4a",
                    "*.aac",
                    "*.ogg",
                    "*.flac",
                    "*.opus",
                    "*.mp4",
                    "*.mov",
                ):
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
                    self.chosen = f.get_path()
                except GLib.Error:
                    self.chosen = None
                self.quit()

            dialog.open(None, None, done)

    app = Picker()
    app.run([])
    return app.chosen


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    kind = args[0] if args else "video"
    if kind not in ("video", "audio"):
        print(f"usage: python3 -m clip_editor.pick video|audio", file=sys.stderr)
        return 2
    try:
        path = _pick(kind)
    except Exception as exc:  # noqa: BLE001
        print(f"picker failed: {exc}", file=sys.stderr)
        return 2
    if not path:
        return 1
    sys.stdout.write(path + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
