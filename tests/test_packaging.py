from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clip_editor.eagle import inbox_dir
from clip_editor.server import _pick_native


class InstalledRuntimeTest(unittest.TestCase):
    def test_eagle_inbox_config_does_not_require_eagle_browse_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "config.toml"
            config.write_text('inbox = "incoming"\n', encoding="utf-8")

            with patch.dict(
                os.environ,
                {"EAGLE_BROWSE_CONFIG": str(config)},
                clear=False,
            ):
                self.assertEqual(inbox_dir(), (root / "incoming").resolve())

    def test_native_picker_does_not_set_a_source_checkout_cwd(self) -> None:
        completed = subprocess.CompletedProcess([], 1, "", "")
        with patch("clip_editor.server.subprocess.run", return_value=completed) as run:
            self.assertIsNone(_pick_native("video"))

        self.assertNotIn("cwd", run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
