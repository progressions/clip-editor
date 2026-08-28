from __future__ import annotations

import re
import unittest

from clip_editor.export import _join_parts


def _two_part_track(kind: str, filters: list[str]) -> str:
    """Join two touching segments the way the per-track audio loop does."""
    parts = [("seg", 0.0, 2.0, "m1"), ("seg", 2.0, 2.0, "m1")]
    labels = ["[x0]", "[x1]"]
    label, _duration = _join_parts(
        filters, parts, labels, kind=kind, crossfade_s=0.0
    )
    return label


class JoinPartsLabelTest(unittest.TestCase):
    def test_audio_tracks_do_not_share_intermediate_labels(self) -> None:
        # Both tracks reach _join_parts and used to emit the same [ajoinN]
        # names. ffmpeg only accepted that because each label happened to be
        # consumed before the next track reused it; distinct names per track
        # remove the dependency on emission order.
        filters: list[str] = []
        _two_part_track("audio1", filters)
        _two_part_track("audio2", filters)

        produced = re.findall(r"\[([A-Za-z0-9_]+)\]$", "\n".join(filters), re.MULTILINE)
        self.assertEqual(len(produced), len(set(produced)), produced)

    def test_video_join_still_uses_the_video_filters(self) -> None:
        filters: list[str] = []
        label = _two_part_track("video", filters)

        self.assertIn("concat=n=2:v=1:a=0", "".join(filters))
        self.assertTrue(label.startswith("[video"), label)

    def test_audio_join_uses_the_audio_filters(self) -> None:
        filters: list[str] = []
        label = _two_part_track("audio1", filters)

        self.assertIn("concat=n=2:v=0:a=1", "".join(filters))
        self.assertTrue(label.startswith("[audio1"), label)


if __name__ == "__main__":
    unittest.main()
