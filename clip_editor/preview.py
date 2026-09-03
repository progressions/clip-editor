"""Compiled transition previews: range math, cache, and proxy helpers.

Preview renders reuse ``export.build_cmd`` / ``run_export`` with
``PREVIEW_PROFILE``. Files land only under ``~/.cache/clip-editor/previews/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clip_editor.aspects import dest_size, even
from clip_editor.eagle import inbox_dir
from clip_editor.project import (
    TRANSITION_NONE,
    ClipInst,
    MediaItem,
    normalize_transition,
)

PREVIEW_CACHE_DIR = Path.home() / ".cache" / "clip-editor" / "previews"
PREVIEW_PAD_S = 2.0
PREVIEW_CACHE_KEEP = 40
PREVIEW_SHORT_AXIS = 540
SEGMENT_MIN_S = 0.04


@dataclass(frozen=True, slots=True)
class EncodeProfile:
    name: str
    preset: str
    crf: int
    proxy: bool = False
    audio_bitrate: str = "128k"


FINAL_PROFILE = EncodeProfile(name="final", preset="slow", crf=20, proxy=False)
PREVIEW_PROFILE = EncodeProfile(
    name="preview", preset="veryfast", crf=27, proxy=True
)


def proxy_dest_size(aspect: str, short_axis: int = PREVIEW_SHORT_AXIS) -> tuple[int, int]:
    """Even pixel size preserving aspect with ``short_axis`` on the short side."""
    # Medium aspect geometry only — preview proxies stay fixed-size regardless
    # of the project's Low/Medium/High export resolution.
    fw, fh = dest_size(aspect, "medium")
    short_axis = max(64, int(short_axis))
    if fw <= fh:
        dw = even(short_axis)
        dh = even(int(round(short_axis * fh / fw)))
    else:
        dh = even(short_axis)
        dw = even(int(round(short_axis * fw / fh)))
    return max(2, dw), max(2, dh)


def profile_dest_size(
    aspect: str,
    profile: EncodeProfile,
    resolution: str | None = None,
) -> tuple[int, int]:
    if profile.proxy:
        return proxy_dest_size(aspect)
    return dest_size(aspect, resolution)


def _clip_used_range(
    clip: ClipInst, src_dur: float
) -> tuple[float, float, float, float]:
    """Return ``(t0, t1, inn, out)`` on the timeline / in source."""
    inn = max(0.0, float(clip.in_s))
    out = float(clip.out_s) if clip.out_s > inn else (src_dur if src_dur > inn else inn)
    if src_dur > 0:
        out = min(out, src_dur)
    if out <= inn:
        return float(clip.start), float(clip.start), inn, inn
    speed = clip.playback_speed()
    t0 = float(clip.start) + inn
    t1 = t0 + (out - inn) / speed
    return t0, t1, inn, out


def timeline_end(
    video_clips: list[ClipInst],
    src_durs: dict[str, float] | float,
    audio_clips: list[ClipInst] | None = None,
) -> float:
    end = 0.0
    for c in video_clips:
        dur = _dur_lookup(c, src_durs)
        t0, t1, _, _ = _clip_used_range(c, dur)
        end = max(end, t1)
    for c in audio_clips or []:
        dur = _dur_lookup(c, src_durs)
        t0, t1, _, _ = _clip_used_range(c, dur)
        end = max(end, t1)
    return max(end, 0.05)


def _dur_lookup(clip: ClipInst, src_durs: dict[str, float] | float) -> float:
    if isinstance(src_durs, dict):
        if clip.media_id and clip.media_id in src_durs:
            return float(src_durs[clip.media_id] or 0.0)
        if len(src_durs) == 1:
            return float(next(iter(src_durs.values())) or 0.0)
        return 0.0
    return float(src_durs or 0.0)


def cut_time_after_clip(
    video_clips: list[ClipInst],
    index: int,
    src_durs: dict[str, float] | float,
) -> float | None:
    """Timeline time of the selected clip's outgoing edge, or None."""
    if not 0 <= index < len(video_clips):
        return None
    dur = _dur_lookup(video_clips[index], src_durs)
    _t0, t1, inn, out = _clip_used_range(video_clips[index], dur)
    if out <= inn + 0.04:
        return None
    return t1


def has_touching_follower(
    video_clips: list[ClipInst],
    index: int,
    src_durs: dict[str, float] | float,
    *,
    eps: float = 0.04,
) -> bool:
    cut = cut_time_after_clip(video_clips, index, src_durs)
    if cut is None:
        return False
    for j, other in enumerate(video_clips):
        if j == index:
            continue
        dur = _dur_lookup(other, src_durs)
        t0, _t1, inn, out = _clip_used_range(other, dur)
        if out <= inn + 0.04:
            continue
        if abs(t0 - cut) <= eps:
            return True
    return False


def selected_cut_window(
    video_clips: list[ClipInst],
    index: int,
    src_durs: dict[str, float] | float,
    *,
    pad_s: float = PREVIEW_PAD_S,
    audio_clips: list[ClipInst] | None = None,
) -> tuple[float, float, float]:
    """Return ``(window_start, window_end, cut_time)`` clamped to the timeline."""
    cut = cut_time_after_clip(video_clips, index, src_durs)
    if cut is None:
        raise ValueError("selected clip has no usable range")
    if not has_touching_follower(video_clips, index, src_durs):
        raise ValueError("no touching video segment follows the selected clip")
    end = timeline_end(video_clips, src_durs, audio_clips)
    pad = max(0.1, float(pad_s))
    t0 = max(0.0, cut - pad)
    t1 = min(end, cut + pad)
    if t1 <= t0 + 0.1:
        t0 = max(0.0, cut - 0.5)
        t1 = min(end, max(t0 + 0.1, cut + 0.5))
    return t0, t1, cut


def rebase_clips_for_window(
    clips: list[ClipInst],
    window_start: float,
    window_end: float,
    src_durs: dict[str, float] | float,
) -> list[ClipInst]:
    """Keep only the overlap with ``[window_start, window_end)``, shifted to 0.

    Outgoing transitions survive only when the clip's original used end stays
    inside the window (so the real cut is still present).
    """
    t0 = float(window_start)
    t1 = float(window_end)
    out: list[ClipInst] = []
    for c in clips:
        dur = _dur_lookup(c, src_durs)
        used0, used1, _inn, _out = _clip_used_range(c, dur)
        if used1 <= used0 + 0.04:
            continue
        o0 = max(used0, t0)
        o1 = min(used1, t1)
        if o1 <= o0 + 0.04:
            continue
        new_in = o0 - float(c.start)
        new_out = o1 - float(c.start)
        new_start = float(c.start) - t0
        ttype, tdur = normalize_transition(c.transition, c.transition_s)
        # Drop the outgoing transition if we truncated the clip's end.
        if abs(o1 - used1) > 0.02:
            ttype, tdur = TRANSITION_NONE, 0.0
        out.append(
            ClipInst(
                start=new_start,
                in_s=new_in,
                out_s=new_out,
                media_id=c.media_id,
                transform_x=c.transform_x,
                transform_y=c.transform_y,
                scale=c.scale,
                track=c.track,
                transition=ttype,
                transition_s=tdur,
                speed=c.playback_speed(),
            )
        )
    return out


def render_fingerprint(
    *,
    aspect: str,
    pan_x: float,
    pan_y: float,
    audio_follows_in: bool,
    use_video_soundtrack: bool,
    audio_offset: float,
    video_clips: list[ClipInst],
    audio_clips: list[ClipInst],
    media: list[MediaItem],
    kind: str,
    window: tuple[float, float] | None = None,
) -> str:
    """Stable hash of every field that changes the rendered pixels/audio."""
    media_rows = []
    for m in media:
        try:
            path = str(m.path.resolve())
        except OSError:
            path = str(m.path)
        media_rows.append({"id": m.id, "kind": m.kind, "path": path})
    payload: dict[str, Any] = {
        "aspect": aspect,
        "pan_x": round(float(pan_x), 6),
        "pan_y": round(float(pan_y), 6),
        "audio_follows_in": bool(audio_follows_in),
        "use_video_soundtrack": bool(use_video_soundtrack),
        "audio_offset": round(float(audio_offset), 6),
        "kind": kind,
        "window": None
        if window is None
        else [round(float(window[0]), 6), round(float(window[1]), 6)],
        "media": media_rows,
        "video_clips": [
            {
                "start": round(float(c.start), 6),
                "in_s": round(float(c.in_s), 6),
                "out_s": round(float(c.out_s), 6),
                "media_id": c.media_id,
                "transform_x": round(float(c.transform_x), 6),
                "transform_y": round(float(c.transform_y), 6),
                "scale": round(float(c.scale), 6),
                "track": int(c.track),
                "transition": c.transition,
                "transition_s": round(float(c.transition_s), 6),
                "speed": round(float(c.playback_speed()), 6),
            }
            for c in video_clips
        ],
        "audio_clips": [
            {
                "start": round(float(c.start), 6),
                "in_s": round(float(c.in_s), 6),
                "out_s": round(float(c.out_s), 6),
                "media_id": c.media_id,
                "track": int(c.track),
                "speed": round(float(c.playback_speed()), 6),
            }
            for c in audio_clips
        ],
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TimelineSegment:
    """One non-overlapping timeline span for the render-cache bar (#532)."""

    t0: float
    t1: float
    render_t0: float
    render_t1: float
    fingerprint: str

    @property
    def path(self) -> Path:
        return preview_out_path(self.fingerprint, "seg")

    def is_green(self) -> bool:
        path = self.path
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    def file_offset(self, timeline_t: float) -> float:
        """Seconds into the cached MP4 for a timeline time (file starts at render_t0)."""
        return max(0.0, float(timeline_t) - float(self.render_t0))


def _unique_times(values: list[float], *, eps: float = 0.001) -> list[float]:
    if not values:
        return []
    ordered = sorted(float(v) for v in values)
    out = [ordered[0]]
    for t in ordered[1:]:
        if t - out[-1] > eps:
            out.append(t)
    return out


def clips_touching_window(
    clips: list[ClipInst],
    t0: float,
    t1: float,
    src_durs: dict[str, float] | float,
    *,
    eps: float = 0.001,
) -> list[ClipInst]:
    """Clips whose used range overlaps ``[t0, t1)``."""
    a, b = float(t0), float(t1)
    hit: list[ClipInst] = []
    for c in clips:
        u0, u1, inn, out = _clip_used_range(c, _dur_lookup(c, src_durs))
        if out <= inn + SEGMENT_MIN_S:
            continue
        if u1 > a + eps and u0 < b - eps:
            hit.append(c)
    return hit


def _cut_has_transition(
    video_clips: list[ClipInst],
    cut_t: float,
    src_durs: dict[str, float] | float,
    *,
    eps: float = 0.04,
) -> bool:
    """True if a clip ending at *cut_t* has an outgoing transition into a follower."""
    for i, c in enumerate(video_clips):
        _u0, u1, inn, out = _clip_used_range(c, _dur_lookup(c, src_durs))
        if out <= inn + SEGMENT_MIN_S:
            continue
        if abs(u1 - cut_t) > eps:
            continue
        ttype, _tdur = normalize_transition(c.transition, c.transition_s)
        if ttype == TRANSITION_NONE:
            continue
        if has_touching_follower(video_clips, i, src_durs, eps=eps):
            return True
    return False


def _render_pad_for_edge(
    video_clips: list[ClipInst],
    edge_t: float,
    src_durs: dict[str, float] | float,
) -> float:
    return PREVIEW_PAD_S if _cut_has_transition(video_clips, edge_t, src_durs) else 0.0


def build_timeline_segments(
    *,
    video_clips: list[ClipInst],
    audio_clips: list[ClipInst],
    src_durs: dict[str, float] | float,
    aspect: str,
    pan_x: float,
    pan_y: float,
    audio_follows_in: bool,
    use_video_soundtrack: bool,
    audio_offset: float,
    media: list[MediaItem],
) -> list[TimelineSegment]:
    """Cover used video ranges with fingerprintable segments.

    Gaps with no video stay off the bar (gray). Render windows grow by
    ``PREVIEW_PAD_S`` only at cuts that have an outgoing transition.
    """
    times: list[float] = []
    used: list[tuple[float, float]] = []
    for c in video_clips:
        u0, u1, inn, out = _clip_used_range(c, _dur_lookup(c, src_durs))
        if out <= inn + SEGMENT_MIN_S or u1 <= u0 + SEGMENT_MIN_S:
            continue
        used.append((u0, u1))
        times.extend((u0, u1))
    if not used:
        return []
    bounds = _unique_times(times)
    end = timeline_end(video_clips, src_durs, audio_clips)
    segs: list[TimelineSegment] = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a < SEGMENT_MIN_S:
            continue
        if not any(u0 < b - 0.001 and u1 > a + 0.001 for u0, u1 in used):
            continue
        pad0 = _render_pad_for_edge(video_clips, a, src_durs)
        pad1 = _render_pad_for_edge(video_clips, b, src_durs)
        render_t0 = max(0.0, a - pad0)
        render_t1 = min(end, b + pad1)
        if render_t1 <= render_t0 + SEGMENT_MIN_S:
            render_t1 = min(end, render_t0 + SEGMENT_MIN_S + 0.1)
        v_touch = clips_touching_window(video_clips, render_t0, render_t1, src_durs)
        a_touch = clips_touching_window(audio_clips, render_t0, render_t1, src_durs)
        fp = render_fingerprint(
            aspect=aspect,
            pan_x=pan_x,
            pan_y=pan_y,
            audio_follows_in=audio_follows_in,
            use_video_soundtrack=use_video_soundtrack,
            audio_offset=audio_offset,
            video_clips=v_touch,
            audio_clips=a_touch,
            media=media,
            kind="seg",
            window=(render_t0, render_t1),
        )
        segs.append(
            TimelineSegment(
                t0=a,
                t1=b,
                render_t0=render_t0,
                render_t1=render_t1,
                fingerprint=fp,
            )
        )
    return segs


def segment_at(
    timeline_t: float, segments: list[TimelineSegment]
) -> TimelineSegment | None:
    t = float(timeline_t)
    for seg in segments:
        if seg.t0 - 0.001 <= t < seg.t1 - 0.001:
            return seg
        if abs(t - seg.t1) <= 0.02 and seg is segments[-1]:
            return seg
    return None


def playback_source(
    timeline_t: float, segments: list[TimelineSegment]
) -> str:
    """``cache`` if the playhead sits in a green segment, else ``edit``."""
    seg = segment_at(timeline_t, segments)
    if seg is not None and seg.is_green():
        return "cache"
    return "edit"


def dirty_segments(segments: list[TimelineSegment]) -> list[TimelineSegment]:
    return [s for s in segments if not s.is_green()]


def preview_cache_dir() -> Path:
    PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return PREVIEW_CACHE_DIR


def preview_out_path(fingerprint: str, kind: str) -> Path:
    safe_kind = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in kind)[:32]
    return preview_cache_dir() / f"{fingerprint[:20]}_{safe_kind}.mp4"


def assert_preview_path_safe(path: Path) -> None:
    """Refuse to write a preview into Eagle intake."""
    resolved = path.expanduser().resolve()
    cache = preview_cache_dir().resolve()
    try:
        resolved.relative_to(cache)
    except ValueError as exc:
        raise ValueError(f"preview path escapes cache dir: {resolved}") from exc
    try:
        intake = inbox_dir().resolve()
    except OSError:
        return
    try:
        resolved.relative_to(intake)
    except ValueError:
        return
    raise ValueError(f"preview path is under intake: {resolved}")


def cleanup_preview_cache(
    *, keep: int = PREVIEW_CACHE_KEEP, max_age_s: float = 7 * 24 * 3600
) -> list[Path]:
    """Delete old previews; keep the newest ``keep`` mp4s. Returns removed paths."""
    folder = preview_cache_dir()
    files = [p for p in folder.glob("*.mp4") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed: list[Path] = []
    now = time.time()
    for i, path in enumerate(files):
        stale_age = (now - path.stat().st_mtime) > max_age_s
        over_keep = i >= keep
        if not (stale_age or over_keep):
            continue
        try:
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".tmp").unlink(missing_ok=True)
            removed.append(path)
        except OSError:
            continue
    # Stray temps
    for tmp in folder.glob("*.mp4.tmp"):
        try:
            if now - tmp.stat().st_mtime > 3600:
                tmp.unlink(missing_ok=True)
                removed.append(tmp)
        except OSError:
            continue
    return removed


def extract_xfade_signature(filter_complex: str) -> list[tuple[str, str]]:
    """``(transition_name, duration)`` pairs from an ffmpeg filter graph."""
    import re

    return re.findall(
        r"xfade=transition=([A-Za-z0-9_]+):duration=([0-9.]+)",
        filter_complex or "",
    )


def extract_acrossfade_signature(filter_complex: str) -> list[str]:
    import re

    return re.findall(r"acrossfade=d=([0-9.]+)", filter_complex or "")


# Actions that mutate the project or timeline while compiled preview is active.
COMPILED_BLOCKED_ACTIONS = frozenset(
    {
        "trim",
        "move",
        "split",
        "delete",
        "duplicate",
        "track",
        "transform",
        "speed",
        "transition",
        "audio_route",
        "media_place",
        "undo",
        "redo",
        "pan",
        "aspect",
        "fit",
        "follow_in",
        "clear_audio",
        "set_in_out",
        "drop_media",
        "select_rebind",
    }
)

# Transport / non-mutating actions that stay available in compiled preview.
COMPILED_ALLOWED_ACTIONS = frozenset(
    {
        "seek",
        "play_pause",
        "back_to_edit",
        "save",
        "export",
        "cancel_preview",
        "open_project",
        "new_project",
    }
)


def compiled_allows_action(action: str, *, compiled_mode: bool) -> bool:
    """Return whether ``action`` may run given compiled-preview mode."""
    if not compiled_mode:
        return True
    if action in COMPILED_ALLOWED_ACTIONS:
        return True
    return action not in COMPILED_BLOCKED_ACTIONS


def compiled_playhead_seconds(
    *,
    playing: bool,
    duration: float,
    media_timestamp_us: int | None,
    play_t0: float,
    play_mono: float,
    now_mono: float,
    paused_playhead: float,
) -> float:
    """Timeline time for compiled preview; prefer MediaFile clock when playing."""
    duration = max(0.05, float(duration))
    if playing and media_timestamp_us is not None and media_timestamp_us >= 0:
        return min(max(0.0, media_timestamp_us / 1_000_000.0), duration)
    if playing:
        return min(max(0.0, float(play_t0) + (now_mono - play_mono)), duration)
    return min(max(0.0, float(paused_playhead)), duration)
