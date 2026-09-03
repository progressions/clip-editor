"""Build and run the single ffmpeg export command."""

from __future__ import annotations

import os
import subprocess
import threading
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from clip_editor.aspects import cover_crop, normalize_resolution
from clip_editor.eagle import inbox_dir
from clip_editor.preview import (
    FINAL_PROFILE,
    EncodeProfile,
    profile_dest_size,
)
from clip_editor.probe import ProbeError, gate_h264, probe, which_ffmpeg
from clip_editor.project import (
    TRANSITION_DISSOLVE,
    TRANSITION_NONE,
    TRANSITION_WHITE_FLASH,
    ClipInst,
    MediaItem,
    apply_legacy_crossfade,
    atempo_chain,
    normalize_speed,
    normalize_transition,
)

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
) -> list[tuple]:
    """Return timeline/source bounds, media id, transform, transition, speed.

    Row: ``(t0, t1, sinn, sout, mid, tx, ty, scale, transition, transition_s, speed)``.
    ``t1 - t0`` is timeline length ``(sout - sinn) / speed``. Later clips
    overwrite earlier ones on overlap, matching playback.
    """
    segs: list[tuple] = []
    for c in clips:
        dur = _dur_for(c, src_dur)
        inn = max(0.0, float(c.in_s))
        out = float(c.out_s) if c.out_s > inn else (dur if dur > inn else inn)
        if dur > 0:
            out = min(out, dur)
        if out <= inn + 0.04:
            continue
        speed = normalize_speed(c.speed)
        source_len = out - inn
        timeline_len = source_len / speed
        t0 = float(c.start) + inn
        t1 = t0 + timeline_len
        if t1 <= 0.02:
            continue
        if t0 < 0:
            # Shift source in-point so timeline starts at 0.
            skip_tl = -t0
            skip_src = skip_tl * speed
            inn += skip_src
            t0 = 0.0
            source_len = out - inn
            if source_len <= 0.04:
                continue
            timeline_len = source_len / speed
            t1 = t0 + timeline_len
        if out <= inn + 0.04:
            continue
        mid = c.media_id or ""
        ttype, tdur = normalize_transition(c.transition, c.transition_s)
        nxt: list[tuple] = []
        for row in segs:
            st0, st1, sinn, sout, smid, sx, sy, scale = row[:8]
            st_type = row[8] if len(row) > 8 else TRANSITION_NONE
            st_dur = row[9] if len(row) > 9 else 0.0
            st_speed = float(row[10]) if len(row) > 10 else 1.0
            if st1 <= t0 + 0.001 or st0 >= t1 - 0.001:
                nxt.append(
                    (
                        st0,
                        st1,
                        sinn,
                        sout,
                        smid,
                        sx,
                        sy,
                        scale,
                        st_type,
                        st_dur,
                        st_speed,
                    )
                )
                continue
            # Overlap remap in timeline space → source via this remnant's speed.
            if st0 < t0 - 0.001:
                keep_tl = t0 - st0
                keep_src = keep_tl * st_speed
                nxt.append(
                    (
                        st0,
                        t0,
                        sinn,
                        sinn + keep_src,
                        smid,
                        sx,
                        sy,
                        scale,
                        st_type,
                        st_dur,
                        st_speed,
                    )
                )
            if st1 > t1 + 0.001:
                skip_tl = t1 - st0
                skip_src = skip_tl * st_speed
                nxt.append(
                    (
                        t1,
                        st1,
                        sinn + skip_src,
                        sout,
                        smid,
                        sx,
                        sy,
                        scale,
                        st_type,
                        st_dur,
                        st_speed,
                    )
                )
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
                ttype,
                tdur,
                speed,
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
    resolution: str | None = None,
    src: dict[str, Any] | None = None,
    profile: EncodeProfile = FINAL_PROFILE,
) -> tuple[list[str], dict[str, Any]]:
    """Return (ffmpeg argv, meta dict with crop/duration/dest)."""
    video = Path(video)
    out = Path(out)
    profile = profile or FINAL_PROFILE
    resolution = normalize_resolution(resolution)
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

    dw, dh = profile_dest_size(aspect, profile, resolution)
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
            resolution=resolution,
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
            profile=profile,
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
        cmd += [
            "-c:a",
            "aac",
            "-b:a",
            profile.audio_bitrate,
            "-ar",
            "48000",
            "-ac",
            "2",
        ]
        cmd += ["-disposition:a", "default"]
    else:
        cmd += ["-filter:v", ",".join(vchain), "-map", "0:v:0", "-an"]

    cmd += _encode_tail(out, out_dur, profile=profile)
    meta = {
        "crop": {"x": crop.x, "y": crop.y, "w": crop.w, "h": crop.h},
        "dest": {
            "width": dw,
            "height": dh,
            "aspect": aspect,
            "resolution": resolution,
        },
        "in_s": in_s,
        "out_s": in_s + duration,
        "duration": out_dur,
        "audio": str(audio_path) if audio_path else None,
        "audio_follows_in": bool(audio_follows_in),
        "audio_offset": a_start if (use_replacement or use_source_audio) else None,
        "video_start": v_place,
        "audio_start": a_place,
        "profile": profile.name,
    }
    return cmd, meta


def _timeline_parts(flat: list[tuple], out_dur: float) -> list[tuple]:
    """Build gap/seg parts. Seg: ``(seg, sinn, source_len, mid, tx, ty, scale, ttype, tdur, speed)``."""
    parts: list[tuple] = []
    t = 0.0
    for row in flat:
        t0, t1, sinn, sout = (
            float(row[0]),
            float(row[1]),
            float(row[2]),
            float(row[3]),
        )
        mid = str(row[4]) if len(row) > 4 else ""
        if t0 > t + 0.02:
            parts.append(("gap", t0 - t))
        transform = tuple(row[5:8]) if len(row) >= 8 else (0.0, 0.0, 1.0)
        ttype = row[8] if len(row) > 8 else TRANSITION_NONE
        tdur = float(row[9]) if len(row) > 9 else 0.0
        speed = normalize_speed(row[10] if len(row) > 10 else 1.0)
        source_len = max(0.0, sout - sinn)
        # Prefer source_len from bounds; fall back to timeline*speed if needed.
        if source_len <= 0.04:
            source_len = max(0.0, (t1 - t0) * speed)
        parts.append(("seg", sinn, source_len, mid, *transform, ttype, tdur, speed))
        t = t1
    if out_dur > t + 0.02:
        parts.append(("gap", out_dur - t))
    if not parts:
        parts.append(("gap", max(out_dur, 0.05)))
    return parts


def _xfade_name(transition: str) -> str | None:
    if transition == TRANSITION_DISSOLVE:
        return "fade"
    if transition == TRANSITION_WHITE_FLASH:
        return "fadewhite"
    return None


def _clamp_transition_duration(
    requested: float, prev_dur: float, next_dur: float
) -> float:
    return min(
        max(0.0, float(requested or 0.0)),
        max(0.0, float(prev_dur) - 0.05),
        max(0.0, float(next_dur) - 0.05),
    )


def _part_outgoing(part: tuple) -> tuple[str, float]:
    if part[0] != "seg":
        return TRANSITION_NONE, 0.0
    if len(part) >= 9:
        return normalize_transition(part[7], part[8])
    return TRANSITION_NONE, 0.0


def _join_transitions_for_parts(parts: list[tuple]) -> list[dict[str, Any]]:
    """Effective transition from parts[i] into parts[i+1] (gaps force hard cuts)."""
    out: list[dict[str, Any]] = []
    for i in range(len(parts) - 1):
        prev, nxt = parts[i], parts[i + 1]
        if prev[0] != "seg" or nxt[0] != "seg":
            out.append(
                {
                    "type": TRANSITION_NONE,
                    "requested_s": 0.0,
                    "duration": 0.0,
                    "applied": False,
                }
            )
            continue
        ttype, requested = _part_outgoing(prev)
        effective = _clamp_transition_duration(
            requested if ttype != TRANSITION_NONE else 0.0,
            _part_duration(prev),
            _part_duration(nxt),
        )
        applied = ttype != TRANSITION_NONE and effective > 0.001
        out.append(
            {
                "type": ttype if applied else TRANSITION_NONE,
                "requested_s": requested if ttype != TRANSITION_NONE else 0.0,
                "duration": effective if applied else 0.0,
                "applied": applied,
            }
        )
    return out


def _transitions_by_boundary_time(
    parts: list[tuple], joins: list[dict[str, Any]]
) -> dict[float, dict[str, Any]]:
    by_t: dict[float, dict[str, Any]] = {}
    t = 0.0
    for i, part in enumerate(parts[:-1]):
        t += _part_duration(part)
        by_t[round(t, 3)] = joins[i]
    return by_t


def _lookup_transition_at(
    by_t: dict[float, dict[str, Any]], boundary: float
) -> dict[str, Any]:
    key = round(boundary, 3)
    if key in by_t:
        return by_t[key]
    for k, val in by_t.items():
        if abs(k - boundary) <= 0.02:
            return val
    return {
        "type": TRANSITION_NONE,
        "requested_s": 0.0,
        "duration": 0.0,
        "applied": False,
    }


def _encode_tail(
    out: Path, out_dur: float, *, profile: EncodeProfile = FINAL_PROFILE
) -> list[str]:
    return [
        "-c:v",
        "libx264",
        "-preset",
        profile.preset,
        "-crf",
        str(int(profile.crf)),
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


def _part_speed(part: tuple) -> float:
    if part[0] != "seg":
        return 1.0
    if len(part) >= 10:
        return normalize_speed(part[9])
    return 1.0


def _part_duration(part: tuple) -> float:
    """Timeline duration of a gap or seg (source_len / speed for segs)."""
    if part[0] == "gap":
        return max(0.0, float(part[1]))
    source_len = max(0.0, float(part[2]))
    return source_len / _part_speed(part)


def _join_parts(
    filters: list[str],
    parts: list[tuple],
    labels: list[str],
    *,
    kind: str,
    crossfade_s: float = 0.0,
    join_transitions: list[dict[str, Any]] | None = None,
) -> tuple[str, float, list[dict[str, Any]]]:
    """Join prepared timeline parts with optional per-cut transitions.

    ``join_transitions[i]`` describes the cut from ``parts[i]`` into
    ``parts[i + 1]``. Gaps force hard cuts. When omitted, a legacy
    ``crossfade_s`` applies dissolve to every touching media pair.
    """
    if not labels:
        raise ExportError(f"no {kind} parts to join")
    if len(labels) == 1:
        return labels[0], _part_duration(parts[0]), []

    if join_transitions is None:
        legacy = max(0.0, float(crossfade_s or 0.0))
        join_transitions = []
        for i in range(len(parts) - 1):
            prev, nxt = parts[i], parts[i + 1]
            if legacy > 0.001 and prev[0] == "seg" and nxt[0] == "seg":
                dur = _clamp_transition_duration(
                    legacy, _part_duration(prev), _part_duration(nxt)
                )
                join_transitions.append(
                    {
                        "type": TRANSITION_DISSOLVE if dur > 0.001 else TRANSITION_NONE,
                        "requested_s": legacy,
                        "duration": dur if dur > 0.001 else 0.0,
                        "applied": dur > 0.001,
                    }
                )
            else:
                join_transitions.append(
                    {
                        "type": TRANSITION_NONE,
                        "requested_s": 0.0,
                        "duration": 0.0,
                        "applied": False,
                    }
                )
    elif len(join_transitions) != len(parts) - 1:
        raise ExportError(
            f"{kind} join_transitions length {len(join_transitions)} "
            f"!= {len(parts) - 1}"
        )

    current = labels[0]
    duration = _part_duration(parts[0])
    previous = parts[0]
    applied: list[dict[str, Any]] = []

    for i, (part, label) in enumerate(zip(parts[1:], labels[1:]), start=1):
        part_dur = _part_duration(part)
        info = join_transitions[i - 1]
        ttype = str(info.get("type") or TRANSITION_NONE)
        transition = float(info.get("duration") or 0.0)
        if previous[0] != "seg" or part[0] != "seg":
            ttype = TRANSITION_NONE
            transition = 0.0
        else:
            transition = _clamp_transition_duration(
                transition if ttype != TRANSITION_NONE else 0.0,
                _part_duration(previous),
                part_dur,
            )
            if transition <= 0.001:
                ttype = TRANSITION_NONE
                transition = 0.0

        # Full `kind`, not kind[0]: the audio callers pass "audio1"/"audio2", so
        # every track emitted the same [ajoinN] names. ffmpeg accepts that only
        # because each label is consumed before the next track reuses it -- an
        # emission-order dependency nothing enforces. Distinct names per track
        # remove it.
        out_label = f"{kind}join{i}"
        xfade = _xfade_name(ttype) if transition > 0.001 else None
        if xfade is not None:
            if kind == "video":
                offset = max(0.0, duration - transition)
                filters.append(
                    f"{current}{label}xfade=transition={xfade}:"
                    f"duration={transition:.6f}:offset={offset:.6f}[{out_label}]"
                )
            else:
                # Visual dissolve / white flash both use an audio acrossfade when
                # audio is continuous across the same cut.
                filters.append(
                    f"{current}{label}acrossfade=d={transition:.6f}:c1=tri:c2=tri"
                    f"[{out_label}]"
                )
            duration += part_dur - transition
            applied.append(
                {
                    "type": ttype,
                    "requested_s": float(info.get("requested_s") or transition),
                    "duration": transition,
                    "applied": True,
                }
            )
        else:
            if kind == "video":
                filters.append(f"{current}{label}concat=n=2:v=1:a=0[{out_label}]")
            else:
                filters.append(f"{current}{label}concat=n=2:v=0:a=1[{out_label}]")
            duration += part_dur
            applied.append(
                {
                    "type": TRANSITION_NONE,
                    "requested_s": float(info.get("requested_s") or 0.0),
                    "duration": 0.0,
                    "applied": False,
                }
            )
        current = f"[{out_label}]"
        previous = part

    return current, duration, applied


def _build_cmd_many(
    video: Path,
    out: Path,
    *,
    audio_path: Path | None,
    aspect: str,
    resolution: str,
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
    profile: EncodeProfile = FINAL_PROFILE,
) -> tuple[list[str], dict[str, Any]]:
    profile = profile or FINAL_PROFILE
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
    if crossfade_s and float(crossfade_s) > 0.001:
        apply_legacy_crossfade(vwork, float(crossfade_s))
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
            speed = _part_speed(part)
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
            if abs(speed - 1.0) > 1e-6:
                chain.append(f"setpts=(PTS-STARTPTS)/{speed:.6f}")
            else:
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
    v_joins = _join_transitions_for_parts(v_parts)
    v_applied: list[dict[str, Any]] = []
    if len(v_labs) > 1:
        v_label, video_duration, v_applied = _join_parts(
            filters,
            v_parts,
            v_labs,
            kind="video",
            join_transitions=v_joins,
        )
        if v_label != "[v]":
            filters.append(f"{v_label}null[v]")
    v_cut_by_t = _transitions_by_boundary_time(v_parts, v_joins)

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
                    speed = _part_speed(part)
                    ii = idx_of.get(mid, 0)
                    off = max(0.0, float(audio_offset or 0.0))
                    if a_idx_count[ii] > 1:
                        k = a_split_at[ii]
                        a_split_at[ii] = k + 1
                        pad = f"[ain{ii}_{k}]"
                    else:
                        pad = f"[{ii}:a]"
                    # Pitch follows rate (atempo); documented default for #531.
                    achain = [
                        f"atrim=start={sinn + off:.6f}:duration={sdur:.6f}",
                        "asetpts=PTS-STARTPTS",
                        *atempo_chain(speed),
                        "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo",
                    ]
                    filters.append(f"{pad}{','.join(achain)}[{lab}]")
                a_labs.append(f"[{lab}]")
            if len(a_labs) > 1:
                a_joins: list[dict[str, Any]] = []
                t_cursor = 0.0
                for i in range(len(a_parts) - 1):
                    t_cursor += _part_duration(a_parts[i])
                    prev, nxt = a_parts[i], a_parts[i + 1]
                    if prev[0] == "seg" and nxt[0] == "seg":
                        # Match the video cut at this timeline boundary when audio
                        # is continuous across the same cut.
                        a_joins.append(_lookup_transition_at(v_cut_by_t, t_cursor))
                    else:
                        a_joins.append(
                            {
                                "type": TRANSITION_NONE,
                                "requested_s": 0.0,
                                "duration": 0.0,
                                "applied": False,
                            }
                        )
                a_label, _, _ = _join_parts(
                    filters,
                    a_parts,
                    a_labs,
                    kind=f"audio{track}",
                    join_transitions=a_joins,
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
        cmd += [
            "-c:a",
            "aac",
            "-b:a",
            profile.audio_bitrate,
            "-ar",
            "48000",
            "-ac",
            "2",
        ]
        cmd += ["-disposition:a", "default"]
    else:
        cmd += ["-map", "[v]", "-an"]
    cmd += _encode_tail(out, out_dur, profile=profile)
    effective = [row for row in v_applied if row.get("applied")]
    meta = {
        "crop": {"x": crop.x, "y": crop.y, "w": crop.w, "h": crop.h},
        "dest": {
            "width": dw,
            "height": dh,
            "aspect": aspect,
            "resolution": resolution,
        },
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
        "crossfade_s": max(
            (float(row["duration"]) for row in effective),
            default=max(0.0, float(crossfade_s or 0.0)),
        ),
        "transitions": v_applied,
        "profile": profile.name,
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


class ExportCancelled(ExportError):
    """Raised when ``cancel_event`` is set during ``run_export``."""


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
    resolution: str | None = None,
    profile: EncodeProfile = FINAL_PROFILE,
    progress: ProgressCb | None = None,
    cancel_event: threading.Event | None = None,
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
        resolution=resolution,
        src=src,
        profile=profile or FINAL_PROFILE,
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
    cancelled = False
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                proc.kill()
                break
            now, ended = _parse_progress_line(line)
            if now is not None and dest_dur > 0 and progress:
                progress(min(1.0, now / dest_dur), "encoding")
            if ended:
                break
        if not cancelled:
            proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        tmp.unlink(missing_ok=True)
        raise ExportError(f"ffmpeg timed out after {timeout:.0f}s") from None
    t.join(timeout=5)
    if cancelled or (cancel_event is not None and cancel_event.is_set()):
        tmp.unlink(missing_ok=True)
        raise ExportCancelled("preview render cancelled")
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
