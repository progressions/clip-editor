import unittest
from unittest.mock import patch

from clip_editor.project import ClipInst
from clip_editor.ui import Gdk, Gtk, Timeline


class TimelineZoomTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check() or Gdk.Display.get_default() is None:
            raise unittest.SkipTest('GTK display unavailable')

    def setUp(self):
        self.timeline = Timeline()
        self.viewport = patch.object(self.timeline, '_viewport_width', return_value=800)
        self.viewport.start()
        self.addCleanup(self.viewport.stop)
        self.timeline.set_clips(vclips=[ClipInst(out_s=600)],
                                aclips=[ClipInst(start=600, out_s=120)])

    def test_fit_includes_audio_beyond_video_and_resizes(self):
        self.assertGreater(self.timeline._desired_width(), 800)
        self.timeline.fit_view()
        self.assertEqual(self.timeline.duration, 720)
        self.assertEqual(self.timeline._desired_width(), 800)
        with patch.object(self.timeline, '_viewport_width', return_value=1200):
            self.assertEqual(self.timeline._desired_width(), 1200)

    def test_zoom_changes_width_without_editing_clips(self):
        before = [vars(c).copy() for c in self.timeline.vclips + self.timeline.aclips]
        self.timeline.fit_view()
        self.timeline.zoom_view(1.5)
        zoomed = self.timeline._desired_width()
        self.assertGreater(zoomed, 800)
        self.timeline.zoom_view(1 / 1.5)
        self.assertLess(self.timeline._desired_width(), zoomed)
        self.assertEqual(before, [vars(c).copy() for c in self.timeline.vclips + self.timeline.aclips])
        for _ in range(10):
            self.timeline.zoom_view(1 / 1.5)
        self.assertEqual(self.timeline._desired_width(), 800)

    def test_fit_resets_scroll_and_empty_timeline_is_safe(self):
        scroll = Gtk.ScrolledWindow()
        scroll.set_child(self.timeline)
        adj = scroll.get_hadjustment()
        adj.configure(100, 0, 1000, 10, 100, 200)
        self.timeline.fit_view()
        self.assertEqual(adj.get_value(), 0)
        self.timeline.set_clips(vclips=[], aclips=[])
        self.timeline.zoom_view(1.5)
        self.timeline.fit_view()
        self.assertEqual(self.timeline._desired_width(), 800)
