import unittest
from unittest.mock import Mock, patch

from clip_editor.project import ClipInst
from clip_editor.ui import Gdk, Gtk, Timeline


class PlayheadEdgeScrollTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check() or Gdk.Display.get_default() is None:
            raise unittest.SkipTest('GTK display unavailable')

    def setUp(self):
        self.timeline = Timeline()
        self.timeline.set_clips(vclips=[ClipInst(out_s=60)], aclips=[])
        self.adjustment = Gtk.Adjustment(value=300, lower=0, upper=2400,
                                         page_size=800)
        for name, value in [('_scroll_adjustment', self.adjustment),
                            ('get_width', 2400), ('_viewport_width', 800)]:
            patcher = patch.object(self.timeline, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.timeline._stop_seek_scroll)
        self.timeline._drag_mode = 'seek'
        self.timeline.on_seek = Mock()

    def test_held_left_edge_scrolls_and_seeks_all_the_way_to_zero(self):
        before = vars(self.timeline.vclips[0]).copy()
        self.timeline._drag_seek_x(305)
        first = self.timeline.playhead
        for _ in range(30):
            self.timeline._scroll_seek_edge()
        self.assertGreater(first, 0)
        self.assertEqual(self.adjustment.get_value(), 0)
        self.assertEqual(self.timeline.playhead, 0)
        self.timeline.on_seek.assert_called_with(0)
        self.assertEqual(vars(self.timeline.vclips[0]), before)

    def test_held_right_edge_scrolls_without_further_pointer_events(self):
        self.timeline._drag_seek_x(1095)
        first = self.timeline.playhead
        self.timeline._scroll_seek_edge()
        self.assertGreater(self.adjustment.get_value(), 300)
        self.assertGreater(self.timeline.playhead, first)
        for _ in range(100):
            self.timeline._scroll_seek_edge()
        self.assertEqual(self.adjustment.get_value(), 1600)

    def test_return_to_center_stops_scrolling_and_reversing_works(self):
        self.timeline._drag_seek_x(1095)
        self.timeline._scroll_seek_edge()
        position = self.adjustment.get_value()
        self.timeline._drag_seek_x(position + 400)
        self.timeline._scroll_seek_edge()
        self.assertEqual(self.adjustment.get_value(), position)
        self.timeline._drag_seek_x(position + 5)
        self.timeline._scroll_seek_edge()
        self.assertLess(self.adjustment.get_value(), position)

    def test_drag_end_and_unmap_remove_timer(self):
        self.timeline._drag_seek_x(305)
        self.assertTrue(self.timeline._seek_scroll_source)
        self.timeline._on_drag_end()
        self.assertEqual(self.timeline._seek_scroll_source, 0)
        self.timeline._drag_mode = 'seek'
        self.timeline._drag_seek_x(305)
        self.timeline.emit('unmap')
        self.assertEqual(self.timeline._seek_scroll_source, 0)

    def test_no_scrolling_for_clip_drags_or_unscrollable_view(self):
        self.timeline._drag_mode = 'video'
        self.assertFalse(self.timeline._scroll_seek_edge())
        self.timeline._drag_mode = 'seek'
        self.adjustment.configure(0, 0, 800, 10, 100, 800)
        self.timeline._drag_seek_x(795)
        self.timeline._scroll_seek_edge()
        self.assertEqual(self.adjustment.get_value(), 0)

    def test_read_only_preview_seeking_also_scrolls(self):
        self.timeline.read_only = True
        gesture = Mock()
        gesture.get_start_point.return_value = (True, 305, 8)
        self.timeline._on_drag_begin(gesture, 305, 8)
        self.assertEqual(self.timeline._drag_mode, 'seek')
        self.assertTrue(self.timeline._seek_scroll_source)
        self.timeline._scroll_seek_edge()
        self.assertLess(self.adjustment.get_value(), 300)
