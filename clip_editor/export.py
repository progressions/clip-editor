"""Build and run the single ffmpeg export command."""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable

from clip_editor.aspects import cover_crop, dest_size
from clip_editor.eagle import inbox_dir
from clip_editor.probe import ProbeError, gate_h264, probe, which_ffmpeg

ProgressCb = Callable[[float, str], None]


class ExportError(RuntimeError):
    pass


def _trim_needed(in_s: float, duration: float, src_duration: float) -> bool:
    if in_s > 0.0005:
        return True
    if duration + in_s < src_duration - 0.0005:
        return True
    return False


def build_cmd(
    video: Path,
    out: Path,
    *,
    audio: Path | None,
    aspect: str,
    pan_x: float,
    pan_y: float,
    in_s: float,
    out_s: float | None,
    audio_follows_in: bool,
    audio_offset: float,
    video_start: float = 0.0,
    src: dict[str, Any] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Return (ffmpeg argv, meta dict with crop/duration/dest)."""
    video = Path(video)
    out = Path(out)
    src = src or probe(video)
    if not src.get("has_video"):
        raise ExportError(f"no video stream: {video}")
    src_w = int(src["width"])
    src_h = int(src["height"])
    src_dur = float(src["duration"] or 0.0)
    if src_w < 2 or src_h < 2:
        raise ExportError(f"bad video size {src_w}x{src_h}")
    if src_dur <= 0:
        raise ExportError(f"unknown duration for {video}")

    in_s = max(0.0, float(in_s or 0.0))
    if in_s >= src_dur:
        raise ExportError(f"in-point {in_s:.3f}s is past duration {src_dur:.3f}s")
    end = float(out_s) if out_s is not None else src_dur
    end = min(end, src_dur)
    if end <= in_s + 0.04:
        raise ExportError("out-point must be after in-point")
    duration = end - in_s

    dw, dh = dest_size(aspect)
    crop = cover_crop(src_w, src_h, dw, dh, pan_x, pan_y)

    vchain = []
    if _trim_needed(in_s, duration, src_dur):
        vchain.append(f"trim=start={in_s:.6f}:duration={duration:.6f}")
        vchain.append("setpts=PTS-STARTPTS")
    vchain.append(crop.as_ffmpeg())
    vchain.append(f"scale={dw}:{dh}:flags=lanczos")
    vchain.append("setsar=1")
    vchain.append("format=yuv420p")

    audio_path: Path | None = Path(audio) if audio else None
    use_replacement = audio_path is not None
    use_source_audio = (not use_replacement) and bool(src.get("has_audio"))

    a_start = float(audio_offset or 0.0)
    if a_start < 0:
        a_start = 0.0
    v_start = max(0.0, float(video_start or 0.0))
    # Original soundtrack stays locked to the picture. Replacement audio
    # only follows the in-point when the user asks (driver sync).
    # Sliding the video clip (video_start) shifts which part of a music
    # bed sits under the export.
    if use_source_audio or (use_replacement and audio_follows_in):
        a_start += in_s
    elif use_replacement:
        a_start += v_start

    achain = [
        f"atrim=start={a_start:.6f}:duration={duration:.6f}",
        "asetpts=PTS-STARTPTS",
        f"apad=whole_dur={duration:.6f}",
        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
    ]

    cmd = [
        which_ffmpeg(),
        "-hide_banner",
        "-nostdin",
        "-y",
        "-progress",
        "pipe:1",
        "-nostats",
        "-loglevel",
        "error",
        "-i",
        str(video),
    ]
    if use_replacement:
        cmd += ["-i", str(audio_path)]

    if use_replacement or use_source_audio:
        a_in = "[1:a]" if use_replacement else "[0:a]"
        filt = f"[0:v]{','.join(vchain)}[v];{a_in}{','.join(achain)}[a]"
        cmd += ["-filter_complex", filt, "-map", "[v]", "-map", "[a]"]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
        cmd += ["-disposition:a", "default"]
    else:
        cmd += ["-filter:v", ",".join(vchain), "-map", "0:v:0", "-an"]

    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "slow",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-movflags",
        "+faststart",
        "-t",
        f"{duration:.6f}",
        "-f",
        "mp4",
        str(out),
    ]
    meta = {
        "crop": {"x": crop.x, "y": crop.y, "w": crop.w, "h": crop.h},
        "dest": {"width": dw, "height": dh, "aspect": aspect},
        "in_s": in_s,
        "out_s": in_s + duration,
        "duration": duration,
        "audio": str(audio_path) if audio_path else None,
        "audio_follows_in": bool(audio_follows_in),
        "audio_offset": a_start if (use_replacement or use_source_audio) else None,
        "video_start": v_start,
    }
    return cmd, meta


def _parse_progress_line(line: str) -> tuple[float | None, bool]:
    line = line.strip()
    if line == "progress=end":
        return None, True
    if line.startswith("out_time_us="):
        try:
            us = int(line.split("=", 1)[1])
            if us >= 0:
                return us / 1_000_000.0, False
        except ValueError:
            return None, False
    if line.startswith("out_time_ms="):
        try:
            ms = int(line.split("=", 1)[1])
            if ms >= 0:
                return ms / 1000.0, False
        except ValueError:
            return None, False
    return None, False


def run_export(
    video: Path,
    out: Path,
    *,
    audio: Path | None = None,
    aspect: str = "9:16",
    pan_x: float = 0.5,
    pan_y: float = 0.5,
    in_s: float = 0.0,
    out_s: float | None = None,
    audio_follows_in: bool = False,
    audio_offset: float = 0.0,
    video_start: float = 0.0,
    progress: ProgressCb | None = None,
    timeout: float = 1800.0,
) -> dict[str, Any]:
    video = Path(video).expanduser().resolve()
    out = Path(out).expanduser().resolve()
    audio_p = Path(audio).expanduser().resolve() if audio else None
    if audio_p is not None and not audio_p.is_file():
        raise ExportError(f"audio not found: {audio_p}")
    if not video.is_file():
        raise ExportError(f"video not found: {video}")

    src = probe(video)
    # Encode to *.mp4.tmp so eagle-inbox-watch never sees a half-written file
    # (it skips .tmp). Then os.replace onto the final name.
    tmp = out.with_name(out.name + ".tmp")
    cmd, meta = build_cmd(
        video,
        tmp,
        audio=audio_p,
        aspect=aspect,
        pan_x=pan_x,
        pan_y=pan_y,
        in_s=in_s,
        out_s=out_s,
        audio_follows_in=audio_follows_in,
        audio_offset=audio_offset,
        video_start=video_start,
        src=src,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp.unlink(missing_ok=True)
    if progress:
        progress(0.0, "encoding")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    err_box: list[str] = []

    def _read_err() -> None:
        try:
            err_box.append(proc.stderr.read() if proc.stderr else "")
        except OSError:
            err_box.append("")

    t = threading.Thread(target=_read_err, daemon=True)
    t.start()
    dest_dur = float(meta["duration"])
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            now, ended = _parse_progress_line(line)
            if now is not None and dest_dur > 0 and progress:
                progress(min(1.0, now / dest_dur), "encoding")
            if ended:
                break
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        tmp.unlink(missing_ok=True)
        raise ExportError(f"ffmpeg timed out after {timeout:.0f}s") from None
    t.join(timeout=5)
    stderr = err_box[0] if err_box else ""
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise ExportError(
            f"ffmpeg failed (exit {proc.returncode}): {stderr.strip()[:800]}"
        )

    gated = gate_h264(tmp)
    if not gated.get("gate_ok"):
        tmp.unlink(missing_ok=True)
        raise ExportError(gated.get("gate_reason") or "H.264 gate failed")
    os.replace(tmp, out)
    gated = gate_h264(out)
    if progress:
        progress(1.0, "done")
    return {
        "ok": True,
        "out": str(out),
        "cmd": cmd,
        "meta": meta,
        "gate": gated,
    }


def default_out_path(
    video: Path,
    aspect: str,
    *,
    original_name: str | None = None,
    dest_dir: Path | None = None,
) -> Path:
    """Always `{stem}_{9x16}.mp4` (or _2, _3, …). Never keeps a non-mp4 suffix."""
    video = Path(video)
    safe = aspect.replace(":", "x")
    tag = f"_{safe}"
    stem = Path(original_name or video.name).stem
    if stem.endswith(tag):
        out_stem = stem
    else:
        out_stem = f"{stem}{tag}"
    folder = Path(dest_dir) if dest_dir else inbox_dir()
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / f"{out_stem}.mp4"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        alt = folder / f"{out_stem}_{n}.mp4"
        if not alt.exists():
            return alt
        n += 1
