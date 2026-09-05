import unittest
from clip_editor.project import ClipInst
from clip_editor.keyboard_edits import clip_boundary_delta, move_clips


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

    def test_clip_boundary_delta_uses_visible_starts_and_excludes_group(self):
        clips = [ClipInst(start=10), ClipInst(start=0, in_s=2), ClipInst(start=5)]
        self.assertEqual(clip_boundary_delta(clips, 0, {0}, -1), -5)
        self.assertEqual(clip_boundary_delta(clips, 1, {0, 1}, 1), 3)
        self.assertIsNone(clip_boundary_delta(clips, 1, {0, 1}, -1))
        self.assertIsNone(clip_boundary_delta(clips, 0, {0, 1, 2}, 1))
