"""Exercise keyboard editing/history on a real window with external I/O stubbed."""
import unittest
from pathlib import Path
from unittest.mock import patch

from clip_editor.project import ClipInst, MediaItem
from clip_editor.ui import EditorWindow, Gdk, Gtk


class KeyboardEditingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check() or Gdk.Display.get_default() is None:
            raise unittest.SkipTest('GTK display unavailable')

    def setUp(self):
        for name in ('_restore_autosave', '_schedule_autosave', '_schedule_checkpoint',
                     '_load_media', '_apply_timeline_frame', '_refresh_cache_bar',
                     '_install_media_list'):
            patcher = patch.object(EditorWindow, name, return_value=False)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.win = EditorWindow()
        self.addCleanup(self.win.destroy)
        self.win.media = [MediaItem(id='v', path=Path('/tmp/keyboard-fixture.mp4'), kind='video')]
        self.win.media_info = {'v': {'duration': 20, 'has_audio': True, 'width': 320, 'height': 240}}
        self.win.video_path = self.win.media[0].path
        self.win.video_info = self.win.media_info['v']
        self.win.video_clips = [ClipInst(start=0, out_s=5, media_id='v'),
                               ClipInst(start=5, out_s=5, media_id='v')]
        self.win.sel_v, self.win.sel_vs, self.win.sel_kind = 0, {0}, 'video'
        self.win._refresh_fit()
        self.win._history = []
        self.win._hist_i = -1
        self.win._checkpoint()
        self.win.timeline.grab_focus()

    def key(self, key):
        return self.win._on_key_pressed(None, key, 0, 0)

    def test_move_and_undo_are_atomic_and_escape_preserves_selection(self):
        self.key(Gdk.KEY_m)
        self.assertEqual(self.win.keyboard_mode, 'move')
        self.key(Gdk.KEY_l)
        self.assertEqual(self.win.video_clips[0].start, 1)
        self.assertEqual(len(self.win._history), 2)
        self.key(Gdk.KEY_Escape)
        self.assertEqual(self.win.keyboard_mode, '')
        self.assertEqual(self.win.sel_v, 0)
        self.win._on_undo()
        self.assertEqual(self.win.video_clips[0].start, 0)
        self.win._on_redo()
        self.assertEqual(self.win.video_clips[0].start, 1)

    def test_source_audio_detaches_only_when_changed_and_undo_restores_link(self):
        self.key(Gdk.KEY_j)
        self.assertEqual(self.win.sel_kind, 'audio')
        self.key(Gdk.KEY_m)
        self.key(Gdk.KEY_h)
        self.assertTrue(self.win.use_video_soundtrack)
        self.assertEqual(self.win.audio_clips, [])
        self.key(Gdk.KEY_l)
        self.assertFalse(self.win.use_video_soundtrack)
        self.assertEqual(self.win.audio_clips[0].start, 1)
        self.assertEqual(self.win.video_clips[0].start, 0)
        self.assertEqual(len(self.win._history), 2)
        self.win._on_undo()
        self.assertTrue(self.win.use_video_soundtrack)
        self.assertEqual(self.win.audio_clips, [])

    def test_track_change_and_focus_loss_exit_mode(self):
        self.key(Gdk.KEY_m)
        self.key(Gdk.KEY_j)
        self.assertEqual(self.win.keyboard_mode, '')

    def test_increment_ladder_and_boundary_move(self):
        self.key(Gdk.KEY_m)
        self.key(Gdk.KEY_Up)
        self.assertEqual(self.win.keyboard_increment, 10)
        self.key(Gdk.KEY_Down)
        self.key(Gdk.KEY_Down)
        self.assertEqual(self.win.keyboard_increment, .1)
        self.key(Gdk.KEY_Down)
        self.assertEqual(self.win.keyboard_increment, 'clip')
        self.key(Gdk.KEY_l)
        self.assertEqual(self.win.video_clips[0].start, 5)
        self.key(Gdk.KEY_Escape)
        self.key(Gdk.KEY_m)
        self.assertEqual(self.win.keyboard_increment, 'clip')
        self.key(Gdk.KEY_m)
        self.win.command_entry.grab_focus()
        # Focus controller leave is delivered by GTK's focus transition.
        self.assertEqual(self.win.keyboard_mode, '')

    def test_right_trim_on_audio_ripples_and_undo_restores_everything(self):
        self.key(Gdk.KEY_j)
        self.key(Gdk.KEY_bracketright)
        self.key(Gdk.KEY_h)
        self.assertAlmostEqual(self.win.audio_clips[0].out_s, 4.9)
        self.assertAlmostEqual(self.win.audio_clips[1].start, 4.9)
        self.assertEqual(self.win.video_clips[1].start, 5)
        self.assertEqual(len(self.win._history), 2)
        self.win._on_undo()
        self.assertEqual(self.win.audio_clips, [])
        self.assertTrue(self.win.use_video_soundtrack)

    def test_seek_mode_moves_playhead_without_editing_clips(self):
        before = [c.copy() for c in self.win.video_clips]
        self.key(Gdk.KEY_s)
        self.assertEqual(self.win.keyboard_mode, 'seek')
        self.key(Gdk.KEY_l)
        self.assertAlmostEqual(self.win.timeline.playhead, .1)
        self.key(Gdk.KEY_Down)
        self.key(Gdk.KEY_l)
        self.assertAlmostEqual(self.win.timeline.playhead, 4 / 30)
        self.key(Gdk.KEY_Down)
        self.key(Gdk.KEY_l)
        self.assertAlmostEqual(self.win.timeline.playhead, 5)
        self.assertEqual(self.win.video_clips, before)
        self.assertEqual(len(self.win._history), 1)

    def test_split_uses_playback_speed_and_selects_right_piece(self):
        self.win.video_clips[0].speed = 2
        self.win.timeline.nav_kind = 'video'
        self.win.timeline.nav_track = 1
        self.win.timeline.set_playhead(2)
        self.key(Gdk.KEY_t)
        left, right = self.win.video_clips[:2]
        self.assertAlmostEqual(left.out_s, 4)
        self.assertAlmostEqual(right.in_s, 4)
        self.assertAlmostEqual(right.start, -2)
        self.assertEqual(self.win.sel_v, 1)
        self.assertEqual(len(self.win._history), 2)

    def test_audio_reorder_and_undo(self):
        self.key(Gdk.KEY_j)
        self.key(Gdk.KEY_r)
        self.key(Gdk.KEY_l)
        self.assertEqual([c.start for c in self.win.audio_clips], [5, 0])
        self.assertEqual(self.win.timeline.audio_kind, 'replace')
        self.assertEqual([c.start for c in self.win.timeline.aclips], [5, 0])
        self.assertEqual([c.start for c in self.win.video_clips], [0, 5])
        self.assertEqual(len(self.win._history), 2)
        self.win._on_undo()
        self.assertEqual(self.win.audio_clips, [])
        self.assertTrue(self.win.use_video_soundtrack)

    def test_question_opens_and_closes_help_without_changing_mode_or_history(self):
        before = [c.copy() for c in self.win.video_clips]
        self.key(Gdk.KEY_m)
        history_len = len(self.win._history)
        self.key(Gdk.KEY_question)
        self.assertTrue(self.win.keyboard_help.get_visible())
        self.assertEqual(self.win.keyboard_mode, 'move')
        self.assertEqual(self.win.video_clips, before)
        self.assertEqual(len(self.win._history), history_len)
        self.assertIn('Timeline navigation', self.win.keyboard_help_body.get_text())
        self.key(Gdk.KEY_Escape)
        self.assertFalse(self.win.keyboard_help.get_visible())
        self.assertEqual(self.win.keyboard_mode, 'move')
        self.key(Gdk.KEY_question)
        self.assertTrue(self.win.keyboard_help.get_visible())
        self.key(Gdk.KEY_question)
        self.assertFalse(self.win.keyboard_help.get_visible())
