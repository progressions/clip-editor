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
VERSION = 3
SUFFIX = ".clip.json"
STATE_DIR = Path.home() / ".local" / "state" / "clip-editor"
AUTOSAVE_PATH = STATE_DIR / "autosave.clip.json"


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


@dataclass
class ClipInst:
    """One instance of a bin item on the timeline."""

    start: float = 0.0
    in_s: float = 0.0
    out_s: float = 0.0
    media_id: str = ""

    def used(self) -> tuple[float, float]:
        inn = max(0.0, float(self.in_s))
        out = float(self.out_s) if self.out_s > inn else inn
        return inn, out

    def used_times(self) -> tuple[float, float]:
        inn, out = self.used()
        return self.start + inn, self.start + out

    def copy(self) -> ClipInst:
        return ClipInst(
            start=self.start, in_s=self.in_s, out_s=self.out_s, media_id=self.media_id
        )

    def split_at(
        self, timeline_t: float, src_dur: float = 0.0, *, min_len: float = 0.05
    ) -> ClipInst | None:
        """Trim this instance to the left of ``timeline_t``; return the right piece.

        Same source file, same ``start``. None if the playhead is not far
        enough inside the used range.
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
            start=self.start, in_s=src_cut, out_s=out, media_id=self.media_id
        )
        self.out_s = src_cut
        return right


def clip_to_dict(c: ClipInst) -> dict:
    d = {"start": float(c.start), "in_s": float(c.in_s), "out_s": float(c.out_s)}
    if c.media_id:
        d["media_id"] = c.media_id
    return d


def clip_from_dict(data: object) -> ClipInst | None:
    if not isinstance(data, dict):
        return None
    try:
        start = float(data.get("start") or 0.0)
        inn = max(0.0, float(data.get("in_s") or 0.0))
        out = float(data.get("out_s") or 0.0)
    except (TypeError, ValueError):
        return None
    mid = str(data.get("media_id") or "")
    return ClipInst(start=start, in_s=inn, out_s=out, media_id=mid)


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
    if rel_s and origin is not None:
        cand = (origin.parent / rel_s).expanduser()
        try:
            cand = cand.resolve()
        except OSError:
            cand = cand
        if cand.is_file():
            return cand
    if abs_s:
        p = Path(abs_s).expanduser()
        try:
            p = p.resolve()
        except OSError:
            pass
        return p
    return None


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
    fallback = next((m.id for m in media if m.kind == kind), "")
    ids = {m.id for m in media if m.kind == kind}
    for c in clips:
        if c.media_id not in ids:
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
        "crossfade_s": max(0.0, float(proj.crossfade_s)),
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
    proj = Project(
        video=video,
        audio=audio,
        aspect=str(data.get("aspect") or "9:16"),
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
        crossfade_s=max(0.0, float(data.get("crossfade_s") or 0.0)),
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
    return proj


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
