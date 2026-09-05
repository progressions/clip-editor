"""Pure timeline edits shared by video and audio keyboard commands."""

from .project import ClipInst
from .ripple import follower_indices

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


def trim_clip(clips: list[ClipInst], primary: int, edge: str, delta: float,
              source_duration: float, min_duration: float = .05) -> list[ClipInst]:
    """Trim the primary edge in timeline seconds; right trim ripples its lane."""
    result = [c.copy() for c in clips]
    clip = result[primary]
    speed = clip.playback_speed()
    start, end = clip.used_times(source_duration)
    original_in = clip.in_s
    out = min(clip.out_s, source_duration) if source_duration > 0 else clip.out_s
    if out <= original_in:
        out = source_duration
    if end - start < min_duration:
        return result
    if edge == 'in':
        target = min(end - min_duration, max(0, start - original_in / speed, start + delta))
        clip.in_s = max(0, original_in + (target - start) * speed)
        clip.start = target - clip.in_s
        clip.out_s = out
    else:
        clip.out_s = max(original_in + min_duration * speed,
                         min(source_duration or out, out + delta * speed))
        change = clip.used_times(source_duration)[1] - end
        followers = follower_indices([c.track for c in clips],
                                     [c.used_times() for c in clips], primary)
        for i in followers:
            result[i].start += change
    return result


def reorder_clips(clips: list[ClipInst], selected: set[int], primary: int,
                  direction: int) -> list[ClipInst]:
    """Swap a touching selected block with its neighbor on the same lane."""
    result = [c.copy() for c in clips]
    lane = clips[primary].track
    order = sorted((i for i, c in enumerate(clips) if c.track == lane),
                   key=lambda i: (clips[i].used_times()[0], i))
    if any(i not in order for i in selected):
        raise ValueError('Ripple reorder requires a selection on one track')
    positions = sorted(order.index(i) for i in selected)
    if not positions or positions != list(range(positions[0], positions[-1] + 1)):
        raise ValueError('Ripple reorder requires a contiguous selection')
    first, last = positions[0], positions[-1]
    neighbor = first - 1 if direction < 0 else last + 1
    if not 0 <= neighbor < len(order):
        return result
    affected = order[min(first, neighbor):max(last, neighbor) + 1]
    for before, after in zip(affected, affected[1:]):
        if abs(clips[before].used_times()[1] - clips[after].used_times()[0]) > 1e-6:
            raise ValueError('Ripple reorder requires touching clips (no gap or overlap)')
    block = order[first:last + 1]
    reordered = block + [order[neighbor]] if direction < 0 else [order[neighbor]] + block
    time = clips[affected[0]].used_times()[0]
    for index in reordered:
        result[index].start = time - result[index].in_s
        time += result[index].timeline_len()
    return result
