# Changelog

All notable changes to Clip Editor are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.16.0] — 2026-10-03

### Added

- Reverse selected video clips from the clip controls while keeping their trimmed range, timeline placement, and speed. Linked sound reverses with the video; direction is preserved by splitting, undo/redo, saved projects, rendered playback, and export. Projects now use format version 9.

## [0.15.0] — 2026-10-03

### Added

- Drag the selected video clip inside the preview to move it in X/Y; use corner handles to resize proportionally, with one undo step per drag. Selection remains the edit target at adjacent cuts and on overlapping tracks.
- Scale clips down to 5% for smaller overlays. V2 composites over V1 in the preview and export, revealing the lower track or black background around transformed clips. Existing preview caches are invalidated for the new compositor.

## [0.14.2] — 2026-10-02

### Added

- Keyboard shortcut reference in the current themed popover style, opened with the status line's `? help` hint or `?` on the timeline. Includes zoom, clip settings, playback, project, and editing shortcuts; `Esc` dismisses help without changing the active edit mode or selection (PR #34).

## [0.14.1] — 2026-10-01

### Fixed

- Plain `=` and `-` zoom the focused timeline without Shift; `+` and keypad add/subtract also work. Fit moves to `F`, with updated key hints.
- Space toggles playback from any focused editor control, with key-repeat protection.

## [0.14.0] — 2026-10-01

### Changed

- New Omarchy-style interface: bordered panes like Hyprland windows (focused pane gets the accent border), terminal font, key hints in place of buttons, and a statusline with the keyboard mode, status, export target and project menu. The header bar and sidebar are gone; the media bin is a strip under the timeline.
- Clip settings (trim, speed, frame, fades, clip and track volume) are in a popover: Enter or double-click a clip. Transitions open from the diamond at a cut.
- Colors come from the active Omarchy theme, including derived surfaces, hue-matched status colors and btop's key-hint color; the window repaints when the theme changes.

### Added

- Timeline filmstrips on video clips and waveforms on audio clips.
- Drag fade knobs to set fades and the line across an audio clip to set its volume.
- Scroll on the preview to scale the selected clip.
- Safe-zone overlay for 9:16 (`z`): shades where TikTok, Reels and Shorts draw their interface.
- Timeline keys: `Enter` clip settings, `z` safe zones, `e` export, `-` / `+` zoom, `=` fit.

### Fixed

- Audio trim fields and playhead buttons edit the selected audio clip, with undo/redo and source-duration limits.
- Small fade/volume drags are committed; cancelled drags restore their original values. Clicking a fade knob keeps the clip selected.
- Filmstrip and waveform jobs are deduplicated, limited to two concurrent workers, and matched to current media after project switches. Closing a window releases its theme listener and visual cache.
- Closing a settings popover no longer steals focus from another open popover.

## [0.13.0] — 2026-10-01

### Added

- Independent clip and A1/A2 track volume controls (0–200%) for simultaneous audio mixing. Clip and track gains multiply and apply to playback, rendered previews, and exports (PR #45).
- Project format 8 persists volumes with undo/redo and preview-cache invalidation. Older projects default to 100% volume.

### Changed

- Add audio preserves the video soundtrack as editable A1 clips and places new audio on the free layer. When both layers are occupied, new files stay in the media bin for explicit placement.
- Mixing no longer automatically halves overlapping audio layers; the selected clip and track volumes determine their levels.

## [0.12.3] — 2026-10-01

### Fixed

- Dragging the playhead near either visible timeline edge scrolls and seeks into hidden time, including back to 0:00. Holding the pointer at the edge continues scrolling; release, cancellation, or hiding the timeline stops it. Rendered previews support the same behavior (PR #43).

## [0.12.2] — 2026-10-01

### Fixed

- Timeline zoom-out continues below Fit, shrinking clips and showing more empty time after them. Repeated clicks preserve the selected scale, and Fit restores the full-project view (PR #41).

## [0.12.1] — 2026-10-01

### Fixed

- Timeline Fit uses edited clip endpoints across video and audio tracks, including trims, placement, and playback speed. Full-source ghost outlines no longer enlarge the view.

## [0.12.0] — 2026-10-01

### Added

- Timeline −, +, and Fit controls. Zoom preserves the visible center; Fit shows all video and audio clips and adapts to the available width. Ruler ticks gain detail when zooming.

### Fixed

- Explicitly close GTK media pipelines when replacing media or exiting. Application shutdown now runs cleanup as well as window close, preventing decoder workers from continuing into process teardown.

## [0.11.0] — 2026-09-13

### Added

- Clip fade in / fade out with set duration ([#567](https://app.fizzy.do/6109848/cards/567)): per-clip opacity/gain fades (0.1–3.0s), timeline markers, inspector toggles, FFmpeg `fade`/`afade` in preview and export. Distinct from per-cut Dissolve / White flash (#487). Project format version 7.

## [0.10.0] — 2026-09-05

### Added

- Keyboard clip movement across video and audio tracks ([#541](https://app.fizzy.do/6109848/cards/541), [#542](https://app.fizzy.do/6109848/cards/542)): `m` enters move mode with 10s, 1s, 0.1s, and visible clip-boundary increments. Up/Down changes granularity while `j`/`k` remain track navigation.
- Keyboard ripple reorder ([#543](https://app.fizzy.do/6109848/cards/543)): `r` enters reorder mode; `h`/`l` moves a selected clip or contiguous group across a touching neighbor on the active track, preserving unequal durations and other tracks.
- Keyboard clip-edge trimming ([#547](https://app.fizzy.do/6109848/cards/547)): `[` and `]` enter left/right trim modes. Right-edge edits ripple following clips on the same track by the actual duration change, with source bounds, playback speed, gaps, and undo preserved.
- Keyboard seek and split workflow ([#548](https://app.fizzy.do/6109848/cards/548)): `s` enters seek mode with time, frame-grid, and clip-edge increments; `t` splits the selected video or audio clip at the playhead while preserving source alignment and speed. Mirrored source audio becomes independent only when split.

## [0.9.1] — 2026-09-04

### Fixed

- Preserve the source aspect ratio when a clip has X/Y position or Scale applied (PR #23). Rendered previews and exports now scale the full source layer before clipping it to the project viewport, preventing square clips from being squeezed into 3:4 or other project aspects.

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

[Unreleased]: https://github.com/progressions/clip-editor/compare/v0.14.2...HEAD
[0.14.2]: https://github.com/progressions/clip-editor/compare/v0.14.1...v0.14.2
[0.14.1]: https://github.com/progressions/clip-editor/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/progressions/clip-editor/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/progressions/clip-editor/compare/v0.12.3...v0.13.0
[0.12.3]: https://github.com/progressions/clip-editor/compare/v0.12.2...v0.12.3
[0.12.2]: https://github.com/progressions/clip-editor/compare/v0.12.1...v0.12.2
[0.12.1]: https://github.com/progressions/clip-editor/compare/v0.12.0...v0.12.1
[0.12.0]: https://github.com/progressions/clip-editor/compare/v0.11.0...v0.12.0
[0.11.0]: https://github.com/progressions/clip-editor/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/progressions/clip-editor/compare/v0.9.1...v0.10.0
[0.9.1]: https://github.com/progressions/clip-editor/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/progressions/clip-editor/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/progressions/clip-editor/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/progressions/clip-editor/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/progressions/clip-editor/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/progressions/clip-editor/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/progressions/clip-editor/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/progressions/clip-editor/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/progressions/clip-editor/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/progressions/clip-editor/releases/tag/v0.1.0
