"""Join hit-test and same-track ripple (#533)."""

from __future__ import annotations

import unittest

from clip_editor.ripple import (
    follower_indices,
    prefer_outgoing_at_join,
    resolve_edge_hits,
    ripple_starts,
)


class PreferOutgoingAtJoinTest(unittest.TestCase):
    def test_in_at_abut_becomes_previous_out(self) -> None:
        others = [(0, 0.0, 2.0), (1, 2.0, 4.0)]
        self.assertEqual(
            prefer_outgoing_at_join(1, "in", 2.0, others),
            (0, "out"),
        )

    def test_out_unchanged(self) -> None:
        others = [(0, 0.0, 2.0), (1, 2.0, 4.0)]
        self.assertEqual(
            prefer_outgoing_at_join(0, "out", 0.0, others),
            (0, "out"),
        )

    def test_in_with_gap_stays_in(self) -> None:
        others = [(0, 0.0, 1.0), (1, 3.0, 4.0)]
        self.assertEqual(
            prefer_outgoing_at_join(1, "in", 3.0, others),
            (1, "in"),
        )


class ResolveEdgeHitsTest(unittest.TestCase):
    def test_join_picks_left_out_over_right_in(self) -> None:
        hits = [
            (1, "in", 2.0, 4.0),
            (0, "out", 0.0, 2.0),
        ]
        self.assertEqual(resolve_edge_hits(hits), (0, "out"))

    def test_only_in_with_no_abut(self) -> None:
        hits = [(1, "in", 5.0, 7.0)]
        self.assertEqual(resolve_edge_hits(hits), (1, "in"))

    def test_empty(self) -> None:
        self.assertIsNone(resolve_edge_hits([]))


class FollowerIndicesTest(unittest.TestCase):
    def test_packed_same_track(self) -> None:
        tracks = [1, 1, 1]
        times = [(0.0, 2.0), (2.0, 4.0), (4.0, 6.0)]
        self.assertEqual(follower_indices(tracks, times, 0), [1, 2])
        self.assertEqual(follower_indices(tracks, times, 1), [2])
        self.assertEqual(follower_indices(tracks, times, 2), [])

    def test_other_track_ignored(self) -> None:
        tracks = [1, 2, 1]
        times = [(0.0, 2.0), (2.0, 4.0), (2.0, 3.0)]
        self.assertEqual(follower_indices(tracks, times, 0), [2])

    def test_gap_still_follows(self) -> None:
        tracks = [1, 1]
        times = [(0.0, 1.0), (3.0, 4.0)]
        self.assertEqual(follower_indices(tracks, times, 0), [1])


class RippleApplyOnClipsTest(unittest.TestCase):
    def test_shorten_packed_v1_moves_follower_start(self) -> None:
        from clip_editor.project import ClipInst

        a = ClipInst(start=0.0, in_s=0.0, out_s=4.0, track=1)
        b = ClipInst(start=4.0, in_s=0.0, out_s=3.0, track=1)
        clips = [a, b]
        times = [(0.0, 4.0), (4.0, 7.0)]
        tracks = [1, 1]
        follow = follower_indices(tracks, times, 0)
        self.assertEqual(follow, [1])
        starts0 = {i: clips[i].start for i in follow}
        t1_0 = 4.0
        a.out_s = 2.0
        t1 = 2.0
        for i, start in ripple_starts(starts0, t1 - t1_0).items():
            clips[i].start = start
        self.assertAlmostEqual(b.start, 2.0)
        self.assertAlmostEqual(b.in_s, 0.0)
        self.assertAlmostEqual(b.out_s, 3.0)


class RippleStartsTest(unittest.TestCase):
    def test_shift(self) -> None:
        self.assertEqual(
            ripple_starts({1: 2.0, 2: 4.0}, -0.5),
            {1: 1.5, 2: 3.5},
        )
        self.assertEqual(ripple_starts({1: 2.0}, 1.0), {1: 3.0})


if __name__ == "__main__":
    unittest.main()
