"""Intra-clip fade in / fade out (#567)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from clip_editor.export import (
    _append_clip_fades,
    _flatten_clips,
    _join_transitions_for_parts,
    _timeline_parts,
    build_cmd,
)
from clip_editor.preview import FINAL_PROFILE, rebase_clips_for_window, render_fingerprint
from clip_editor.project import (
    TRANSITION_DISSOLVE,
    TRANSITION_NONE,
    VERSION,
    ClipInst,
    MediaItem,
    Project,
    clamp_clip_fades,
    clip_from_dict,
    clip_to_dict,
    from_dict,
    normalize_fade_s,
    write_project,
)


class FadeClampTest(unittest.TestCase):
    def test_normalize_off_and_range(self) -> None:
        self.assertEqual(normalize_fade_s(0), 0.0)
        self.assertEqual(normalize_fade_s(-1), 0.0)
        self.assertAlmostEqual(normalize_fade_s(0.05), 0.1)
        self.assertAlmostEqual(normalize_fade_s(9), 3.0)

    def test_clamp_scales_both(self) -> None:
        fi, fo = clamp_clip_fades(0.5, 1.0, 2.0)
        self.assertAlmostEqual(fi, 0.5)
        self.assertAlmostEqual(fo, 1.0)

    def test_short_clip_clamps(self) -> None:
        fi, fo = clamp_clip_fades(0.5, 1.0, 0.4)
        self.assertLessEqual(fi + fo, 0.4 - 0.05 + 1e-6)
        self.assertGreater(fi + fo, 0.0)


class FadeModelTest(unittest.TestCase):
    def test_round_trip_json(self) -> None:
        c = ClipInst(
            start=0.0,
            in_s=0.0,
            out_s=3.0,
            fade_in_s=0.5,
            fade_out_s=1.0,
        )
        data = clip_to_dict(c)
        self.assertEqual(data["fade_in_s"], 0.5)
        self.assertEqual(data["fade_out_s"], 1.0)
        loaded = clip_from_dict(data)
        assert loaded is not None
        self.assertAlmostEqual(loaded.fade_in_s, 0.5)
        self.assertAlmostEqual(loaded.fade_out_s, 1.0)

    def test_copy_is_independent(self) -> None:
        src = ClipInst(out_s=2.0, fade_in_s=0.5)
        copied = src.copy()
        copied.fade_in_s = 1.0
        self.assertAlmostEqual(src.fade_in_s, 0.5)

    def test_split_keeps_in_on_left_out_on_right(self) -> None:
        left = ClipInst(
            start=0.0,
            in_s=0.0,
            out_s=4.0,
            fade_in_s=0.5,
            fade_out_s=1.0,
            transition=TRANSITION_DISSOLVE,
            transition_s=0.5,
        )
        right = left.split_at(2.0)
        assert right is not None
        self.assertAlmostEqual(left.fade_in_s, 0.5)
        self.assertEqual(left.fade_out_s, 0.0)
        self.assertEqual(right.fade_in_s, 0.0)
        self.assertAlmostEqual(right.fade_out_s, 1.0)
        self.assertEqual(left.transition, TRANSITION_NONE)
        self.assertEqual(right.transition, TRANSITION_DISSOLVE)

    def test_project_save_reload(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "fade.clip.json"
            vid = Path(td) / "v.mp4"
            vid.write_bytes(b"\x00")
            proj = Project(
                video=vid,
                media=[MediaItem(id="m1", path=vid, kind="video")],
                video_clips=[
                    ClipInst(
                        media_id="m1",
                        out_s=3.0,
                        fade_in_s=0.5,
                        fade_out_s=1.0,
                    )
                ],
                path=path,
            )
            write_project(path, proj)
            raw = json.loads(path.read_text())
            self.assertEqual(raw["version"], VERSION)
            self.assertEqual(raw["video_clips"][0]["fade_in_s"], 0.5)
            loaded = from_dict(raw, origin=path)
            self.assertAlmostEqual(loaded.video_clips[0].fade_in_s, 0.5)
            self.assertAlmostEqual(loaded.video_clips[0].fade_out_s, 1.0)


class FadeGraphTest(unittest.TestCase):
    def test_video_fade_filters(self) -> None:
        chain: list[str] = []
        _append_clip_fades(
            chain, kind="video", timeline_dur=3.0, fade_in_s=0.5, fade_out_s=1.0
        )
        self.assertIn("format=rgb24", chain)
        self.assertIn("fade=t=in:st=0:d=0.500000:c=black", chain)
        self.assertIn("fade=t=out:st=2.000000:d=1.001000:c=black", chain)
        self.assertTrue(any(x.startswith("drawbox=") and "color=black" in x for x in chain))
        self.assertIn("format=yuv420p", chain)

    def test_audio_afade_filters(self) -> None:
        chain: list[str] = []
        _append_clip_fades(
            chain, kind="audio", timeline_dur=2.0, fade_in_s=0.5, fade_out_s=0.5
        )
        self.assertIn("afade=t=in:st=0:d=0.500000", chain)
        self.assertIn("afade=t=out:st=1.500000:d=0.500000", chain)

    def test_flatten_carries_fades(self) -> None:
        clips = [
            ClipInst(start=0.0, in_s=0.0, out_s=3.0, media_id="m1", fade_in_s=0.5, fade_out_s=1.0)
        ]
        flat = _flatten_clips(clips, 3.0)
        self.assertEqual(len(flat), 1)
        self.assertAlmostEqual(flat[0][11], 0.5)
        self.assertAlmostEqual(flat[0][12], 1.0)
        parts = _timeline_parts(flat, 3.0)
        self.assertEqual(parts[0][0], "seg")
        self.assertAlmostEqual(parts[0][10], 0.5)
        self.assertAlmostEqual(parts[0][11], 1.0)

    def test_short_clip_flatten_clamps(self) -> None:
        clips = [
            ClipInst(start=0.0, in_s=0.0, out_s=0.3, media_id="m1", fade_in_s=0.5, fade_out_s=1.0)
        ]
        flat = _flatten_clips(clips, 0.3)
        fi, fo = flat[0][11], flat[0][12]
        self.assertLessEqual(fi + fo, 0.3 - 0.05 + 1e-6)

    def test_transition_join_still_independent(self) -> None:
        """Intra-clip fades do not replace #487 per-cut transitions."""
        parts = [
            (
                "seg",
                0.0,
                2.0,
                "m1",
                0.0,
                0.0,
                1.0,
                TRANSITION_DISSOLVE,
                0.5,
                1.0,
                0.5,
                1.0,
            ),
            ("seg", 0.0, 2.0, "m1", 0.0, 0.0, 1.0, TRANSITION_NONE, 0.0, 1.0, 0.0, 0.0),
        ]
        joins = _join_transitions_for_parts(parts)
        self.assertTrue(joins[0]["applied"])
        self.assertEqual(joins[0]["type"], TRANSITION_DISSOLVE)

    def test_fingerprint_includes_fades(self) -> None:
        media = [MediaItem(id="m1", path=Path("/tmp/x.mp4"), kind="video")]
        a = [
            ClipInst(media_id="m1", out_s=2.0, fade_in_s=0.5),
        ]
        b = [
            ClipInst(media_id="m1", out_s=2.0, fade_in_s=1.0),
        ]
        fa = render_fingerprint(
            aspect="9:16",
            pan_x=0.0,
            pan_y=0.0,
            audio_follows_in=True,
            use_video_soundtrack=True,
            audio_offset=0.0,
            video_clips=a,
            audio_clips=[],
            media=media,
            kind="final",
        )
        fb = render_fingerprint(
            aspect="9:16",
            pan_x=0.0,
            pan_y=0.0,
            audio_follows_in=True,
            use_video_soundtrack=True,
            audio_offset=0.0,
            video_clips=b,
            audio_clips=[],
            media=media,
            kind="final",
        )
        self.assertNotEqual(fa, fb)

    def test_rebase_preserves_fades_when_window_covers(self) -> None:
        clips = [
            ClipInst(start=0.0, in_s=0.0, out_s=3.0, media_id="m1", fade_in_s=0.5, fade_out_s=1.0)
        ]
        rebased = rebase_clips_for_window(clips, 0.0, 3.0, {"m1": 3.0})
        self.assertEqual(len(rebased), 1)
        self.assertAlmostEqual(rebased[0].fade_in_s, 0.5)
        self.assertAlmostEqual(rebased[0].fade_out_s, 1.0)


class FadeBuildCmdTest(unittest.TestCase):
    def test_build_cmd_emits_fade_filters(self) -> None:
        # Use a real tiny mp4 if available via selftest media; otherwise skip.
        # Prefer synthesizing via ffmpeg if present.
        from clip_editor.probe import which_ffmpeg

        try:
            ffmpeg = which_ffmpeg()
        except Exception:
            self.skipTest("ffmpeg not available")
        with tempfile.TemporaryDirectory() as td:
            td_p = Path(td)
            src = td_p / "src.mp4"
            out = td_p / "out.mp4"
            import subprocess

            subprocess.check_call(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=red:s=320x240:d=3",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=f=440:d=3",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(src),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            media = [MediaItem(id="m1", path=src, kind="video")]
            clips = [
                ClipInst(
                    media_id="m1",
                    start=0.0,
                    in_s=0.0,
                    out_s=3.0,
                    fade_in_s=0.5,
                    fade_out_s=1.0,
                )
            ]
            # Force multi path with a second tiny clip after
            clips.append(
                ClipInst(media_id="m1", start=3.0, in_s=0.0, out_s=0.5)
            )
            cmd, meta = build_cmd(
                src,
                out,
                audio=None,
                aspect="9:16",
                pan_x=0.0,
                pan_y=0.0,
                in_s=0.0,
                out_s=None,
                audio_follows_in=True,
                audio_offset=0.0,
                video_clips=clips,
                audio_clips=None,
                media=media,
                use_video_soundtrack=True,
                profile=FINAL_PROFILE,
            )
            fc = ""
            for i, arg in enumerate(cmd):
                if arg == "-filter_complex" and i + 1 < len(cmd):
                    fc = cmd[i + 1]
                    break
            self.assertIn("fade=t=in:st=0:d=0.500000:c=black", fc)
            self.assertIn("fade=t=out:st=2.000000", fc)
            self.assertIn(":c=black", fc)
            self.assertIn("drawbox=", fc)
            # Source audio should get matching afades when soundtrack is used.
            self.assertIn("afade=t=in:st=0:d=0.500000", fc)
            self.assertIn("afade=t=out:st=2.000000", fc)
            # H.264 gate path still encodes libx264.
            self.assertIn("libx264", cmd)
            self.assertAlmostEqual(float(meta["duration"]), 3.5, places=2)


if __name__ == "__main__":
    unittest.main()
