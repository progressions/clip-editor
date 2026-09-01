"""Clip editor project files (``.clip.json``).

JSON document. Version 1 records the edit, not the rendered MP4.

Paths are stored absolute, plus relative to the project file when that
works, so a folder of media + ``name.clip.json`` can move together.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = "clip-editor-project"
VERSION = 6
SUFFIX = ".clip.json"
STATE_DIR = Path.home() / ".local" / "state" / "clip-editor"
AUTOSAVE_PATH = STATE_DIR / "autosave.clip.json"

TRANSITION_NONE = "none"
TRANSITION_DISSOLVE = "dissolve"
TRANSITION_WHITE_FLASH = "white_flash"
TRANSITION_TYPES = (TRANSITION_NONE, TRANSITION_DISSOLVE, TRANSITION_WHITE_FLASH)
DEFAULT_TRANSITION_S = {
    TRANSITION_DISSOLVE: 0.5,
    TRANSITION_WHITE_FLASH: 0.25,
}


class ProjectError(RuntimeError):
    pass


@dataclass
class MediaItem:
    """A source file in the media bin."""

    id: str
    path: Path
    kind: str  # "video" | "audio"

    def copy(self) -> MediaItem:
        return MediaItem(id=self.id, path=self.path, kind=self.kind)


def next_media_id(items: list[MediaItem]) -> str:
    used = {m.id for m in items}
    n = 1
    while f"m{n}" in used:
        n += 1
    return f"m{n}"


def normalize_transition(type_s: object, duration_s: object = 0.0) -> tuple[str, float]:
    """Return a valid ``(transition, transition_s)`` pair."""
    raw = str(type_s or TRANSITION_NONE).strip().lower().replace("-", "_")
    if raw in ("fade", "crossfade", "cross_fade"):
        raw = TRANSITION_DISSOLVE
    if raw in ("fadewhite", "whiteflash", "flash_white"):
        raw = TRANSITION_WHITE_FLASH
    if raw not in TRANSITION_TYPES:
        raw = TRANSITION_NONE
    try:
        dur = float(duration_s or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    if raw == TRANSITION_NONE:
        return TRANSITION_NONE, 0.0
    if dur <= 0.0:
        dur = DEFAULT_TRANSITION_S.get(raw, 0.5)
    return raw, max(0.1, min(3.0, dur))


@dataclass
class ClipInst:
    """One instance of a bin item on the timeline."""

    start: float = 0.0
    in_s: float = 0.0
    out_s: float = 0.0
    media_id: str = ""
    transform_x: float = 0.0
    transform_y: float = 0.0
    scale: float = 1.0
    track: int = 1
    # Outgoing cut into the next touching flattened video segment.
    transition: str = TRANSITION_NONE
    transition_s: float = 0.0

    def used(self) -> tuple[float, float]:
        inn = max(0.0, float(self.in_s))
        out = float(self.out_s) if self.out_s > inn else inn
        return inn, out

    def used_times(self) -> tuple[float, float]:
        inn, out = self.used()
        return self.start + inn, self.start + out

    def copy(self) -> ClipInst:
        return ClipInst(
            start=self.start,
            in_s=self.in_s,
            out_s=self.out_s,
            media_id=self.media_id,
            transform_x=self.transform_x,
            transform_y=self.transform_y,
            scale=self.scale,
            track=self.track,
            transition=self.transition,
            transition_s=self.transition_s,
        )

    def split_at(
        self, timeline_t: float, src_dur: float = 0.0, *, min_len: float = 0.05
    ) -> ClipInst | None:
        """Trim this instance to the left of ``timeline_t``; return the right piece.

        Same source file, same ``start``. None if the playhead is not far
        enough inside the used range. The outgoing transition moves to the
        right piece; the new internal cut is hard.
        """
        inn = max(0.0, float(self.in_s))
        out = float(self.out_s) if self.out_s > inn else inn
        if src_dur > 0:
            if out <= inn:
                out = src_dur
            out = min(out, src_dur)
        t0 = float(self.start) + inn
        t1 = float(self.start) + out
        t = float(timeline_t)
        if t <= t0 + min_len or t >= t1 - min_len:
            return None
        src_cut = t - float(self.start)
        right = ClipInst(
            start=self.start,
            in_s=src_cut,
            out_s=out,
            media_id=self.media_id,
            transform_x=self.transform_x,
            transform_y=self.transform_y,
            scale=self.scale,
            track=self.track,
            transition=self.transition,
            transition_s=self.transition_s,
        )
        self.out_s = src_cut
        self.transition = TRANSITION_NONE
        self.transition_s = 0.0
        return right


def clip_to_dict(c: ClipInst) -> dict:
    d = {"start": float(c.start), "in_s": float(c.in_s), "out_s": float(c.out_s)}
    if c.media_id:
        d["media_id"] = c.media_id
    if abs(float(c.transform_x)) > 0.0001:
        d["transform_x"] = float(c.transform_x)
    if abs(float(c.transform_y)) > 0.0001:
        d["transform_y"] = float(c.transform_y)
    if abs(float(c.scale) - 1.0) > 0.0001:
        d["scale"] = float(c.scale)
    if int(c.track) != 1:
        d["track"] = max(1, min(2, int(c.track)))
    ttype, tdur = normalize_transition(c.transition, c.transition_s)
    if ttype != TRANSITION_NONE:
        d["transition"] = ttype
        d["transition_s"] = tdur
    return d


def clip_from_dict(data: object) -> ClipInst | None:
    if not isinstance(data, dict):
        return None
    try:
        start = float(data.get("start") or 0.0)
        inn = max(0.0, float(data.get("in_s") or 0.0))
        out = float(data.get("out_s") or 0.0)
        transform_x = float(data.get("transform_x") or 0.0)
        transform_y = float(data.get("transform_y") or 0.0)
        scale = max(0.05, float(data.get("scale") or 1.0))
        track = max(1, min(2, int(data.get("track") or 1)))
    except (TypeError, ValueError):
        return None
    mid = str(data.get("media_id") or "")
    ttype, tdur = normalize_transition(data.get("transition"), data.get("transition_s"))
    return ClipInst(
        start=start,
        in_s=inn,
        out_s=out,
        media_id=mid,
        transform_x=transform_x,
        transform_y=transform_y,
        scale=scale,
        track=track,
        transition=ttype,
        transition_s=tdur,
    )


def apply_legacy_crossfade(clips: list[ClipInst], crossfade_s: float) -> None:
    """Stamp a project-wide crossfade onto every clip as an outgoing dissolve."""
    requested = max(0.0, float(crossfade_s or 0.0))
    if requested <= 0.001:
        return
    ttype, tdur = normalize_transition(TRANSITION_DISSOLVE, requested)
    for c in clips:
        if c.transition == TRANSITION_NONE:
            c.transition = ttype
            c.transition_s = tdur


def _clips_from_data(
    data: dict,
    key: str,
    *,
    legacy_start: float,
    legacy_in: float,
    legacy_out: float | None,
    present: bool,
) -> list[ClipInst]:
    raw = data.get(key)
    if isinstance(raw, list) and raw:
        clips = [c for c in (clip_from_dict(item) for item in raw) if c is not None]
        if clips:
            return clips
    if not present:
        return []
    return [ClipInst(start=legacy_start, in_s=legacy_in, out_s=float(legacy_out or 0.0))]


@dataclass
class Project:
    video: Path | None = None
    audio: Path | None = None
    aspect: str = "9:16"
    resolution: str = "medium"
    pan_x: float = 0.5
    pan_y: float = 0.5
    in_s: float = 0.0
    out_s: float | None = None
    video_start: float = 0.0
    audio_start: float = 0.0
    audio_in: float = 0.0
    audio_out: float | None = None
    audio_follows_in: bool = False
    audio_fit: bool = False
    use_video_soundtrack: bool = True
    crossfade_s: float = 0.0
    media: list[MediaItem] = field(default_factory=list)
    video_clips: list[ClipInst] = field(default_factory=list)
    audio_clips: list[ClipInst] = field(default_factory=list)
    path: Path | None = None


def _rel(media: Path | None, origin: Path | None) -> str | None:
    if media is None or origin is None:
        return None
    try:
        return str(media.resolve().relative_to(origin.parent.resolve()))
    except ValueError:
        return None


def _resolve(abs_s: str | None, rel_s: str | None, origin: Path | None) -> Path | None:
    """Resolve a media path.

    Prefer a relative path that exists beside the project file. Otherwise keep
    the absolute path (even when missing) so callers can report it. If only a
    relative path is stored and the file is gone, still return that candidate
    so load validation can name it instead of silently dropping the media row.
    """
    rel_cand: Path | None = None
    if rel_s and origin is not None:
        cand = (origin.parent / rel_s).expanduser()
        try:
            cand = cand.resolve()
        except OSError:
            pass
        if cand.is_file():
            return cand
        rel_cand = cand
    if abs_s:
        p = Path(str(abs_s)).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        return p
    return rel_cand


def ensure_suffix(path: Path) -> Path:
    name = path.name
    if name.endswith(SUFFIX):
        return path
    if name.endswith(".json"):
        return path.with_name(path.stem + SUFFIX)
    return path.with_name(name + SUFFIX)


def _primary(media: list[MediaItem], kind: str) -> Path | None:
    for m in media:
        if m.kind == kind:
            return m.path
    return None


def _media_from_data(data: dict, origin: Path | None) -> list[MediaItem]:
    raw = data.get("media")
    items: list[MediaItem] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("kind") or "")
            if kind not in ("video", "audio"):
                continue
            path = _resolve(row.get("path"), row.get("path_rel"), origin)
            if path is None:
                continue
            mid = str(row.get("id") or next_media_id(items))
            if mid in seen:
                mid = next_media_id(items)
            seen.add(mid)
            items.append(MediaItem(id=mid, path=path, kind=kind))
    return items


def _ensure_media(
    items: list[MediaItem], video: Path | None, audio: Path | None
) -> list[MediaItem]:
    out = list(items)

    def has(path: Path, kind: str) -> bool:
        for m in out:
            if m.kind != kind:
                continue
            try:
                if m.path.resolve() == path.resolve():
                    return True
            except OSError:
                if m.path == path:
                    return True
        return False

    if video is not None and not has(video, "video"):
        out.append(MediaItem(id=next_media_id(out), path=video, kind="video"))
    if audio is not None and not has(audio, "audio"):
        out.append(MediaItem(id=next_media_id(out), path=audio, kind="audio"))
    return out


def _bind_clip_media(clips: list[ClipInst], media: list[MediaItem], kind: str) -> None:
    """Attach clips to media ids.

    Empty ``media_id`` (legacy single-file projects) binds to the first media
    item of ``kind``. A non-empty id that is not in the bin is left alone so
    load validation can reject it instead of silently swapping to another file.
    """
    fallback = next((m.id for m in media if m.kind == kind), "")
    ids = {m.id for m in media if m.kind == kind}
    for c in clips:
        if c.media_id in ids:
            continue
        if not c.media_id and fallback:
            c.media_id = fallback


def to_dict(proj: Project) -> dict:
    origin = proj.path
    media = _ensure_media(proj.media, proj.video, proj.audio)
    video = _primary(media, "video") or (proj.video.resolve() if proj.video else None)
    audio = _primary(media, "audio") or (proj.audio.resolve() if proj.audio else None)
    if video is not None:
        try:
            video = video.resolve()
        except OSError:
            pass
    if audio is not None:
        try:
            audio = audio.resolve()
        except OSError:
            pass
    saved = None
    if proj.path is not None:
        try:
            if proj.path.resolve() != AUTOSAVE_PATH.resolve():
                saved = str(proj.path.resolve())
        except OSError:
            saved = str(proj.path)
    media_rows = []
    for m in media:
        try:
            p = m.path.resolve()
        except OSError:
            p = m.path
        media_rows.append(
            {
                "id": m.id,
                "kind": m.kind,
                "path": str(p),
                "path_rel": _rel(p, origin),
            }
        )
    return {
        "format": FORMAT,
        "version": VERSION,
        "aspect": proj.aspect,
        "resolution": proj.resolution,
        "pan_x": float(proj.pan_x),
        "pan_y": float(proj.pan_y),
        "in_s": float(proj.in_s),
        "out_s": None if proj.out_s is None else float(proj.out_s),
        "video_start": float(proj.video_start),
        "audio_start": float(proj.audio_start),
        "audio_in": float(proj.audio_in),
        "audio_out": None if proj.audio_out is None else float(proj.audio_out),
        "audio_follows_in": bool(proj.audio_follows_in),
        "audio_fit": bool(proj.audio_fit),
        "use_video_soundtrack": bool(proj.use_video_soundtrack),
        # Deprecated project-wide crossfade; per-clip transition fields are source of truth.
        "crossfade_s": 0.0,
        "media": media_rows,
        "video_clips": [clip_to_dict(c) for c in proj.video_clips],
        "audio_clips": [clip_to_dict(c) for c in proj.audio_clips],
        "video": str(video) if video else None,
        "video_rel": _rel(video, origin),
        "audio": str(audio) if audio else None,
        "audio_rel": _rel(audio, origin),
        "saved_as": saved,
    }


def from_dict(data: dict, *, origin: Path | None = None) -> Project:
    if not isinstance(data, dict):
        raise ProjectError("project file is not a JSON object")
    fmt = data.get("format")
    if fmt is not None and fmt != FORMAT:
        raise ProjectError(f"not a clip-editor project ({fmt!r})")
    version = int(data.get("version") or 1)
    if version > VERSION:
        raise ProjectError(f"project version {version} is newer than this app")
    video = _resolve(data.get("video"), data.get("video_rel"), origin)
    audio = _resolve(data.get("audio"), data.get("audio_rel"), origin)
    media = _ensure_media(_media_from_data(data, origin), video, audio)
    if video is None:
        video = _primary(media, "video")
    if audio is None:
        audio = _primary(media, "audio")
    out_raw = data.get("out_s")
    saved_raw = data.get("saved_as")
    saved_as = Path(str(saved_raw)).expanduser() if saved_raw else None
    path = origin
    if saved_as is not None:
        path = saved_as
    elif origin is not None:
        try:
            if origin.resolve() == AUTOSAVE_PATH.resolve():
                path = None
        except OSError:
            pass
    raw_res = str(data.get("resolution") or "medium").strip().lower()
    if raw_res not in ("low", "medium", "high"):
        raw_res = "medium"
    proj = Project(
        video=video,
        audio=audio,
        aspect=str(data.get("aspect") or "9:16"),
        resolution=raw_res,
        pan_x=float(data.get("pan_x") if data.get("pan_x") is not None else 0.5),
        pan_y=float(data.get("pan_y") if data.get("pan_y") is not None else 0.5),
        in_s=float(data.get("in_s") or 0.0),
        out_s=None if out_raw is None or out_raw == "" else float(out_raw),
        video_start=float(data.get("video_start") or 0.0),
        audio_start=(
            float(data["audio_start"])
            if data.get("audio_start") is not None
            else (
                float(data.get("video_start") or 0.0)
                if data.get("audio_follows_in")
                else float(data.get("in_s") or 0.0)
            )
        ),
        audio_in=max(0.0, float(data.get("audio_in") or 0.0)),
        audio_out=(
            None
            if data.get("audio_out") is None or data.get("audio_out") == ""
            else float(data.get("audio_out"))
        ),
        audio_follows_in=bool(data.get("audio_follows_in") or False),
        audio_fit=bool(data.get("audio_fit") or False),
        use_video_soundtrack=(
            True
            if data.get("use_video_soundtrack") is None
            else bool(data.get("use_video_soundtrack"))
        ),
        crossfade_s=0.0,
        media=media,
        video_clips=_clips_from_data(
            data,
            "video_clips",
            legacy_start=float(data.get("video_start") or 0.0),
            legacy_in=float(data.get("in_s") or 0.0),
            legacy_out=None if out_raw is None or out_raw == "" else float(out_raw),
            present=video is not None,
        ),
        audio_clips=_clips_from_data(
            data,
            "audio_clips",
            legacy_start=(
                float(data["audio_start"])
                if data.get("audio_start") is not None
                else (
                    float(data.get("video_start") or 0.0)
                    if data.get("audio_follows_in")
                    else float(data.get("in_s") or 0.0)
                )
            ),
            legacy_in=max(0.0, float(data.get("audio_in") or 0.0)),
            legacy_out=(
                None
                if data.get("audio_out") is None or data.get("audio_out") == ""
                else float(data.get("audio_out"))
            ),
            present=audio is not None,
        ),
        path=path,
    )
    _bind_clip_media(proj.video_clips, media, "video")
    _bind_clip_media(proj.audio_clips, media, "audio")
    legacy_xf = max(0.0, float(data.get("crossfade_s") or 0.0))
    # Pre-v6 projects used one project-wide crossfade. Stamp dissolves onto clips
    # when none already carry a transition field.
    if version < 6 and legacy_xf > 0.001:
        apply_legacy_crossfade(proj.video_clips, legacy_xf)
    return proj


def media_load_errors(proj: Project) -> list[str]:
    """Return unresolved-media warnings for ``proj``."""
    errors: list[str] = []
    by_id = {m.id: m for m in proj.media}
    for m in proj.media:
        if not m.path.is_file():
            errors.append(f"missing media {m.id}: {m.path}")
    for label, clips, kind in (
        ("video", proj.video_clips, "video"),
        ("audio", proj.audio_clips, "audio"),
    ):
        for i, c in enumerate(clips):
            mid = c.media_id
            if not mid:
                errors.append(f"{label} clip {i + 1} has no media_id")
                continue
            item = by_id.get(mid)
            if item is None:
                errors.append(
                    f"{label} clip {i + 1} references unknown media {mid!r}"
                )
            elif item.kind != kind:
                errors.append(
                    f"{label} clip {i + 1} references {item.kind} media {mid!r}"
                )
    return errors


def ensure_project_loadable(proj: Project) -> None:
    """Raise ``ProjectError`` when media/clip bindings are not safe to open."""
    errors = media_load_errors(proj)
    if errors:
        raise ProjectError("; ".join(errors))


def read_project(path: Path) -> Project:
    path = Path(path).expanduser()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectError(f"could not read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProjectError(f"invalid JSON in {path.name}: {exc}") from exc
    return from_dict(data, origin=path)


def _atomic_write(path: Path, proj: Project) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(to_dict(proj), indent=2) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, path)
    return path


def write_project(path: Path, proj: Project) -> Path:
    path = ensure_suffix(Path(path).expanduser())
    proj.path = path
    return _atomic_write(path, proj)


def write_autosave(proj: Project) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _atomic_write(AUTOSAVE_PATH, proj)


def read_autosave() -> Project | None:
    if not AUTOSAVE_PATH.is_file():
        return None
    try:
        return read_project(AUTOSAVE_PATH)
    except ProjectError:
        return None


def clear_autosave() -> None:
    try:
        AUTOSAVE_PATH.unlink(missing_ok=True)
    except OSError:
        try:
            _atomic_write(AUTOSAVE_PATH, Project())
        except OSError:
            pass
