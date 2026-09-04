"""Repeated --video / --audio argv parsing (#540)."""

from __future__ import annotations

import unittest
from pathlib import Path

from clip_editor.cli_paths import cli_flag_path, cli_flag_paths
from clip_editor.__main__ import build_parser


class CliFlagPathsTest(unittest.TestCase):
    def test_repeated_video(self) -> None:
        args = ["gui", "--video", "/tmp/a.mp4", "--video", "/tmp/b.mp4"]
        paths = cli_flag_paths(args, "--video")
        self.assertEqual(paths, [Path("/tmp/a.mp4"), Path("/tmp/b.mp4")])

    def test_mixed_video_and_audio(self) -> None:
        args = [
            "clip-editor",
            "gui",
            "--video",
            "a.mp4",
            "--audio",
            "bed.m4a",
            "--video",
            "b.mp4",
        ]
        self.assertEqual(
            cli_flag_paths(args, "--video"),
            [Path("a.mp4"), Path("b.mp4")],
        )
        self.assertEqual(cli_flag_paths(args, "--audio"), [Path("bed.m4a")])

    def test_missing_flag(self) -> None:
        self.assertEqual(cli_flag_paths(["gui", "--new"], "--video"), [])
        self.assertIsNone(cli_flag_path(["gui"], "--video"))

    def test_flag_without_value_ignored(self) -> None:
        self.assertEqual(cli_flag_paths(["gui", "--video"], "--video"), [])

    def test_first_path_helper(self) -> None:
        args = ["--video", "one.mp4", "--video", "two.mp4"]
        self.assertEqual(cli_flag_path(args, "--video"), Path("one.mp4"))

    def test_argparse_append_video(self) -> None:
        p = build_parser()
        ns = p.parse_args(
            ["gui", "--video", "a.mp4", "--video", "b.mp4", "--audio", "x.m4a"]
        )
        self.assertEqual(ns.video, ["a.mp4", "b.mp4"])
        self.assertEqual(ns.audio, ["x.m4a"])
        self.assertFalse(ns.new)

    def test_argparse_new_and_positional(self) -> None:
        p = build_parser()
        ns = p.parse_args(["gui", "--new", "--video", "a.mp4", "extra.mp4"])
        self.assertTrue(ns.new)
        self.assertEqual(ns.video, ["a.mp4"])
        self.assertEqual(ns.video_path, "extra.mp4")


if __name__ == "__main__":
    unittest.main()
