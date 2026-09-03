"""Ripple right-edge trim: join hit-test and follower start shifts (#533)."""

from __future__ import annotations

JOIN_EPS = 0.05


def prefer_outgoing_at_join(
    index: int,
    part: str,
    t0: float,
    others: list[tuple[int, float, float]],
    *,
    eps: float = JOIN_EPS,
) -> tuple[int, str]:
    """If *part* is the in-edge of a clip that abuts a previous out, take that out.

    *others* is ``(index, t0, t1)`` for clips on the same track.
    """
    if part != "in":
        return index, part
    for j, _jt0, jt1 in others:
        if j == index:
            continue
        if abs(float(jt1) - float(t0)) <= eps:
            return j, "out"
    return index, part


def resolve_edge_hits(
    hits: list[tuple[int, str, float, float]],
    *,
    eps: float = JOIN_EPS,
) -> tuple[int, str] | None:
    """Pick one ``(index, in|out)`` from overlapping edge hits.

    At an abutting join, the earlier clip's **out** wins over the later clip's **in**.
    """
    if not hits:
        return None
    others = [(i, t0, t1) for i, _part, t0, t1 in hits]
    rewritten: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    for i, part, t0, _t1 in hits:
        ri, rp = prefer_outgoing_at_join(i, part, t0, others, eps=eps)
        key = (ri, rp)
        if key not in seen:
            seen.add(key)
            rewritten.append(key)
    for i, part in rewritten:
        if part == "out":
            return i, part
    return rewritten[0]


def follower_indices(
    tracks: list[int],
    times: list[tuple[float, float]],
    index: int,
    *,
    eps: float = JOIN_EPS,
) -> list[int]:
    """Same-track clips whose used start is at or after *index*'s used end."""
    if not 0 <= index < len(times):
        return []
    _t0, t1 = times[index]
    track = int(tracks[index])
    out: list[int] = []
    for i, (u0, _u1) in enumerate(times):
        if i == index:
            continue
        if int(tracks[i]) != track:
            continue
        if u0 >= t1 - eps:
            out.append(i)
    return out


def ripple_starts(starts0: dict[int, float], delta: float) -> dict[int, float]:
    """Shift stored follower *start* values by *delta* (timeline seconds)."""
    return {i: float(s) + float(delta) for i, s in starts0.items()}
