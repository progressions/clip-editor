"""CLI: export, serve, selftest."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from clip_editor import __version__
from clip_editor.aspects import ASPECTS, cover_crop, dest_size
from clip_editor.export import ExportError, default_out_path, run_export
from clip_editor.probe import ProbeError, which_ffmpeg, which_ffprobe


def _add_export_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--video", required=True, help="source video")
    p.add_argument("--audio", default=None, help="replacement audio (optional)")
    p.add_argument(
        "--aspect",
        default="9:16",
        choices=list(ASPECTS),
        help="export aspect (default 9:16)",
    )
    p.add_argument("--pan-x", type=float, default=0.5, help="0=left 1=right (default 0.5)")
    p.add_argument("--pan-y", type=float, default=0.5, help="0=top 1=bottom (default 0.5)")
    p.add_argument("--in", dest="in_s", type=float, default=0.0, help="in-point seconds")
    p.add_argument("--out", dest="out_s", type=float, default=None, help="out-point seconds")
    p.add_argument(
        "--out-file",
        dest="out_file",
        default=None,
        help="output mp4 (default: <video>_<aspect>.mp4)",
    )
    p.add_argument(
        "--audio-follows-in",
        action="store_true",
        help="start replacement audio at the video in-point (driver sync)",
    )
    p.add_argument(
        "--audio-offset",
        type=float,
        default=0.0,
        help="seconds into the audio file to start",
    )
    p.add_argument("--json", action="store_true", help="print result as JSON")


def cmd_export(args: argparse.Namespace) -> int:
    video = Path(args.video)
    out = Path(args.out_file) if args.out_file else default_out_path(video, args.aspect)
    try:
        result = run_export(
            video,
            out,
            audio=Path(args.audio) if args.audio else None,
            aspect=args.aspect,
            pan_x=args.pan_x,
            pan_y=args.pan_y,
            in_s=args.in_s,
            out_s=args.out_s,
            audio_follows_in=args.audio_follows_in,
            audio_offset=args.audio_offset,
            progress=None
            if args.json
            else (lambda pct, st: print(f"\r{st} {pct*100:5.1f}%", end="", file=sys.stderr, flush=True)),
        )
    except (ExportError, ProbeError) as exc:
        if not args.json:
            print(file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1
    if not args.json:
        print(file=sys.stderr)
        g = result["gate"]
        print(f"wrote  {result['out']}")
        print(
            f"gate   {g['vcodec']} {g['width']}x{g['height']} "
            f"audio={g.get('acodec') or 'none'} {g['duration']:.3f}s"
        )
        c = result["meta"]["crop"]
        print(f"crop   {c['w']}x{c['h']}+{c['x']}+{c['y']}")
        return 0
    printable = {
        "ok": True,
        "out": result["out"],
        "meta": result["meta"],
        "gate": {
            k: result["gate"][k]
            for k in (
                "width",
                "height",
                "duration",
                "vcodec",
                "acodec",
                "gate_ok",
            )
        },
    }
    print(json.dumps(printable, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from clip_editor.server import serve

    serve(host=args.host, port=args.port, open_browser=args.open)
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    which_ffmpeg()
    which_ffprobe()
    # Crop math: 1024² → 9:16 is 576×1024 centered (x=224).
    r = cover_crop(1024, 1024, 1080, 1920, 0.5, 0.5)
    assert r.w == 576 and r.h == 1024, r
    assert r.x == 224 and r.y == 0, r
    r_left = cover_crop(1024, 1024, 1080, 1920, 0.0, 0.5)
    assert r_left.x == 0, r_left
    r_right = cover_crop(1024, 1024, 1080, 1920, 1.0, 0.5)
    assert r_right.x == 1024 - 576, r_right
    # Already 9:16: full frame.
    r_v = cover_crop(1080, 1920, 1080, 1920, 0.5, 0.5)
    assert r_v.w == 1080 and r_v.h == 1920 and r_v.x == 0 and r_v.y == 0, r_v
    dw, dh = dest_size("9:16")
    assert (dw, dh) == (1080, 1920)

    with tempfile.TemporaryDirectory(prefix="clip-editor-selftest-") as td:
        td_p = Path(td)
        video = td_p / "square.mp4"
        audio = td_p / "tone.wav"
        out = td_p / "out_9x16.mp4"
        subprocess.check_call(
            [
                which_ffmpeg(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=1024x1024:rate=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=48000",
                "-t",
                "2",
                "-pix_fmt",
                "yuv420p",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(video),
            ]
        )
        subprocess.check_call(
            [
                which_ffmpeg(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=220:sample_rate=48000",
                "-t",
                "3",
                str(audio),
            ]
        )
        result = run_export(video, out, audio=audio, aspect="9:16")
        g = result["gate"]
        assert g["gate_ok"], g
        assert g["vcodec"] == "h264", g
        assert g["acodec"] == "aac", g
        assert g["width"] == 1080 and g["height"] == 1920, g
        assert 1.8 <= float(g["duration"]) <= 2.2, g
        print("selftest ok")
        print(f"  crop {result['meta']['crop']}")
        print(f"  {g['width']}x{g['height']} {g['vcodec']}+{g['acodec']} {g['duration']:.3f}s")
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clip-editor",
        description="Cover-crop a clip, mux audio, export Buffer-safe H.264.",
    )
    p.add_argument("--version", action="version", version=f"clip-editor {__version__}")
    sub = p.add_subparsers(dest="cmd", required=False)

    e = sub.add_parser("export", help="encode one file")
    _add_export_args(e)
    e.set_defaults(func=cmd_export)

    s = sub.add_parser("serve", help="(legacy) Chromium window, not used")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--open", action="store_true", help="launch Chromium --app")
    s.set_defaults(func=cmd_serve)

    g = sub.add_parser("gui", help="native GTK window")
    g.set_defaults(func=cmd_gui)

    t = sub.add_parser("selftest", help="crop math + a 2s lavfi encode")
    t.set_defaults(func=cmd_selftest)
    return p


def cmd_gui(_args: argparse.Namespace) -> int:
    from clip_editor.ui import run

    return run()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "cmd", None) is None:
        return cmd_gui(args)
    try:
        return int(args.func(args))
    except (ExportError, ProbeError) as exc:
        print(exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
