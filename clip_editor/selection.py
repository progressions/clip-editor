"""Timeline multi-select helpers (Fizzy #530)."""

from __future__ import annotations


def next_video_selection(
    *,
    clicked: int,
    primary: int,
    selected: set[int],
    shift: bool,
    n_clips: int,
) -> tuple[int, set[int]]:
    """Compute the next primary index and selected set after a clip click.

    * Plain click → single selection.
    * Shift+click → add the clicked clip to the selection (additive).
    """
    if n_clips <= 0 or not 0 <= clicked < n_clips:
        return -1, set()

    if shift:
        current = set(selected)
        if not current and 0 <= primary < n_clips:
            current.add(primary)
        current.add(clicked)
        return clicked, current

    return clicked, {clicked}


def move_video_selection(
    *,
    delta: int,
    primary: int,
    selected: set[int],
    extend: bool,
    n_clips: int,
) -> tuple[int, set[int]]:
    """Move the primary video selection one clip left or right.

    Plain movement replaces the selection.  Shift movement retains the
    existing selection and includes the clip reached by this movement.  At a
    timeline edge nothing changes, including a multi-selection.
    """
    if n_clips <= 0:
        return -1, set()
    if not 0 <= primary < n_clips:
        destination = n_clips - 1 if delta < 0 else 0
        return destination, {destination}

    destination = max(0, min(n_clips - 1, primary + int(delta)))
    if destination == primary:
        return primary, set(selected)

    if extend:
        current = {i for i in selected if 0 <= i < n_clips}
        current.add(primary)
        current.add(destination)
        return destination, current

    return destination, {destination}


def prune_video_selection(
    selected: set[int], primary: int, n_clips: int
) -> tuple[int, set[int]]:
    """Drop out-of-range indices after clips are added/removed."""
    if n_clips <= 0:
        return -1, set()
    kept = {i for i in selected if 0 <= i < n_clips}
    if primary in kept:
        return primary, kept
    if kept:
        return min(kept), kept
    return -1, set()


def group_moved_starts(
    starts: dict[int, float], *, anchor: int, new_anchor_start: float
) -> dict[int, float]:
    """Translate every start by the same delta as the anchor clip."""
    if anchor not in starts:
        return dict(starts)
    delta = float(new_anchor_start) - float(starts[anchor])
    return {i: float(s) + delta for i, s in starts.items()}
