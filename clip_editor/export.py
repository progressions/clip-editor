"""Build and run the single ffmpeg export command."""

from __future__ import annotations

import os
import subprocess
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from clip_editor.aspects import cover_crop, dest_size
from clip_editor.eagle import inbox_dir
from clip_editor.probe import ProbeError, gate_h264, probe, which_ffmpeg
from clip_editor.project import ClipInst, MediaItem

ProgressCb = Callable[[float, str], None]


class ExportError(RuntimeError):
    pass


def _dur_for(c: ClipInst, src_dur: float | dict[str, float]) -> float:
    if isinstance(src_dur, dict):
        if c.media_id and c.media_id in src_dur:
            return float(src_dur[c.media_id] or 0.0)
        if "" in src_dur:
            return float(src_dur[""] or 0.0)
        if len(src_dur) == 1:
            return float(next(iter(src_dur.values())) or 0.0)
        return 0.0
    return float(src_dur or 0.0)


def _flatten_clips(
    clips: list[ClipInst], src_dur: float | dict[str, float]
) -> list[tuple[float, float, float, float, str, float, float, float]]:
    """Return timeline/source bounds, media id, x/y translation, and scale.

    Later clips overwrite earlier ones on overlap, matching playback.
    """
    segs: list[tuple[float, float, float, float, str, float, float, float]] = []
    for c in clips:
        dur = _dur_for(c, src_dur)
        inn = max(0.0, float(c.in_s))
        out = float(c.out_s) if c.out_s > inn else (dur if dur > inn else inn)
        if dur > 0:
            out = min(out, dur)
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
        mid = c.media_id or ""
        nxt: list[tuple[float, float, float, float, str, float, float, float]] = []
        for st0, st1, sinn, sout, smid, sx, sy, scale in segs:
            if st1 <= t0 + 0.001 or st0 >= t1 - 0.001:
                nxt.append((st0, st1, sinn, sout, smid, sx, sy, scale))
                continue
            if st0 < t0 - 0.001:
                keep = t0 - st0
                nxt.append((st0, t0, sinn, sinn + keep, smid, sx, sy, scale))
            if st1 > t1 + 0.001:
                skip = t1 - st0
                nxt.append((t1, st1, sinn + skip, sout, smid, sx, sy, scale))
        nxt.append(
            (
                t0,
                t1,
                inn,
                out,
                mid,
                float(c.transform_x),
                float(c.transform_y),
                max(0.05, float(c.scale)),
            )
        )
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
    media: list[MediaItem] | None = None,
    use_video_soundtrack: bool = True,
    crossfade_s: float = 0.0,
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
    use_source_audio = (
        (not use_replacement)
        and bool(src.get("has_audio"))
        and use_video_soundtrack
    )
    many = (video_clips is not None and len(video_clips) > 1) or (
        audio_clips is not None and len(audio_clips) > 1
    )
    if any(
        abs(float(c.transform_x)) > 0.0001
        or abs(float(c.transform_y)) > 0.0001
        or abs(float(c.scale) - 1.0) > 0.0001
        for c in (video_clips or [])
    ):
        many = True
    vmids = {c.media_id for c in (video_clips or []) if c.media_id}
    amids = {c.media_id for c in (audio_clips or []) if c.media_id}
    if len(vmids) > 1 or len(amids) > 1:
        many = True
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
            media=media,
            src=src,
            src_w=src_w,
            src_h=src_h,
            src_dur=src_dur,
            dw=dw,
            dh=dh,
            crop=crop,
            use_replacement=use_replacement,
            use_source_audio=use_source_audio,
            crossfade_s=crossfade_s,
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


def _timeline_parts(flat: list[tuple], out_dur: float) -> list[tuple]:
    parts: list[tuple] = []
    t = 0.0
    for row in flat:
        t0, t1, sinn = float(row[0]), float(row[1]), float(row[2])
        mid = str(row[4]) if len(row) > 4 else ""
        if t0 > t + 0.02:
            parts.append(("gap", t0 - t))
        transform = tuple(row[5:8]) if len(row) >= 8 else (0.0, 0.0, 1.0)
        parts.append(("seg", sinn, t1 - t0, mid, *transform))
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


def _part_duration(part: tuple) -> float:
    if part[0] == "gap":
        return max(0.0, float(part[1]))
    return max(0.0, float(part[2]))


def _join_parts(
    filters: list[str],
    parts: list[tuple],
    labels: list[str],
    *,
    kind: str,
    crossfade_s: float,
) -> tuple[str, float]:
    """Join prepared timeline parts, dissolving only touching media segments."""
    if not labels:
        raise ExportError(f"no {kind} parts to join")
    if len(labels) == 1:
        return labels[0], _part_duration(parts[0])

    current = labels[0]
    duration = _part_duration(parts[0])
    previous = parts[0]
    requested = max(0.0, float(crossfade_s or 0.0))

    for i, (part, label) in enumerate(zip(parts[1:], labels[1:]), start=1):
        part_dur = _part_duration(part)
        transition = 0.0
        if requested > 0 and previous[0] == "seg" and part[0] == "seg":
            transition = min(
                requested,
                max(0.0, _part_duration(previous) - 0.05),
                max(0.0, part_dur - 0.05),
            )

        # Full `kind`, not kind[0]: the audio callers pass "audio1"/"audio2", so
        # every track emitted the same [ajoinN] names. ffmpeg accepts that only
        # because each label is consumed before the next track reuses it -- an
        # emission-order dependency nothing enforces. Distinct names per track
        # remove it.
        out_label = f"{kind}join{i}"
        if transition > 0.001:
            if kind == "video":
                offset = max(0.0, duration - transition)
                filters.append(
                    f"{current}{label}xfade=transition=fade:"
                    f"duration={transition:.6f}:offset={offset:.6f}[{out_label}]"
                )
            else:
                filters.append(
                    f"{current}{label}acrossfade=d={transition:.6f}:c1=tri:c2=tri"
                    f"[{out_label}]"
                )
            duration += part_dur - transition
        else:
            if kind == "video":
                filters.append(f"{current}{label}concat=n=2:v=1:a=0[{out_label}]")
            else:
                filters.append(f"{current}{label}concat=n=2:v=0:a=1[{out_label}]")
            duration += part_dur
        current = f"[{out_label}]"
        previous = part

    return current, duration


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
    crossfade_s: float = 0.0,
    media: list[MediaItem] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    media = list(media or [])
    if not any(m.kind == "video" for m in media):
        media = [MediaItem(id="m1", path=video, kind="video")] + media
    if use_replacement and audio_path is not None and not any(m.kind == "audio" for m in media):
        media.append(MediaItem(id="m2", path=audio_path, kind="audio"))
    by_id = {m.id: m for m in media}
    prim_v = next((m.id for m in media if m.kind == "video"), "")
    prim_a = next((m.id for m in media if m.kind == "audio"), "")
    probes: dict[str, dict[str, Any]] = {}
    durs: dict[str, float] = {}
    for m in media:
        try:
            info = probe(m.path)
        except ProbeError as exc:
            raise ExportError(str(exc)) from exc
        probes[m.id] = info
        durs[m.id] = float(info.get("duration") or 0.0)
    vwork = []
    # Higher video tracks take priority wherever their clips are present.
    for c in sorted(video_clips, key=lambda clip: int(clip.track)):
        cc = c.copy()
        if not cc.media_id:
            cc.media_id = prim_v
        vwork.append(cc)
    vflat = _flatten_clips(vwork, durs if durs else src_dur)
    if not vflat:
        raise ExportError("no video on the timeline")
    aflats: dict[int, list[tuple]] = {}
    if use_replacement and audio_clips:
        for track in (1, 2):
            awork = []
            for c in audio_clips:
                if int(c.track) != track:
                    continue
                cc = c.copy()
                if not cc.media_id:
                    cc.media_id = prim_a or prim_v
                awork.append(cc)
            flat = _flatten_clips(awork, durs if durs else src_dur)
            if flat:
                aflats[track] = flat
    elif use_source_audio:
        flat = [row for row in vflat if probes.get(row[4], {}).get("has_audio")]
        if flat:
            aflats[1] = flat
    out_dur = vflat[-1][1]
    for aflat in aflats.values():
        out_dur = max(out_dur, aflat[-1][1])
    out_dur = max(out_dur, 0.05)
    v_parts = _timeline_parts(vflat, out_dur)
    a_parts_by_track = {
        track: _timeline_parts(aflat, out_dur) for track, aflat in aflats.items()
    }

    inputs: list[Path] = []
    idx_of: dict[str, int] = {}

    def add_input(mid: str) -> int:
        if mid in idx_of:
            return idx_of[mid]
        item = by_id.get(mid)
        path = item.path.resolve() if item is not None else Path(video)
        for i, existing in enumerate(inputs):
            if existing == path:
                idx_of[mid] = i
                return i
        idx_of[mid] = len(inputs)
        inputs.append(path)
        return idx_of[mid]

    for row in vflat:
        add_input(str(row[4]))
    for aflat in aflats.values():
        for row in aflat:
            add_input(str(row[4]))
    if not inputs:
        inputs = [video]
        idx_of[prim_v] = 0

    fps = float(src.get("fps") or 30) or 30.0
    if fps < 1:
        fps = 30.0

    v_mids = [str(p[3]) for p in v_parts if p[0] == "seg"]
    v_idx_count = Counter(idx_of.get(mid, 0) for mid in v_mids)
    filters: list[str] = []
    v_split_at = {i: 0 for i in v_idx_count}
    for i, n in v_idx_count.items():
        if n > 1:
            filters.append(
                f"[{i}:v]split={n}" + "".join(f"[vin{i}_{k}]" for k in range(n))
            )

    v_labs: list[str] = []
    for i, part in enumerate(v_parts):
        lab = "v" if len(v_parts) == 1 else f"vs{i}"
        if part[0] == "gap":
            d = float(part[1])
            filters.append(
                f"color=c=black:s={dw}x{dh}:r={fps:.4f}:d={d:.6f},"
                f"format=yuv420p,fps={fps:.4f},settb=AVTB,"
                f"setpts=PTS-STARTPTS,setsar=1[{lab}]"
            )
        else:
            sinn, sdur, mid = float(part[1]), float(part[2]), str(part[3])
            tx, ty = float(part[4]), float(part[5])
            clip_scale = max(0.05, float(part[6]))
            ii = idx_of.get(mid, 0)
            info = probes.get(mid) or src
            dur = float(info.get("duration") or src_dur or 0)
            try:
                iw, ih = int(info.get("width") or src_w), int(info.get("height") or src_h)
            except (TypeError, ValueError):
                iw, ih = src_w, src_h
            this_crop = cover_crop(iw, ih, dw, dh, pan_x, pan_y) if iw >= 2 and ih >= 2 else crop
            chain = []
            if _trim_needed(sinn, sdur, dur):
                chain.append(f"trim=start={sinn:.6f}:duration={sdur:.6f}")
            chain.append("setpts=PTS-STARTPTS")
            chain += [
                this_crop.as_ffmpeg(),
                f"scale={dw}:{dh}:flags=lanczos",
            ]
            transformed = (
                abs(tx) > 0.0001
                or abs(ty) > 0.0001
                or abs(clip_scale - 1.0) > 0.0001
            )
            if transformed:
                tw = max(2, int(round(dw * clip_scale / 2.0)) * 2)
                th = max(2, int(round(dh * clip_scale / 2.0)) * 2)
                chain.append(f"scale={tw}:{th}:flags=lanczos")
            chain += [
                f"fps={fps:.4f}",
                "settb=AVTB",
                "setsar=1",
                "format=yuv420p",
            ]
            if v_idx_count[ii] > 1:
                k = v_split_at[ii]
                v_split_at[ii] = k + 1
                pad = f"[vin{ii}_{k}]"
            else:
                pad = f"[{ii}:v]"
            if transformed:
                fg = f"vfg{i}"
                bg = f"vbg{i}"
                xexpr = f"(W-w)/2+{tx:.3f}"
                yexpr = f"(H-h)/2+{ty:.3f}"
                filters.append(f"{pad}{','.join(chain)}[{fg}]")
                filters.append(
                    f"color=c=black:s={dw}x{dh}:r={fps:.4f}:d={sdur:.6f},"
                    f"format=yuv420p[{bg}]"
                )
                filters.append(
                    f"[{bg}][{fg}]overlay=x={xexpr}:y={yexpr}:"
                    f"shortest=1:eof_action=pass,fps={fps:.4f},"
                    f"settb=AVTB,setpts=PTS-STARTPTS[{lab}]"
                )
            else:
                filters.append(f"{pad}{','.join(chain)}[{lab}]")
        v_labs.append(f"[{lab}]")
    video_duration = out_dur
    if len(v_labs) > 1:
        v_label, video_duration = _join_parts(
            filters, v_parts, v_labs, kind="video", crossfade_s=crossfade_s
        )
        if v_label != "[v]":
            filters.append(f"{v_label}null[v]")

    have_audio = bool(a_parts_by_track) and (use_replacement or use_source_audio)
    if have_audio:
        all_a_parts = [p for parts in a_parts_by_track.values() for p in parts]
        a_mids = [str(p[3]) for p in all_a_parts if p[0] == "seg"]
        a_idx_count = Counter(idx_of.get(mid, 0) for mid in a_mids)
        a_split_at = {i: 0 for i in a_idx_count}
        for i, n in a_idx_count.items():
            if n > 1:
                filters.append(
                    f"[{i}:a]asplit={n}" + "".join(f"[ain{i}_{k}]" for k in range(n))
                )
        track_outputs: list[str] = []
        for track, a_parts in sorted(a_parts_by_track.items()):
            a_labs: list[str] = []
            for i, part in enumerate(a_parts):
                lab = f"a{track}s{i}"
                if part[0] == "gap":
                    d = float(part[1])
                    filters.append(
                        f"anullsrc=r=48000:cl=stereo:d={d:.6f},"
                        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[{lab}]"
                    )
                else:
                    sinn, sdur, mid = float(part[1]), float(part[2]), str(part[3])
                    ii = idx_of.get(mid, 0)
                    off = max(0.0, float(audio_offset or 0.0))
                    if a_idx_count[ii] > 1:
                        k = a_split_at[ii]
                        a_split_at[ii] = k + 1
                        pad = f"[ain{ii}_{k}]"
                    else:
                        pad = f"[{ii}:a]"
                    filters.append(
                        f"{pad}atrim=start={sinn + off:.6f}:duration={sdur:.6f},"
                        f"asetpts=PTS-STARTPTS,"
                        f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[{lab}]"
                    )
                a_labs.append(f"[{lab}]")
            if len(a_labs) > 1:
                a_label, _ = _join_parts(
                    filters, a_parts, a_labs, kind=f"audio{track}", crossfade_s=crossfade_s
                )
            else:
                a_label = a_labs[0]
            track_out = f"atrack{track}"
            filters.append(f"{a_label}anull[{track_out}]")
            track_outputs.append(f"[{track_out}]")
        if len(track_outputs) == 1:
            filters.append(f"{track_outputs[0]}anull[a]")
        else:
            filters.append(
                "".join(track_outputs)
                + f"amix=inputs={len(track_outputs)}:duration=longest:normalize=0,"
                + f"volume={1.0 / len(track_outputs):.6f}[a]"
            )

    out_dur = max(0.05, video_duration)

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
    ]
    for p in inputs:
        cmd += ["-i", str(p)]
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
        "inputs": len(inputs),
        "crossfade_s": max(0.0, float(crossfade_s or 0.0)),
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
    media: list[MediaItem] | None = None,
    use_video_soundtrack: bool = True,
    crossfade_s: float = 0.0,
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
        media=media,
        use_video_soundtrack=use_video_soundtrack,
        crossfade_s=crossfade_s,
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
