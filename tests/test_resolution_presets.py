"""Low / Medium / High export resolution presets."""

from __future__ import annotations

import unittest

from clip_editor.aspects import (
    ASPECTS,
    DEFAULT_RESOLUTION,
    RESOLUTIONS,
    dest_size,
    normalize_resolution,
)
from clip_editor.preview import FINAL_PROFILE, PREVIEW_PROFILE, profile_dest_size
from clip_editor.project import Project, from_dict, to_dict


class ResolutionPresetTests(unittest.TestCase):
    def test_medium_matches_legacy_aspects_table(self) -> None:
        for aspect, size in ASPECTS.items():
            self.assertEqual(dest_size(aspect), size)
            self.assertEqual(dest_size(aspect, "medium"), size)

    def test_low_and_high_short_edges(self) -> None:
        self.assertEqual(dest_size("9:16", "low"), (720, 1280))
        self.assertEqual(dest_size("9:16", "high"), (1440, 2560))
        self.assertEqual(dest_size("16:9", "low"), (1280, 720))
        self.assertEqual(dest_size("16:9", "high"), (2560, 1440))
        self.assertEqual(dest_size("1:1", "low"), (720, 720))
        self.assertEqual(dest_size("4:5", "high"), (1440, 1800))

    def test_sizes_are_even(self) -> None:
        for aspect in ASPECTS:
            for res in RESOLUTIONS:
                w, h = dest_size(aspect, res)
                self.assertEqual(w % 2, 0, f"{aspect} {res} width")
                self.assertEqual(h % 2, 0, f"{aspect} {res} height")

    def test_normalize_resolution(self) -> None:
        self.assertEqual(normalize_resolution(None), DEFAULT_RESOLUTION)
        self.assertEqual(normalize_resolution("HIGH"), "high")
        with self.assertRaises(ValueError):
            normalize_resolution("ultra")

    def test_preview_proxy_ignores_export_resolution(self) -> None:
        low = profile_dest_size("9:16", PREVIEW_PROFILE, "low")
        high = profile_dest_size("9:16", PREVIEW_PROFILE, "high")
        self.assertEqual(low, high)
        self.assertEqual(
            profile_dest_size("9:16", FINAL_PROFILE, "high"),
            (1440, 2560),
        )

    def test_project_round_trip_persists_resolution(self) -> None:
        proj = Project(aspect="1:1", resolution="high")
        data = to_dict(proj)
        self.assertEqual(data["resolution"], "high")
        loaded = from_dict(data)
        self.assertEqual(loaded.resolution, "high")

    def test_old_projects_default_to_medium(self) -> None:
        loaded = from_dict({"format": "clip-editor-project", "version": 6, "aspect": "9:16"})
        self.assertEqual(loaded.resolution, "medium")


if __name__ == "__main__":
    unittest.main()
