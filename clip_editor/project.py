"""Clip editor project files (``.clip.json``).

JSON document. Version 1 records the edit, not the rendered MP4.

Paths are stored absolute, plus relative to the project file when that
works, so a folder of media + ``name.clip.json`` can move together.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

FORMAT = "clip-editor-project"
VERSION = 1
SUFFIX = ".clip.json"
STATE_DIR = Path.home() / ".local" / "state" / "clip-editor"
AUTOSAVE_PATH = STATE_DIR / "autosave.clip.json"


class ProjectError(RuntimeError):
    pass


@dataclass
class Project:
    video: Path | None = None
    audio: Path | None = None
    aspect: str = "9:16"
    pan_x: float = 0.5
    pan_y: float = 0.5
    in_s: float = 0.0
    out_s: float | None = None
    audio_follows_in: bool = False
    audio_fit: bool = False
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


def to_dict(proj: Project) -> dict:
    origin = proj.path
    video = proj.video.resolve() if proj.video else None
    audio = proj.audio.resolve() if proj.audio else None
    saved = None
    if proj.path is not None:
        try:
            if proj.path.resolve() != AUTOSAVE_PATH.resolve():
                saved = str(proj.path.resolve())
        except OSError:
            saved = str(proj.path)
    return {
        "format": FORMAT,
        "version": VERSION,
        "aspect": proj.aspect,
        "pan_x": float(proj.pan_x),
        "pan_y": float(proj.pan_y),
        "in_s": float(proj.in_s),
        "out_s": None if proj.out_s is None else float(proj.out_s),
        "audio_follows_in": bool(proj.audio_follows_in),
        "audio_fit": bool(proj.audio_fit),
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
    return Project(
        video=video,
        audio=audio,
        aspect=str(data.get("aspect") or "9:16"),
        pan_x=float(data.get("pan_x") if data.get("pan_x") is not None else 0.5),
        pan_y=float(data.get("pan_y") if data.get("pan_y") is not None else 0.5),
        in_s=float(data.get("in_s") or 0.0),
        out_s=None if out_raw is None or out_raw == "" else float(out_raw),
        audio_follows_in=bool(data.get("audio_follows_in") or False),
        audio_fit=bool(data.get("audio_fit") or False),
        path=path,
    )


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
