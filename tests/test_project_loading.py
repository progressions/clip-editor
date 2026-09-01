from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from clip_editor.probe import which_ffmpeg
from clip_editor.project import (
    FORMAT,
    TRANSITION_DISSOLVE,
    TRANSITION_NONE,
    TRANSITION_WHITE_FLASH,
    VERSION,
    ClipInst,
    MediaItem,
    Project,
    ProjectError,
    ensure_project_loadable,
    from_dict,
    media_load_errors,
    read_project,
    to_dict,
    write_project,
)


def _make_media(td: Path) -> tuple[Path, Path, Path]:
    """Create tiny video/audio fixtures, including a spaced non-ASCII name."""
    ffmpeg = which_ffmpeg()
    video = td / "clip_a.mp4"
    video_b = td / "clip b — café.mp4"
    audio = td / "bed tone.m4a"
    for out, lavfi, dur, extra in (
        (video, "testsrc=size=320x240:rate=24", "1.0", ["-pix_fmt", "yuv420p", "-c:v", "libx264"]),
        (video_b, "testsrc=size=320x240:rate=24", "1.0", ["-pix_fmt", "yuv420p", "-c:v", "libx264"]),
        (audio, "sine=frequency=440:sample_rate=48000", "1.5", ["-c:a", "aac"]),
    ):
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            lavfi,
            "-t",
            dur,
            *extra,
            str(out),
        ]
        subprocess.check_call(cmd)
    return video, video_b, audio


def _meaningful(data: dict) -> dict:
    keys = (
        "format",
        "version",
        "aspect",
        "pan_x",
        "pan_y",
        "in_s",
        "out_s",
        "video_start",
        "audio_start",
        "audio_in",
        "audio_out",
        "audio_follows_in",
        "audio_fit",
        "use_video_soundtrack",
        "crossfade_s",
        "video_clips",
        "audio_clips",
    )
    out = {k: data.get(k) for k in keys}
    out["media"] = sorted(
        [
            {
                "id": m["id"],
                "kind": m["kind"],
                "name": Path(m["path"]).name,
                "path_rel": m.get("path_rel"),
            }
            for m in (data.get("media") or [])
        ],
        key=lambda row: row["id"],
    )
    return out


class ProjectLoadingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._td = tempfile.TemporaryDirectory(prefix="clip-editor-load-")
        cls.root = Path(cls._td.name)
        cls.video, cls.video_b, cls.audio = _make_media(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._td.cleanup()

    def _rich_project(self) -> Project:
        media = [
            MediaItem(id="m1", path=self.video, kind="video"),
            MediaItem(id="m2", path=self.video_b, kind="video"),
            MediaItem(id="m3", path=self.audio, kind="audio"),
        ]
        return Project(
            video=self.video,
            audio=self.audio,
            aspect="9:16",
            pan_x=0.25,
            pan_y=0.75,
            in_s=0.0,
            out_s=2.0,
            video_start=0.0,
            audio_start=0.1,
            audio_in=0.0,
            audio_out=1.2,
            audio_follows_in=False,
            audio_fit=True,
            use_video_soundtrack=False,
            media=media,
            video_clips=[
                ClipInst(
                    start=0.0,
                    in_s=0.0,
                    out_s=0.5,
                    media_id="m1",
                    transform_x=10.0,
                    transform_y=-5.0,
                    scale=1.25,
                    track=1,
                    transition=TRANSITION_WHITE_FLASH,
                    transition_s=0.25,
                ),
                ClipInst(
                    start=0.5,
                    in_s=0.0,
                    out_s=0.5,
                    media_id="m2",
                    track=2,
                    transition=TRANSITION_DISSOLVE,
                    transition_s=0.4,
                ),
            ],
            audio_clips=[
                ClipInst(start=0.1, in_s=0.0, out_s=1.2, media_id="m3", track=2),
            ],
        )

    def test_current_format_file_round_trip(self) -> None:
        path = self.root / "rich.clip.json"
        write_project(path, self._rich_project())
        loaded = read_project(path)
        ensure_project_loadable(loaded)
        again = to_dict(loaded)
        first = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(first["version"], VERSION)
        self.assertEqual(_meaningful(first), _meaningful(again))
        self.assertEqual(loaded.video_clips[0].transition, TRANSITION_WHITE_FLASH)
        self.assertEqual(loaded.video_clips[1].track, 2)
        self.assertEqual(loaded.audio_clips[0].track, 2)
        self.assertAlmostEqual(loaded.video_clips[0].scale, 1.25)
        self.assertEqual(loaded.audio_clips[0].media_id, "m3")

    def test_legacy_v5_crossfade_migrates(self) -> None:
        data = {
            "format": FORMAT,
            "version": 5,
            "aspect": "4:5",
            "crossfade_s": 0.35,
            "video": str(self.video),
            "audio": str(self.audio),
            "media": [
                {"id": "m1", "kind": "video", "path": str(self.video)},
                {"id": "m2", "kind": "audio", "path": str(self.audio)},
            ],
            "video_clips": [
                {"start": 0.0, "in_s": 0.0, "out_s": 0.5, "media_id": "m1"},
                {"start": 0.5, "in_s": 0.0, "out_s": 0.5, "media_id": "m1"},
            ],
            "audio_clips": [
                {"start": 0.0, "in_s": 0.0, "out_s": 1.0, "media_id": "m2"},
            ],
            "use_video_soundtrack": False,
        }
        loaded = from_dict(data, origin=self.root / "legacy.clip.json")
        self.assertEqual(loaded.video_clips[0].transition, TRANSITION_DISSOLVE)
        self.assertAlmostEqual(loaded.video_clips[0].transition_s, 0.35)
        self.assertEqual(loaded.video_clips[1].transition, TRANSITION_DISSOLVE)
        saved = to_dict(loaded)
        self.assertEqual(saved["version"], VERSION)
        self.assertEqual(saved["crossfade_s"], 0.0)
        self.assertEqual(saved["video_clips"][0]["transition"], TRANSITION_DISSOLVE)

    def test_legacy_v1_single_files_bind_media(self) -> None:
        data = {
            "format": FORMAT,
            "version": 1,
            "aspect": "9:16",
            "in_s": 0.0,
            "out_s": 1.0,
            "video": str(self.video),
            "audio": str(self.audio),
            "audio_fit": True,
            "use_video_soundtrack": False,
        }
        loaded = from_dict(data, origin=self.root / "v1.clip.json")
        ensure_project_loadable(loaded)
        self.assertEqual(len(loaded.media), 2)
        self.assertEqual(len(loaded.video_clips), 1)
        self.assertEqual(len(loaded.audio_clips), 1)
        self.assertTrue(loaded.video_clips[0].media_id)
        self.assertTrue(loaded.audio_clips[0].media_id)
        self.assertEqual(loaded.video_clips[0].transition, TRANSITION_NONE)

    def test_relative_paths_survive_folder_move(self) -> None:
        folder = self.root / "bundle"
        folder.mkdir()
        shutil.copy2(self.video, folder / "clip_a.mp4")
        shutil.copy2(self.audio, folder / "bed tone.m4a")
        proj = Project(
            video=folder / "clip_a.mp4",
            audio=folder / "bed tone.m4a",
            media=[
                MediaItem(id="m1", path=folder / "clip_a.mp4", kind="video"),
                MediaItem(id="m2", path=folder / "bed tone.m4a", kind="audio"),
            ],
            video_clips=[ClipInst(start=0.0, in_s=0.0, out_s=0.5, media_id="m1")],
            audio_clips=[ClipInst(start=0.0, in_s=0.0, out_s=0.5, media_id="m2")],
            use_video_soundtrack=False,
        )
        path = folder / "bundle.clip.json"
        write_project(path, proj)
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["video_rel"], "clip_a.mp4")
        self.assertEqual(raw["audio_rel"], "bed tone.m4a")

        moved = self.root / "moved bundle"
        shutil.copytree(folder, moved)
        loaded = read_project(moved / "bundle.clip.json")
        ensure_project_loadable(loaded)
        self.assertTrue(loaded.video.is_file())
        self.assertTrue(loaded.audio.is_file())
        self.assertEqual(loaded.video.parent, moved)
        self.assertEqual(loaded.video_clips[0].media_id, "m1")

    def test_unknown_media_id_is_not_silently_rebound(self) -> None:
        data = {
            "format": FORMAT,
            "version": VERSION,
            "aspect": "9:16",
            "media": [
                {"id": "m1", "kind": "video", "path": str(self.video)},
                {"id": "m2", "kind": "video", "path": str(self.video_b)},
            ],
            "video_clips": [
                {"start": 0.0, "in_s": 0.0, "out_s": 0.5, "media_id": "m2"},
                {"start": 0.5, "in_s": 0.0, "out_s": 0.5, "media_id": "GONE"},
            ],
            "video": str(self.video),
        }
        loaded = from_dict(data)
        self.assertEqual(loaded.video_clips[0].media_id, "m2")
        self.assertEqual(loaded.video_clips[1].media_id, "GONE")
        errs = media_load_errors(loaded)
        self.assertTrue(any("GONE" in e for e in errs))
        with self.assertRaises(ProjectError):
            ensure_project_loadable(loaded)

    def test_missing_media_file_errors(self) -> None:
        data = {
            "format": FORMAT,
            "version": VERSION,
            "aspect": "9:16",
            "media": [
                {
                    "id": "m1",
                    "kind": "video",
                    "path": str(self.root / "does-not-exist.mp4"),
                }
            ],
            "video_clips": [
                {"start": 0.0, "in_s": 0.0, "out_s": 0.5, "media_id": "m1"},
            ],
        }
        loaded = from_dict(data, origin=self.root / "missing.clip.json")
        self.assertEqual(len(loaded.media), 1)
        with self.assertRaises(ProjectError) as ctx:
            ensure_project_loadable(loaded)
        self.assertIn("missing media m1", str(ctx.exception))

    def test_relative_only_missing_media_is_kept_for_errors(self) -> None:
        data = {
            "format": FORMAT,
            "version": VERSION,
            "aspect": "9:16",
            "media": [{"id": "m1", "kind": "video", "path_rel": "gone.mp4"}],
            "video_clips": [
                {"start": 0.0, "in_s": 0.0, "out_s": 0.5, "media_id": "m1"},
            ],
        }
        loaded = from_dict(data, origin=self.root / "rel.clip.json")
        self.assertEqual(len(loaded.media), 1)
        self.assertEqual(loaded.media[0].path.name, "gone.mp4")
        with self.assertRaises(ProjectError):
            ensure_project_loadable(loaded)

    def test_unsupported_version_and_bad_json(self) -> None:
        with self.assertRaises(ProjectError):
            from_dict({"format": FORMAT, "version": VERSION + 10})
        with self.assertRaises(ProjectError):
            from_dict({"format": "other", "version": 1})
        bad = self.root / "bad.clip.json"
        bad.write_text("{nope", encoding="utf-8")
        with self.assertRaises(ProjectError):
            read_project(bad)

    def test_open_failure_leaves_caller_state_untouched(self) -> None:
        """Mimic UI fail-closed: validate before replacing session state."""
        good = self._rich_project()
        session = {
            "media": [m.copy() for m in good.media],
            "video_clips": [c.copy() for c in good.video_clips],
            "title": "before",
        }
        broken = Project(
            media=[MediaItem(id="m1", path=self.root / "nope.mp4", kind="video")],
            video_clips=[ClipInst(start=0.0, in_s=0.0, out_s=0.5, media_id="m1")],
        )
        try:
            ensure_project_loadable(broken)
            session["title"] = "replaced"  # pragma: no cover
        except ProjectError:
            pass
        self.assertEqual(session["title"], "before")
        self.assertEqual(len(session["media"]), 3)
        self.assertEqual(session["video_clips"][0].media_id, "m1")

    def test_real_saved_project_copy_loads(self) -> None:
        src = Path("/home/isaac/Videos/maroon-bodycon.clip.json")
        if not src.is_file():
            self.skipTest("real project not present on this machine")
        original_bytes = src.read_bytes()
        # Copy only the project JSON; media stay at absolute paths.
        copy = self.root / "maroon-bodycon.copy.clip.json"
        shutil.copy2(src, copy)
        loaded = read_project(copy)
        ensure_project_loadable(loaded)
        self.assertGreaterEqual(len(loaded.media), 1)
        self.assertGreaterEqual(len(loaded.video_clips), 1)
        # Round-trip the copy without touching the original.
        write_project(copy, loaded)
        again = read_project(copy)
        self.assertEqual(
            _meaningful(to_dict(loaded)),
            _meaningful(to_dict(again)),
        )
        self.assertEqual(src.read_bytes(), original_bytes)


class ProjectLoadingUITest(unittest.TestCase):
    def test_gtk_load_restores_timeline_and_resets_undo(self) -> None:
        # Adw.Application tears down GObject types on exit; run in a child
        # process so the rest of the suite is unaffected.
        script = r"""
from __future__ import annotations
import json, sys, tempfile
from pathlib import Path
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw
from clip_editor.project import (
    TRANSITION_DISSOLVE, TRANSITION_NONE, ClipInst, MediaItem, Project, write_project,
)
from clip_editor.ui import EditorWindow
from tests.test_project_loading import _make_media

td = tempfile.TemporaryDirectory(prefix="clip-editor-load-ui-")
root = Path(td.name)
video, video_b, audio = _make_media(root)
path = root / "ui.clip.json"
write_project(
    path,
    Project(
        video=video, audio=audio, aspect="9:16",
        media=[
            MediaItem(id="m1", path=video, kind="video"),
            MediaItem(id="m2", path=video_b, kind="video"),
            MediaItem(id="m3", path=audio, kind="audio"),
        ],
        video_clips=[
            ClipInst(start=0.0, in_s=0.0, out_s=0.4, media_id="m1",
                     transition=TRANSITION_DISSOLVE, transition_s=0.3),
            ClipInst(start=0.4, in_s=0.0, out_s=0.4, media_id="m2", track=2),
        ],
        audio_clips=[ClipInst(start=0.0, in_s=0.0, out_s=1.0, media_id="m3")],
        use_video_soundtrack=False,
    ),
)
app = Adw.Application(application_id="local.clip-editor.load-ui-test")
result = {}

def on_activate(application):
    win = EditorWindow(application=application)
    win.media = [MediaItem(id="old", path=video, kind="video")]
    win.video_clips = [ClipInst(start=0.0, in_s=0.0, out_s=0.2, media_id="old")]
    win._history = [win._current_project()]
    win._hist_i = 0
    missing = root / "missing.clip.json"
    write_project(
        missing,
        Project(
            media=[MediaItem(id="m1", path=root / "absent.mp4", kind="video")],
            video_clips=[ClipInst(start=0.0, in_s=0.0, out_s=0.2, media_id="m1")],
        ),
    )
    win._load_project_file(missing)
    result["failed_status"] = win.status.get_text()
    result["unchanged_clips"] = [c.media_id for c in win.video_clips]
    win._load_project_file(path)
    result.update({
        "status": win.status.get_text(),
        "n_media": len(win.media),
        "n_vc": len(win.video_clips),
        "n_ac": len(win.audio_clips),
        "transitions": [c.transition for c in win.video_clips],
        "tracks": [c.track for c in win.video_clips],
        "title": win.get_title(),
        "hist_len": len(win._history),
        "hist_i": win._hist_i,
        "undo_reaches_prior": win._hist_i > 0,
    })
    application.quit()

app.connect("activate", on_activate)
app.run([])
print(json.dumps(result))
"""
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            self.fail(proc.stderr or proc.stdout or f"exit {proc.returncode}")
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
        self.assertTrue(lines, proc.stdout)
        result = json.loads(lines[-1])
        self.assertIn("missing media", result["failed_status"])
        self.assertEqual(result["unchanged_clips"], ["old"])
        self.assertEqual(result["status"], "Opened ui.clip.json")
        self.assertEqual(result["n_media"], 3)
        self.assertEqual(result["n_vc"], 2)
        self.assertEqual(result["n_ac"], 1)
        self.assertEqual(result["transitions"], [TRANSITION_DISSOLVE, TRANSITION_NONE])
        self.assertEqual(result["tracks"], [1, 2])
        self.assertIn("ui.clip.json", result["title"])
        self.assertEqual(result["hist_len"], 1)
        self.assertEqual(result["hist_i"], 0)
        self.assertFalse(result["undo_reaches_prior"])


if __name__ == "__main__":
    unittest.main()
