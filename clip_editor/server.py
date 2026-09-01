"""Local HTTP UI. Bind 127.0.0.1 only. One project at a time."""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from clip_editor import __version__
from clip_editor.aspects import (
    ASPECTS,
    DEFAULT_RESOLUTION,
    RESOLUTIONS,
    cover_crop,
    dest_size,
)
from clip_editor.export import ExportError, default_out_path, run_export
from clip_editor.probe import ProbeError, probe, which_ffmpeg

STATIC_DIR = Path(__file__).resolve().parent / "static"
CACHE_DIR = Path.home() / ".cache" / "clip-editor"
UPLOAD_DIR = CACHE_DIR / "uploads"
STATE_LOCK = threading.Lock()


class Project:
    def __init__(self) -> None:
        self.video: Path | None = None
        self.audio: Path | None = None
        self.video_info: dict[str, Any] | None = None
        self.audio_info: dict[str, Any] | None = None
        self.video_original_name: str | None = None
        self.export: dict[str, Any] = {
            "state": "idle",
            "percent": 0.0,
            "error": None,
            "out": None,
        }


PROJECT = Project()


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, indent=2).encode("utf-8")


def _sanitize_filename(name: str) -> str:
    base = Path(name or "upload.bin").name
    base = base.replace("\x00", "")
    if not base or base in (".", ".."):
        return "upload.bin"
    return base


def _update_poster(path: Path) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / "poster.jpg"
    try:
        subprocess.check_call(
            [
                which_ffmpeg(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                "0",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-q:v",
                "4",
                str(dest),
            ],
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ProbeError):
        dest.unlink(missing_ok=True)


def _export_dir(video: Path) -> Path:
    from clip_editor.eagle import inbox_dir

    return inbox_dir()


def _export_path(video: Path, aspect: str) -> Path:
    return default_out_path(
        video,
        aspect,
        original_name=PROJECT.video_original_name,
        dest_dir=_export_dir(video),
    )


def _set_media(role: str, path: Path, original_name: str | None = None) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ProbeError(f"file not found: {path}")
    info = probe(path)
    with STATE_LOCK:
        if role == "video":
            if not info.get("has_video"):
                raise ProbeError("that file has no video stream")
            PROJECT.video = path
            PROJECT.video_info = info
            PROJECT.video_original_name = _sanitize_filename(
                original_name or path.name
            )
        elif role == "audio":
            if not info.get("has_audio"):
                raise ProbeError("that file has no audio stream")
            PROJECT.audio = path
            PROJECT.audio_info = info
        else:
            raise ProbeError(f"role must be video or audio, not {role!r}")
    if role == "video":
        _update_poster(path)
    return info


def _project_payload() -> dict[str, Any]:
    with STATE_LOCK:
        video = PROJECT.video_info
        audio = PROJECT.audio_info
        export = dict(PROJECT.export)
        vpath = str(PROJECT.video) if PROJECT.video else None
        apath = str(PROJECT.audio) if PROJECT.audio else None
    suggested = {}
    if PROJECT.video:
        for key in ASPECTS:
            suggested[key] = str(_export_path(PROJECT.video, key))
    return {
        "video": video,
        "audio": audio,
        "video_path": vpath,
        "audio_path": apath,
        "aspects": {k: {"width": w, "height": h} for k, (w, h) in ASPECTS.items()},
        "resolutions": list(RESOLUTIONS),
        "default_resolution": DEFAULT_RESOLUTION,
        "aspect_sizes": {
            k: {
                res: {"width": rw, "height": rh}
                for res in RESOLUTIONS
                for rw, rh in [dest_size(k, res)]
            }
            for k in ASPECTS
        },
        "suggested_out": suggested.get("9:16"),
        "suggested_names": suggested,
        "export": export,
        "version": __version__,
    }


def _pick_native(kind: str) -> Path | None:
    env = os.environ.copy()
    cmd = [sys.executable, "-m", "clip_editor.pick", kind]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise ProbeError("file picker timed out")
    if proc.returncode == 1:
        return None
    if proc.returncode != 0:
        err = (proc.stderr or "").strip() or f"picker exit {proc.returncode}"
        raise ProbeError(err)
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return None
    return Path(line[-1])


def _save_upload(role: str, filename: str, body: Any, length: int) -> Path:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(_sanitize_filename(filename)).suffix or (
        ".mp4" if role == "video" else ".mp3"
    )
    dest = UPLOAD_DIR / f"{role}-{uuid.uuid4().hex}{suffix}"
    remaining = length
    with dest.open("wb") as f:
        while remaining > 0:
            chunk = body.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            f.write(chunk)
            remaining -= len(chunk)
    if dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise ProbeError("uploaded file was empty")
    return dest


def _start_export(body: dict[str, Any]) -> None:
    with STATE_LOCK:
        video = PROJECT.video
        audio = PROJECT.audio
        if PROJECT.export.get("state") == "running":
            raise ExportError("an export is already running")
        if video is None:
            raise ExportError("open a video first")
        PROJECT.export = {
            "state": "running",
            "percent": 0.0,
            "error": None,
            "out": None,
        }

    aspect = str(body.get("aspect") or "9:16")
    resolution = str(body.get("resolution") or DEFAULT_RESOLUTION)
    pan_x = float(body.get("pan_x", 0.5))
    pan_y = float(body.get("pan_y", 0.5))
    in_s = float(body.get("in_s") or 0.0)
    out_raw = body.get("out_s")
    out_s = float(out_raw) if out_raw is not None and out_raw != "" else None
    audio_follows_in = bool(body.get("audio_follows_in") or False)
    audio_offset = float(body.get("audio_offset") or 0.0)
    out = _export_path(video, aspect)

    def progress(pct: float, state: str) -> None:
        with STATE_LOCK:
            PROJECT.export["percent"] = pct
            if PROJECT.export["state"] == "running":
                PROJECT.export["state"] = "running"

    def worker() -> None:
        try:
            result = run_export(
                video,
                out,
                audio=audio,
                aspect=aspect,
                resolution=resolution,
                pan_x=pan_x,
                pan_y=pan_y,
                in_s=in_s,
                out_s=out_s,
                audio_follows_in=audio_follows_in,
                audio_offset=audio_offset,
                progress=progress,
            )
            with STATE_LOCK:
                PROJECT.export = {
                    "state": "done",
                    "percent": 1.0,
                    "error": None,
                    "out": result["out"],
                    "gate": result.get("gate"),
                    "meta": result.get("meta"),
                }
        except Exception as exc:  # noqa: BLE001
            with STATE_LOCK:
                PROJECT.export = {
                    "state": "error",
                    "percent": 0.0,
                    "error": str(exc),
                    "out": None,
                }

    threading.Thread(target=worker, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = f"ClipEditor/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj: Any, status: int = 200) -> None:
        self._send(status, _json_bytes(obj), "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        n = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(n) if n else b"{}"
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ProbeError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ProbeError("JSON body must be an object")
        return data

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/":
                self._send_static("index.html")
                return
            if path in ("/app.css", "/app.js"):
                self._send_static(path.lstrip("/"))
                return
            if path == "/api/project":
                self._send_json(_project_payload())
                return
            if path == "/api/export/status":
                with STATE_LOCK:
                    payload = dict(PROJECT.export)
                self._send_json(payload)
                return
            if path == "/api/preview-crop":
                # unused GET; JS computes CSS pan itself
                self._send_json({"ok": True})
                return
            if path.startswith("/media/"):
                role = path.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
                if role == "poster":
                    self._send_poster()
                    return
                self._send_media(path)
                return
            self._send_json({"error": "not found"}, 404)
        except ProbeError as exc:
            self._send_json({"error": str(exc)}, 400)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/open":
                body = self._read_json()
                role = str(body.get("role") or "")
                file_path = Path(str(body.get("path") or "")).expanduser()
                info = _set_media(role, file_path)
                self._send_json({"ok": True, "info": info, "project": _project_payload()})
                return
            if path == "/api/pick":
                body = self._read_json()
                role = str(body.get("role") or "video")
                picked = _pick_native(role)
                if picked is None:
                    self._send_json({"ok": False, "cancelled": True})
                    return
                info = _set_media(role, picked)
                self._send_json({"ok": True, "info": info, "project": _project_payload()})
                return
            if path == "/api/clear-audio":
                with STATE_LOCK:
                    PROJECT.audio = None
                    PROJECT.audio_info = None
                self._send_json({"ok": True, "project": _project_payload()})
                return
            if path == "/api/export":
                body = self._read_json()
                _start_export(body)
                self._send_json({"ok": True, "project": _project_payload()})
                return
            if path == "/api/crop-preview":
                body = self._read_json()
                self._send_json(self._crop_preview(body))
                return
            self._send_json({"error": "not found"}, 404)
        except (ProbeError, ExportError, ValueError) as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            self._send_json({"error": str(exc)}, 500)

    def do_PUT(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path in ("/api/upload/video", "/api/upload/audio"):
                role = path.rsplit("/", 1)[-1]
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0:
                    raise ProbeError("empty upload")
                filename = self.headers.get("X-Filename") or f"{role}.bin"
                dest = _save_upload(role, filename, self.rfile, length)
                try:
                    info = _set_media(role, dest, original_name=filename)
                except ProbeError:
                    dest.unlink(missing_ok=True)
                    raise
                self._send_json({"ok": True, "info": info, "project": _project_payload()})
                return
            self._send_json({"error": "not found"}, 404)
        except (ProbeError, ValueError) as exc:
            self._send_json({"error": str(exc)}, 400)

    def _crop_preview(self, body: dict[str, Any]) -> dict[str, Any]:
        with STATE_LOCK:
            info = PROJECT.video_info
        if not info:
            raise ProbeError("open a video first")
        aspect = str(body.get("aspect") or "9:16")
        resolution = str(body.get("resolution") or DEFAULT_RESOLUTION)
        dw, dh = dest_size(aspect, resolution)
        crop = cover_crop(
            int(info["width"]),
            int(info["height"]),
            dw,
            dh,
            float(body.get("pan_x", 0.5)),
            float(body.get("pan_y", 0.5)),
        )
        return {
            "crop": {"x": crop.x, "y": crop.y, "w": crop.w, "h": crop.h},
            "dest": {
                "width": dw,
                "height": dh,
                "aspect": aspect,
                "resolution": resolution,
            },
        }

    def _send_static(self, name: str) -> None:
        name = posixpath.basename(name)
        path = STATIC_DIR / name
        if not path.is_file():
            self._send_json({"error": "not found"}, 404)
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if name.endswith(".js"):
            ctype = "text/javascript; charset=utf-8"
        elif name.endswith(".css"):
            ctype = "text/css; charset=utf-8"
        elif name.endswith(".html"):
            ctype = "text/html; charset=utf-8"
        self._send(200, data, ctype)

    def _send_poster(self) -> None:
        poster = CACHE_DIR / "poster.jpg"
        if not poster.is_file():
            self._send_json({"error": "no poster"}, 404)
            return
        data = poster.read_bytes()
        self._send(200, data, "image/jpeg")

    def _send_media(self, path: str) -> None:
        role = path.rstrip("/").rsplit("/", 1)[-1]
        with STATE_LOCK:
            file_path = PROJECT.video if role == "video" else PROJECT.audio
        if file_path is None or not file_path.is_file():
            self._send_json({"error": f"no {role} open"}, 404)
            return
        ctype = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        size = file_path.stat().st_size
        range_h = self.headers.get("Range")
        start = 0
        end = size - 1
        status = 200
        if range_h and range_h.startswith("bytes="):
            spec = range_h.split("=", 1)[1].split(",")[0].strip()
            left, _, right = spec.partition("-")
            try:
                if left:
                    start = int(left)
                if right:
                    end = int(right)
            except ValueError:
                start, end = 0, size - 1
            start = max(0, start)
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with file_path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    url = f"http://{host}:{port}/"
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
        httpd.allow_reuse_address = True
    except OSError:
        # Already running (desktop launcher / Super+Space again).
        print(f"clip-editor already running  {url}", flush=True)
        if open_browser:
            _open_browser(url)
        return
    print(f"clip-editor {__version__}  {url}", flush=True)
    if open_browser:
        threading.Thread(target=lambda: _open_browser(url), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        httpd.server_close()


def _open_browser(url: str) -> None:
    time.sleep(0.25)
    chrome = shutil.which("chromium") or shutil.which("chromium-browser")
    profile = CACHE_DIR / "chromium"
    profile.mkdir(parents=True, exist_ok=True)
    if chrome:
        subprocess.Popen(
            [
                chrome,
                "--class=clip-editor",
                "--name=clip-editor",
                f"--app={url}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--disable-extensions",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    opener = shutil.which("xdg-open")
    if opener:
        subprocess.Popen(
            [opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
