"""Cover-crop math. Largest dest-aspect window inside the source, then scale."""

from __future__ import annotations

from dataclasses import dataclass

# Export pixel sizes. Always even (yuv420p).
ASPECTS: dict[str, tuple[int, int]] = {
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "16:9": (1920, 1080),
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


def dest_size(aspect: str) -> tuple[int, int]:
    key = str(aspect).strip()
    if key not in ASPECTS:
        known = ", ".join(ASPECTS)
        raise ValueError(f"unknown aspect {aspect!r}; use {known}")
    return ASPECTS[key]
