---
name: resolve-editing
description: How an agent edits alongside a person in their DaVinci Resolve timeline with pointman-resolve, keeping it trustworthy and clean. Use it whenever you change a Resolve timeline.
---

# Editing in a person's Resolve timeline

For agents changing a DaVinci Resolve timeline. Skip it otherwise.

**Their timeline, their call.**
- Change only the clips you placed, and the ones the person asked about. Their own tracks (camera, screen, voice) need their OK for each change. A marker of theirs asking for a change is that OK.
- Never build, switch or rename timelines under them.
- Don't move their playhead while they're working. Read `GetEndFrame()` and `GetCurrentTimecode()` twice, a few seconds apart, and wait if either changes: the first catches edits, the second scrubbing and playback. Put the playhead back afterwards.
- Every write has a dry run and an undo record. A new version of a file gets a new name (`-v2`); never overwrite media Resolve has open.

**Check in Resolve, not in your head.**
- Crop, Pan and Tilt are Resolve units, not pixels: one unit is 2 px on a 1920×1080 timeline. Measure other sizes before you trust a formula.
- After any change to position, zoom, crop, colour or transitions, look at Resolve's own frame, not only a shadow render: `resolve_frame_check` does it with the playhead guard and shows where the two differ.
- After any cut or move, check frame by frame that stacked pieces still follow the clip under them.

**Keep the timeline clean.**
- One clip per continuous run of source: join the through-edits you make.
- Don't place what nobody will see, and remove your pieces that end up under an opaque full-frame clip.
- When a covering clip's look changes (zoomed out, cropped), re-check what now shows beneath it.
- Media nobody sees under a covering clip is clutter: offer to remove it.
- One named track per role: camera, screen, loft, desktop, cutout, graphics, music, sfx.
- `resolve_qc` counts the clutter per track: check it before and after a batch of changes, and don't leave it higher.

**Make it readable at a glance.**
- Colour clips by kind, the same way everywhere: one colour per layout or block type, so the person can find them all and change them in one pass. Tell them the key.

**A look the person sets by hand is the reference.** Copy their exact values from that clip to every clip of the same kind. Judge "same kind" from each clip's first, middle and last frames, never one frame.

**Markers.** A marker that calls you is a request. Do it, check it, reply in the thread, then mark it done: react ✅, or call `resolve.marker_done`.
- **Don't ask back.** Someone who leaves notes as markers is heads down in Resolve and may not see a question for an hour. Make the call yourself, keep it undoable, and report what you did, why, and how to undo it. Ask only when no choice is safe to undo.

More, with the API details: [skills/resolve-rules.md](skills/resolve-rules.md).
