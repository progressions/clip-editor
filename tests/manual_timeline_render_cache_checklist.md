# Manual regression: timeline render cache (#532)

Use a project with at least two abutting video clips and a dissolve on the first.

1. Confirm the thin bar above the lanes is **red** on used ranges (gray in gaps).
2. Click **Render dirty**. Status counts segments. Bar turns **green** on those ranges.
3. Play from a green span without entering “Rendered preview — editing locked”. Trim/move still work.
4. Picture includes the dissolve; audio comes from the cache file (no second ffplay mix).
5. Edit the **first** clip (trim or transform). Only that span (and the transition neighbor) turn **red**; a later unused clip stays green.
6. **Render dirty** again — only red ranges re-bake.
7. **Preview transition** / **Render full preview** still enter locked QC mode; **Back to edit** restores the cache bar.
8. Cache files live under `~/.cache/clip-editor/previews/` (`*_seg.mp4`), not Eagle intake.
