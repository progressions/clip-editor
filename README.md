# Clip editor

Local Ginger tool: open a video, optionally replace its audio, cover-crop to a
social aspect, export one Buffer-safe H.264/AAC MP4.

Not a kdenlive clone. No effects, titles, or codec menus.

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
clip-editor export --video in.mp4 --audio bed.mp3 --aspect 9:16
clip-editor selftest
```

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

Export writes a `{stem}_{9x16}.mp4` (always `.mp4`) **flat into Eagle Browse
intake**, using the same config eagle-browse does (`eagle-browse.toml`,
`~/.config/eagle-browse/config.toml`, `EAGLE_INBOX`). Encode lands as
`*.mp4.tmp` then `os.replace` so the Ginger watcher never sees a half-written
file.

## First-goal job

Open a (usually 1:1) gen, add a music or driver track, set 9:16, drag the
subject into frame, Export.
