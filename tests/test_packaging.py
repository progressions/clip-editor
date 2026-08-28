from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clip_editor.eagle import inbox_dir
from clip_editor.server import _pick_native
from clip_editor.theme import build_css


class InstalledRuntimeTest(unittest.TestCase):
    def test_eagle_inbox_config_does_not_require_eagle_browse_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            config.write_text('inbox = "incoming"\n', encoding="utf-8")

            # EAGLE_INBOX short-circuits inbox_dir() and EAGLE_LIBRARY steers
            # _config_files(); both are documented overrides for this vault's
            # tooling, so drop them rather than let a developer's shell decide.
            env = {
                key: value
                for key, value in os.environ.items()
                if key not in ("EAGLE_INBOX", "EAGLE_LIBRARY")
            }
            env["EAGLE_BROWSE_CONFIG"] = str(config)

            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(inbox_dir(), (root / "incoming").resolve())

    def test_native_picker_does_not_set_a_source_checkout_cwd(self) -> None:
        completed = subprocess.CompletedProcess([], 1, "", "")
        with patch("clip_editor.server.subprocess.run", return_value=completed) as run:
            self.assertIsNone(_pick_native("video"))

        self.assertNotIn("cwd", run.call_args.kwargs)

    def test_accent_foreground_is_derived_from_accent_luminance(self) -> None:
        # libadwaita does not re-derive --accent-fg-color from an overridden
        # --accent-bg-color, so a light accent needs an explicit dark label.
        light, _ = build_css({"accent": "#ffe066", "background": "#ffffff"})
        self.assertIn("--accent-fg-color: #1a1a1a;", light.decode())

        dark, _ = build_css({"accent": "#1f3a93", "background": "#1e1e2e"})
        self.assertIn("--accent-fg-color: #ffffff;", dark.decode())

    def test_status_colors_are_emitted(self) -> None:
        css, _mode = build_css({"accent": "#faa968"})
        text = css.decode()
        for name in ("destructive", "success", "warning", "error"):
            self.assertIn(f"--{name}-fg-color:", text)
        self.assertIn("@define-color accent_fg_color", text)


if __name__ == "__main__":
    unittest.main()
