# Manual regression: rendered preview read-only + audio

Use after `Render full preview` or `Preview transition` on a project that has
audible audio in the compiled output.

## Audio + transport

1. Enter rendered preview. Confirm status/label shows **Rendered preview — editing locked**.
2. Press Play. Confirm you hear the compiled audio once, in sync with picture.
3. Pause, seek on the timeline, Play again. Audio and picture resume from the seek point together.
4. Seek while playing. Audio and picture jump together with no second audio source.
5. Let playback reach the end. Playback stops; pressing Play restarts from the beginning.
6. Repeat with a silent compiled preview (no audio stream). Picture-only play/pause/seek/end still work.

## No dual audio

7. Before entering rendered preview, play the edit timeline so ffplay/mpv preview audio is running.
8. Start a compiled preview render / enter cached rendered preview. Edit-preview audio must stop immediately. Only the compiled MediaFile audio may play.

## Mutation lock

9. While rendered preview is active, attempt each of:
   - drag a clip body
   - drag a clip edge (trim)
   - drag a clip to the other track
   - Delete / T (split)
   - Ctrl+Z / Ctrl+Y
   - transform spins / Reset
   - transition type or duration
   - Open video / Open audio / Clear / Fit
   - drop a media file onto the window or timeline
   - pan the cover preview
10. None of those may change the project. Controls should be disabled or ignored with **Rendered preview — editing locked**.
11. Seek and Play/Pause still work. Save and Export may stay available.

## Exit / cleanup

12. Click **Back to edit**. Playback stops, edit preview returns, controls re-enable, playhead is at a sensible position.
13. Enter rendered preview again, then New project / Open project / close the window. Compiled playback and preview audio stop cleanly; no leftover ffplay/mpv.
