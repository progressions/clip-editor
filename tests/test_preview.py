from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from clip_editor.export import ExportCancelled, build_cmd, run_export
from clip_editor.preview import (
    FINAL_PROFILE,
    PREVIEW_CACHE_DIR,
    PREVIEW_PROFILE,
    assert_preview_path_safe,
    cleanup_preview_cache,
    extract_acrossfade_signature,
    extract_xfade_signature,
    has_touching_follower,
    preview_out_path,
    proxy_dest_size,
    rebase_clips_for_window,
    render_fingerprint,
    selected_cut_window,
)
from clip_editor.project import (
    TRANSITION_DISSOLVE,
    TRANSITION_NONE,
    TRANSITION_WHITE_FLASH,
    ClipInst,
    MediaItem,
)


def _filter_complex(cmd: list[str]) -> str:
    for i, part in enumerate(cmd):
        if part == "-filter_complex" and i + 1 < len(cmd):
            return cmd[i + 1]
    return ""


class SelectedCutWindowTest(unittest.TestCase):
    def test_pad_clamped_at_timeline_start(self) -> None:
        clips = [
            ClipInst(
                start=0.0,
                in_s=0.0,
                out_s=1.0,
                transition=TRANSITION_WHITE_FLASH,
                transition_s=0.25,
            ),
            ClipInst(start=1.0, in_s=0.0, out_s=2.0),
        ]
        t0, t1, cut = selected_cut_window(clips, 0, 10.0, pad_s=2.0)
        self.assertEqual(t0, 0.0)
        self.assertAlmostEqual(cut, 1.0)
        self.assertAlmostEqual(t1, 3.0)

    def test_pad_clamped_at_timeline_end(self) -> None:
        clips = [
            ClipInst(
                start=0.0,
                in_s=0.0,
                out_s=4.0,
                transition=TRANSITION_DISSOLVE,
                transition_s=0.5,
            ),
            ClipInst(start=4.0, in_s=0.0, out_s=0.5),
        ]
        t0, t1, cut = selected_cut_window(clips, 0, {"m1": 10.0}, pad_s=2.0)
        self.assertAlmostEqual(cut, 4.0)
        self.assertAlmostEqual(t0, 2.0)
        self.assertAlmostEqual(t1, 4.5)

    def test_gap_has_no_touching_follower(self) -> None:
        clips = [
            ClipInst(start=0.0, in_s=0.0, out_s=1.0, transition=TRANSITION_DISSOLVE, transition_s=0.5),
            ClipInst(start=1.5, in_s=0.0, out_s=1.0),
        ]
        self.assertFalse(has_touching_follower(clips, 0, 10.0))
        with self.assertRaises(ValueError):
            selected_cut_window(clips, 0, 10.0)

    def test_short_clips_still_get_a_window(self) -> None:
        clips = [
            ClipInst(
                start=0.0,
                in_s=0.0,
                out_s=0.2,
                transition=TRANSITION_DISSOLVE,
                transition_s=0.1,
            ),
            ClipInst(start=0.2, in_s=0.0, out_s=0.2),
        ]
        t0, t1, cut = selected_cut_window(clips, 0, 10.0, pad_s=2.0)
        self.assertAlmostEqual(cut, 0.2)
        self.assertLess(t0, cut)
        self.assertGreater(t1, cut)

    def test_rebase_keeps_transition_when_cut_inside_window(self) -> None:
        clips = [
            ClipInst(
                start=0.0,
                in_s=0.0,
                out_s=3.0,
                transition=TRANSITION_DISSOLVE,
                transition_s=0.5,
            ),
            ClipInst(start=3.0, in_s=0.0, out_s=3.0),
        ]
        t0, t1, _cut = selected_cut_window(clips, 0, 10.0, pad_s=2.0)
        rebased = rebase_clips_for_window(clips, t0, t1, 10.0)
        self.assertEqual(len(rebased), 2)
        self.assertEqual(rebased[0].transition, TRANSITION_DISSOLVE)
        self.assertAlmostEqual(rebased[0].transition_s, 0.5)
        self.assertAlmostEqual(rebased[0].start + rebased[0].out_s, 2.0)

    def test_rebase_clears_transition_when_end_truncated(self) -> None:
        clips = [
            ClipInst(
                start=0.0,
                in_s=0.0,
                out_s=5.0,
                transition=TRANSITION_DISSOLVE,
                transition_s=0.5,
            ),
        ]
        rebased = rebase_clips_for_window(clips, 1.0, 3.0, 10.0)
        self.assertEqual(len(rebased), 1)
        self.assertEqual(rebased[0].transition, TRANSITION_NONE)


class FingerprintAndCacheTest(unittest.TestCase):
    def test_fingerprint_covers_render_affecting_fields(self) -> None:
        base = dict(
            aspect="9:16",
            pan_x=0.5,
            pan_y=0.5,
            audio_follows_in=False,
            use_video_soundtrack=True,
            audio_offset=0.0,
            video_clips=[
                ClipInst(
                    start=0.0,
                    in_s=0.0,
                    out_s=1.0,
                    media_id="m1",
                    transition=TRANSITION_DISSOLVE,
                    transition_s=0.5,
                ),
                ClipInst(start=1.0, in_s=0.0, out_s=1.0, media_id="m1"),
            ],
            audio_clips=[],
            media=[MediaItem(id="m1", path=Path("/tmp/a.mp4"), kind="video")],
            kind="full",
        )
        h0 = render_fingerprint(**base)
        changed = dict(base)
        changed["pan_x"] = 0.6
        self.assertNotEqual(h0, render_fingerprint(**changed))
        changed = dict(base)
        changed["video_clips"] = [
            ClipInst(
                start=0.0,
                in_s=0.0,
                out_s=1.0,
                media_id="m1",
                transition=TRANSITION_WHITE_FLASH,
                transition_s=0.25,
            ),
            ClipInst(start=1.0, in_s=0.0, out_s=1.0, media_id="m1"),
        ]
        self.assertNotEqual(h0, render_fingerprint(**changed))
        changed = dict(base)
        changed["kind"] = "cut"
        changed["window"] = (0.0, 4.0)
        self.assertNotEqual(h0, render_fingerprint(**changed))

    def test_preview_path_stays_in_cache_not_intake(self) -> None:
        out = preview_out_path("abc123def456", "cut")
        self.assertTrue(str(out).startswith(str(PREVIEW_CACHE_DIR)))
        assert_preview_path_safe(out)
        with tempfile.TemporaryDirectory() as td:
            intake = Path(td) / "intake"
            intake.mkdir()
            with mock.patch("clip_editor.preview.inbox_dir", return_value=intake):
                assert_preview_path_safe(out)
                with self.assertRaises(ValueError):
                    assert_preview_path_safe(intake / "sneaky.mp4")

    def test_cleanup_keeps_newest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            with mock.patch("clip_editor.preview.PREVIEW_CACHE_DIR", folder):
                paths = []
                for i in range(5):
                    p = folder / f"p{i}.mp4"
                    p.write_bytes(b"x" * 10)
                    paths.append(p)
                # Make p0 oldest
                import os

                older = paths[0].stat().st_mtime - 100
                os.utime(paths[0], (older, older))
                removed = cleanup_preview_cache(keep=3, max_age_s=10_000)
                self.assertTrue(any(p.name == "p0.mp4" for p in removed))
                remaining = list(folder.glob("*.mp4"))
                self.assertLessEqual(len(remaining), 3)


class SharedGraphTest(unittest.TestCase):
    def test_preview_and_final_share_transition_graph(self) -> None:
        video = Path("/tmp/missing-shared-graph.mp4")
        clips = [
            ClipInst(
                start=0.0,
                in_s=0.0,
                out_s=1.0,
                media_id="m1",
                transition=TRANSITION_WHITE_FLASH,
                transition_s=0.25,
            ),
            ClipInst(
                start=1.0,
                in_s=0.0,
                out_s=1.0,
                media_id="m1",
                transition=TRANSITION_DISSOLVE,
                transition_s=0.5,
            ),
            ClipInst(start=2.0, in_s=0.0, out_s=1.0, media_id="m1"),
        ]
        media = [MediaItem(id="m1", path=video, kind="video")]
        src = {
            "has_video": True,
            "has_audio": True,
            "width": 1080,
            "height": 1920,
            "duration": 5.0,
            "fps": 30.0,
        }

        def fake_probe(path: Path) -> dict:
            return dict(src)

        with mock.patch("clip_editor.export.probe", side_effect=fake_probe), mock.patch(
            "clip_editor.export.which_ffmpeg", return_value="ffmpeg"
        ):
            final_cmd, _ = build_cmd(
                video,
                Path("/tmp/final.mp4"),
                audio=None,
                aspect="9:16",
                pan_x=0.5,
                pan_y=0.5,
                in_s=0.0,
                out_s=None,
                audio_follows_in=False,
                audio_offset=0.0,
                video_clips=clips,
                media=media,
                use_video_soundtrack=True,
                profile=FINAL_PROFILE,
                src=src,
            )
            preview_cmd, preview_meta = build_cmd(
                video,
                Path("/tmp/preview.mp4"),
                audio=None,
                aspect="9:16",
                pan_x=0.5,
                pan_y=0.5,
                in_s=0.0,
                out_s=None,
                audio_follows_in=False,
                audio_offset=0.0,
                video_clips=clips,
                media=media,
                use_video_soundtrack=True,
                profile=PREVIEW_PROFILE,
                src=src,
            )

        self.assertEqual(
            extract_xfade_signature(_filter_complex(final_cmd)),
            extract_xfade_signature(_filter_complex(preview_cmd)),
        )
        self.assertEqual(
            extract_acrossfade_signature(_filter_complex(final_cmd)),
            extract_acrossfade_signature(_filter_complex(preview_cmd)),
        )
        self.assertEqual(preview_meta["dest"]["width"], proxy_dest_size("9:16")[0])
        self.assertIn("veryfast", preview_cmd)
        self.assertIn("slow", final_cmd)


class CancelCleanupTest(unittest.TestCase):
    def test_cancel_removes_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            video = root / "src.mp4"
            video.write_bytes(b"fake")
            out = root / "out.mp4"
            tmp = out.with_name(out.name + ".tmp")
            cancel = threading.Event()

            class FakeProc:
                returncode = -9

                def __init__(self) -> None:
                    self.stdout = self
                    self.stderr = self
                    self._lines = ["out_time_ms=100\n", "progress=continue\n"]
                    self._i = 0
                    tmp.write_bytes(b"partial")

                def __iter__(self):
                    return self

                def __next__(self) -> str:
                    if self._i == 0:
                        cancel.set()
                    if self._i >= len(self._lines):
                        raise StopIteration
                    line = self._lines[self._i]
                    self._i += 1
                    return line

                def read(self) -> str:
                    return ""

                def kill(self) -> None:
                    pass

                def wait(self, timeout: float | None = None) -> int:
                    return self.returncode

            with mock.patch("clip_editor.export.probe", return_value={
                "has_video": True,
                "has_audio": False,
                "width": 64,
                "height": 64,
                "duration": 1.0,
                "fps": 30.0,
            }), mock.patch(
                "clip_editor.export.build_cmd",
                return_value=(["ffmpeg"], {"duration": 1.0}),
            ), mock.patch(
                "clip_editor.export.subprocess.Popen", return_value=FakeProc()
            ):
                with self.assertRaises(ExportCancelled):
                    run_export(
                        video,
                        out,
                        profile=PREVIEW_PROFILE,
                        cancel_event=cancel,
                        video_clips=[ClipInst(start=0, in_s=0, out_s=1)],
                    )
            self.assertFalse(tmp.exists())
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
