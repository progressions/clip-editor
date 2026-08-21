# Clip editor

Local Ginger tool: open a video, optionally replace its audio, cover-crop to a
social aspect, export one Buffer-safe H.264/AAC MP4.

Not a kdenlive clone. No titles or codec menus.

Lives here on purpose (`~/tech/`), not in the Dropbox vault — encode temp files
and the Chromium profile should not sync.

Vault note: `GENNIE/Ops/clip-editor.md`.

## Run

Super+Space, type **editor**. Native GTK window (not a browser). Colors follow
the current Omarchy theme (`colors.toml`), same mapping as Eagle Browse, and
update if you switch themes while the window is open.

```bash
clip-editor                 # GTK window
clip-editor gui             # same
clip-editor gui --video clip.mp4            # add video to the current project
clip-editor gui --new --video clip.mp4      # new project with that video
clip-editor gui --audio bed.m4a             # add audio; keep the current project
clip-editor export --video in.mp4 --audio bed.mp3 --aspect 9:16
clip-editor selftest
```

Eagle Browse: **Shift+E** sends `--video` / `--audio` (add). **Ctrl+Shift+E** sends `--new --video` (new project). A second launch is handed to the running window.

The launcher is `~/tech/clip-editor/clip-editor`. Symlink it onto PATH:

```bash
ln -sfn "$HOME/tech/clip-editor/clip-editor" "$HOME/.local/bin/clip-editor"
```

Window is `http://127.0.0.1:8765/` (localhost only).

## Export contract

Matches `buffer-publish`:

- MP4, `libx264 -preset slow -crf 20`, `yuv420p`, `+faststart`
- AAC 128k when there is audio
- Even width/height
- Cover-crop, never stretch, never letterbox
- Then the H.264 ffprobe gate

| Aspect | Size |
|--------|------|
| 9:16 | 1080×1920 |
| 4:5 | 1080×1350 |
| 1:1 | 1080×1080 |
| 16:9 | 1920×1080 |

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
