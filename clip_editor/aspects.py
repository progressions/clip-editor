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
    """Place the full source over a destination without revealing its background.

    ``transform_x`` and ``transform_y`` move the *source layer* in output
    pixels.  They do not move an already cropped output frame.  Scale is a
    cover multiplier, so values below 1 are treated as 1: a clip can zoom in
    but cannot be zoomed out far enough to leave an empty edge.
    """
    if min(src_w, src_h, dest_w, dest_h) <= 0:
        raise ValueError("source and destination dimensions must be positive")
    pan_x = min(1.0, max(0.0, float(pan_x)))
    pan_y = min(1.0, max(0.0, float(pan_y)))
    cover = max(dest_w / src_w, dest_h / src_h)
    factor = cover * max(1.0, float(scale))
    w = max(2, even(round(src_w * factor)))
    h = max(2, even(round(src_h * factor)))
    # The initial position honors the project's crop pan. Clamp the translated
    # layer to the frame so X/Y never uncover a black margin.
    x = int(round((dest_w - w) * pan_x + float(transform_x)))
    y = int(round((dest_h - h) * pan_y + float(transform_y)))
    x = min(0, max(dest_w - w, x))
    y = min(0, max(dest_h - h, y))
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
