# Clip editor

Local Ginger tool: open a video, optionally replace its audio, cover-crop to a
social aspect, export one Buffer-safe H.264/AAC MP4.

Not a kdenlive clone. No titles or codec menus.

The installed application and its cache stay on local disk rather than in the
Dropbox vault, so encode temporary files and the optional Chromium profile do
not sync.

Vault note: `GENNIE/Ops/clip-editor.md`.

## Install

Clip Editor is distributed as an Arch package. A release is built from a Git
tag whose version matches `pkgver` in `PKGBUILD`.

```bash
git clone https://github.com/progressions/clip-editor.git
cd clip-editor
makepkg -si
```

The package installs the Python application, `/usr/bin/clip-editor`, and the
desktop entry. It declares GTK, libadwaita, GStreamer, and FFmpeg runtime
dependencies. Chromium is optional and is only used by the legacy `serve`
command.

### Migrating from the source launcher

Remove the old user-owned symlink and desktop-file copy after installing the
package. This does not touch projects or application state.

```bash
unlink ~/.local/bin/clip-editor
rm ~/.local/share/applications/clip-editor.desktop
```

If either path is not present, skip that command. The package-owned desktop
entry under `/usr/share/applications` is then used by the app launcher.

### Upgrade, downgrade, and uninstall

Build a newer checked-out tag with `makepkg -si` to upgrade. To downgrade,
install a previously built package from the Pacman cache:

```bash
sudo pacman -U /var/cache/pacman/pkg/clip-editor-<version>-any.pkg.tar.zst
```

Uninstall with `sudo pacman -Rns clip-editor`. Pacman removes program files
only. It preserves projects, exported media, Eagle intake files, and
`~/.local/state/clip-editor`.

Maintainer release procedure:

1. Update the version in `clip_editor/__init__.py` and `PKGBUILD`; reset
   `pkgrel` to 1.
2. Run `clip-editor selftest` and build/test the package.
3. Commit, tag the commit as `v<version>`, and push the commit and tag.
4. Run `makepkg -si` from the tagged checkout.

## Run

Super+Space, type **editor**. Native GTK window (not a browser). Colors follow
the current Omarchy theme (`colors.toml`), same mapping as Eagle Browse, and
update if you switch themes while the window is open.

```bash
clip-editor                 # GTK window (installed package)
clip-editor gui             # same
clip-editor gui --video clip.mp4            # add video to the current project
clip-editor gui --video a.mp4 --video b.mp4 # add several videos (repeatable)
clip-editor gui --new --video clip.mp4      # new project with that video
clip-editor gui --audio bed.m4a             # add audio; keep the current project
clip-editor export --video in.mp4 --audio bed.mp3 --aspect 9:16
clip-editor selftest
```

### Develop while the package is installed

```bash
~/tech/clip-editor/clip-editor              # source; PYTHONPATH = checkout
~/tech/clip-editor/clip-editor selftest
```

Keep using `/usr/bin/clip-editor` for normal launches. Do not restore a
`~/.local/bin/clip-editor` symlink over the package. See
`GENNIE/Ops/isaac-archrepo.md`.

Eagle Browse: **Shift+E** sends `--video` / `--audio` (add). **Ctrl+Shift+E** sends `--new --video` (new project). A second launch is handed to the running window.

The installed console entry point is `/usr/bin/clip-editor`; it does not use
the source checkout, `PYTHONPATH`, or a particular working directory.

Window is `http://127.0.0.1:8765/` (localhost only).

## Export contract

Matches `buffer-publish`:

- MP4, `libx264 -preset slow -crf 20`, `yuv420p`, `+faststart`
- AAC 128k when there is audio
- Even width/height
- Cover-crop, never stretch, never letterbox
- Then the H.264 ffprobe gate

| Aspect | Low (720) | Medium (1080) | High (1440) |
|--------|-----------|---------------|-------------|
| 9:16 | 720×1280 | 1080×1920 | 1440×2560 |
| 4:5 | 720×900 | 1080×1350 | 1440×1800 |
| 1:1 | 720×720 | 1080×1080 | 1440×1440 |
| 16:9 | 1280×720 | 1920×1080 | 2560×1440 |

Resolution is a Low / Medium / High preset (short-edge). Medium is the default and matches the previous fixed sizes. CLI: `--resolution low|medium|high`.

Drag the preview to reframe. In/out points trim the output (H3 head-blip).
Replacement audio starts at 0 of the audio file unless “Audio follows video
in-point” is on (driver sync). If the music is longer than the picture, **Fit**
cuts it to the video length (no speed change). Export always does that cut.

Set **Cross-fade** to a duration such as `0.5 seconds` to dissolve between
adjacent touching clips. `0` disables transitions. Clips separated by a gap do
not cross-fade. Source or replacement audio clips use the same fade duration.
This first version applies the transition during export; timeline preview still
shows a hard cut.

Export writes a `{stem}_{9x16}.mp4` (always `.mp4`) **flat into Eagle Browse
intake**, using the same config eagle-browse does (`eagle-browse.toml`,
`~/.config/eagle-browse/config.toml`, `EAGLE_INBOX`). Encode lands as
`*.mp4.tmp` then `os.replace` so the Ginger watcher never sees a half-written
file.

## Project files

Timeline shortcuts run only while the timeline has keyboard focus (click it
or reach it with Tab). Text fields, inspector controls, menus, and dialogs keep
their native keys, including text undo. Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y edit the
project history only from the timeline. Save and Open remain application-wide.
The colon command entry owns typing until Enter or Esc returns focus to the
timeline.

`h/l` selects previous/next clips in time order on the active video or audio
track. `Shift+H/L` extends the selection. `j/k` changes tracks through
V2, V1, A1, A2 and selects the clip under the playhead, otherwise the nearest
clip edge (ties prefer the earlier clip). Empty tracks clear selection.
`Esc` clears selection. Source-soundtrack audio can be selected without
detaching it; audio edits will make it independent of video as needed.

Format: JSON, ``format: "clip-editor-project"``, ``version: 3``, suffix
``.clip.json``. Records video/audio paths (absolute plus relative to the
project file), aspect, pan, in/out, Fit, cross-fade duration, and “audio follows
in-point”.

The hamburger menu: Open project, Save (Ctrl+S), Save As (Ctrl+Shift+S).
The current edit auto-saves every 800ms to
``~/.local/state/clip-editor/autosave.clip.json``, and to the named project
file once you have saved once. Reopening the app restores the last session.

Drop a ``.clip.json`` onto the window to open it.

## First-goal job

Open a (usually 1:1) gen, add a music or driver track, set 9:16, drag the
subject into frame, Export.
