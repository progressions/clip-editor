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
from clip_editor.project import ClipInst

ProgressCb = Callable[[float, str], None]


class ExportError(RuntimeError):
    pass


def _flatten_clips(clips: list[ClipInst], src_dur: float) -> list[tuple[float, float, float, float]]:
    """Return (timeline_t0, timeline_t1, source_in, source_out).

    Later clips overwrite earlier ones on overlap, matching playback.
    """
    segs: list[tuple[float, float, float, float]] = []
    for c in clips:
        inn = max(0.0, float(c.in_s))
        out = float(c.out_s) if c.out_s > inn else (src_dur if src_dur > inn else inn)
        if src_dur > 0:
            out = min(out, src_dur)
        if out <= inn + 0.04:
            continue
        t0 = float(c.start) + inn
        t1 = float(c.start) + out
        if t1 <= 0.02:
            continue
        if t0 < 0:
            inn += -t0
            t0 = 0.0
        if out <= inn + 0.04:
            continue
        nxt: list[tuple[float, float, float, float]] = []
        for st0, st1, sinn, sout in segs:
            if st1 <= t0 + 0.001 or st0 >= t1 - 0.001:
                nxt.append((st0, st1, sinn, sout))
                continue
            if st0 < t0 - 0.001:
                keep = t0 - st0
                nxt.append((st0, t0, sinn, sinn + keep))
            if st1 > t1 + 0.001:
                skip = t1 - st0
                nxt.append((t1, st1, sinn + skip, sout))
        nxt.append((t0, t1, inn, out))
        segs = nxt
    segs = [s for s in segs if s[1] > s[0] + 0.04]
    segs.sort(key=lambda row: row[0])
    return segs


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
    audio_start: float = 0.0,
    audio_in: float = 0.0,
    audio_out: float | None = None,
    video_clips: list[ClipInst] | None = None,
    audio_clips: list[ClipInst] | None = None,
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

    audio_path: Path | None = Path(audio) if audio else None
    use_replacement = audio_path is not None
    use_source_audio = (not use_replacement) and bool(src.get("has_audio"))
    many = (video_clips is not None and len(video_clips) > 1) or (
        audio_clips is not None and len(audio_clips) > 1
    )
    if many:
        return _build_cmd_many(
            video,
            out,
            audio_path=audio_path,
            aspect=aspect,
            pan_x=pan_x,
            pan_y=pan_y,
            audio_follows_in=audio_follows_in,
            audio_offset=audio_offset,
            video_clips=video_clips or [],
            audio_clips=audio_clips,
            src=src,
            src_w=src_w,
            src_h=src_h,
            src_dur=src_dur,
            dw=dw,
            dh=dh,
            crop=crop,
            use_replacement=use_replacement,
            use_source_audio=use_source_audio,
        )

    vchain = []
    if _trim_needed(in_s, duration, src_dur):
        vchain.append(f"trim=start={in_s:.6f}:duration={duration:.6f}")
        vchain.append("setpts=PTS-STARTPTS")
    vchain.append(crop.as_ffmpeg())
    vchain.append(f"scale={dw}:{dh}:flags=lanczos")
    vchain.append("setsar=1")
    vchain.append("format=yuv420p")

    a_start = float(audio_offset or 0.0)
    if a_start < 0:
        a_start = 0.0
    v_place = float(video_start or 0.0)
    a_place = float(audio_start or 0.0)
    # Black lead-in is the used clip’s left edge, not unused source
    # sitting before timeline 0.
    lead = max(0.0, v_place + in_s)
    out_dur = duration + lead
    if lead > 0.04:
        vchain.append(f"tpad=start_duration={lead:.6f}:color=black")

    # Original soundtrack stays locked to the picture. Replacement audio
    # only follows the in-point when the user asks (driver sync).
    # Otherwise the A clip sits at audio_start on the timeline.
    picture_locked = use_source_audio or (use_replacement and audio_follows_in)
    ain = max(0.0, float(audio_in or 0.0))
    if picture_locked:
        a_start += in_s
        a_len = duration
        delay = lead
    else:
        delay = max(0.0, a_place + ain)
        skip = max(0.0, -(a_place + ain))
        a_start += ain + skip
        used = None if audio_out is None else max(0.05, float(audio_out) - ain - skip)
        remain = max(0.05, out_dur - delay)
        a_len = remain if used is None else min(used, remain)

    achain = [
        f"atrim=start={a_start:.6f}:duration={a_len:.6f}",
        "asetpts=PTS-STARTPTS",
    ]
    if delay > 0.04:
        ms = int(round(delay * 1000.0))
        achain.append(f"adelay={ms}:all=1")
    achain += [
        f"apad=whole_dur={out_dur:.6f}",
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
        f"{out_dur:.6f}",
        "-f",
        "mp4",
        str(out),
    ]
    meta = {
        "crop": {"x": crop.x, "y": crop.y, "w": crop.w, "h": crop.h},
        "dest": {"width": dw, "height": dh, "aspect": aspect},
        "in_s": in_s,
        "out_s": in_s + duration,
        "duration": out_dur,
        "audio": str(audio_path) if audio_path else None,
        "audio_follows_in": bool(audio_follows_in),
        "audio_offset": a_start if (use_replacement or use_source_audio) else None,
        "video_start": v_place,
        "audio_start": a_place,
    }
    return cmd, meta


def _timeline_parts(
    flat: list[tuple[float, float, float, float]], out_dur: float
) -> list[tuple]:
    parts: list[tuple] = []
    t = 0.0
    for t0, t1, sinn, sout in flat:
        if t0 > t + 0.02:
            parts.append(("gap", t0 - t))
        parts.append(("seg", sinn, t1 - t0))
        t = t1
    if out_dur > t + 0.02:
        parts.append(("gap", out_dur - t))
    if not parts:
        parts.append(("gap", max(out_dur, 0.05)))
    return parts


def _encode_tail(out: Path, out_dur: float) -> list[str]:
    return [
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
        f"{out_dur:.6f}",
        "-f",
        "mp4",
        str(out),
    ]


def _build_cmd_many(
    video: Path,
    out: Path,
    *,
    audio_path: Path | None,
    aspect: str,
    pan_x: float,
    pan_y: float,
    audio_follows_in: bool,
    audio_offset: float,
    video_clips: list[ClipInst],
    audio_clips: list[ClipInst] | None,
    src: dict[str, Any],
    src_w: int,
    src_h: int,
    src_dur: float,
    dw: int,
    dh: int,
    crop: Any,
    use_replacement: bool,
    use_source_audio: bool,
) -> tuple[list[str], dict[str, Any]]:
    fps = float(src.get("fps") or 30) or 30.0
    if fps < 1:
        fps = 30.0
    vflat = _flatten_clips(video_clips, src_dur)
    if not vflat:
        raise ExportError("no video on the timeline")
    a_src_dur = src_dur
    if use_replacement and audio_path is not None:
        ainfo = probe(audio_path)
        a_src_dur = float(ainfo.get("duration") or 0.0)
    if use_replacement and audio_clips:
        aflat = _flatten_clips(audio_clips, a_src_dur)
    elif use_source_audio:
        aflat = list(vflat)
    else:
        aflat = []
    out_dur = vflat[-1][1]
    if aflat:
        out_dur = max(out_dur, aflat[-1][1])
    out_dur = max(out_dur, 0.05)
    v_parts = _timeline_parts(vflat, out_dur)
    a_parts = _timeline_parts(aflat, out_dur) if aflat else []

    filters: list[str] = []
    v_labs: list[str] = []
    v_seg_n = sum(1 for p in v_parts if p[0] != "gap")
    if v_seg_n > 1:
        filters.append(
            "[0:v]split=" + str(v_seg_n) + "".join(f"[vv{i}]" for i in range(v_seg_n))
        )
    v_seg_i = 0
    for i, part in enumerate(v_parts):
        lab = "v" if len(v_parts) == 1 else f"vs{i}"
        if part[0] == "gap":
            d = float(part[1])
            filters.append(
                f"color=c=black:s={dw}x{dh}:r={fps:.4f}:d={d:.6f},"
                f"format=yuv420p,setsar=1[{lab}]"
            )
        else:
            sinn, sdur = float(part[1]), float(part[2])
            chain = []
            if _trim_needed(sinn, sdur, src_dur):
                chain.append(f"trim=start={sinn:.6f}:duration={sdur:.6f}")
                chain.append("setpts=PTS-STARTPTS")
            chain += [
                crop.as_ffmpeg(),
                f"scale={dw}:{dh}:flags=lanczos",
                "setsar=1",
                "format=yuv420p",
            ]
            src = f"[vv{v_seg_i}]" if v_seg_n > 1 else "[0:v]"
            v_seg_i += 1
            filters.append(f"{src}{','.join(chain)}[{lab}]")
        v_labs.append(f"[{lab}]")
    if len(v_labs) > 1:
        filters.append("".join(v_labs) + f"concat=n={len(v_labs)}:v=1:a=0[v]")

    have_audio = bool(a_parts) and (use_replacement or use_source_audio)
    a_in = "[1:a]" if use_replacement else "[0:a]"
    if have_audio:
        a_labs: list[str] = []
        a_seg_n = sum(1 for p in a_parts if p[0] != "gap")
        if a_seg_n > 1:
            filters.append(
                f"{a_in}asplit=" + str(a_seg_n) + "".join(f"[aa{i}]" for i in range(a_seg_n))
            )
        a_seg_i = 0
        for i, part in enumerate(a_parts):
            lab = "a" if len(a_parts) == 1 else f"as{i}"
            if part[0] == "gap":
                d = float(part[1])
                filters.append(
                    f"anullsrc=r=48000:cl=stereo:d={d:.6f},"
                    f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[{lab}]"
                )
            else:
                sinn, sdur = float(part[1]), float(part[2])
                off = max(0.0, float(audio_offset or 0.0))
                src = f"[aa{a_seg_i}]" if a_seg_n > 1 else a_in
                a_seg_i += 1
                filters.append(
                    f"{src}atrim=start={sinn + off:.6f}:duration={sdur:.6f},"
                    f"asetpts=PTS-STARTPTS,"
                    f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[{lab}]"
                )
            a_labs.append(f"[{lab}]")
        if len(a_labs) > 1:
            filters.append("".join(a_labs) + f"concat=n={len(a_labs)}:v=0:a=1[a]")

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
    cmd += ["-filter_complex", ";".join(filters)]
    if have_audio:
        cmd += ["-map", "[v]", "-map", "[a]"]
        cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
        cmd += ["-disposition:a", "default"]
    else:
        cmd += ["-map", "[v]", "-an"]
    cmd += _encode_tail(out, out_dur)
    meta = {
        "crop": {"x": crop.x, "y": crop.y, "w": crop.w, "h": crop.h},
        "dest": {"width": dw, "height": dh, "aspect": aspect},
        "in_s": vflat[0][2],
        "out_s": vflat[-1][3],
        "duration": out_dur,
        "audio": str(audio_path) if audio_path else None,
        "audio_follows_in": bool(audio_follows_in),
        "audio_offset": float(audio_offset or 0.0) if have_audio else None,
        "video_start": float(video_clips[0].start) if video_clips else 0.0,
        "audio_start": float(audio_clips[0].start) if audio_clips else 0.0,
        "clips": len(vflat),
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
    audio_start: float = 0.0,
    audio_in: float = 0.0,
    audio_out: float | None = None,
    video_clips: list[ClipInst] | None = None,
    audio_clips: list[ClipInst] | None = None,
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
        audio_start=audio_start,
        audio_in=audio_in,
        audio_out=audio_out,
        video_clips=video_clips,
        audio_clips=audio_clips,
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
