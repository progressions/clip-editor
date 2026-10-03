"""Render real colored sources to verify geometry, alpha and track order."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from clip_editor.export import build_cmd
from clip_editor.project import ClipInst, MediaItem


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg unavailable')
class VideoCompositingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        for name in ('red', 'blue'):
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            f'color=c={name}:s=160x160:r=10:d=2', '-c:v', 'libx264',
                            '-threads', '1', '-pix_fmt', 'yuv420p', str(cls.root / f'{name}.mp4')],
                           check=True, capture_output=True)

    def render(self, clips, name='out', **options):
        red, blue = self.root/'red.mp4', self.root/'blue.mp4'
        output = self.root/f'{name}.mp4'
        cmd, meta = build_cmd(red, output, audio=None, aspect='1:1', resolution='low',
                              pan_x=.5, pan_y=.5, in_s=0, out_s=2,
                              audio_follows_in=False, audio_offset=0, use_video_soundtrack=False,
                              video_clips=clips,
                              media=[MediaItem('red', red, 'video'), MediaItem('blue', blue, 'video')], **options)
        result = subprocess.run(cmd, capture_output=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr.decode()[-3000:])
        return output

    def pixel(self, output, t, x, y):
        raw = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', str(t), '-i', str(output),
                                       '-vf', f'crop=2:2:{x}:{y},format=rgb24', '-frames:v', '1',
                                       '-f', 'rawvideo', '-threads', '1', '-'], timeout=15)
        return tuple(raw[:3])

    def assert_red(self, pixel):
        self.assertGreater(pixel[0], 200)
        self.assertLess(pixel[1], 25)
        self.assertLess(pixel[2], 25)

    def assert_blue(self, pixel):
        self.assertLess(pixel[0], 25)
        self.assertLess(pixel[1], 25)
        self.assertGreater(pixel[2], 200)

    def test_small_upper_clip_reveals_lower_track_and_gaps_are_transparent(self):
        output = self.render([
            ClipInst(out_s=2, media_id='red'),
            ClipInst(start=.5, out_s=1, media_id='blue', track=2, scale=.5, transform_x=180),
        ])
        self.assert_red(self.pixel(output, .2, 500, 300))
        self.assert_blue(self.pixel(output, .8, 500, 300))
        self.assert_red(self.pixel(output, .8, 100, 300))
        self.assert_red(self.pixel(output, .8, 500, 50))
        self.assert_red(self.pixel(output, 1.8, 500, 300))

    def test_small_single_clip_has_black_background(self):
        output = self.render([ClipInst(out_s=1, media_id='blue', scale=.5)], name='single')
        self.assert_blue(self.pixel(output, .3, 300, 300))
        self.assertLess(max(self.pixel(output, .3, 50, 50)), 15)

    def test_preview_proxy_preserves_relative_transform(self):
        from clip_editor.preview import PREVIEW_PROFILE
        output = self.render([
            ClipInst(out_s=1, media_id='red'),
            ClipInst(out_s=1, media_id='blue', track=2, scale=.5, transform_x=180),
        ], name='proxy', profile=PREVIEW_PROFILE)
        self.assert_blue(self.pixel(output, .3, 300, 250))
        self.assert_red(self.pixel(output, .3, 250, 250))

    def test_upper_fade_reveals_lower_track(self):
        output = self.render([
            ClipInst(out_s=2, media_id='red'),
            ClipInst(out_s=2, media_id='blue', track=2, scale=.5, fade_in_s=.5, fade_out_s=.5),
        ], name='fade')
        self.assert_red(self.pixel(output, 0, 300, 300))
        self.assert_blue(self.pixel(output, 1, 300, 300))
        self.assert_red(self.pixel(output, 1.9, 300, 300))

    def test_transition_on_upper_track_composites_successfully(self):
        from clip_editor.project import TRANSITION_DISSOLVE
        output = self.render([
            ClipInst(out_s=2, media_id='red'),
            ClipInst(out_s=1, media_id='blue', track=2, scale=.5,
                     transition=TRANSITION_DISSOLVE, transition_s=.2),
            ClipInst(start=1, out_s=1, media_id='red', track=2, scale=.5),
        ], name='transition')
        self.assert_blue(self.pixel(output, .3, 300, 300))
        self.assert_red(self.pixel(output, .3, 50, 50))
        self.assert_red(self.pixel(output, 1.5, 300, 300))

    def test_reversed_upper_layer_keeps_trim_placement_and_transparency(self):
        # Reverse only the first second of blue, retaining its .5s placement.
        upper = ClipInst(start=.5, out_s=1, media_id='blue', track=2,
                         scale=.5, transform_x=180)
        upper.set_reverse(True, 2)
        output = self.render([ClipInst(out_s=2, media_id='red'), upper],
                             name='reverse-overlay')
        self.assert_red(self.pixel(output, .2, 500, 300))
        self.assert_blue(self.pixel(output, .8, 500, 300))
        self.assert_red(self.pixel(output, .8, 100, 300))
        self.assert_red(self.pixel(output, 1.8, 500, 300))
