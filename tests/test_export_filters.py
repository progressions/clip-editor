from __future__ import annotations

import re
import unittest

from clip_editor.export import (
    _join_parts,
    _join_transitions_for_parts,
    _timeline_parts,
)
from clip_editor.project import (
    TRANSITION_DISSOLVE,
    TRANSITION_NONE,
    TRANSITION_WHITE_FLASH,
    ClipInst,
)


def _two_part_track(kind: str, filters: list[str]) -> str:
    """Join two touching segments the way the per-track audio loop does."""
    parts = [("seg", 0.0, 2.0, "m1"), ("seg", 2.0, 2.0, "m1")]
    labels = ["[x0]", "[x1]"]
    label, _duration, _applied = _join_parts(
        filters, parts, labels, kind=kind, crossfade_s=0.0
    )
    return label


def _seg(
    dur: float,
    *,
    transition: str = TRANSITION_NONE,
    transition_s: float = 0.0,
    mid: str = "m1",
) -> tuple:
    return ("seg", 0.0, dur, mid, 0.0, 0.0, 1.0, transition, transition_s)


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


class PerCutTransitionGraphTest(unittest.TestCase):
    def test_mixed_transitions_and_hard_cut(self) -> None:
        parts = [
            _seg(1.0, transition=TRANSITION_WHITE_FLASH, transition_s=0.25),
            _seg(1.0, transition=TRANSITION_NONE),
            _seg(1.0, transition=TRANSITION_DISSOLVE, transition_s=0.5),
        ]
        labels = ["[a]", "[b]", "[c]"]
        filters: list[str] = []
        joins = _join_transitions_for_parts(parts)
        _label, duration, applied = _join_parts(
            filters, parts, labels, kind="video", join_transitions=joins
        )
        graph = ";".join(filters)
        self.assertIn("xfade=transition=fadewhite", graph)
        self.assertIn("concat=n=2:v=1:a=0", graph)
        self.assertNotIn("transition=fade:", graph.split("concat")[0])
        self.assertTrue(applied[0]["applied"])
        self.assertEqual(applied[0]["type"], TRANSITION_WHITE_FLASH)
        self.assertAlmostEqual(applied[0]["duration"], 0.25)
        self.assertFalse(applied[1]["applied"])
        self.assertAlmostEqual(duration, 2.75)

    def test_gap_blocks_transition(self) -> None:
        parts = [
            _seg(1.0, transition=TRANSITION_DISSOLVE, transition_s=0.5),
            ("gap", 0.4),
            _seg(1.0),
        ]
        labels = ["[a]", "[b]", "[c]"]
        filters: list[str] = []
        joins = _join_transitions_for_parts(parts)
        _label, _duration, applied = _join_parts(
            filters, parts, labels, kind="video", join_transitions=joins
        )
        self.assertTrue(all(not row["applied"] for row in applied))
        self.assertNotIn("xfade", "".join(filters))

    def test_short_clip_clamps_duration(self) -> None:
        parts = [
            _seg(0.2, transition=TRANSITION_DISSOLVE, transition_s=0.5),
            _seg(0.2),
        ]
        labels = ["[a]", "[b]"]
        filters: list[str] = []
        joins = _join_transitions_for_parts(parts)
        _label, duration, applied = _join_parts(
            filters, parts, labels, kind="video", join_transitions=joins
        )
        self.assertTrue(applied[0]["applied"])
        self.assertAlmostEqual(applied[0]["duration"], 0.15)
        self.assertAlmostEqual(duration, 0.25)

    def test_audio_acrossfade_follows_visual_cut(self) -> None:
        parts = [
            _seg(1.0, transition=TRANSITION_WHITE_FLASH, transition_s=0.25),
            _seg(1.0),
        ]
        labels = ["[a0]", "[a1]"]
        filters: list[str] = []
        joins = _join_transitions_for_parts(parts)
        _label, _duration, applied = _join_parts(
            filters, parts, labels, kind="audio1", join_transitions=joins
        )
        self.assertIn("acrossfade=d=0.250000", "".join(filters))
        self.assertEqual(applied[0]["type"], TRANSITION_WHITE_FLASH)

    def test_legacy_crossfade_s_still_dissolves(self) -> None:
        parts = [_seg(1.0), _seg(1.0)]
        labels = ["[a]", "[b]"]
        filters: list[str] = []
        _label, _duration, applied = _join_parts(
            filters, parts, labels, kind="video", crossfade_s=0.3
        )
        self.assertIn("xfade=transition=fade:", "".join(filters))
        self.assertAlmostEqual(applied[0]["duration"], 0.3)

    def test_timeline_parts_carry_outgoing_transition(self) -> None:
        flat = [
            (0.0, 1.0, 0.0, 1.0, "m1", 0.0, 0.0, 1.0, TRANSITION_DISSOLVE, 0.5),
            (1.2, 2.2, 0.0, 1.0, "m1", 0.0, 0.0, 1.0, TRANSITION_NONE, 0.0),
        ]
        parts = _timeline_parts(flat, 2.2)
        self.assertEqual(parts[0][0], "seg")
        self.assertEqual(parts[0][7], TRANSITION_DISSOLVE)
        self.assertEqual(parts[1][0], "gap")
        self.assertEqual(parts[2][0], "seg")


class ClipTransitionModelTest(unittest.TestCase):
    def test_split_moves_outgoing_transition(self) -> None:
        left = ClipInst(
            start=0.0,
            in_s=0.0,
            out_s=2.0,
            transition=TRANSITION_DISSOLVE,
            transition_s=0.5,
        )
        right = left.split_at(1.0)
        assert right is not None
        self.assertEqual(left.transition, TRANSITION_NONE)
        self.assertEqual(left.transition_s, 0.0)
        self.assertEqual(right.transition, TRANSITION_DISSOLVE)
        self.assertAlmostEqual(right.transition_s, 0.5)


if __name__ == "__main__":
    unittest.main()
