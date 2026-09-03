"""Clip playback speed (#531): project round-trip, timeline length, export filters."""

from __future__ import annotations

import unittest

from clip_editor.export import _flatten_clips, _part_duration, _timeline_parts
from clip_editor.project import (
    DEFAULT_SPEED,
    MAX_SPEED,
    MIN_SPEED,
    ClipInst,
    atempo_chain,
    clip_from_dict,
    clip_to_dict,
    normalize_speed,
)


class NormalizeSpeedTest(unittest.TestCase):
    def test_default_and_clamp(self) -> None:
        self.assertEqual(normalize_speed(None), DEFAULT_SPEED)
        self.assertEqual(normalize_speed("nope"), DEFAULT_SPEED)
        self.assertEqual(normalize_speed(0), DEFAULT_SPEED)
        self.assertEqual(normalize_speed(MIN_SPEED / 2), MIN_SPEED)
        self.assertEqual(normalize_speed(MAX_SPEED * 2), MAX_SPEED)
        self.assertAlmostEqual(normalize_speed(1.5), 1.5)


class ClipInstSpeedTest(unittest.TestCase):
    def test_timeline_len_scales_with_speed(self) -> None:
        c = ClipInst(start=0.0, in_s=0.0, out_s=10.0, speed=2.0)
        self.assertAlmostEqual(c.source_len(), 10.0)
        self.assertAlmostEqual(c.timeline_len(), 5.0)
        t0, t1 = c.used_times()
        self.assertAlmostEqual(t0, 0.0)
        self.assertAlmostEqual(t1, 5.0)

    def test_round_trip_omits_default_speed(self) -> None:
        c = ClipInst(start=1.0, in_s=0.0, out_s=4.0)
        d = clip_to_dict(c)
        self.assertNotIn("speed", d)
        back = clip_from_dict(d)
        assert back is not None
        self.assertAlmostEqual(back.playback_speed(), 1.0)

    def test_round_trip_persists_nondefault_speed(self) -> None:
        c = ClipInst(start=0.0, in_s=1.0, out_s=5.0, speed=0.5)
        d = clip_to_dict(c)
        self.assertAlmostEqual(d["speed"], 0.5)
        back = clip_from_dict(d)
        assert back is not None
        self.assertAlmostEqual(back.speed, 0.5)
        self.assertAlmostEqual(back.timeline_len(), 8.0)


class AtempoChainTest(unittest.TestCase):
    def test_identity(self) -> None:
        self.assertEqual(atempo_chain(1.0), [])

    def test_splits_outside_half_to_double(self) -> None:
        self.assertEqual(atempo_chain(4.0), ["atempo=2.000000", "atempo=2.000000"])
        self.assertEqual(atempo_chain(0.25), ["atempo=0.500000", "atempo=0.500000"])


class FlattenSpeedTest(unittest.TestCase):
    def test_flatten_timeline_uses_speed(self) -> None:
        clips = [ClipInst(start=0.0, in_s=0.0, out_s=10.0, media_id="m", speed=2.0)]
        flat = _flatten_clips(clips, {"m": 10.0})
        self.assertEqual(len(flat), 1)
        t0, t1, sinn, sout = flat[0][:4]
        self.assertAlmostEqual(t0, 0.0)
        self.assertAlmostEqual(t1, 5.0)
        self.assertAlmostEqual(sinn, 0.0)
        self.assertAlmostEqual(sout, 10.0)
        self.assertAlmostEqual(float(flat[0][10]), 2.0)

    def test_timeline_parts_duration_is_sped(self) -> None:
        clips = [ClipInst(start=0.0, in_s=0.0, out_s=8.0, media_id="m", speed=2.0)]
        flat = _flatten_clips(clips, {"m": 8.0})
        parts = _timeline_parts(flat, flat[0][1])
        self.assertEqual(parts[0][0], "seg")
        self.assertAlmostEqual(float(parts[0][2]), 8.0)  # source_len
        self.assertAlmostEqual(_part_duration(parts[0]), 4.0)


if __name__ == "__main__":
    unittest.main()
