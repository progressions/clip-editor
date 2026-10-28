"""Audio timing must not depend on clip count, effects, or the calling API."""

import array
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from clip_editor.export import ExportError, build_cmd
from clip_editor.preview import FINAL_PROFILE, PREVIEW_PROFILE
from clip_editor.probe import probe
from clip_editor.project import ClipInst, MediaItem


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg unavailable")
class ExportAudioTimingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="clip-editor-audio-timing-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        cls.audio = cls.root / "driver.wav"
        cls.video = cls.root / "video.mp4"
        cls.rate = 48000
        # A different tone each second detects starting at the wrong source time.
        samples = array.array("h", (
            int(16000 * math.sin(2 * math.pi * 220 * (i // cls.rate + 1) * i / cls.rate))
            for i in range(6 * cls.rate)
        ))
        with wave.open(str(cls.audio), "wb") as wav:
            wav.setparams((1, 2, cls.rate, 0, "NONE", "not compressed"))
            wav.writeframes(samples.tobytes())
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=c=gray:s=160x90:r=12:d=4", "-i", str(cls.audio),
            "-t", "4", "-c:v", "libx264", "-c:a", "aac", str(cls.video),
        ], check=True, capture_output=True, timeout=30)
        cls.src = probe(cls.video)
        cls.media = [MediaItem("v", cls.video, "video"),
                     MediaItem("a", cls.audio, "audio")]

    def render(self, name, **options):
        output = self.root / f"{name}.mp4"
        kwargs = dict(
            audio=self.audio, aspect="16:9", pan_x=.5, pan_y=.5,
            in_s=1, out_s=3, audio_follows_in=True, audio_offset=0,
            media=self.media, src=self.src, profile=PREVIEW_PROFILE,
        )
        kwargs.update(options)
        with patch("clip_editor.export.profile_dest_size", return_value=(160, 90)):
            cmd, meta = build_cmd(self.video, output, **kwargs)
        subprocess.run(cmd, check=True, capture_output=True, timeout=30)
        self.assertAlmostEqual(probe(output)["duration"], meta["duration"], delta=.1)
        pcm = array.array("f", subprocess.check_output([
            "ffmpeg", "-v", "error", "-i", str(output), "-vn", "-ac", "1",
            "-ar", str(self.rate), "-f", "f32le", "-",
        ], timeout=30))
        return meta, pcm

    def assert_audio(self, pcm, time, frequency=None):
        segment = pcm[int(time * self.rate):int((time + .2) * self.rate)]
        self.assertGreater(len(segment), self.rate * .19)
        rms = math.sqrt(sum(x*x for x in segment) / len(segment))
        if frequency is None:
            self.assertLess(rms, .001)
        else:
            self.assertGreater(rms, .1)
            measured = sum(a <= 0 < b for a, b in zip(segment, segment[1:])) / .2
            self.assertAlmostEqual(measured, frequency, delta=10)

    def video_clips(self, split, grain=0, start=0):
        bounds = ((1, 2), (2, 3)) if split else ((1, 3),)
        return [ClipInst(start=start, in_s=inn, out_s=out, media_id="v",
                         grain_strength=grain) for inn, out in bounds]

    def test_driver_sync_and_duration_survive_grain_and_splits_in_both_profiles(self):
        for profile in (FINAL_PROFILE, PREVIEW_PROFILE):
            for split in (False, True):
                for grain in (0, 20):
                    with self.subTest(profile=profile.name, split=split, grain=grain):
                        meta, pcm = self.render(
                            f"driver-{profile.name}-{split}-{grain}", profile=profile,
                            video_clips=self.video_clips(split, grain),
                            audio_clips=[ClipInst(out_s=6, media_id="a")],
                        )
                        self.assertAlmostEqual(meta["duration"], 3)
                        self.assert_audio(pcm, .2)
                        self.assert_audio(pcm, 1.2, 440)
                        self.assert_audio(pcm, 2.2, 660)

    def test_independent_audio_keeps_its_trim_placement_and_offset(self):
        for split in (False, True):
            for grain in (0, 20):
                with self.subTest(split=split, grain=grain):
                    meta, pcm = self.render(
                        f"independent-{split}-{grain}", audio_follows_in=False,
                        audio_offset=.5, video_clips=self.video_clips(split, grain),
                        audio_clips=[ClipInst(start=-.5, in_s=1, out_s=6, media_id="a")],
                    )
                    self.assertAlmostEqual(meta["duration"], 3)
                    self.assert_audio(pcm, .1)
                    self.assert_audio(pcm, .7, 440)
                    self.assert_audio(pcm, 1.2, 660)

    def test_cli_arguments_and_explicit_clips_share_audio_timing(self):
        for follows in (False, True):
            for explicit in (False, True):
                with self.subTest(follows=follows, explicit=explicit):
                    audio_start = .5 if follows else -.5
                    clips = dict(
                        video_clips=self.video_clips(False, start=.5),
                        audio_clips=[ClipInst(start=audio_start, in_s=1, out_s=6,
                                              media_id="a")],
                    ) if explicit else {}
                    meta, pcm = self.render(
                        f"cli-{follows}-{explicit}", audio_follows_in=follows,
                        video_start=.5, audio_start=audio_start, audio_in=1,
                        audio_out=6, audio_offset=.5, **clips,
                    )
                    self.assertAlmostEqual(meta["duration"], 3.5)
                    self.assert_audio(pcm, .2)
                    self.assert_audio(pcm, 1.7 if follows else .7, 440)
                    self.assert_audio(pcm, 2.2 if follows else 1.2, 660)

    def test_source_soundtrack_keeps_picture_trim_with_grain_and_splits(self):
        for split in (False, True):
            for grain in (0, 20):
                with self.subTest(split=split, grain=grain):
                    meta, pcm = self.render(
                        f"soundtrack-{split}-{grain}", audio=None,
                        video_clips=self.video_clips(split, grain),
                    )
                    self.assertAlmostEqual(meta["duration"], 3)
                    self.assert_audio(pcm, .2)
                    self.assert_audio(pcm, 1.2, 440)
                    self.assert_audio(pcm, 2.2, 660)

    def test_short_audio_is_padded_and_audio_after_picture_is_silent(self):
        for start, out in ((0, .5), (5, 6)):
            with self.subTest(start=start):
                meta, pcm = self.render(
                    f"silence-{start}", audio_follows_in=False,
                    video_clips=self.video_clips(False),
                    audio_clips=[ClipInst(start=start, out_s=out, media_id="a")],
                )
                self.assertAlmostEqual(meta["duration"], 3)
                self.assert_audio(pcm, 2.5)

    def test_driver_audio_is_silent_in_video_gaps(self):
        meta, pcm = self.render(
            "driver-gap",
            video_clips=[ClipInst(out_s=1, media_id="v"),
                         ClipInst(in_s=2, out_s=3, media_id="v")],
            audio_clips=[ClipInst(out_s=6, media_id="a")],
        )
        self.assertAlmostEqual(meta["duration"], 3)
        self.assert_audio(pcm, .2, 220)
        self.assert_audio(pcm, 1.2)
        self.assert_audio(pcm, 2.2, 660)

    def test_cli_inputs_do_not_reuse_a_conflicting_bin_id(self):
        media = [MediaItem("m1", self.audio, "audio")]
        meta, pcm = self.render("bin-id", media=media)
        self.assertAlmostEqual(meta["duration"], 3)
        self.assert_audio(pcm, .2)
        self.assert_audio(pcm, 1.2, 440)
        self.assertEqual(len(media), 1)

    def test_unused_offline_media_does_not_prevent_export(self):
        meta, pcm = self.render(
            "offline-bin", media=self.media + [MediaItem("offline", self.root / "missing.mp4", "video")],
        )
        self.assertAlmostEqual(meta["duration"], 3)
        self.assert_audio(pcm, 1.2, 440)

    def test_explicit_clip_bounds_override_stale_inspector_values(self):
        meta, pcm = self.render(
            "stale-inspector", in_s=99, out_s=100, audio_follows_in=False,
            video_clips=self.video_clips(False),
            audio_clips=[ClipInst(out_s=6, media_id="a")],
        )
        self.assertAlmostEqual(meta["duration"], 3)
        self.assert_audio(pcm, .2, 220)

    def test_explicit_empty_video_timeline_is_not_recreated(self):
        with self.assertRaisesRegex(ExportError, "no video on the timeline"):
            build_cmd(self.video, self.root / "empty.mp4", audio=None,
                      aspect="16:9", pan_x=.5, pan_y=.5, in_s=0, out_s=None,
                      audio_follows_in=False, audio_offset=0, video_clips=[])
