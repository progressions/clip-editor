"""Audio layers, persisted gain, and rendered samples agree."""
import array
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from clip_editor.project import (ClipInst, MediaItem, Project, from_dict, to_dict,
                                 normalize_volume, normalize_track_volumes)
from clip_editor.preview import PREVIEW_PROFILE, rebase_clips_for_window
from clip_editor.export import build_cmd, _flatten_clips, _timeline_parts
from clip_editor.ui import EditorWindow, Gdk, Gtk


class AudioVolumeModelTest(unittest.TestCase):
    def test_roundtrip_mute_tracks_copy_split_and_old_projects(self):
        c = ClipInst(out_s=10, volume=0)
        p = Project(audio_clips=[c], audio_track_volumes={1: .35, 2: 0})
        restored = from_dict(json.loads(json.dumps(to_dict(p))))
        self.assertEqual(restored.audio_clips[0].volume, 0)
        self.assertEqual(restored.audio_track_volumes, {1: .35, 2: 0})
        self.assertEqual(c.copy().volume, 0)
        self.assertEqual(c.split_at(5).volume, 0)
        legacy = from_dict({'format': 'clip-editor-project', 'version': 7,
                            'audio_clips': [{'out_s': 2}]})
        self.assertEqual(legacy.audio_clips[0].volume, 1)
        self.assertEqual(legacy.audio_track_volumes, {1: 1, 2: 1})
        self.assertEqual(normalize_volume(float('nan')), 1)
        self.assertEqual(normalize_volume('bad'), 1)
        self.assertEqual(normalize_volume(5), 2)
        self.assertEqual(normalize_track_volumes(None), {1: 1, 2: 1})

    def test_overlap_remnants_and_cut_preview_retain_gain(self):
        clips = [ClipInst(out_s=10, volume=.3), ClipInst(start=3, out_s=2, volume=.7)]
        flat = _flatten_clips(clips, 10)
        self.assertEqual([r[13] for r in flat], [.3, .7, .3])
        self.assertEqual([r[12] for r in _timeline_parts(flat, 10)], [.3, .7, .3])
        cut = rebase_clips_for_window(clips, 1, 6, 10)
        self.assertEqual([c.volume for c in cut], [.3, .7])


class AudioMixerWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not Gtk.init_check() or Gdk.Display.get_default() is None:
            raise unittest.SkipTest('GTK display unavailable')

    def setUp(self):
        for name in ('_restore_autosave', '_schedule_autosave', '_schedule_checkpoint',
                     '_load_media', '_apply_timeline_frame', '_refresh_cache_bar',
                     '_install_media_list'):
            p = patch.object(EditorWindow, name, return_value=False)
            p.start()
            self.addCleanup(p.stop)
        self.win = EditorWindow()
        self.addCleanup(self.win.destroy)
        self.win.media = [MediaItem('v', Path('/tmp/mixer-video.mp4'), 'video'),
                          MediaItem('a', Path('/tmp/mixer-a.wav'), 'audio'),
                          MediaItem('b', Path('/tmp/mixer-b.wav'), 'audio')]
        self.win.media_info = {m.id: {'duration': 10, 'has_audio': True,
                                      'width': 320, 'height': 240} for m in self.win.media}
        self.win.video_path = self.win.media[0].path
        self.win.video_info = self.win.media_info['v']
        self.win.video_clips = [ClipInst(out_s=10, media_id='v')]
        self.win.sel_v, self.win.sel_vs, self.win.sel_kind = 0, {0}, 'video'
        self.win._refresh_fit()
        self.win._history = []
        self.win._hist_i = -1
        self.win._checkpoint()

    def test_add_preserves_soundtrack_and_uses_second_lane(self):
        self.win._place_clip('audio', 0, 'a')
        self.assertEqual([c.track for c in self.win.audio_clips], [1, 2])
        self.assertEqual(self.win.audio_clips[1].media_id, 'a')
        source = self.win._clip_item(self.win.audio_clips[0], 'audio')
        self.assertEqual(source.path, self.win.video_path)
        self.assertEqual(source.kind, 'audio')
        self.assertEqual(len(self.win._preview_audio_specs(1)), 2)
        saved = from_dict(json.loads(json.dumps(to_dict(self.win._current_project()))))
        self.assertEqual([c.track for c in saved.audio_clips], [1, 2])
        self.assertEqual(saved.audio_clips[0].media_id, source.id)
        self.win._on_undo()
        self.assertEqual(self.win.audio_clips, [])
        self.assertTrue(self.win.use_video_soundtrack)
        self.win._on_redo()
        self.assertEqual([c.track for c in self.win.audio_clips], [1, 2])

    def test_add_two_external_files_and_keep_full_lanes_unchanged(self):
        self.win.use_video_soundtrack = False
        with patch('clip_editor.ui.probe', return_value={'has_audio': True, 'duration': 10}):
            self.win._add_media(Path('/tmp/mixer-a.wav'))
            self.win._add_media(Path('/tmp/mixer-b.wav'))
        self.assertEqual([(c.media_id, c.track) for c in self.win.audio_clips], [('a', 1), ('b', 2)])
        self.win._place_clip('audio', 0, 'a')
        self.assertEqual(len(self.win.audio_clips), 2)

    def test_clip_and_track_gain_multiply_preview_render_history_and_cache(self):
        self.win.use_video_soundtrack = False
        self.win._place_clip('audio', 0, 'a')
        self.win._place_clip('audio', 0, 'b')
        original_key = self.win._current_render_fingerprint(kind='play')
        self.win.audio_volume_spins['clip'].set_value(50)
        self.win.audio_volume_spins[2].set_value(40)
        self.assertEqual(self.win.audio_clips[1].volume, .5)
        self.assertEqual(self.win._render_clips('audio')[1].volume, .2)
        specs = self.win._preview_audio_specs(1)
        self.assertEqual([s[3] for s in specs], [1, .2])
        self.assertNotEqual(original_key, self.win._current_render_fingerprint(kind='play'))
        self.win._on_undo()
        self.assertEqual(self.win.audio_track_volumes[2], 1)
        self.assertEqual(self.win.audio_clips[1].volume, .5)
        self.win._on_redo()
        self.assertEqual(self.win.audio_track_volumes[2], .4)
        self.win.audio_volume_spins[2].set_value(0)
        self.assertEqual(self.win._render_clips('audio')[1].volume, 0)

    def test_double_clip_and_track_gain_survives_render_copy(self):
        self.win.use_video_soundtrack = False
        self.win._place_clip('audio', 0, 'a')
        self.win.audio_volume_spins['clip'].set_value(200)
        self.win.audio_volume_spins[1].set_value(200)
        rendered = self.win._render_clips('audio')[0]
        self.assertEqual(rendered.volume, 4)
        self.assertEqual(rendered.copy().volume, 4)
        self.assertEqual(self.win._preview_audio_specs(1)[0][3], 4)

    def test_volume_edit_resumes_playback_and_compiled_preview_blocks_edits(self):
        self.win.use_video_soundtrack = False
        self.win._place_clip('audio', 0, 'a')
        self.win.playing = True
        with patch.object(self.win, '_timeline_now', return_value=2.5), \
             patch.object(self.win, '_begin_timeline_play') as resume:
            self.win.audio_volume_spins[1].set_value(50)
            resume.assert_called_once_with(2.5)
        self.win._compiled_mode = True
        self.win.audio_volume_spins[1].set_value(20)
        self.assertEqual(self.win.audio_track_volumes[1], .5)
        self.assertFalse(self.win.audio_volume_spins[1].get_sensitive())
        self.win._compiled_mode = False

    def test_live_mixer_applies_both_gains_without_automatic_attenuation(self):
        self.win.use_video_soundtrack = False
        self.win._place_clip('audio', 0, 'a')
        self.win._place_clip('audio', 0, 'b')
        self.win.audio_clips[0].volume = .5
        self.win.audio_track_volumes[1] = .5
        self.win.audio_track_volumes[2] = .8
        with tempfile.TemporaryDirectory() as td, \
             patch('clip_editor.ui.Path.home', return_value=Path(td)), \
             patch('clip_editor.ui.subprocess.Popen') as popen, \
             patch('clip_editor.ui.shutil.which', return_value='/usr/bin/ffplay'), \
             patch('clip_editor.ui.GLib.timeout_add'):
            self.win._start_preview_audio(1)
            command = popen.call_args_list[0].args[0]
            filters = command[command.index('-filter_complex') + 1]
            self.assertIn('[0:a]volume=0.250000', filters)
            self.assertIn('[1:a]volume=0.800000', filters)
            self.assertIn('amix=inputs=2:duration=longest:normalize=0[a]', filters)
            self.win._stop_preview_audio()
        self.win.audio_clips[1].out_s = 2
        specs = self.win._preview_audio_specs(3)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0][3], .25)

    def test_source_clip_volume_and_track_gain_and_new_project_reset(self):
        self.win.audio_volume_spins['clip'].set_value(50)
        self.win.audio_volume_spins[1].set_value(60)
        self.assertAlmostEqual(self.win._preview_audio_specs(1)[0][3], .3)
        self.assertAlmostEqual(self.win._render_clips('video')[0].volume, .3)
        self.win._clear_session()
        self.assertEqual(self.win.audio_track_volumes, {1: 1, 2: 1})


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg unavailable')
class AudioMixerRenderTest(unittest.TestCase):
    def test_two_tones_mix_at_requested_gain_and_track_mute_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video, tone_a, tone_b = [root / n for n in ('video.mp4', 'a.wav', 'b.wav')]
            def ff(*args):
                subprocess.run(['ffmpeg', '-v', 'error', '-y', *args], check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            ff('-f', 'lavfi', '-i', 'color=s=160x90:r=30:d=1.2', '-c:v', 'libx264', str(video))
            for path, freq in ((tone_a, 440), (tone_b, 880)):
                ff('-f', 'lavfi', '-i', f'sine=frequency={freq}:sample_rate=48000:duration=1.2', str(path))
            media = [MediaItem('v', video, 'video'), MediaItem('a', tone_a, 'audio'), MediaItem('b', tone_b, 'audio')]
            for gain_a, gain_b in ((.25, .8), (.25, 0), (0, None)):
                with self.subTest(gain_a=gain_a, gain_b=gain_b):
                    out = root / 'out.mp4'
                    cmd, _ = build_cmd(video, out, audio=tone_a, aspect='16:9', pan_x=.5, pan_y=.5,
                        in_s=0, out_s=1.2, audio_follows_in=False, audio_offset=0,
                        video_clips=[ClipInst(out_s=1.2, media_id='v')],
                        audio_clips=[ClipInst(out_s=1.2, media_id='a', volume=gain_a)] +
                                    ([ClipInst(out_s=1.2, media_id='b', track=2, volume=gain_b)]
                                     if gain_b is not None else []),
                        media=media, profile=PREVIEW_PROFILE)
                    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                    pcm = subprocess.check_output(['ffmpeg', '-v', 'error', '-i', str(out), '-ss', '0.2', '-t', '0.8',
                        '-vn', '-ac', '1', '-ar', '48000', '-f', 'f32le', '-'])
                    samples = array.array('f', pcm)
                    def amplitude(freq):
                        n = len(samples)
                        re = sum(x * math.cos(2 * math.pi * freq * i / 48000) for i, x in enumerate(samples))
                        im = sum(x * math.sin(2 * math.pi * freq * i / 48000) for i, x in enumerate(samples))
                        return 2 * math.hypot(re, im) / n
                    self.assertAlmostEqual(amplitude(440), .125 * gain_a, delta=.004)
                    self.assertAlmostEqual(amplitude(880), .125 * (gain_b or 0), delta=.006)
