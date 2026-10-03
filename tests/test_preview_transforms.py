"""Direct preview edits target selection, not whichever clip touches the playhead."""
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from clip_editor.project import ClipInst, MediaItem
from clip_editor.ui import EditorWindow, Gdk, Gtk


class PreviewTransformTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check() or Gdk.Display.get_default() is None:
            raise unittest.SkipTest('GTK display unavailable')

    def setUp(self):
        for name in ('_restore_autosave', '_schedule_autosave', '_schedule_checkpoint',
                     '_load_media', '_refresh_cache_bar', '_install_media_list', '_flush_autosave'):
            p = patch.object(EditorWindow, name, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(Gtk.MediaFile, "new_for_filename", return_value=Mock())
        p.start()
        self.addCleanup(p.stop)
        self.win = EditorWindow()
        self.addCleanup(self.win.destroy)
        self.addCleanup(self.win._on_close)
        self.win.media = [MediaItem('v', Path('/tmp/preview-transform.mp4'), 'video')]
        self.win.media_info = {'v': dict(width=1920, height=1080, duration=20)}
        self.win.video_path = self.win.media[0].path
        self.win.video_info = self.win.media_info['v']
        self.win.video_clips = [ClipInst(start=0, out_s=5, media_id='v', scale=2),
                               ClipInst(start=5, out_s=5, media_id='v')]
        self.win.sel_kind, self.win.sel_v, self.win.sel_vs = 'video', 0, {0}
        self.win._vmedia = Mock()
        self.win.preview.set_media = Mock()
        self.win._sync_transform_controls()
        self.win._history = []
        self.win._hist_i = -1
        self.win._checkpoint()
        for dimension in ('get_width', 'get_height'):
            p = patch.object(self.win.preview, dimension, return_value=500)
            p.start()
            self.addCleanup(p.stop)

    def test_drag_changes_selected_xy_not_global_pan_or_adjacent_clip(self):
        self.win.timeline.playhead = 5
        other = self.win.video_clips[1].copy()
        self.win._cached_playback_available = Mock(return_value=True)
        self.win._show_playthrough = Mock()
        self.win.preview._drag_begin()
        self.win.preview._drag_update(None, 20, 30)
        clip = self.win.video_clips[0]
        self.assertGreater(clip.transform_x, 0)
        self.assertGreater(clip.transform_y, 0)
        self.assertEqual(self.win.transform_x_spin.get_value(), clip.transform_x)
        self.assertEqual(self.win.transform_y_spin.get_value(), clip.transform_y)
        self.assertEqual((self.win.preview.pan_x, self.win.preview.pan_y), (.5, .5))
        self.assertEqual(self.win.video_clips[1], other)
        self.win._show_playthrough.assert_not_called()
        self.assertEqual(self.win.timeline.playhead, 0)
        self.win.preview._drag_end()
        self.assertEqual(len(self.win._history), 2)
        self.assertEqual(self.win._history[-1].video_clips[0].transform_x, clip.transform_x)

    def test_selected_lower_track_is_edited_instead_of_upper_track(self):
        self.win.video_clips[1].start = 0
        self.win.video_clips[1].track = 2
        self.assertIs(self.win._video_at(1), self.win.video_clips[1])
        self.win.timeline.playhead = 1
        self.win.preview._drag_begin()
        self.win.preview._drag_update(None, -10, -20)
        self.win.preview._drag_end()
        self.assertLess(self.win.video_clips[0].transform_x, 0)
        self.assertEqual(self.win.video_clips[1].transform_x, 0)
        self.assertEqual(self.win.preview.transform_x, self.win.video_clips[0].transform_x)

    def test_numeric_transform_bypasses_cached_frame_at_cut(self):
        self.win.timeline.playhead = 5
        self.win._cached_playback_available = Mock(return_value=True)
        self.win._show_playthrough = Mock()
        self.win.transform_x_spin.set_value(100)
        self.assertEqual(self.win.preview.transform_x, 100)
        self.assertEqual(self.win.video_clips[1].transform_x, 0)
        self.win._show_playthrough.assert_not_called()

    def test_unselected_or_locked_preview_cannot_edit(self):
        for kind, locked in (('audio', False), ('video', True)):
            self.win.sel_kind = kind
            self.win.preview.read_only = locked
            self.win.preview._drag_begin()
            self.win.preview._drag_update(None, 20, 20)
            self.win.preview._drag_end()
            self.assertEqual(self.win.video_clips[0].transform_x, 0)

    def test_drag_does_not_switch_target_if_selection_changes(self):
        self.win.preview._drag_begin()
        self.win.sel_v = 1
        self.win.preview._drag_update(None, 20, 20)
        self.win.preview._drag_end()
        self.assertEqual([c.transform_x for c in self.win.video_clips], [0, 0])

    def test_corner_resize_preserves_opposite_corner_and_commits_once(self):
        from clip_editor.aspects import cover_source_placement
        self.win.aspect = '1:1'
        self.win.media_info['v'].update(width=1080, height=1080)
        clip = self.win.video_clips[0]
        clip.scale = 1
        self.win._sync_transform_controls()
        self.win._apply_timeline_frame(0, start_media=False, edit_clip=clip)
        dw, dh = self.win.preview.output_width, self.win.preview.output_height
        before = cover_source_placement(1080, 1080, dw, dh)
        right, bottom = self.win.preview._selection_box()[2:]
        self.win.preview._drag_begin(None, right, bottom)
        self.assertEqual(self.win._preview_drag_mode, 'se')
        self.win.preview._drag_update(None, -125, -125)
        after = cover_source_placement(1080, 1080, dw, dh,
                                      transform_x=clip.transform_x,
                                      transform_y=clip.transform_y, scale=clip.scale)
        self.assertAlmostEqual(clip.scale, .75)
        self.assertEqual((before.x, before.y), (after.x, after.y))
        self.win.preview._drag_end()
        self.assertEqual(len(self.win._history), 2)

    def test_returning_pointer_to_origin_restores_transform(self):
        original = self.win.video_clips[0].copy()
        self.win.preview._drag_begin()
        self.win.preview._drag_update(None, 30, -20)
        self.win.preview._drag_update(None, 0, 0)
        self.win.preview._drag_end()
        self.assertEqual(self.win.video_clips[0], original)
        self.assertEqual(len(self.win._history), 1)

    def test_cancel_restores_transform_without_checkpoint(self):
        original = self.win.video_clips[0].copy()
        self.win.preview._drag_begin()
        self.win.preview._drag_update(None, 30, -20)
        self.win.preview._drag_cancel()
        self.win.preview._drag_end()
        self.assertEqual(self.win.video_clips[0], original)
        self.assertEqual(len(self.win._history), 1)

    def test_outside_small_overlay_does_not_drag_and_cached_outline_is_available(self):
        self.win.video_clips[0].scale = .1
        self.win._sync_transform_controls()
        self.win.preview.layers = []  # Baked playback has no individual paintables.
        box = self.win.preview._selection_box()
        self.assertIsNotNone(box)
        self.win.preview._drag_begin(None, 0, 0)
        self.win.preview._drag_update(None, 100, 100)
        self.assertEqual(self.win.video_clips[0].transform_x, 0)
        self.assertEqual(self.win.preview._hit_transform((box[0]+box[2])/2, (box[1]+box[3])/2), 'move')

    def test_layer_order_and_decoder_cleanup(self):
        self.win.video_clips[1].start = 0
        self.win.video_clips[1].track = 2
        self.win._sync_preview_layers(1, start_media=False)
        self.assertEqual([row[0].track for row in self.win.preview.layers], [1, 2])
        lower = next(iter(self.win._layer_media.values()))[0]
        self.win._clear_preview_layers()
        lower.clear.assert_called_once()
        self.assertFalse(self.win.preview.layers)
        self.assertFalse(self.win._layer_media)

    def test_paused_seek_waits_for_prepared_media(self):
        media = self.win._vmedia
        media.is_prepared.return_value = False
        self.win._play_media_at(2.5, start_media=False)
        callback = media.connect.call_args.args[1]
        media.seek.assert_not_called()
        media.is_prepared.return_value = True
        callback()
        media.seek.assert_called_once_with(2_500_000)
        media.play.assert_not_called()
        media.pause.assert_called_once()

    def test_stopping_before_media_prepares_does_not_restart_playback(self):
        media = self.win._vmedia
        media.is_prepared.return_value = False
        self.win.playing = True
        self.win._play_media_at(1)
        callback = media.connect.call_args.args[1]
        self.win._stop()
        media.play.reset_mock()
        media.is_prepared.return_value = True
        callback()
        media.play.assert_not_called()

    def test_snapshot_draws_both_layers_and_blanks_gaps(self):
        self.win.video_clips[1].track = 2
        self.win.video_clips[1].scale = .5
        lower, upper = Mock(), Mock()
        self.win.preview.layers = [(self.win.video_clips[0], lower, 1920, 1080),
                                   (self.win.video_clips[1], upper, 1920, 1080)]
        self.win.preview.do_snapshot(Gtk.Snapshot())
        lower.snapshot.assert_called_once()
        upper.snapshot.assert_called_once()
        self.win.preview.set_blank(True)
        self.win.preview.do_snapshot(Gtk.Snapshot())
        lower.snapshot.assert_called_once()
        upper.snapshot.assert_called_once()
