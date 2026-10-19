# Resolve editing rules, with the API details

These are from editing a 47-minute talking-head video with screen recordings in DaVinci Resolve 21.1, on
a 25 fps 1920×1080 timeline, alongside the person who owns it. Each rule says why it exists.

## The API is in the Scripting folder's `.pyi`, not README.txt

`/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/DaVinciResolveScript.pyi`
and `CHANGELOG.md` describe 21.x. README.txt is from 2024 and misses things like `Get/SetFades`, `SetSpeed`,
`AddTransition`, `GetType()` and audio properties.

## Placing clips

- `MediaPool.AppendToTimeline` takes **source** frames (`startFrame`, `endFrame`) and a record frame. For a
  60 fps source on a 25 fps timeline: `start = round(in / 25 * 60)`, `end = start + ceil(frames * 60 / 25)`.
  Fractional frames are cut off, which left 1-frame gaps after 53 clips once. Check the placed clip's
  `GetStart()`, `GetDuration()` and `GetSourceStartFrame()` every time.
- Appending onto an occupied spot returns an item but changes nothing. Check the result.
- `GetLeftOffset()` is in timeline frames. On a fractionally placed 60 fps clip it can round either way, so
  compare 60 fps sources in source time (`GetSourceStartFrame()` plus 2.4 per timeline frame).
- `GetUniqueId()` survives ripples, trims, restarts and the head of a razor cut. Use it to find your clips
  again after the person edits; positions move.
- `ImportTimelineFromFile` switches the current timeline. Don't call it on someone's open project.
- `AppendToTimeline` only places on the current timeline: there's no way to add to another one without
  switching to it.
- On 25 fps sources, `GetSourceStartFrame()` is a frame off on about a quarter of items. Use `GetLeftOffset()`
  there, and `GetSourceStartFrame()` only for faster sources (60 fps screen recordings).
- Connecting to Resolve resets the process's locale to C, so open every file with `encoding="utf-8"`.
  Non-ASCII names broke a check that relied on the default.

## Units and looks

- Crop, Pan and Tilt: one unit is 2 px on a 1920×1080 timeline for a 16:9 source (Pan 100 moved a clip 200 px,
  Tilt 100 moved it 200 px up, CropRight 260 cut 520 px). The units are shares of the clip's fitted image, not
  the frame: 960 across its width, 540 down its height. A 1728×1116 screen recording fits to 1672×1080, so its
  Pan unit is 1.74 px while its Tilt unit stays 2 px. Using 2 px put a desktop 2–3 px off. Measured for Pan
  on a pillarboxed source only: that crops on a non-16:9 clip and Tilt on a letterboxed (wider than 16:9) one
  follow the same rule is inferred. Check the first such clip with `resolve_frame_check`. A crop of 520 per side in "pixels" removes the whole picture, and
  the clip vanishes without an error. OTIO export stores them as units ÷ 960 (Pan, CropLeft/Right) and
  ÷ 540 (Tilt, CropTop/Bottom).
- Zoom scales about the frame centre after the crop. Pan is right, Tilt is up.
- `TimelineItem.SetCDL({"NodeIndex": "1", "Slope": "r g b", ...})` sets a grade, but there's no `GetCDL`, and
  `ExportLUT` refuses on the Edit page. Record every CDL you set in `~/.pointman-resolve/grades.json` under the
  clip's unique id (README, Grades): the shadow render and frame checks draw it, and undo can put it back.
- `Get/SetProperty` still works but is deprecated. Read every key you'll change before you change it.

## Checking your work

- `Project.ExportCurrentFrameAsStill(path)` exports the frame under the playhead, from the edit page. To
  check another frame: `SetCurrentTimecode`, export, then put the playhead back, and only while the person
  isn't editing.
- Keep a frame map: for every timeline frame, which file and source frame each track shows. Comparing maps
  before and after a tidy proves nothing changed.
- A shadow render (ffmpeg from the sources) is fast and doesn't touch Resolve, but it's only as right as its
  model of Resolve. Calibrate it against Resolve's own stills.

## Keeping it clean

- Agent placements copy every cut onto every track they add. That's how a 47-minute cut ended up with
  1,000+ clips. Join through-edits (same file, same settings, the source carrying on frame for frame): a
  delete plus one append, with the first piece's settings.
- A piece fully under an opaque full-frame clip is never seen. Don't place it, or remove it.
- When the covering clip stops covering (zoomed out to show a whole screen), what's beneath shows at the
  edges. Re-check, then remove the unused media (with the person's OK) or fill it on purpose.

## Transitions and fades

- `TimelineItem.AddTransition({"type": "Cross Dissolve", "category": "simple", "position": "end",
  "alignment": "center", "duration": 16})` adds a transition at the cut. Both clips need handles.
- A cross dissolve between two alpha overlays (person cutouts) double-exposes them, and the background shows
  through mid-dissolve. Dissolve the background and hard-cut the overlay.
- `SetFades({"FadeIn": n, "FadeOut": n})` fades a clip's start or end, in frames. Use it where a dissolve
  has no clip to dissolve to.

## Background jobs

`CreateSubtitlesFromAudio` and similar run in the background. Once, one hung, and every `SaveProject` and
`LoadProject` returned False until Resolve was restarted. Never start one on the person's project.
