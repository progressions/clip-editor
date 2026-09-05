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
        self.key(Gdk.KEY_m)
        self.win.command_entry.grab_focus()
        # Focus controller leave is delivered by GTK's focus transition.
        self.assertEqual(self.win.keyboard_mode, '')
