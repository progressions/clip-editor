"""Cover-crop math. Largest dest-aspect window inside the source, then scale."""

from __future__ import annotations

from dataclasses import dataclass

# Medium (default) export pixel sizes. Always even (yuv420p).
ASPECTS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "3:4": (1080, 1440),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "16:9": (1920, 1080),
}

# Short-edge targets for Low / Medium / High. Medium matches ASPECTS.
RESOLUTIONS: tuple[str, ...] = ("low", "medium", "high")
DEFAULT_RESOLUTION = "medium"
RESOLUTION_SHORT_AXIS: dict[str, int] = {
    "low": 720,
    "medium": 1080,
    "high": 1440,
}


def even(n: int) -> int:
    n = int(n)
    return n if n % 2 == 0 else n - 1


@dataclass(frozen=True, slots=True)
class CropRect:
    x: int
    y: int
    w: int
    h: int

    def as_ffmpeg(self) -> str:
        return f"crop={self.w}:{self.h}:{self.x}:{self.y}"


@dataclass(frozen=True, slots=True)
class SourcePlacement:
    """A cover-scaled source layer positioned inside an output frame."""

    w: int
    h: int
    x: int
    y: int


def cover_source_placement(
    src_w: int,
    src_h: int,
    dest_w: int,
    dest_h: int,
    pan_x: float = 0.5,
    pan_y: float = 0.5,
    transform_x: float = 0.0,
    transform_y: float = 0.0,
    scale: float = 1.0,
) -> SourcePlacement:
    """Place a source layer in output pixels, allowing smaller overlays.

    Scale 1 covers the frame. Smaller scales reveal lower tracks; X/Y are
    unrestricted translations relative to the project's crop-pan origin.
    """
    if min(src_w, src_h, dest_w, dest_h) <= 0:
        raise ValueError("source and destination dimensions must be positive")
    pan_x = min(1.0, max(0.0, float(pan_x)))
    pan_y = min(1.0, max(0.0, float(pan_y)))
    cover = max(dest_w / src_w, dest_h / src_h)
    factor = cover * max(0.05, float(scale))
    w = max(2, even(round(src_w * factor)))
    h = max(2, even(round(src_h * factor)))
    x = int(round((dest_w - w) * pan_x + float(transform_x)))
    y = int(round((dest_h - h) * pan_y + float(transform_y)))
    return SourcePlacement(w, h, x, y)


def cover_crop(
    src_w: int,
    src_h: int,
    dest_w: int,
    dest_h: int,
    pan_x: float = 0.5,
    pan_y: float = 0.5,
) -> CropRect:
    """Largest dest-aspect rectangle inside the source.

    pan_x / pan_y are 0..1. 0.5 is centered. 0 is left/top, 1 is right/bottom.
    Only the axis with leftover source pixels actually moves.
    """
    if src_w <= 0 or src_h <= 0:
        raise ValueError(f"bad source size {src_w}x{src_h}")
    if dest_w <= 0 or dest_h <= 0:
        raise ValueError(f"bad dest size {dest_w}x{dest_h}")

    pan_x = min(1.0, max(0.0, float(pan_x)))
    pan_y = min(1.0, max(0.0, float(pan_y)))

    # Cross-multiply so we do not divide aspect floats first.
    # src wider than dest → crop width; else crop height.
    if src_w * dest_h > src_h * dest_w:
        crop_h = even(src_h)
        crop_w = even(src_h * dest_w // dest_h)
        if crop_w < 2:
            crop_w = 2
        if crop_w > src_w:
            crop_w = even(src_w)
        slack_x = src_w - crop_w
        slack_y = src_h - crop_h
    else:
        crop_w = even(src_w)
        crop_h = even(src_w * dest_h // dest_w)
        if crop_h < 2:
            crop_h = 2
        if crop_h > src_h:
            crop_h = even(src_h)
        slack_x = src_w - crop_w
        slack_y = src_h - crop_h

    x = even(int(round(slack_x * pan_x)))
    y = even(int(round(slack_y * pan_y)))
    x = max(0, min(x, src_w - crop_w))
    y = max(0, min(y, src_h - crop_h))
    if x + crop_w > src_w:
        x = max(0, even(src_w - crop_w))
    if y + crop_h > src_h:
        y = max(0, even(src_h - crop_h))
    return CropRect(x, y, crop_w, crop_h)


def normalize_resolution(resolution: str | None) -> str:
    key = str(resolution or DEFAULT_RESOLUTION).strip().lower()
    if key not in RESOLUTION_SHORT_AXIS:
        known = ", ".join(RESOLUTIONS)
        raise ValueError(f"unknown resolution {resolution!r}; use {known}")
    return key


def dest_size(aspect: str, resolution: str | None = None) -> tuple[int, int]:
    """Return even WxH for ``aspect`` at ``resolution`` (low/medium/high).

    Medium is the historical ASPECTS table (1080 short edge). Low/high scale
    that table so the short edge is 720 / 1440.
    """
    key = str(aspect).strip()
    if key not in ASPECTS:
        known = ", ".join(ASPECTS)
        raise ValueError(f"unknown aspect {aspect!r}; use {known}")
    fw, fh = ASPECTS[key]
    res = normalize_resolution(resolution)
    short = RESOLUTION_SHORT_AXIS[res]
    base_short = min(fw, fh)
    if base_short <= 0:
        raise ValueError(f"bad aspect size {fw}x{fh}")
    if short == base_short:
        return fw, fh
    scale = short / base_short
    dw = even(int(round(fw * scale)))
    dh = even(int(round(fh * scale)))
    return max(2, dw), max(2, dh)


def resize_source_from_corner(
    src_w: int, src_h: int, dest_w: int, dest_h: int,
    pan_x: float, pan_y: float, x: float, y: float, scale: float,
    corner: str, dx: float, dy: float,
) -> tuple[float, float, float]:
    """Aspect-locked resize in output pixels, keeping the opposite corner fixed."""
    old = cover_source_placement(src_w, src_h, dest_w, dest_h, pan_x, pan_y, x, y, scale)
    sx = -1 if 'w' in corner else 1
    sy = -1 if 'n' in corner else 1
    ratio = 1 + (sx * dx * old.w + sy * dy * old.h) / (old.w**2 + old.h**2)
    new_scale = min(4.0, max(.05, scale * ratio))
    new = cover_source_placement(src_w, src_h, dest_w, dest_h, pan_x, pan_y, 0, 0, new_scale)
    left = old.x + old.w - new.w if sx < 0 else old.x
    top = old.y + old.h - new.h if sy < 0 else old.y
    return left - (dest_w - new.w) * pan_x, top - (dest_h - new.h) * pan_y, new_scale
