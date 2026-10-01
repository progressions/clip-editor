import unittest
from unittest.mock import Mock, patch

from clip_editor.project import TRANSITION_DISSOLVE, ClipInst
from clip_editor.ui import Gdk, Gtk, Timeline


class TimelineHandlesTest(unittest.TestCase):
    """Fade knobs, the audio volume line and cut diamonds on the timeline."""

    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check() or Gdk.Display.get_default() is None:
            raise unittest.SkipTest("GTK display unavailable")

    def setUp(self):
        self.timeline = Timeline()
        self.timeline.audio_kind = "file"
        self.timeline.set_clips(
            vclips=[ClipInst(start=0, out_s=5, media_id="v"),
                    ClipInst(start=5, out_s=5, media_id="v")],
            aclips=[ClipInst(start=0, out_s=10, media_id="a")],
        )
        self.timeline.src_durs = {"v": 5.0, "a": 10.0}
        patcher = patch.object(self.timeline, "get_width", return_value=1000)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _drag(self, x, y, dx, dy):
        gesture = Mock()
        gesture.get_start_point.return_value = (True, x, y)
        gesture.get_offset.return_value = (True, dx, dy)
        self.timeline._on_drag_begin(gesture, x, y)
        self.timeline._on_drag_update(gesture, dx, dy)
        self.timeline._on_drag_end(gesture, dx, dy)

    def test_volume_line_drag_sets_clip_volume(self):
        self.timeline.on_volume = Mock()
        x0, _y, x1, _h = self.timeline._clip_box("audio", 0)
        vy = self.timeline._volume_y(0)
        self.assertEqual(self.timeline._hit_handle((x0 + x1) / 2, vy)[0], "volume")
        self._drag((x0 + x1) / 2, vy, 0, 8)
        self.assertLess(self.timeline.aclips[0].volume, 1.0)
        kind, index, volume, final = self.timeline.on_volume.call_args.args
        self.assertEqual((kind, index, final), ("audio", 0, True))
        self.assertAlmostEqual(volume, self.timeline.aclips[0].volume)

    def test_volume_snaps_to_unity_near_100_percent(self):
        x0, _y, x1, _h = self.timeline._clip_box("audio", 0)
        self._drag((x0 + x1) / 2, self.timeline._volume_y(0), 0, 0.5)
        self.assertEqual(self.timeline.aclips[0].volume, 1.0)

    def test_fade_in_knob_drag_sets_fade(self):
        self.timeline.sel_v = 0
        self.timeline.on_fade = Mock()
        kx = self.timeline._fade_knob_x("video", 0, "in")
        _x0, ky, _x1, _h = self.timeline._clip_box("video", 0)
        self.assertEqual(self.timeline._hit_handle(kx, ky)[0], "fade-in")
        self._drag(kx, ky, 100, 0)
        self.assertGreater(self.timeline.vclips[0].fade_in_s, 0.0)
        self.assertEqual(self.timeline.vclips[0].fade_out_s, 0.0)
        self.assertTrue(self.timeline.on_fade.call_args.args[-1])

    def test_knobs_hidden_on_unselected_clip_without_fades(self):
        self.timeline.sel_v = 0
        kx = self.timeline._fade_knob_x("video", 1, "in")
        _x0, ky, _x1, _h = self.timeline._clip_box("video", 1)
        self.assertNotEqual(self.timeline._hit_handle(kx, ky)[0], "fade-in")

    def test_cut_diamond_click_opens_transition(self):
        self.timeline.vclips[0].transition = TRANSITION_DISSOLVE
        self.timeline.on_transition = Mock()
        self.assertEqual(self.timeline._cuts(), [(0, 5.0)])
        _x0, y, x1, h = self.timeline._clip_box("video", 0)
        self._drag(x1, y + h, 0, 0)
        self.timeline.on_transition.assert_called_once_with(0)

    def test_no_cut_marker_when_clips_do_not_touch(self):
        self.timeline.vclips[1].start = 6
        self.assertEqual(self.timeline._cuts(), [])


if __name__ == "__main__":
    unittest.main()
