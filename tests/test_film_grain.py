"""Grain persists through edits and changes the rendered luma without color noise."""

import hashlib
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from clip_editor.export import _flatten_clips, _timeline_parts, build_cmd
from clip_editor.preview import (
    FINAL_PROFILE, PREVIEW_PROFILE, build_timeline_segments,
    rebase_clips_for_window, render_fingerprint,
)
from clip_editor.project import (
    ClipInst, MediaItem, Project, clip_from_dict, clip_to_dict, from_dict,
    normalize_grain_strength, to_dict,
)
from clip_editor.ui import EditorWindow, Gdk, Gtk


class GrainModelTest(unittest.TestCase):
    def test_roundtrip_copy_split_and_old_projects(self):
        clip = ClipInst(out_s=10, grain_strength=35, reverse=True, speed=2)
        project = Project(video_clips=[clip])
        restored = from_dict(json.loads(json.dumps(to_dict(project))))
        self.assertEqual(restored.video_clips[0].grain_strength, 35)
        copied = clip.copy()
        copied.grain_strength = 80
        self.assertEqual(clip.grain_strength, 35)
        self.assertEqual(clip.split_at(2).grain_strength, 35)
        legacy = from_dict({'format': 'clip-editor-project', 'version': 9,
                            'video_clips': [{'out_s': 2}]})
        self.assertEqual(legacy.video_clips[0].grain_strength, 0)
        self.assertNotIn('grain_strength', clip_to_dict(ClipInst()))

    def test_invalid_strength_is_safe(self):
        for value in (None, 'bad', float('nan'), float('inf'), -10):
            with self.subTest(value=value):
                self.assertEqual(normalize_grain_strength(value), 0)
                self.assertEqual(clip_from_dict({'grain_strength': value}).grain_strength, 0)
        self.assertEqual(normalize_grain_strength(150), 100)

    def test_overlap_and_preview_window_preserve_each_clips_strength(self):
        clips = [ClipInst(out_s=10, grain_strength=20),
                 ClipInst(start=3, out_s=2, grain_strength=80)]
        flat = _flatten_clips(clips, 10)
        self.assertEqual([r[15] for r in flat], [20, 80, 20])
        self.assertEqual([p[14] for p in _timeline_parts(flat, 10)], [20, 80, 20])
        cut = rebase_clips_for_window(clips, 1, 6, 10)
        self.assertEqual([c.grain_strength for c in cut], [20, 80])

    def test_grain_change_invalidates_only_affected_preview_segment(self):
        clips = [ClipInst(out_s=2), ClipInst(start=2, out_s=2)]
        kwargs = dict(aspect='16:9', pan_x=.5, pan_y=.5, audio_follows_in=False,
                      use_video_soundtrack=False, audio_offset=0,
                      video_clips=clips, audio_clips=[], media=[])
        before = render_fingerprint(**kwargs, kind='full')
        segments = build_timeline_segments(**kwargs, src_durs=4)
        clips[0].grain_strength = 40
        self.assertNotEqual(render_fingerprint(**kwargs, kind='full'), before)
        changed = build_timeline_segments(**kwargs, src_durs=4)
        self.assertNotEqual(segments[0].fingerprint, changed[0].fingerprint)
        self.assertEqual(segments[1].fingerprint, changed[1].fingerprint)


class GrainWindowTest(unittest.TestCase):
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
        self.win.media = [MediaItem('v', Path('/tmp/grain-video.mp4'), 'video')]
        self.win.media_info = {'v': {'duration': 10, 'width': 320, 'height': 240}}
        self.win.video_path = self.win.media[0].path
        self.win.video_info = self.win.media_info['v']
        self.win.video_clips = [ClipInst(out_s=2, media_id='v'),
                                ClipInst(start=2, out_s=2, media_id='v')]
        self.win.sel_v, self.win.sel_vs, self.win.sel_kind = 0, {0}, 'video'
        self.win._sync_timeline_clips()
        self.win._history = []
        self.win._hist_i = -1
        self.win._checkpoint()

    def test_selection_multi_edit_undo_redo_and_off(self):
        self.win.grain_scale.set_value(35)
        self.win._checkpoint()
        self.assertEqual([c.grain_strength for c in self.win.video_clips], [35, 0])
        self.win._on_undo()
        self.assertEqual(self.win.video_clips[0].grain_strength, 0)
        self.assertEqual(self.win.grain_scale.get_value(), 0)
        self.win._on_redo()
        self.assertEqual(self.win.video_clips[0].grain_strength, 35)
        self.assertEqual(self.win.grain_scale.get_value(), 35)
        self.win.sel_vs = {0, 1}
        self.win._sync_grain_controls()
        self.assertIn('2 selected', self.win.grain_hint.get_text())
        self.win.grain_scale.set_value(80)
        self.assertEqual([c.grain_strength for c in self.win.video_clips], [80, 80])
        self.win.grain_scale.set_value(0)
        self.assertEqual([c.grain_strength for c in self.win.video_clips], [0, 0])

    def command(self, text):
        self.win._show_command_line()
        self.assertFalse(self.win._on_key_pressed(None, Gdk.KEY_space, 0, 0))
        self.win.command_entry.set_text(text)
        self.win.command_entry.emit('activate')

    def test_command_updates_slider_preview_and_undo_redo_atomically(self):
        before = self.win._current_render_fingerprint(kind='play')
        self.win._schedule_autosave.reset_mock()
        self.win._refresh_cache_bar.reset_mock()
        self.command('grain 25')
        self.assertEqual([c.grain_strength for c in self.win.video_clips], [25, 0])
        self.assertEqual(self.win.grain_scale.get_value(), 25)
        self.assertEqual(len(self.win._history), 2)
        self.assertFalse(self.win.command_revealer.get_reveal_child())
        self.assertIs(self.win.get_focus(), self.win.timeline)
        self.assertNotEqual(self.win._current_render_fingerprint(kind='play'), before)
        self.win._refresh_cache_bar.assert_called()
        self.win._schedule_autosave.assert_called()
        self.win._on_undo()
        self.assertEqual(self.win.video_clips[0].grain_strength, 0)
        self.win._on_redo()
        self.assertEqual(self.win.video_clips[0].grain_strength, 25)

    def test_command_turns_mixed_selection_off_when_primary_is_already_off(self):
        self.win.video_clips[1].grain_strength = 60
        self.win.sel_vs = {0, 1}
        self.win._sync_grain_controls()
        self.assertEqual(self.win.grain_scale.get_value(), 0)
        self.command(':grain 0')
        self.assertEqual([c.grain_strength for c in self.win.video_clips], [0, 0])
        self.assertEqual(len(self.win._history), 3)
        self.win._on_undo()
        self.assertEqual([c.grain_strength for c in self.win.video_clips], [0, 60])
        self.win._on_redo()
        self.command('grain 0')
        self.assertEqual(len(self.win._history), 3)

    def test_command_keeps_prior_slider_edits_as_a_separate_undo_step(self):
        self.win.grain_scale.set_value(10)
        self.assertEqual(len(self.win._history), 1)
        self.command('grain 40')
        self.assertEqual(len(self.win._history), 3)
        self.win._on_undo()
        self.assertEqual(self.win.video_clips[0].grain_strength, 10)
        self.win._on_undo()
        self.assertEqual(self.win.video_clips[0].grain_strength, 0)

    def test_command_requires_video_selection_and_obeys_rendering_locks(self):
        for state in ('audio', 'unselected', 'rendering', 'exporting', 'compiled'):
            with self.subTest(state=state):
                self.win.sel_v, self.win.sel_vs = 0, {0}
                self.win.sel_kind = 'audio' if state == 'audio' else 'video'
                if state == 'unselected':
                    self.win.sel_v, self.win.sel_vs = -1, set()
                self.win._preview_rendering = state == 'rendering'
                self.win.exporting = state == 'exporting'
                self.win._compiled_mode = state == 'compiled'
                self.command('grain 25')
                self.assertEqual([c.grain_strength for c in self.win.video_clips], [0, 0])
                self.assertEqual(len(self.win._history), 1)
        self.win._compiled_mode = self.win._preview_rendering = self.win.exporting = False

    def test_invalid_command_reports_usage_without_editing(self):
        for text in ('grain', 'grain -1', 'grain 101', 'grain 1.5', 'grain bad'):
            with self.subTest(text=text):
                self.command(text)
                self.assertIn('Usage: :grain', self.win.status.get_text())
                self.assertEqual([c.grain_strength for c in self.win.video_clips], [0, 0])
                self.assertEqual(len(self.win._history), 1)

    def test_audio_selection_rendering_and_compiled_mode_block_grain(self):
        for state in ('audio', 'rendering', 'compiled'):
            with self.subTest(state=state):
                self.win.sel_kind = 'audio' if state == 'audio' else 'video'
                self.win._preview_rendering = state == 'rendering'
                self.win._compiled_mode = state == 'compiled'
                self.win._sync_grain_controls()
                self.assertFalse(self.win.grain_scale.get_sensitive())
                self.win.grain_scale.set_value(60)
                self.assertEqual(self.win.video_clips[0].grain_strength, 0)
        self.win._compiled_mode = False
        self.win._preview_rendering = False


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg unavailable')
class GrainRenderTest(unittest.TestCase):
    def test_real_renders_are_animated_monochrome_and_strength_matches_profiles(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / 'source.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                            'color=c=gray:s=160x90:r=12:d=1', '-c:v', 'libx264', str(video)],
                           check=True, capture_output=True, timeout=30)
            source_hash = hashlib.sha256(video.read_bytes()).hexdigest()
            deviations = {}
            for profile in (FINAL_PROFILE, PREVIEW_PROFILE):
                for strength in (0, 20, 80):
                    with self.subTest(profile=profile.name, strength=strength):
                        out = root / f'{profile.name}-{strength}.mp4'
                        with patch('clip_editor.export.profile_dest_size', return_value=(160, 90)):
                            cmd, _ = build_cmd(video, out, audio=None, aspect='16:9',
                                pan_x=.5, pan_y=.5, in_s=0, out_s=1,
                                audio_follows_in=False, audio_offset=0,
                                use_video_soundtrack=False, profile=profile,
                                video_clips=[ClipInst(out_s=1, grain_strength=strength)])
                        flag = '-filter_complex' if '-filter_complex' in cmd else '-filter:v'
                        filters = cmd[cmd.index(flag)+1]
                        if strength:
                            self.assertIn(f'noise=c0s={strength}:c0f=t+u:c1s=0:c2s=0:c3s=0', filters)
                            self.assertEqual(cmd[cmd.index('-crf')+1], str(FINAL_PROFILE.crf))
                        else:
                            self.assertNotIn('noise=', filters)
                        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
                        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(out),
                            '-frames:v', '3', '-pix_fmt', 'yuv420p', '-f', 'rawvideo', '-'], timeout=30)
                        pixels = 160 * 90
                        frame_size = pixels * 3 // 2
                        self.assertEqual(len(raw), 3 * frame_size)
                        frames = [raw[i*frame_size:(i+1)*frame_size] for i in range(3)]
                        deviations[profile.name, strength] = statistics.pstdev(frames[0][:pixels])
                        # Neutral chroma is unchanged, even at heavy grain.
                        for frame in frames:
                            self.assertLessEqual(max(abs(v-128) for v in frame[pixels:]), 1)
                        if strength:
                            self.assertNotEqual(frames[0][:pixels], frames[1][:pixels])
                self.assertLess(deviations[profile.name, 0], 1)
                self.assertGreater(deviations[profile.name, 20], 3)
                self.assertGreater(deviations[profile.name, 80], 2*deviations[profile.name, 20])
            for strength in (20, 80):
                ratio = deviations['preview', strength] / deviations['final', strength]
                self.assertGreater(ratio, .65)
                self.assertLess(ratio, 1.35)
            self.assertEqual(hashlib.sha256(video.read_bytes()).hexdigest(), source_hash)


if __name__ == '__main__':
    unittest.main()
