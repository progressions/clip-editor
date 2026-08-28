"""Small, self-contained Omarchy palette adapter for the installed application."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

COLORS_FILE = (
    Path.home() / ".local/state/omarchy/current/theme/colors.toml"
)
PALETTE: dict[str, str] = {}
_provider: Any = None
_monitor: Any = None


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


def _luminance(color: str) -> float:
    rgb = _hex_rgb(color)
    if rgb is None:
        return 0.0

    def lin(channel: float) -> float:
        return (
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
        )

    red, green, blue = (lin(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _on_color(background: str) -> str:
    """Readable foreground for `background`.

    libadwaita does not re-derive --accent-fg-color when --accent-bg-color is
    overridden, so a light accent would otherwise keep the dark theme's white
    label and render white-on-light.
    """
    return "#1a1a1a" if _luminance(background) > 0.55 else "#ffffff"


def cairo_rgb(
    name: str, fallback: tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> tuple[float, float, float]:
    return _hex_rgb(PALETTE.get(name, "")) or fallback


def build_css(colors: dict[str, str]) -> tuple[bytes, str]:
    """Return (css, mode) for `colors`, and refresh PALETTE."""
    mode = (colors.get("mode") or colors.get("theme_type") or "dark").lower()
    if mode not in ("dark", "light"):
        mode = "dark"
    background = _pick(colors, "background", "bg", default="#1e1e2e")
    dark = _pick(colors, "dark_background", "dark_bg", default=background)
    darker = _pick(colors, "darker_background", "darker_bg", default=dark)
    lighter = _pick(colors, "lighter_background", "lighter_bg", default=background)
    foreground = _pick(colors, "foreground", "fg", default="#cdd6f4")
    muted = _pick(colors, "muted", "dark_foreground", "dark_fg", default=foreground)
    accent = _pick(colors, "accent", "blue", default="#89b4fa")
    selection = _pick(colors, "selection", default=accent)
    red = _pick(colors, "red", default="#e64553")
    green = _pick(colors, "green", default="#40a02b")
    yellow = _pick(colors, "yellow", default="#df8e1d")
    accent_fg = _on_color(accent)
    window_fg_on = _on_color(background)
    selected_fg = window_fg_on if _luminance(selection) < 0.45 else foreground

    PALETTE.clear()
    PALETTE.update(
        colors
        | {
            "mode": mode,
            "background": background,
            "dark_background": dark,
            "darker_background": darker,
            "lighter_background": lighter,
            "foreground": foreground,
            "muted": muted,
            "accent": accent,
            "selection": selection,
            "red": red,
            "green": green,
            "yellow": yellow,
        }
    )

    css = f"""
      :root {{
        --accent-bg-color: {accent};
        --accent-color: {accent};
        --accent-fg-color: {accent_fg};
        --destructive-bg-color: {red};
        --destructive-color: {red};
        --destructive-fg-color: {_on_color(red)};
        --success-bg-color: {green};
        --success-color: {green};
        --success-fg-color: {_on_color(green)};
        --warning-bg-color: {yellow};
        --warning-color: {yellow};
        --warning-fg-color: {_on_color(yellow)};
        --error-bg-color: {red};
        --error-color: {red};
        --error-fg-color: {_on_color(red)};
        --window-bg-color: {background};
        --window-fg-color: {foreground};
        --view-bg-color: {dark};
        --view-fg-color: {foreground};
        --headerbar-bg-color: {darker};
        --headerbar-fg-color: {foreground};
        --headerbar-backdrop-color: {background};
        --headerbar-border-color: {lighter};
        --sidebar-bg-color: {darker};
        --sidebar-fg-color: {foreground};
        --secondary-sidebar-bg-color: {dark};
        --secondary-sidebar-fg-color: {foreground};
        --card-bg-color: {lighter};
        --card-fg-color: {foreground};
        --dialog-bg-color: {dark};
        --dialog-fg-color: {foreground};
        --popover-bg-color: {lighter};
        --popover-fg-color: {foreground};
      }}
      @define-color accent_bg_color {accent};
      @define-color accent_fg_color {accent_fg};
      @define-color accent_color {accent};
      @define-color theme_bg_color {background};
      @define-color theme_fg_color {foreground};
      @define-color theme_base_color {dark};
      @define-color theme_text_color {foreground};
      @define-color theme_selected_bg_color {selection};
      @define-color theme_selected_fg_color {selected_fg};
      @define-color insensitive_fg_color {muted};
      @define-color borders {lighter};
    """.encode()
    return css, mode


def apply_omarchy_theme() -> None:
    """Apply the active Omarchy colors to GTK; no-op on other desktops."""
    global _provider
    colors = _read_palette()
    if not colors:
        return

    from gi.repository import Adw, Gdk, Gtk  # noqa: PLC0415

    css, mode = build_css(colors)

    display = Gdk.Display.get_default()
    if display is None:
        return
    Adw.StyleManager.get_default().set_color_scheme(
        Adw.ColorScheme.FORCE_LIGHT if mode == "light" else Adw.ColorScheme.FORCE_DARK
    )
    if _provider is None:
        _provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_display(
            display, _provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )
    _provider.load_from_data(css)
    _watch()


def _watch() -> None:
    """Re-apply when the Omarchy theme changes under a running window."""
    global _monitor
    if _monitor is not None:
        return
    from gi.repository import Gio, GLib  # noqa: PLC0415

    try:
        gfile = Gio.File.new_for_path(str(COLORS_FILE))
        _monitor = gfile.monitor_file(Gio.FileMonitorFlags.NONE, None)
    except Exception:  # noqa: BLE001
        return

    def on_changed(*_args: object) -> None:
        GLib.idle_add(apply_omarchy_theme)

    _monitor.connect("changed", on_changed)
