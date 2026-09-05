"""Pure timeline edits shared by video and audio keyboard commands."""

from .project import ClipInst

MOVE_INCREMENTS = (10.0, 1.0, 0.1, 'clip')


def boundary_delta(clips: list[ClipInst], selected: set[int], primary: int,
                   direction: int) -> float:
    """Delta to the next unselected visible clip start on the primary lane."""
    anchor = clips[primary]
    start = anchor.used_times()[0]
    targets = [c.used_times()[0] for i, c in enumerate(clips)
               if i not in selected and c.track == anchor.track
               and (c.used_times()[0] - start) * direction > 1e-8]
    if not targets:
        return 0.0
    target = min(targets) if direction > 0 else max(targets)
    delta = target - start
    if any(clips[i].used_times()[0] + delta < -1e-8 for i in selected):
        return 0.0
    return delta


def move_clips(clips: list[ClipInst], selected: set[int], delta: float) -> list[ClipInst]:
    result = [c.copy() for c in clips]
    indices = {i for i in selected if 0 <= i < len(clips)}
    if not indices:
        return result
    # ClipInst.start is a source origin; only the visible start must stay >= 0.
    delta = max(delta, -min(clips[i].used_times()[0] for i in indices))
    for i in indices:
        result[i].start += delta
    return result
