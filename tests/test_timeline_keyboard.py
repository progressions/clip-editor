import unittest
from clip_editor.ui import Gdk, Gtk, Timeline
from clip_editor.project import ClipInst


class TimelineNavigationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check() or Gdk.Display.get_default() is None:
            raise unittest.SkipTest('GTK display unavailable')

    def test_audio_navigation_selection_and_empty_track(self):
        timeline = Timeline()
        selected = []
        timeline.on_select = lambda *args: selected.append(args)
        timeline.set_clips(
            vclips=[ClipInst(out_s=5)],
            aclips=[ClipInst(start=5, out_s=3),
                    ClipInst(start=0, in_s=1, out_s=4),
                    ClipInst(start=9, out_s=3)],
        )
        timeline.set_playhead(2)
        timeline.move_navigation_track(1)
        self.assertEqual((timeline.nav_kind, timeline.nav_track), ('audio', 1))
        self.assertEqual(selected[-1], ('audio', 1, frozenset({1})))
        timeline.move_clip_selection(1, extend=True)
        self.assertEqual(selected[-1], ('audio', 0, frozenset({0, 1})))
        timeline.move_clip_selection(1, extend=False)
        self.assertEqual(selected[-1], ('audio', 2, frozenset({2})))
        timeline.move_navigation_track(1)
        self.assertEqual(selected[-1], ('audio', -1, frozenset()))
        self.assertEqual((timeline.sel_v, timeline.sel_a), (-1, -1))

    def test_source_audio_is_selectable_without_detaching(self):
        timeline = Timeline()
        timeline.set_clips(vclips=[ClipInst(out_s=5)],
                           aclips=[ClipInst(out_s=5)], audio_kind='source')
        timeline.move_navigation_track(1)
        self.assertEqual(timeline.sel_a, 0)
        self.assertEqual(timeline.audio_kind, 'source')
        timeline.clear_selection()
        self.assertEqual(timeline.sel_as, set())
