from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clip_editor.project import (
    FORMAT,
    TRANSITION_DISSOLVE,
    TRANSITION_NONE,
    TRANSITION_WHITE_FLASH,
    VERSION,
    ClipInst,
    Project,
    from_dict,
    read_project,
    to_dict,
    write_project,
)


class ProjectTransitionRoundTripTest(unittest.TestCase):
    def test_round_trip_preserves_per_clip_transitions(self) -> None:
        proj = Project(
            aspect="9:16",
            video_clips=[
                ClipInst(
                    start=0.0,
                    in_s=0.0,
                    out_s=1.0,
                    media_id="m1",
                    transition=TRANSITION_WHITE_FLASH,
                    transition_s=0.25,
                ),
                ClipInst(
                    start=1.0,
                    in_s=0.0,
                    out_s=1.0,
                    media_id="m1",
                    transition=TRANSITION_DISSOLVE,
                    transition_s=0.5,
                ),
                ClipInst(start=2.0, in_s=0.0, out_s=1.0, media_id="m1"),
            ],
        )
        data = to_dict(proj)
        self.assertEqual(data["version"], VERSION)
        self.assertEqual(data["crossfade_s"], 0.0)
        self.assertEqual(data["video_clips"][0]["transition"], TRANSITION_WHITE_FLASH)
        self.assertNotIn("transition", data["video_clips"][2])

        loaded = from_dict(data)
        self.assertEqual(loaded.video_clips[0].transition, TRANSITION_WHITE_FLASH)
        self.assertAlmostEqual(loaded.video_clips[0].transition_s, 0.25)
        self.assertEqual(loaded.video_clips[1].transition, TRANSITION_DISSOLVE)
        self.assertEqual(loaded.video_clips[2].transition, TRANSITION_NONE)

    def test_legacy_crossfade_migrates_on_load(self) -> None:
        data = {
            "format": FORMAT,
            "version": 5,
            "aspect": "9:16",
            "crossfade_s": 0.4,
            "video": "/tmp/missing-video.mp4",
            "video_clips": [
                {"start": 0.0, "in_s": 0.0, "out_s": 1.0, "media_id": "m1"},
                {"start": 1.0, "in_s": 0.0, "out_s": 1.0, "media_id": "m1"},
            ],
            "media": [
                {
                    "id": "m1",
                    "kind": "video",
                    "path": "/tmp/missing-video.mp4",
                }
            ],
        }
        loaded = from_dict(data)
        self.assertEqual(loaded.video_clips[0].transition, TRANSITION_DISSOLVE)
        self.assertAlmostEqual(loaded.video_clips[0].transition_s, 0.4)
        self.assertEqual(loaded.video_clips[1].transition, TRANSITION_DISSOLVE)
        saved = to_dict(loaded)
        self.assertEqual(saved["version"], VERSION)
        self.assertEqual(saved["crossfade_s"], 0.0)
        self.assertEqual(saved["video_clips"][0]["transition"], TRANSITION_DISSOLVE)

    def test_copy_and_file_round_trip(self) -> None:
        src = ClipInst(
            start=0.0,
            in_s=0.0,
            out_s=1.5,
            transition=TRANSITION_DISSOLVE,
            transition_s=0.5,
        )
        copied = src.copy()
        self.assertEqual(copied.transition, TRANSITION_DISSOLVE)
        self.assertAlmostEqual(copied.transition_s, 0.5)
        copied.transition_s = 0.8
        self.assertAlmostEqual(src.transition_s, 0.5)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "demo.clip.json"
            proj = Project(
                video=Path("/tmp/missing-video.mp4"),
                video_clips=[src, ClipInst(start=1.5, in_s=0.0, out_s=1.0)],
            )
            write_project(path, proj)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["version"], VERSION)
            reloaded = read_project(path)
            self.assertEqual(reloaded.video_clips[0].transition, TRANSITION_DISSOLVE)
            self.assertAlmostEqual(reloaded.video_clips[0].transition_s, 0.5)


if __name__ == "__main__":
    unittest.main()
