"""Integration coverage for the redesigned clip popovers and background visuals."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from clip_editor.project import ClipInst, MediaItem
from clip_editor.ui import EditorWindow, Gdk, GLib, Gtk
from clip_editor import theme


class ReviewWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check() or Gdk.Display.get_default() is None:
            raise unittest.SkipTest('GTK display unavailable')

    def setUp(self):
        for name in ('_restore_autosave', '_schedule_autosave', '_schedule_checkpoint',
                     '_load_media', '_apply_timeline_frame', '_refresh_cache_bar',
                     '_install_media_list', '_flush_autosave'):
            p = patch.object(EditorWindow, name, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        self.win = EditorWindow()
        self.addCleanup(self.win.destroy)
        self.addCleanup(self.win._on_close)
        self.win.media = [MediaItem('v', Path('/tmp/review-video.mp4'), 'video'),
                          MediaItem('a', Path('/tmp/review-audio.wav'), 'audio')]
        self.win.media_info = {
            'v': {'duration': 20, 'width': 320, 'height': 240, 'has_audio': True},
            'a': {'duration': 30, 'has_audio': True},
        }
        self.win.video_path = self.win.media[0].path
        self.win.video_info = self.win.media_info['v']
        self.win.video_clips = [ClipInst(in_s=1, out_s=10, media_id='v')]
        self.win.audio_clips = [ClipInst(in_s=3, out_s=15, media_id='a', volume=.5)]
        self.win.use_video_soundtrack = False
        self.win.sel_v, self.win.sel_kind = 0, 'video'
        self.win._refresh_fit()
        self.win._on_clip_select('video', 0)
        self.win._history = []
        self.win._hist_i = -1
        self.win._checkpoint()

    def test_audio_trim_uses_audio_values_and_undo_leaves_video_untouched(self):
        self.win._on_clip_select('audio', 0)
        self.assertEqual(self.win.clip_in_spin.get_value(), 3)
        self.assertEqual(self.win.clip_out_spin.get_value(), 15)
        self.assertEqual(self.win.clip_title.get_text(), 'review-audio.wav')
        self.win.clip_in_spin.set_value(4)
        self.assertEqual(self.win.audio_clips[0].in_s, 4)
        self.assertEqual(self.win.video_clips[0].in_s, 1)
        self.assertEqual(self.win.in_spin.get_value(), 1)
        self.win._on_undo()
        self.assertEqual(self.win.audio_clips[0].in_s, 3)
        self.win._on_redo()
        self.assertEqual(self.win.audio_clips[0].in_s, 4)

    def test_video_trim_clamps_and_updates_legacy_video_range(self):
        self.win.clip_out_spin.set_value(100)
        self.assertEqual(self.win.video_clips[0].out_s, 20)
        self.assertEqual(self.win.out_spin.get_value(), 20)
        self.win.clip_in_spin.set_value(100)
        self.assertAlmostEqual(self.win.video_clips[0].in_s, 19.95)
        self.assertAlmostEqual(self.win.video_clips[0].out_s, 20)
        self.assertEqual(self.win.audio_clips[0].out_s, 15)

    def test_playhead_trim_accounts_for_audio_start_and_speed(self):
        clip = self.win.audio_clips[0]
        clip.start, clip.speed = 5, 2
        self.win._on_clip_select('audio', 0)
        # Used start is 8; two timeline seconds later is source time 7.
        self.win.timeline.playhead = 10
        self.win._set_clip_bound(True)
        self.assertEqual(clip.in_s, 7)
        self.assertEqual(self.win.video_clips[0].in_s, 1)

    def test_trim_controls_lock_for_preview_and_linked_soundtrack(self):
        self.win._compiled_mode = True
        self.win._sync_clip_trim_controls()
        self.assertFalse(self.win.clip_in_spin.get_sensitive())
        self.win.clip_in_spin.set_value(5)
        self.assertEqual(self.win.video_clips[0].in_s, 1)
        self.win._compiled_mode = False
        self.win.audio_clips = []
        self.win.use_video_soundtrack = True
        self.win._sync_timeline_clips()
        self.win._on_clip_select('audio', 0)
        self.assertFalse(self.win.clip_in_spin.get_sensitive())
        self.assertIn('Linked soundtrack', self.win.clip_in_spin.get_tooltip_text())

    def test_subpixel_volume_drag_commits_and_can_be_undone(self):
        self.win._on_clip_select('audio', 0)
        timeline = self.win.timeline
        with patch.object(timeline, 'get_width', return_value=1000):
            x0, _y, x1, _h = timeline._clip_box('audio', 0)
            x, y = (x0 + x1) / 2, timeline._volume_y(0)
            gesture = Mock()
            gesture.get_start_point.return_value = True, x, y
            # Finalization must use the stored change, not query an ended gesture.
            gesture.get_offset.return_value = False, 0, 0
            timeline._on_drag_begin(gesture, x, y)
            timeline._on_drag_update(gesture, 0, .5)
            timeline._on_drag_end(gesture, 0, .5)
        self.assertLess(self.win.audio_clips[0].volume, .5)
        self.win._schedule_autosave.assert_called()
        self.win._on_undo()
        self.assertEqual(self.win.audio_clips[0].volume, .5)

    def test_cancelled_handle_restores_original_volume(self):
        timeline = self.win.timeline
        self.win._on_clip_select('audio', 0)
        with patch.object(timeline, 'get_width', return_value=1000):
            x0, _y, x1, _h = timeline._clip_box('audio', 0)
            x, y = (x0 + x1) / 2, timeline._volume_y(0)
            gesture = Mock()
            gesture.get_start_point.return_value = True, x, y
            timeline._on_drag_begin(gesture, x, y)
            timeline._on_drag_update(gesture, 0, 8)
            timeline._on_drag_cancel()
        self.assertEqual(self.win.audio_clips[0].volume, .5)
        self.assertEqual(timeline._drag_mode, '')
        self.assertTrue(all(p.audio_clips[0].volume == .5 for p in self.win._history))

    def test_closing_one_popover_does_not_take_focus_from_the_other(self):
        with patch.object(self.win.clip_popover, 'get_visible', return_value=False), \
             patch.object(self.win.transition_popover, 'get_visible', return_value=True), \
             patch.object(self.win.timeline, 'grab_focus') as focus:
            self.win._on_popover_closed(self.win.clip_popover)
            focus.assert_not_called()

    def test_visual_jobs_deduplicate_and_ignore_reused_ids_after_project_switch(self):
        with tempfile.TemporaryDirectory() as td, patch('clip_editor.ui.threading.Thread') as thread:
            a, b = Path(td)/'a.mp4', Path(td)/'b.mp4'
            a.touch(); b.touch()
            self.win.media = [MediaItem('m1', a, 'video')]
            self.win.media_info = {'m1': {'has_video': True, 'duration': 10}}
            self.win._queue_visuals('m1')
            self.win._queue_visuals('m1')
            thread.assert_called_once()
            old_key = self.win._visual_key(a)
            self.win._clear_visuals()
            self.win.media = [MediaItem('m1', b, 'video')]
            self.win._visuals_ready(old_key, ('old image', 24), None)
            self.assertNotIn('m1', self.win.timeline.filmstrips)

    def test_failed_filmstrip_decode_still_completes_worker(self):
        key = ('/tmp/broken.mp4', 1, 1)
        with patch('clip_editor.ui._load_filmstrip', side_effect=GLib.Error('invalid PNG')), \
             patch('clip_editor.ui._load_waveform', return_value=([.5], 100)), \
             patch('clip_editor.ui.GLib.idle_add') as idle:
            self.win._build_visuals(key, (Path(key[0]), True, True, 10))
            idle.assert_called_once_with(self.win._visuals_ready, key, None, ([.5], 100))

    def test_visual_worker_limit_aliases_and_close_cleanup(self):
        with tempfile.TemporaryDirectory() as td, patch('clip_editor.ui.threading.Thread') as thread:
            self.win.media = []
            for i in range(4):
                path = Path(td)/f'{i}.mp4'; path.touch()
                self.win.media.append(MediaItem(str(i), path, 'video'))
                self.win.media_info[str(i)] = {'has_video': True, 'duration': 10}
                self.win._queue_visuals(str(i))
            self.assertEqual(thread.call_count, 2)
            first = self.win.media[0]
            self.win.media.append(MediaItem('alias', first.path, 'audio'))
            self.win._visuals_ready(self.win._visual_key(first.path), ('strip', 24), ([.5], 100))
            self.assertEqual(thread.call_count, 3)
            self.assertEqual(self.win.timeline.waves['alias'], ([.5], 100))
            self.assertIn(self.win._on_theme_change, theme._listeners)
            self.win._on_close()
            self.assertNotIn(self.win._on_theme_change, theme._listeners)
            self.win._visuals_ready(self.win._visual_key(self.win.media[1].path), ('late', 24), None)
            self.assertFalse(self.win.timeline.filmstrips)
            self.assertEqual(thread.call_count, 3)

    def test_click_on_fade_knob_does_not_clear_selection_or_seek(self):
        timeline = self.win.timeline
        timeline.sel_v = 0
        with patch.object(timeline, 'get_width', return_value=1000), \
             patch.object(timeline, 'clear_selection') as clear, \
             patch.object(timeline, '_seek_x') as seek:
            x = timeline._fade_knob_x('video', 0, 'in')
            _x0, y, _x1, _h = timeline._clip_box('video', 0)
            timeline._on_pressed(Mock(), 1, x, y - 3)
            clear.assert_not_called()
            seek.assert_not_called()
