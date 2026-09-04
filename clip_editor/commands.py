"""Grammar for Clip Editor's compact Vim-style command prompt."""

from __future__ import annotations

from dataclasses import dataclass

from clip_editor.aspects import ASPECTS


@dataclass(frozen=True, slots=True)
class EditorCommand:
    name: str
    value: str | None = None


def parse_command(text: str) -> EditorCommand | None:
    """Parse a colon command, returning ``None`` for unknown input.

    ``:r916`` maps to aspect ``9:16``. The compact encoding is accepted for
    every actual aspect preset, not for ratios the application cannot render.
    """
    command = text.strip().lower().lstrip(":")
    if command == "rp":
        return EditorCommand("render_preview")
    if command.startswith("r"):
        compact = command[1:]
        for aspect in ASPECTS:
            if compact == aspect.replace(":", ""):
                return EditorCommand("aspect", aspect)
    return None
