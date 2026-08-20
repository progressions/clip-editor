"""ffprobe helpers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    pass


def which_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise ProbeError("ffmpeg is not on PATH")
    return path


def which_ffprobe() -> str:
    path = shutil.which("ffprobe")
    if not path:
        raise ProbeError("ffprobe is not on PATH")
    return path


def _run_ffprobe(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ProbeError(f"file not found: {path}")
    try:
        raw = subprocess.check_output(
            [
                which_ffprobe(),
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        raise ProbeError(f"ffprobe failed for {path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"ffprobe timed out for {path}") from exc
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned non-JSON for {path}") from exc
    if not isinstance(data, dict):
        raise ProbeError(f"ffprobe returned unexpected JSON for {path}")
    return data


def _first_stream(data: dict[str, Any], codec_type: str) -> dict[str, Any] | None:
    for s in data.get("streams") or []:
        if isinstance(s, dict) and s.get("codec_type") == codec_type:
            return s
    return None


def _float(value: Any) -> float | None:
    if value is None or value == "N/A":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def probe(path: Path) -> dict[str, Any]:
    """Return width, height, duration, codecs, has_audio, has_video."""
    path = Path(path).expanduser().resolve()
    data = _run_ffprobe(path)
    v = _first_stream(data, "video")
    a = _first_stream(data, "audio")
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    width = int(v.get("width") or 0) if v else 0
    height = int(v.get("height") or 0) if v else 0
    duration = _float(fmt.get("duration"))
    if duration is None and v:
        duration = _float(v.get("duration"))
    if duration is None and a:
        duration = _float(a.get("duration"))
    duration = float(duration or 0.0)
    fps = None
    if v:
        afr = str(v.get("avg_frame_rate") or "")
        if afr and afr != "0/0" and "/" in afr:
            num, den = afr.split("/", 1)
            try:
                n, d = float(num), float(den)
                if d:
                    fps = n / d
            except ValueError:
                fps = None
    return {
        "path": str(path),
        "name": path.name,
        "width": width,
        "height": height,
        "duration": duration,
        "fps": fps,
        "vcodec": (v or {}).get("codec_name"),
        "acodec": (a or {}).get("codec_name"),
        "has_video": v is not None,
        "has_audio": a is not None,
        "size": path.stat().st_size,
    }


def gate_h264(path: Path) -> dict[str, Any]:
    """Buffer-publish gate: video codec must be h264."""
    info = probe(path)
    vcodec = info.get("vcodec")
    ok = vcodec == "h264" and int(info.get("width") or 0) > 0
    info["gate_ok"] = ok
    info["gate_reason"] = None if ok else f"video codec is {vcodec!r}, need h264"
    return info
