"""Small, self-contained Omarchy palette adapter for the installed application."""

from __future__ import annotations

import tomllib
from pathlib import Path

COLORS_FILE = (
    Path.home() / ".local/state/omarchy/current/theme/colors.toml"
)
PALETTE: dict[str, str] = {}
_provider = None


def _read_palette() -> dict[str, str]:
    try:
        raw = tomllib.loads(COLORS_FILE.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {
        str(key): value.strip()
        for key, value in raw.items()
        if isinstance(value, str) and value.strip()
    }


def _pick(colors: dict[str, str], *keys: str, default: str) -> str:
    return next((colors[key] for key in keys if colors.get(key)), default)


def _hex_rgb(color: str) -> tuple[float, float, float] | None:
    value = color.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) != 6:
        return None
    try:
        return tuple(int(value[pos : pos + 2], 16) / 255.0 for pos in (0, 2, 4))
    except ValueError:
        return None


def cairo_rgb(
    name: str, fallback: tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> tuple[float, float, float]:
    return _hex_rgb(PALETTE.get(name, "")) or fallback


def apply_omarchy_theme() -> None:
    """Apply the active Omarchy colors to GTK; no-op on other desktops."""
    global _provider
    colors = _read_palette()
    if not colors:
        return

    from gi.repository import Adw, Gdk, Gtk  # noqa: PLC0415

    mode = (colors.get("mode") or colors.get("theme_type") or "dark").lower()
    background = _pick(colors, "background", "bg", default="#1e1e2e")
    dark = _pick(colors, "dark_background", "dark_bg", default=background)
    darker = _pick(colors, "darker_background", "darker_bg", default=dark)
    lighter = _pick(colors, "lighter_background", "lighter_bg", default=background)
    foreground = _pick(colors, "foreground", "fg", default="#cdd6f4")
    muted = _pick(colors, "muted", "dark_foreground", "dark_fg", default=foreground)
    accent = _pick(colors, "accent", "blue", default="#89b4fa")
    green = _pick(colors, "green", default="#40a02b")

    PALETTE.clear()
    PALETTE.update(
        colors
        | {
            "background": background,
            "dark_background": dark,
            "darker_background": darker,
            "lighter_background": lighter,
            "foreground": foreground,
            "muted": muted,
            "accent": accent,
            "green": green,
        }
    )

    display = Gdk.Display.get_default()
    if display is None:
        return
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_LIGHT if mode == "light" else Adw.ColorScheme.FORCE_DARK
    )
    css = f"""
      :root {{
        --accent-bg-color: {accent};
        --accent-color: {accent};
        --window-bg-color: {background};
        --window-fg-color: {foreground};
        --view-bg-color: {dark};
        --view-fg-color: {foreground};
        --headerbar-bg-color: {darker};
        --headerbar-fg-color: {foreground};
        --card-bg-color: {lighter};
        --card-fg-color: {foreground};
      }}
    """.encode()
    if _provider is None:
        _provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            display, _provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
    _provider.load_from_data(css)
