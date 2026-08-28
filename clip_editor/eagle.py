"""Eagle Browse coupling: intake path and Omarchy theme."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from clip_editor.theme import apply_omarchy_theme, cairo_rgb

_BOOTSTRAP_VAULT = Path.home() / "Dropbox/ISAAC/GENNIE"


def _config_files() -> list[Path]:
    library = Path(
        os.environ.get("EAGLE_LIBRARY", str(_BOOTSTRAP_VAULT / "Eunbi.library"))
    ).expanduser()
    files = [library.parent / "eagle-browse.toml"]
    files.append(Path.home() / ".config/eagle-browse/config.toml")
    if explicit := os.environ.get("EAGLE_BROWSE_CONFIG"):
        files.append(Path(explicit).expanduser())
    return files


def inbox_dir() -> Path:
    """Eagle Browse intake folder (flat). Watcher on Ginger consumes it."""
    env_inbox = os.environ.get("EAGLE_INBOX")
    if env_inbox:
        return Path(env_inbox).expanduser()
    configured: Path | None = None
    for path in _config_files():
        try:
            value = tomllib.loads(path.read_text(encoding="utf-8")).get("inbox")
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if isinstance(value, str) and value.strip():
            configured = Path(value).expanduser()
            if not configured.is_absolute():
                configured = (path.parent / configured).resolve()
    if configured is not None:
        return configured
    return _BOOTSTRAP_VAULT / "intake"


def theme_rgb(
    name: str, fallback: tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> tuple[float, float, float]:
    return cairo_rgb(name, fallback)
