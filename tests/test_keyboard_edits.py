import unittest
from clip_editor.project import ClipInst
from clip_editor.keyboard_edits import (
    boundary_delta, move_clips, reorder_clips, seek_frame, trim_clip,
)


class MoveClipsTest(unittest.TestCase):
    def test_group_clamps_visible_start_and_preserves_internal_timing(self):
        clips = [ClipInst(start=-2, in_s=3, out_s=8),
                 ClipInst(start=4, out_s=5), ClipInst(start=9, out_s=3)]
        result = move_clips(clips, {0, 1}, -10)
        self.assertEqual([c.start for c in result], [-3, 3, 9])
        self.assertEqual(result[0].used_times()[0], 0)
        self.assertEqual([c.start for c in clips], [-2, 4, 9])
        self.assertEqual(result[1].timeline_len(), clips[1].timeline_len())

    def test_audio_and_video_clips_share_move_semantics(self):
        for media in ('audio', 'video'):
            clip = ClipInst(start=2, in_s=1, out_s=5, media_id=media, speed=2)
            result = move_clips([clip], {0}, 1)[0]
            self.assertEqual(result.used_times(), (4, 6))
            self.assertEqual(result.speed, 2)

    def test_boundaries_use_visible_starts_and_ignore_selected_or_other_lanes(self):
        clips = [ClipInst(start=2, in_s=3, out_s=8),
                 ClipInst(start=8, out_s=5), ClipInst(start=6, out_s=2, track=2),
                 ClipInst(start=7, out_s=4)]
        self.assertEqual(boundary_delta(clips, {0, 3}, 0, 1), 3)
        self.assertEqual(boundary_delta(clips, {0, 3}, 0, -1), 0)
        self.assertEqual(boundary_delta(clips, {1}, 1, -1), -1)

    def test_boundary_move_does_not_partially_move_a_group_past_zero(self):
        clips = [ClipInst(start=1, out_s=2), ClipInst(start=5, out_s=2),
                 ClipInst(start=2, out_s=2)]
        self.assertEqual(boundary_delta(clips, {0, 1}, 1, -1), 0)


class TrimClipTest(unittest.TestCase):
    def test_right_trim_ripples_actual_duration_and_preserves_gaps_other_tracks(self):
        clips = [ClipInst(out_s=8, speed=2), ClipInst(start=6, out_s=3),
                 ClipInst(start=4, out_s=2, track=2)]
        shorter = trim_clip(clips, 0, 'out', -1, 10)
        self.assertEqual(shorter[0].out_s, 6)
        self.assertEqual(shorter[1].start, 5)
        self.assertEqual(shorter[2], clips[2])
        longer = trim_clip(clips, 0, 'out', 10, 10)
        self.assertEqual(longer[0].out_s, 10)
        self.assertEqual(longer[1].start, 7)
        self.assertEqual(clips[0].out_s, 8)

    def test_left_trim_keeps_right_edge_fixed_at_non_unit_speed(self):
        clips = [ClipInst(start=1, in_s=2, out_s=10, speed=2)]
        shorter = trim_clip(clips, 0, 'in', 1, 10)[0]
        self.assertEqual(shorter.used_times(), (4, 7))
        self.assertEqual(shorter.in_s, 4)
        longer = trim_clip(clips, 0, 'in', -10, 10)[0]
        self.assertEqual(longer.used_times(), (2, 7))
        self.assertEqual(longer.in_s, 0)

    def test_minimum_duration_and_timeline_zero(self):
        clip = ClipInst(start=-1, in_s=2, out_s=5, speed=.5)
        extended = trim_clip([clip], 0, 'in', -10, 10)[0]
        self.assertAlmostEqual(extended.used_times()[0], 0)
        self.assertAlmostEqual(extended.used_times()[1], 7)
        for edge in ('in', 'out'):
            trimmed = trim_clip([clip], 0, edge, 100 if edge == 'in' else -100, 10)[0]
            self.assertAlmostEqual(trimmed.timeline_len(), .05)


class ReorderClipsTest(unittest.TestCase):
    def test_unequal_clips_swap_symmetrically_preserving_other_tracks(self):
        clips = [ClipInst(out_s=2), ClipInst(start=2, out_s=5),
                 ClipInst(start=7, out_s=3), ClipInst(start=10, out_s=2),
                 ClipInst(start=7, out_s=5, track=2)]
        moved = reorder_clips(clips, {2}, 2, -1)
        self.assertEqual([c.start for c in moved], [0, 5, 2, 10, 7])
        self.assertEqual(reorder_clips(moved, {2}, 2, 1), clips)
        self.assertEqual(reorder_clips(clips, {0}, 0, -1), clips)

    def test_group_reorder_respects_trimmed_starts_and_speed(self):
        clips = [ClipInst(start=-2, in_s=2, out_s=6, speed=2),
                 ClipInst(start=2, out_s=3), ClipInst(start=5, out_s=1)]
        moved = reorder_clips(clips, {1, 2}, 1, -1)
        self.assertEqual([c.used_times()[0] for c in moved], [4, 0, 3])
        self.assertEqual([c.timeline_len() for c in moved], [2, 3, 1])

    def test_invalid_groups_gaps_and_overlaps_are_explicit_no_edits(self):
        clips = [ClipInst(out_s=2), ClipInst(start=2, out_s=2),
                 ClipInst(start=4, out_s=2)]
        with self.assertRaisesRegex(ValueError, 'contiguous'):
            reorder_clips(clips, {0, 2}, 0, 1)
        for start in (1, 3):
            clips[1].start = start
            with self.assertRaisesRegex(ValueError, 'touching'):
                reorder_clips(clips, {0}, 0, 1)
        clips[1].track = 2
        with self.assertRaisesRegex(ValueError, 'one track'):
                reorder_clips(clips, {0, 1}, 0, 1)


class SeekFrameTest(unittest.TestCase):
    def test_frame_step_uses_valid_source_fps_and_clamps(self):
        self.assertAlmostEqual(seek_frame(1.0, 1, 24), 25 / 24)
        self.assertAlmostEqual(seek_frame(1.0, -1, 24), 23 / 24)
        self.assertEqual(seek_frame(-1, -1, 24), 0)
        self.assertAlmostEqual(seek_frame(1.0, 1, 0), 31 / 30)
