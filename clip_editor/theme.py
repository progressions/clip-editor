"""Small, self-contained Omarchy palette adapter for the installed application."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

COLORS_FILE = (
    Path.home() / ".local/state/omarchy/current/theme/colors.toml"
)
# btop's key-hint color; the editor draws its key hints the same way.
BTOP_FILE = COLORS_FILE.with_name("btop.theme")
PALETTE: dict[str, str] = {}
_listeners: list[Any] = []
_provider: Any = None
_monitor: Any = None


def _read_palette() -> dict[str, str]:
    try:
        raw = tomllib.loads(COLORS_FILE.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    colors = {
        str(key): value.strip()
        for key, value in raw.items()
        if isinstance(value, str) and value.strip()
    }
    if "key" not in colors and (hi := _btop_color("hi_fg")):
        colors["key"] = hi
    return colors


def _btop_color(name: str) -> str | None:
    try:
        text = BTOP_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf'^theme\[{re.escape(name)}\]\s*=\s*"(#[0-9a-fA-F]{{6}})"', text, re.M)
    return match.group(1) if match else None


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
    lum = _luminance(background)
    # Pick by WCAG contrast ratio; a fixed luminance cutoff put white on
    # mid-light accents such as #d7accd (1.9:1 vs 9:1 for the dark label).
    dark_contrast = (lum + 0.05) / (_luminance("#1a1a1a") + 0.05)
    light_contrast = 1.05 / (lum + 0.05)
    return "#1a1a1a" if dark_contrast >= light_contrast else "#ffffff"


def _mix(a: str, b: str, amount: float) -> str:
    """`a` moved `amount` (0–1) of the way toward `b`, as #rrggbb."""
    ra, rb = _hex_rgb(a), _hex_rgb(b)
    if ra is None or rb is None:
        return a
    mixed = (x + (y - x) * amount for x, y in zip(ra, rb, strict=True))
    return "#" + "".join(f"{round(c * 255):02x}" for c in mixed)


def _hue_sat(color: str) -> tuple[float, float] | None:
    rgb = _hex_rgb(color)
    if rgb is None:
        return None
    hi, lo = max(rgb), min(rgb)
    if hi == lo:
        return 0.0, 0.0
    delta = hi - lo
    red, green, blue = rgb
    if hi == red:
        hue = ((green - blue) / delta) % 6
    elif hi == green:
        hue = (blue - red) / delta + 2
    else:
        hue = (red - green) / delta + 4
    return hue * 60.0, delta / hi


def _nearest_hue(colors: dict[str, str], target: float) -> str | None:
    """The terminal color (color1–color14) closest in hue to `target` degrees.

    Omarchy themes do not name their status colors, and not every theme keeps
    the ANSI slot order (one puts purple in color2), so match by hue instead.
    """
    best: tuple[float, str] | None = None
    for slot in range(1, 15):
        value = colors.get(f"color{slot}")
        hs = _hue_sat(value) if value else None
        if hs is None or hs[1] < 0.25:
            continue
        gap = abs((hs[0] - target + 180.0) % 360.0 - 180.0)
        if best is None or gap < best[0]:
            best = (gap, value)
    return best[1] if best else None


def cairo_rgb(
    name: str, fallback: tuple[float, float, float] = (0.0, 0.0, 0.0)
) -> tuple[float, float, float]:
    return _hex_rgb(PALETTE.get(name, "")) or fallback


def cairo_on_rgb(name: str) -> tuple[float, float, float]:
    """Readable label color for text drawn on palette color `name`."""
    color = PALETTE.get(name, "")
    if not color:
        return (1.0, 1.0, 1.0)
    return _hex_rgb(_on_color(color)) or (1.0, 1.0, 1.0)


def build_css(colors: dict[str, str]) -> tuple[bytes, str]:
    """Return (css, mode) for `colors`, and refresh PALETTE."""
    mode = (colors.get("mode") or colors.get("theme_type") or "dark").lower()
    if mode not in ("dark", "light"):
        mode = "dark"
    background = _pick(colors, "background", "bg", default="#1e1e2e")
    foreground = _pick(colors, "foreground", "fg", default="#cdd6f4")
    # Most Omarchy themes only define background/foreground, so derive the
    # surface steps from them rather than collapsing every surface to one color.
    dark = _pick(colors, "dark_background", "dark_bg", default=background)
    darker = _pick(
        colors, "darker_background", "darker_bg",
        default=_mix(dark, "#000000", 0.3) if mode == "dark" else _mix(dark, foreground, 0.04),
    )
    lighter = _pick(
        colors, "lighter_background", "lighter_bg", default=_mix(background, foreground, 0.08)
    )
    muted = _pick(
        colors, "muted", "dark_foreground", "dark_fg", default=_mix(background, foreground, 0.55)
    )
    accent = _pick(colors, "accent", "blue", default="#89b4fa")
    selection = _pick(colors, "selection", default=accent)
    red = _pick(colors, "red", default=_nearest_hue(colors, 355.0) or "#e64553")
    green = _pick(colors, "green", default=_nearest_hue(colors, 125.0) or "#40a02b")
    yellow = _pick(colors, "yellow", default=_nearest_hue(colors, 45.0) or "#df8e1d")
    blue = _pick(colors, "blue", default=_nearest_hue(colors, 210.0) or "#7ea7c9")
    key = _pick(colors, "key", default=accent)
    line = _mix(background, foreground, 0.2)
    hover = _mix(background, accent, 0.16)
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
            "blue": blue,
            "key": key,
            "line": line,
            "hover": hover,
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
        --clip-key: {key};
        --clip-line: {line};
        --clip-hover: {hover};
        --clip-muted: {muted};
        --clip-surface: {lighter};
        --clip-accent: {accent};
        --clip-on-accent: {accent_fg};
        --clip-bg: {background};
        --clip-fg: {foreground};
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


def on_theme_change(callback: Any) -> None:
    """Call `callback()` after each re-apply, so custom drawing can repaint."""
    if callback not in _listeners:
        _listeners.append(callback)


def off_theme_change(callback: Any) -> None:
    """Release a window callback when its window closes."""
    if callback in _listeners:
        _listeners.remove(callback)


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
    for callback in list(_listeners):
        callback()


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
