"""Multi-select helpers for timeline clips (#530)."""

from __future__ import annotations

import unittest

from clip_editor.selection import (
    group_moved_starts,
    move_video_selection,
    move_timeline_track,
    next_video_selection,
    prune_video_selection,
)


class NextVideoSelectionTest(unittest.TestCase):
    def test_plain_click_replaces_selection(self) -> None:
        primary, selected = next_video_selection(
            clicked=2,
            primary=0,
            selected={0, 1},
            shift=False,
            n_clips=4,
        )
        self.assertEqual(primary, 2)
        self.assertEqual(selected, {2})

    def test_shift_click_adds_to_selection(self) -> None:
        primary, selected = next_video_selection(
            clicked=2,
            primary=0,
            selected={0},
            shift=True,
            n_clips=4,
        )
        self.assertEqual(primary, 2)
        self.assertEqual(selected, {0, 2})

    def test_shift_click_seeds_from_primary_when_set_empty(self) -> None:
        primary, selected = next_video_selection(
            clicked=1,
            primary=0,
            selected=set(),
            shift=True,
            n_clips=3,
        )
        self.assertEqual(primary, 1)
        self.assertEqual(selected, {0, 1})

    def test_shift_click_keeps_already_selected(self) -> None:
        primary, selected = next_video_selection(
            clicked=0,
            primary=0,
            selected={0, 2},
            shift=True,
            n_clips=4,
        )
        self.assertEqual(primary, 0)
        self.assertEqual(selected, {0, 2})

    def test_invalid_click_clears(self) -> None:
        primary, selected = next_video_selection(
            clicked=9,
            primary=0,
            selected={0},
            shift=True,
            n_clips=3,
        )
        self.assertEqual(primary, -1)
        self.assertEqual(selected, set())


class PruneVideoSelectionTest(unittest.TestCase):
    def test_prunes_out_of_range(self) -> None:
        primary, selected = prune_video_selection({0, 2, 9}, 2, 3)
        self.assertEqual(primary, 2)
        self.assertEqual(selected, {0, 2})

    def test_empty_when_all_invalid(self) -> None:
        primary, selected = prune_video_selection({5, 6}, 5, 2)
        self.assertEqual(primary, -1)
        self.assertEqual(selected, set())


class MoveVideoSelectionTest(unittest.TestCase):
    def test_plain_movement_replaces_selection(self) -> None:
        primary, selected = move_video_selection(
            delta=1, primary=1, selected={0, 1}, extend=False, n_clips=4
        )
        self.assertEqual(primary, 2)
        self.assertEqual(selected, {2})


    def test_shift_movement_extends_selection(self) -> None:
        primary, selected = move_video_selection(
            delta=1, primary=1, selected={1}, extend=True, n_clips=4
        )
        self.assertEqual(primary, 2)
        self.assertEqual(selected, {1, 2})

    def test_repeated_shift_movement_keeps_prior_selection(self) -> None:
        primary, selected = move_video_selection(
            delta=-1, primary=2, selected={1, 2, 3}, extend=True, n_clips=4
        )
        self.assertEqual(primary, 1)
        self.assertEqual(selected, {1, 2, 3})

    def test_repeated_shift_movement_adds_each_destination(self) -> None:
        primary, selected = move_video_selection(
            delta=1, primary=1, selected={1, 2}, extend=True, n_clips=4
        )
        self.assertEqual(primary, 2)
        self.assertEqual(selected, {1, 2})
        primary, selected = move_video_selection(
            delta=1, primary=primary, selected=selected, extend=True, n_clips=4
        )
        self.assertEqual(primary, 3)
        self.assertEqual(selected, {1, 2, 3})

    def test_edge_movement_preserves_selection(self) -> None:
        primary, selected = move_video_selection(
            delta=-1, primary=0, selected={0, 1}, extend=False, n_clips=3
        )
        self.assertEqual(primary, 0)
        self.assertEqual(selected, {0, 1})

    def test_missing_primary_selects_nearest_timeline_edge(self) -> None:
        primary, selected = move_video_selection(
            delta=1, primary=-1, selected={0}, extend=False, n_clips=3
        )
        self.assertEqual(primary, 0)
        self.assertEqual(selected, {0})
        primary, selected = move_video_selection(
            delta=-1, primary=-1, selected=set(), extend=False, n_clips=3
        )
        self.assertEqual(primary, 2)
        self.assertEqual(selected, {2})


class MoveTimelineTrackTest(unittest.TestCase):
    def test_moves_down_in_visual_track_order(self) -> None:
        self.assertEqual(move_timeline_track("video", 2, 1), ("video", 1))
        self.assertEqual(move_timeline_track("video", 1, 1), ("audio", 1))
        self.assertEqual(move_timeline_track("audio", 1, 1), ("audio", 2))

    def test_moves_up_in_visual_track_order(self) -> None:
        self.assertEqual(move_timeline_track("audio", 2, -1), ("audio", 1))
        self.assertEqual(move_timeline_track("audio", 1, -1), ("video", 1))
        self.assertEqual(move_timeline_track("video", 1, -1), ("video", 2))

    def test_clamps_at_top_and_bottom(self) -> None:
        self.assertEqual(move_timeline_track("video", 2, -1), ("video", 2))
        self.assertEqual(move_timeline_track("audio", 2, 1), ("audio", 2))


class GroupMovedStartsTest(unittest.TestCase):
    def test_translates_all_by_anchor_delta(self) -> None:
        starts = {0: 1.0, 2: 4.0, 3: 7.5}
        moved = group_moved_starts(starts, anchor=2, new_anchor_start=5.5)
        self.assertEqual(moved, {0: 2.5, 2: 5.5, 3: 9.0})


if __name__ == "__main__":
    unittest.main()
