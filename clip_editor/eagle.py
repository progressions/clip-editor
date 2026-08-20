"""Eagle Browse coupling: intake path and Omarchy theme."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

_BOOTSTRAP_VAULT = Path.home() / "Dropbox/ISAAC/GENNIE"


def _eagle_browse_root() -> Path | None:
    env = os.environ.get("EAGLE_BROWSE_ROOT")
    candidates = []
    if env:
        candidates.append(Path(env).expanduser())
    candidates.append(Path.home() / "tech" / "eagle-browse")
    for p in candidates:
        if (p / "config.py").is_file():
            return p
    return None


def load_eagle_module(filename: str, *, as_name: str) -> ModuleType | None:
    root = _eagle_browse_root()
    if root is None:
        return None
    path = root / filename
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(as_name, path)
    if spec is None or spec.loader is None:
        return None
    existing = sys.modules.get(as_name)
    if existing is not None:
        return existing
    mod = importlib.util.module_from_spec(spec)
    sys.modules[as_name] = mod
    spec.loader.exec_module(mod)
    return mod


def inbox_dir() -> Path:
    """Eagle Browse intake folder (flat). Watcher on Ginger consumes it."""
    mod = load_eagle_module("config.py", as_name="eagle_browse_config")
    inbox_path = getattr(mod, "inbox_path", None) if mod is not None else None
    if callable(inbox_path):
        return Path(inbox_path()).expanduser()
    env_inbox = os.environ.get("EAGLE_INBOX")
    if env_inbox:
        return Path(env_inbox).expanduser()
    return _BOOTSTRAP_VAULT / "intake"


def apply_omarchy_theme() -> None:
    """Same Omarchy palette mapping Eagle Browse uses. No-op off Omarchy."""
    mod = load_eagle_module("theme.py", as_name="eagle_browse_theme")
    fn = getattr(mod, "apply_omarchy_theme", None) if mod is not None else None
    if callable(fn):
        fn()


def theme_rgb(
    name: str, fallback: tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> tuple[float, float, float]:
    mod = load_eagle_module("theme.py", as_name="eagle_browse_theme")
    fn = getattr(mod, "cairo_rgb", None) if mod is not None else None
    if callable(fn):
        return fn(name, fallback)
    return fallback
