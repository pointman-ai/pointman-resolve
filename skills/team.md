# Editing as a team: editor, FX artist, renders

How a cut runs with more than one agent, from editing a 47-minute video with an editor agent in
Resolve, an FX artist making motion graphics in chat, and a render agent on another machine, all with
the person who owns the video. SKILLS.md has the short version; this is the detail.

## Who does what

| Role | Works in | Owns | Hands over |
|---|---|---|---|
| **Editor** | Resolve, through pointman-resolve | the timeline: every change to it | briefs, placements, checks, the report to the person |
| **FX artist** | chat and their own files | the graphics: cards, transitions, overlays | files that fit their slot, stills, manifest entries |
| **Render agent** | another machine (a GPU) | renders: composites, cutouts, proxies | deliveries posted to the board |
| **The person** | Resolve and the thread | the cut and its look | notes as markers, picks between options |

- **One writer on the timeline.** Only the editor changes it. The FX artist and the render agent never
  place their own files, even when they could: two writers means two undo stories and a timeline nobody
  can account for.
- **One thread per video.** Briefs, deliveries, checks and the person's picks all go in the video's
  thread, so the decision and its history stay in one place. Side threads are for one card's back and
  forth.

## The loop

1. **The person leaves a marker** (`@editor …`) at the moment in the cut. It posts itself to the video's
   thread with a still.
2. **The editor decides who makes it.** A change to clips, timing or a grade is the editor's. A new
   graphic, or a new version of one, goes to the FX artist as a brief. A missing render goes to the render
   agent.
3. **The FX artist answers with an ETA** ("On it: d02b-v2, about 30 min"), works, then posts the delivery.
4. **The editor places it**, checks Resolve's own frames, and reports to the person with stills and the
   undo command. Then the marker is marked done (✅ or `resolve.marker_done`).
5. **Notes on a placed graphic** go back through the editor to the FX artist, as a new brief with the
   person's words quoted. The FX artist doesn't edit the timeline to try a change.

Work runs in parallel: while the FX artist builds a card, the editor carries on with the cut. Several
graphics can be in flight at once. Number them and keep one file per id.

## A brief (editor → FX artist)

Everything the FX artist needs, so nobody has to ask back:

- **The person's words**, quoted from the marker, and its timecode.
- **The slot:** the exact frame count and the timeline's size and fps (`448 frames, 1920×1080, 25p`). If
  the card spans a cut, say where the cut falls in the card's frames.
- **What's underneath:** the shot or the screen, and the area to keep clear (the person's face and
  shoulders, a terminal, a cursor), measured on the real footage at that time, not on a typical frame.
  Mention any punch-in under the card, since the card may need to move with it.
- **The look:** which family (paper, graphite, a glitch), and the reference card if there is one.
- **The ending:** a hard cut out, or an animation out, and whether a transition covers it.
- **Content to check:** code, names, numbers. The FX artist checks them against the source (a repo
  commit, a doc) and marks anything that's a sketch as a sketch.
- **Consecutive markers about one idea** can be one card with steps, rather than two cards back to back.

## A delivery (FX artist → editor)

- **The file:** `graphics/<id>-<look>.mov`, ProRes 4444 with alpha, at the timeline's size and fps,
  with the **frame count checked** (`ffprobe` nb_frames) against the slot.
- **A revision is a new file:** `<id>-<look>-v2.mov`. The old one stays, so undo can put it back. Never
  overwrite a file Resolve has open.
- **Stills at the key frames**, rendered over the real background at that time (frame 30, mid-animation,
  the last frame), so the person and the editor can judge it without opening Resolve.
- **Layout in numbers:** the panel's box (`x 93–811, y 96–645`) and what it stays clear of (`his face at
  x 840`), so the editor can check it against the shot.
- **Timing in numbers:** what happens on which frame (`64: swap to Cohere/Local; 190: step 2 wipes in over
  6 frames`), and how it ends (`447 is the full panel, hard cut`).
- **What changed from the brief** and why (a call wrapped to fit the width, a comment shortened).
- **What wasn't checked:** not seen in Resolve yet, a punch-in not known, disk space low.

## Shared data, not prose

Timing the editor acts on travels in files the editor's scripts read, kept next to the media:

- **A manifest** (`edit/<family>-manifest.json`) listing each card's id, file and frame count, with its
  **cue frames**: the frames where a sound (a tick, a blip) should land. The FX artist updates it with
  every delivery; the editor places sound from it.
- **Visible frames** (`edit/<family>-visible.json`): the frames each card actually shows something, so a
  grade or a dim under it starts and ends on those frames.
- **Punch-ins under cards** (`edit/punch-ins-under-cards.json`): which cards sit over a camera move, so the
  FX artist can add parallax.

A number said only in a post gets lost or retyped wrong; one in the manifest is used as written.

## Options

When a graphic's direction isn't settled, the FX artist makes **two or three real options** in chat:
stills (or a short render) of each over the real background, each with what it's good for. The person
picks in the thread, and the editor places the pick and nothing else. The options that weren't picked
stay as files, named, in case the person changes their mind.

## Renders from another machine

- The render agent names deliveries after their source and range, with a version
  (`C0030-f<start>-alpha-v2.mov`), and says what's in each batch and what's still to come.
- The editor checks disk space before downloading a batch, and checks sync once placed (every frame of
  an overlay shows the same source frame as the camera under it).
- If a render is missing for a stretch the editor needs, the editor asks for those ranges first and
  works around them meanwhile (voice-over on the screen, say), telling the person.
