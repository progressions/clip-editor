"""Reverse clips preserve edit geometry and render the selected source backwards."""
import array
import math
import json
import shutil
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path

from clip_editor.export import _flatten_clips, _timeline_parts, build_cmd
from clip_editor.keyboard_edits import trim_clip
from clip_editor.preview import PREVIEW_PROFILE, rebase_clips_for_window, render_fingerprint
from clip_editor.project import (ClipInst, MediaItem, Project, VERSION,
                                 clip_from_dict, clip_to_dict, from_dict, to_dict)


class ReverseProjectTest(unittest.TestCase):
    def test_toggle_roundtrip_preserves_source_range_position_and_speed(self):
        c = ClipInst(start=4, in_s=2, out_s=5, speed=2, media_id='v')
        before = clip_to_dict(c)
        span = c.used_times(10)
        c.set_reverse(True, 10)
        self.assertEqual((c.in_s, c.out_s), (5, 8))
        self.assertEqual(c.used_times(10), span)
        for copy in (c.copy(), clip_from_dict(clip_to_dict(c))):
            self.assertTrue(copy.reverse)
            copy.set_reverse(False, 10)
            self.assertEqual(clip_to_dict(copy), before)
        self.assertFalse(clip_from_dict(before).reverse)

    def test_saved_project_retains_direction_and_old_projects_default_forward(self):
        c = ClipInst(in_s=1, out_s=3, start=-1, media_id='v')
        c.set_reverse(True, 10)
        saved = json.loads(json.dumps(to_dict(Project(
            video=Path('/tmp/reverse-source.mp4'), video_clips=[c]))))
        self.assertEqual(saved['version'], VERSION)
        restored = from_dict(saved).video_clips[0]
        self.assertEqual(clip_to_dict(restored), clip_to_dict(c))
        saved['version'] = 8
        del saved['video_clips'][0]['reverse']
        self.assertFalse(from_dict(saved).video_clips[0].reverse)

    def test_split_and_keyboard_trim_follow_reverse_playback_order(self):
        c = ClipInst(start=-1, in_s=1, out_s=7, speed=2)
        c.set_reverse(True, 10)  # plays source 7 → 1 over timeline 0 → 3
        right = c.split_at(1, 10)
        self.assertIsNotNone(right)
        self.assertTrue(c.reverse and right.reverse)
        self.assertEqual((c.in_s, c.out_s, right.in_s, right.out_s), (3, 5, 5, 9))
        self.assertEqual((c.used_times(10), right.used_times(10)), ((0, 1), (1, 3)))
        trimmed = trim_clip([right], 0, 'in', .5, 10)[0]
        self.assertEqual((trimmed.in_s, trimmed.out_s), (6, 9))
        self.assertEqual(trimmed.used_times(10), (1.5, 3))

    def test_overlap_negative_start_and_preview_window_keep_direction(self):
        c = ClipInst(start=-2, in_s=0, out_s=8, reverse=True, speed=2)
        flat = _flatten_clips([c, ClipInst(start=1, out_s=.5)], 10)
        self.assertEqual([r[14] for r in flat], [True, False, True])
        self.assertEqual([r[13] for r in _timeline_parts(flat, 2)], [True, False, True])
        self.assertEqual(flat[0][2:4], (4, 6))
        self.assertEqual(flat[2][2:4], (7, 8))
        cut = rebase_clips_for_window([c], .5, 1.5, 10)[0]
        self.assertTrue(cut.reverse)
        self.assertEqual((cut.in_s, cut.out_s), (5, 7))
        self.assertEqual(cut.used_times(10), (0, 1))

    def test_direction_invalidates_preview_even_for_symmetric_trim(self):
        c = ClipInst(out_s=4)
        kwargs = dict(aspect='1:1', pan_x=0, pan_y=0, audio_follows_in=False,
                      use_video_soundtrack=True, audio_offset=0,
                      video_clips=[c], audio_clips=[], media=[], kind='play')
        before = render_fingerprint(**kwargs)
        c.set_reverse(True, 4)
        self.assertNotEqual(before, render_fingerprint(**kwargs))


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg unavailable')
class ReverseRenderTest(unittest.TestCase):
    def test_export_reverses_trimmed_frames_and_linked_audio(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            wav, source, output = root / 'tones.wav', root / 'source.mkv', root / 'out.mp4'
            rate = 48000
            samples = array.array('h', (
                int(16000 * math.sin(2 * math.pi * (220 * (i // rate + 1)) * i / rate))
                for i in range(rate * 4)
            ))
            with wave.open(str(wav), 'wb') as f:
                f.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
                f.writeframes(samples.tobytes())
            # Each source frame has a distinct, increasing brightness.
            frames = b''.join(bytes([30 + n * 4]) * (64 * 64) for n in range(40))
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'gray',
                            '-s', '64x64', '-r', '10', '-i', 'pipe:0', '-i', str(wav),
                            '-c:v', 'ffv1', '-pix_fmt', 'yuv420p', '-c:a', 'pcm_s16le', str(source)],
                           input=frames, check=True, capture_output=True)
            c = ClipInst(start=-1, in_s=1, out_s=3, media_id='v')
            c.set_reverse(True, 4)
            cmd, _ = build_cmd(source, output, audio=None, aspect='1:1', pan_x=0, pan_y=0,
                               in_s=0, out_s=None, audio_follows_in=False, audio_offset=0,
                               video_clips=[c], media=[MediaItem('v', source, 'video')],
                               profile=PREVIEW_PROFILE)
            graph = cmd[cmd.index('-filter_complex') + 1]
            self.assertIn('trim=start=1.000000:duration=2.000000,reverse', graph)
            self.assertIn('atrim=start=1.000000:duration=2.000000,areverse', graph)
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
            raw = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(output),
                                  '-vf', 'scale=1:1', '-pix_fmt', 'gray', '-f', 'rawvideo', '-'],
                                 check=True, capture_output=True).stdout
            self.assertEqual(len(raw), 20)
            # Compare against the same encode pipeline going forwards, so
            # colorspace/range conversions do not masquerade as ordering bugs.
            forward = list(cmd)
            forward[forward.index('-filter_complex') + 1] = graph.replace(
                ',reverse,', ',').replace(',areverse,', ',')
            forward[-1] = str(root / 'forward.mp4')
            subprocess.run(forward, check=True, capture_output=True, timeout=30)
            baseline = subprocess.run(
                ['ffmpeg', '-v', 'error', '-i', forward[-1], '-vf', 'scale=1:1',
                 '-pix_fmt', 'gray', '-f', 'rawvideo', '-'],
                check=True, capture_output=True).stdout
            self.assertEqual(len(baseline), 20)
            for actual, expected in zip(raw, reversed(baseline)):
                self.assertAlmostEqual(actual, expected, delta=2)
            self.assertGreater(raw[0] - raw[-1], 60)
            self.assertTrue(all(a >= b for a, b in zip(raw, raw[1:])))
            pcm = subprocess.run(['ffmpeg', '-v', 'error', '-i', str(output), '-vn',
                                  '-ac', '1', '-ar', str(rate), '-f', 's16le', '-'],
                                 check=True, capture_output=True).stdout
            decoded = array.array('h')
            decoded.frombytes(pcm)
            def frequency(t):
                segment = decoded[int(t * rate):int((t + .3) * rate)]
                return sum(a <= 0 < b for a, b in zip(segment, segment[1:])) / .3
            self.assertAlmostEqual(frequency(.2), 660, delta=10)
            self.assertAlmostEqual(frequency(1.5), 440, delta=10)

    def test_play_builds_proxy_and_resumes_with_reversed_clip(self):
        import time
        from contextlib import ExitStack
        from unittest.mock import patch
        from clip_editor.ui import EditorWindow, Gdk, GLib, Gtk
        from clip_editor.probe import probe
        if not Gtk.init_check() or Gdk.Display.get_default() is None:
            self.skipTest('GTK display unavailable')
        with tempfile.TemporaryDirectory() as td, ExitStack() as stack:
            root = Path(td)
            source = root / 'preview.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=64x64:rate=10:duration=2',
                            '-c:v', 'libx264', str(source)], check=True, capture_output=True)
            stack.enter_context(patch('clip_editor.preview.PREVIEW_CACHE_DIR', root / 'cache'))
            for name in ('_restore_autosave', '_schedule_autosave', '_flush_autosave',
                         '_schedule_checkpoint', '_install_media_list', '_refresh_media',
                         '_start_preview_audio'):
                stack.enter_context(patch.object(EditorWindow, name, return_value=False))
            w = EditorWindow()
            try:
                w.media = [MediaItem('v', source, 'video')]
                w.media_info = {'v': probe(source)}
                w.video_path, w.video_info = source, w.media_info['v']
                w.video_clips = [ClipInst(out_s=2, media_id='v')]
                w.sel_v, w.sel_vs, w.sel_kind = 0, {0}, 'video'
                w.aspect = '1:1'
                w._load_media(source)
                w._refresh_fit()
                w.reverse_check.set_active(True)
                w._on_play()
                self.assertTrue(w._preview_rendering)
                context = GLib.MainContext.default()
                deadline = time.monotonic() + 15
                while not w.playing and time.monotonic() < deadline:
                    while context.pending():
                        context.iteration(False)
                    time.sleep(.01)
                self.assertTrue(w.playing)
                self.assertTrue(w._playthrough_playing)
                self.assertTrue(w._playthrough_path.is_file())
                self.assertEqual(w._playthrough_hash, w._current_render_fingerprint(kind='play'))
                w._stop()
                w.reverse_check.set_active(False)
                self.assertFalse(w._cached_playback_available(.5))
            finally:
                w._on_close()
                w.destroy()
