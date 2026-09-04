# Changelog

All notable changes to Clip Editor are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.9.0] — 2026-09-04

### Added

- Vim-style aspect commands ([#537](https://app.fizzy.do/6109848/cards/537) / PR #21): open `:` and use `:r916`, `:r34`, `:r45`, `:r11`, `:r43`, or `:r169` to select an output aspect ratio. Added 3:4 and 4:3 presets.
- Explicit **Render Preview** control and `:rp` command ([#538](https://app.fizzy.do/6109848/cards/538) / PR #18). Space now starts immediate timeline playback, using rendered cache only for green spans.
- Repeated `--video` / `--audio` GUI arguments ([#540](https://app.fizzy.do/6109848/cards/540) / PR #20), so Eagle Browse can add a selection to the current project.

### Fixed

- Per-clip X/Y position now moves the full source beneath the project viewport instead of shifting a pre-cropped project frame (PR #19), preventing blank side margins for mismatched source and project aspect ratios.

## [0.8.0] — 2026-09-03

### Added

- Keyboard-first timeline navigation ([#536](https://app.fizzy.do/6109848/cards/536) / PR #17): `h` / `l` select previous / next video clips, and Shift extends the selection.
- `j` / `k` move a visible keyboard track cursor through V2, V1, A1, and A2.

## [0.7.0] — 2026-09-03

### Added

- Ripple right-edge trim: at a packed join, drag the earlier clip’s right edge; later clips on the same track stay packed ([#533](https://app.fizzy.do/6109848/cards/533) / PR #15).

### Fixed

- Timeline click no longer restamps clip in/out from the inspector spins (that had rescaled every clip).

## [0.6.0] — 2026-09-03

### Added

- Timeline preview bar: red = not rendered, green = rendered ([#532](https://app.fizzy.do/6109848/cards/532) / PR #13).
- Play (Space) bakes a preview when anything is red, then plays that preview on the same timeline (transitions + audio). Already-green ranges stay green when you add new clips.
- `CLIP_EDITOR_APP_ID` to run a second window beside the packaged app.

## [0.5.0] — 2026-09-03

### Added

- Per-clip playback speed (0.25×–4×) in the inspector; multi-select applies one rate to every selected video clip ([#531](https://app.fizzy.do/6109848/cards/531) / PR #11).
- Timeline bar shrinks/grows with rate; export uses `setpts` + `atempo` (pitch follows rate).
- Speed persists in `.clip.json` (default remains 1×).

## [0.4.0] — 2026-09-02

### Added

- Multi-select timeline clips with Shift+click (additive); Esc or empty-timeline click clears ([#530](https://app.fizzy.do/6109848/cards/530) / PR #9).
- Group drag: moving one selected clip slides the whole selection together.
- Bulk transition edit: type and duration apply to every selected video clip.
- Accent selection chrome so multi-selected clips stay obvious.

### Fixed

- Shift detection under Hyprland/Wayland reads the seat keyboard modifier state (GestureClick event state often drops Shift).
- Plain clip press no longer collapses a multi-select when starting a group drag (GestureClick / GestureDrag race).

## [0.3.0] — 2026-09-01

### Added

- Low / Medium / High export resolution presets (720 / 1080 / 1440 short edge) ([#501](https://app.fizzy.do/6109848/cards/501) / PR #7).

### Changed

- Rendered preview locks editing and plays compiled preview audio in sync ([#492](https://app.fizzy.do/6109848/cards/492) / PR #5).

### Fixed

- Harden saved `.clip.json` project loading with recoverable media warnings ([#493](https://app.fizzy.do/6109848/cards/493) / PR #6).

## [0.2.0] — 2026-08-31

### Added

- Per-cut transitions: dissolve and white flash ([#487](https://app.fizzy.do/6109848/cards/487) / PR #3).
- Compiled transition previews: Preview transition / Render full preview ([#488](https://app.fizzy.do/6109848/cards/488) / PR #4).

## [0.1.0] — 2026-08

- Initial packaged release of the native GTK clip editor for Buffer-safe H.264/AAC exports.

[Unreleased]: https://github.com/progressions/clip-editor/compare/v0.9.0...HEAD
[0.9.0]: https://github.com/progressions/clip-editor/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/progressions/clip-editor/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/progressions/clip-editor/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/progressions/clip-editor/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/progressions/clip-editor/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/progressions/clip-editor/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/progressions/clip-editor/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/progressions/clip-editor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/progressions/clip-editor/releases/tag/v0.1.0
