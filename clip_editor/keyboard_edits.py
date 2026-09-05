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


def clip_boundary_delta(
    clips: list[ClipInst], primary: int, selected: set[int], direction: int
) -> float | None:
    """Return the delta that puts the primary clip on its neighboring start."""
    if not 0 <= primary < len(clips) or direction not in (-1, 1):
        return None
    selected = {i for i in selected if 0 <= i < len(clips)}
    ordered = sorted(
        ((i, clips[i].used_times()[0]) for i in range(len(clips))),
        key=lambda row: (row[1], row[0]),
    )
    position = next((n for n, (i, _start) in enumerate(ordered) if i == primary), -1)
    if position < 0:
        return None
    candidates = ordered[:position] if direction < 0 else ordered[position + 1:]
    candidates = [row for row in candidates if row[0] not in selected]
    if not candidates:
        return None
    target = candidates[-1][1] if direction < 0 else candidates[0][1]
    return target - clips[primary].used_times()[0]
