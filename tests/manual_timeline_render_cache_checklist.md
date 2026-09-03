# Manual regression: timeline render cache (#532)

Production `/usr/bin/clip-editor` does **not** include this until the PR is merged.
Run the worktree **beside** production:

```bash
cd ~/tech/clip-editor-532-timeline-render-cache
CLIP_EDITOR_APP_ID=local.clip.Editor.Dev python3 -m clip_editor
```

Title should read **Clip editor (dev)**. There is no Render button — **Play / Space** bakes if needed.

1. New project, drop one clip. Bar over that clip is **red**.
2. Press Space. It renders, bar turns **green**, then plays (picture + audio) on the same timeline.
3. Add a second clip. First range stays **green**; the new one is **red**.
4. Press Space. Renders the new range (and the join), whole bar **green**, plays through including transitions and audio.
5. Playhead moves at 1× with no skips. Editing is never locked.
4. Picture includes the dissolve; audio comes from the cache file (no second ffplay mix).
5. Edit the **first** clip (trim or transform). Only that span (and the transition neighbor) turn **red**; a later unused clip stays green.
6. **Render dirty** again — only red ranges re-bake.
7. **Preview transition** / **Render full preview** still enter locked QC mode; **Back to edit** restores the cache bar.
8. Cache files live under `~/.cache/clip-editor/previews/` (`*_seg.mp4`), not Eagle intake.
