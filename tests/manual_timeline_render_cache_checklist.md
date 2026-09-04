# Manual regression: timeline render cache (#532)

Production `/usr/bin/clip-editor` does **not** include this until the PR is merged.
Run the worktree **beside** production:

```bash
cd ~/tech/clip-editor-532-timeline-render-cache
CLIP_EDITOR_APP_ID=local.clip.Editor.Dev python3 -m clip_editor
```

Title should read **Clip editor (dev)**. **Play / Space** starts immediately;
**Render Preview** (or `:rp`) is the only action that bakes red spans.

1. New project, drop one clip. Bar over that clip is **red**.
2. Press Space. Playback starts immediately from the playhead; the red span uses
   raw editable playback and cache state remains red.
3. Click **Render Preview** or type `:rp`. It renders, then the bar turns
   **green** without starting playback.
4. Press Space inside the green span. Picture includes the dissolve and audio
   comes from the cache file (no second ffplay mix).
5. Add or edit a second clip. The unaffected first range stays **green** and
   the changed range becomes **red**.
6. Press Space across both ranges. Green uses baked playback; red uses raw
   playback without starting a render.
7. Playhead moves at 1× with no skips. Editing is never locked.
8. Edit the **first** clip (trim or transform). Only that span (and the transition neighbor) turn **red**; a later unused clip stays green.
9. **Render Preview** again — red spans turn green.
10. **Preview transition** / **Render full preview** still enter locked QC mode; **Back to edit** restores the cache bar.
11. Cache files live under `~/.cache/clip-editor/previews/`, not Eagle intake.
