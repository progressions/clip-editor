"""Parse repeated ``--video`` / ``--audio`` flags from argv (#540)."""

from __future__ import annotations

from pathlib import Path


def cli_flag_paths(args: list[str], flag: str) -> list[Path]:
    """Return every value after *flag* (repeatable). Skip a flag with no value."""
    out: list[Path] = []
    i = 0
    n = len(args)
    while i < n:
        if args[i] == flag:
            if i + 1 >= n:
                break
            nxt = args[i + 1]
            if nxt.startswith("-") and not Path(nxt).exists():
                i += 1
                continue
            out.append(Path(nxt).expanduser())
            i += 2
            continue
        i += 1
    return out


def cli_flag_path(args: list[str], flag: str) -> Path | None:
    """First value for *flag*, or None."""
    paths = cli_flag_paths(args, flag)
    return paths[0] if paths else None
