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
    p.add_argument(
        "--crossfade",
        type=float,
        default=0.0,
        help="cross-fade seconds between adjacent clips (default off)",
    )
    p.add_argument(
        "--video-start",
        type=float,
        default=0.0,
        help="timeline offset of the video clip (seconds)",
    )
    p.add_argument(
        "--audio-start",
        type=float,
        default=0.0,
        help="timeline offset of the replacement audio clip (seconds)",
    )
    p.add_argument(
        "--audio-in",
        dest="audio_in",
        type=float,
        default=0.0,
        help="start of used replacement audio, seconds into the file",
    )
    p.add_argument(
        "--audio-out",
        dest="audio_out",
        type=float,
        default=None,
        help="end of used replacement audio, seconds into the file",
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
            crossfade_s=args.crossfade,
            video_start=args.video_start,
            audio_start=args.audio_start,
            audio_in=args.audio_in,
            audio_out=args.audio_out,
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

        from clip_editor.export import _flatten_clips
        from clip_editor.project import ClipInst

        left = ClipInst(
            start=2.0,
            in_s=1.0,
            out_s=8.0,
            transform_x=120.0,
            transform_y=-80.0,
            scale=1.25,
        )
        right = left.split_at(6.0)
        assert right is not None
        assert abs(left.in_s - 1.0) < 1e-9 and abs(left.out_s - 4.0) < 1e-9
        assert abs(right.start - 2.0) < 1e-9
        assert abs(right.in_s - 4.0) < 1e-9 and abs(right.out_s - 8.0) < 1e-9
        assert right.transform_x == 120.0 and right.transform_y == -80.0
        assert right.scale == 1.25
        assert left.split_at(2.0) is None
        assert ClipInst(start=0.0, in_s=0.0, out_s=1.0).split_at(0.5) is not None

        flat = _flatten_clips(
            [
                ClipInst(start=0.0, in_s=0.0, out_s=0.6),
                ClipInst(start=0.3, in_s=0.0, out_s=0.6),
            ],
            2.0,
        )
        assert len(flat) == 2, flat
        assert abs(flat[0][0] - 0.0) < 0.02 and abs(flat[0][1] - 0.3) < 0.02, flat
        assert abs(flat[1][0] - 0.3) < 0.02 and abs(flat[1][1] - 0.9) < 0.02, flat

        out2 = td_p / "out_two.mp4"
        result2 = run_export(
            video,
            out2,
            audio=None,
            aspect="9:16",
            in_s=0.0,
            out_s=2.0,
            video_clips=[
                ClipInst(start=0.0, in_s=0.0, out_s=0.5),
                ClipInst(start=0.6, in_s=0.0, out_s=0.5),
            ],
        )
        g2 = result2["gate"]
        assert g2["gate_ok"], g2
        assert 1.0 <= float(g2["duration"]) <= 1.3, g2

        out3 = td_p / "out_crossfade.mp4"
        result3 = run_export(
            video,
            out3,
            audio=None,
            aspect="9:16",
            in_s=0.0,
            out_s=2.0,
            video_clips=[
                ClipInst(
                    start=0.0,
                    in_s=0.0,
                    out_s=0.8,
                    transform_x=40.0,
                    scale=0.9,
                ),
                ClipInst(start=0.0, in_s=0.8, out_s=1.6),
            ],
            crossfade_s=0.25,
        )
        g3 = result3["gate"]
        assert g3["gate_ok"], g3
        assert 1.25 <= float(g3["duration"]) <= 1.45, g3
        assert abs(float(result3["meta"]["crossfade_s"]) - 0.25) < 1e-9

        out4 = td_p / "out_transform.mp4"
        result4 = run_export(
            video,
            out4,
            audio=None,
            aspect="9:16",
            in_s=0.0,
            out_s=2.0,
            video_clips=[
                ClipInst(
                    start=0.0,
                    in_s=0.0,
                    out_s=0.8,
                    transform_x=120.0,
                    transform_y=-90.0,
                    scale=0.75,
                )
            ],
        )
        g4 = result4["gate"]
        assert g4["gate_ok"], g4
        assert g4["width"] == 1080 and g4["height"] == 1920, g4
        assert 0.7 <= float(g4["duration"]) <= 0.9, g4
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
    g.add_argument("--video", help="add this video to the current project")
    g.add_argument(
        "--audio",
        help="add this audio to the current project (does not start a new project)",
    )
    g.add_argument(
        "--new",
        action="store_true",
        help="start a new project before adding --video/--audio (Eagle Browse Ctrl+Shift+E)",
    )
    g.add_argument("video_path", nargs="?", help="video to open")
    g.set_defaults(func=cmd_gui)

    t = sub.add_parser("selftest", help="crop math + a 2s lavfi encode")
    t.set_defaults(func=cmd_selftest)
    return p


def cmd_gui(args: argparse.Namespace) -> int:
    from clip_editor.ui import run

    video = getattr(args, "video", None) or getattr(args, "video_path", None)
    return run(
        open_video=video,
        open_audio=getattr(args, "audio", None),
        new_project=bool(getattr(args, "new", False)),
    )


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
