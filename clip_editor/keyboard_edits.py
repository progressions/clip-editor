"""Pure timeline edits shared by video and audio keyboard commands."""

from .project import ClipInst


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
