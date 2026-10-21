# resolve

A Pointman extension for DaVinci Resolve on a Mac. It renders small previews of your timelines and grabs
stills, then posts them on the board, where they can be watched and commented on from any device. Notes on
a preview come back to Resolve as markers on its timeline: purple while a note is open, green once it's
resolved. It can also render a preview by itself once the edit has changed and the Mac has been idle for a
while.

Python (standard library only), run by the node on Python 3.12. It drives Resolve through Resolve's own
scripting API.

```
pointman add resolve
```

## What it needs

- **macOS** (Apple silicon or Intel).
- **DaVinci Resolve Studio**, at `/Applications/DaVinci Resolve/DaVinci Resolve.app`, open, with
  **Preferences › System › General › External scripting using** set to **Local**. Its scripting API is
  found where Resolve installs it. The `RESOLVE_SCRIPT_API`, `RESOLVE_SCRIPT_LIB` and `RESOLVE_APP`
  environment variables point at other places.
- **ffmpeg** and **ffprobe** on the PATH, to make each render's proxy, poster and thumbnail.

The node offers the commands only while Resolve is connected (`ready_when = "connected"`), so a request
made while Resolve is closed waits until it's open again.

## Commands

Agents get `resolve_render`, `resolve_still`, `resolve_snapshot`, `resolve_diff`, `resolve_check`, `resolve_shadow`, `resolve_qc`,
`resolve_frame_check` and `resolve_calibrate` as tools, or call any command on the Mac with
`run_on_machine`. Every Resolve call runs on one lane, one at a time, since Resolve's API isn't meant to be
driven from several threads at once.

| Kind | Params | What |
|---|---|---|
| `resolve.render` | `timeline?`, `height?` (144–2160), `thread_id?` | Renders a timeline (the open one, or one by name) to a small MP4 (H.265 where Resolve has it, else H.264) and posts it, with the timeline's layout (tracks, clips and markers), in the thread given or the project's Resolve previews thread. Takes minutes; cancellable |
| `resolve.still` | `thread_id?` | The frame under the playhead, as a PNG, posted |
| `resolve.marker` | `note`, `project`, `timeline` | Mirrors one note as a marker. `null` when that project isn't the one open, so it's tried again later |
| `resolve.snapshot` | `otio?` (default true) | The open timeline as data, **read only**: every track, item and marker, saved as `snapshot-<utc>.json` with Resolve's OTIO export beside it (below). A few seconds |
| `resolve.diff` | `timeline?`, `a?`, `b?`, `full?` | What changed between two snapshots of a timeline, by default its latest and the one before. Reads files only |
| `resolve.check` | `timeline?`, `snapshot?` | A snapshot against the OTIO export taken with it, item by item. Reads files only |
| `resolve.shadow` | `from`, `to` (≤ 3 min), `width?` (960), `thread_id?` | Any range of the open timeline drawn from the source files with ffmpeg: every track, the audio mixed, posted as a video. Resolve isn't asked to render and the playhead doesn't move, so it's safe while someone edits. Lists what it can't draw yet (grades, transitions, speed, titles). ffmpeg runs niced, so playback in Resolve doesn't stutter |
| `resolve.qc` | `from?`, `to?`, `sound?` (true), `sources?` (true), `thread_id?` | A QC report, posted as a document: black frames, flash frames (clips or gaps under 3 frames), screen recordings that are one frozen frame, clutter per track (clips never seen, through-edits, empty or unnamed tracks), and for a range up to 15 min the mix's loudness (target −14 LUFS), true peak and dead air: an estimate from clip levels, without track faders, the bus or Fairlight effects. Resolve and the playhead aren't touched; it holds the lane about 25 s plus 5 s a minute of mix |
| `resolve.frame_check` | `frame`, `min_psnr?` (30), `wait?` (3 s) | Resolve's own picture of one frame against the shadow render of it: PSNR, the box where they differ, and the clips there with their Zoom/Pan/Tilt/crops (closest fit first). Posts Resolve \| shadow \| difference. Moves the playhead only after the timeline has been still for `wait` seconds, and puts it back |
| `resolve.calibrate` | `frame?`, `dry_run?`, `wait?` | Pixels per Crop/Pan/Tilt unit at the open timeline's size, measured on Resolve's own frame: the candidate (0.5–4) whose shadow render matches it best. Picks a frame with a moved or cropped clip itself. Sets nothing on any clip; stored per resolution in `calibration.json`, and later snapshots carry it |
| `resolve.plan` | `plan` or `ops`, `note?` | **Dry run** of an edit plan ([below](#edit-plans)): compiled on a fresh snapshot, with what stops it, the frames each op changes per track, clutter before and after, and its steps in words. Changes nothing |
| `resolve.preview` | `plan`, `limit?`, `pad?` | The shadow render of each op, before above after, around the change. Nothing in Resolve moves |
| `resolve.apply` | `plan`, `wait?` | Applies a plan once the timeline has been still for `wait` s and a fresh dry run gives the same steps; checked as it goes, verified after |
| `resolve.undo` | `plan`, `wait?` | Puts back what the plan changed; stops, changing nothing, if anything in the way isn't the plan's own |

- **Your project is left as it was:** a render saves the project's render settings to a temporary preset
  and restores them after. It also goes back to the page and timeline you were on.
- **Where it posts:** in the board project named like the Resolve project (matched against the folders
  the node allows), else in one called Resolve. A render is a `timeline` card at
  `resolve/<project>/<timeline>`, so each render of a timeline is a new version of the same card, and a
  still is at `.../frame`.
- **Disk space:** a render doesn't start with less than 10 GB free.

## By itself

- **Markers:** every 15 s it reads the notes changed on the board. Each note at a timecode on a Resolve
  preview becomes a marker at that frame, titled "Review: …" (purple) or "Resolved: …" (green), and goes
  once the note is deleted. A note whose project isn't open in Resolve waits until it is.
- **Auto renders:** with `auto_render_idle_minutes` above 0, it renders the open timeline once the edit
  has changed (clips, trims, tracks or markers) and neither it nor the Mac's keyboard and mouse have been
  touched for that long. An edit that's already been rendered isn't rendered again.

## Markers that call an agent

Write `@pm`, `@pointman` or `@<agent>` at the start of a timeline marker's name or note, and the marker
is posted on the board for that agent. The post has the marker's timecode, what you wrote, the clips
under it, and a still of that frame. The still is drawn from the source files by
`timeline/render.py`, so your playhead and page don't move.

- **Where:** `~/.pointman-resolve/routes.toml` maps each Resolve project to a board thread and to the
  agent `@pm` means there. A project you rename in Resolve keeps its route: it's known by its project
  and timeline IDs (`projects.json`).
- **No route:** a project that isn't listed posts to the **fallback**, and the post says so: the thread
  and agent of the last marker posted from a routed project on this machine. An agent moves it with
  `resolve.set_fallback` (tool `resolve_set_fallback`; it's kept in `fallback.json`). On a machine with
  no fallback yet, it starts from the last marker in `markers.json`. If there isn't one, those markers
  wait, untouched, until a routed project posts one or an agent sets the fallback.
- **Who:** `agents` in the same file lists the handles a marker may call (the extension can't ask the
  board who exists). A handle that isn't listed, like a typo, goes to the project's agent with a note
  that it wasn't recognised.
- **The marker answers:** once posted it turns **cyan**, with custom data `pm:<post id>`. It turns
  **green** when the agent calls `resolve.marker_done` (tool `resolve_marker_done`). Where the node lets
  extensions read posts, a ✅ from that agent or from you does it too. The name and note are never
  changed.
- **Claimed before it's posted:** a marker is tagged `pm:claim-<key>` before its post goes out, then given
  `pm:<post id>` wherever it is by then. A slow post (a Wi-Fi drop) and your edits meanwhile can't make it look
  new and post it twice, and a post that fails takes the claim off, so it's tried again.
- **Colours heal:** a posted or done marker that's back at the colour it had before (an undo in Resolve takes
  back our delete + add) gets cyan or green again. One you've given another colour is left alone.
- **No loops:** markers with custom data `dailies:…` (board notes mirrored as markers) or `pm:…` (ours) are
  never picked up. Each marker is posted once, remembered in `~/.pointman-resolve/markers.json`,
  even if tagging it in Resolve fails. At most three go out per 10 s round.
- **The only write:** `markers.MarkerWriter` can only tag, delete and re-add a marker, the same kind of
  write `resolve.marker` makes. It tags first, so the marker is never posted twice. A colour change is
  a delete and re-add, and if Resolve refuses the re-add, the marker is put back as it was. It leaves
  alone a marker you've edited since it was posted.
- Turn it off with the `marker_calls` setting.

## Edit plans

Resolve 21.1's API can't move, slip or split a clip. So every change to where a clip is or what it shows
is a delete plus an `AppendToTimeline`, with everything about the clip put back. Edit
plans do that the same careful way every time (PFE-11; they replace Spacesheep's fix_sync.py,
fix_sync_constant.py and move_block.py).

A plan is a list of ops on item ids from a snapshot, frames counted from the timeline's start:

| op | fields | what it does |
|---|---|---|
| `slip` | `items`, `by`, `follow?` | moves the source in-point by `by` frames, same place and length; `follow` (e.g. `["V3", "V5"]`) takes the pieces stacked inside each clip along |
| `swap` | `a`, `b`, `c`, `stay?` | `[a, b)` and `[b, c)` trade places on every track but `stay`; markers move with them |
| `move` | `items`, `to?`, `track?` | to another frame or track, no ripple |
| `replace` | `item`, `file`, `in?` | another file or in-point in the same place (a `-v2` composite) |
| `place` | `track`, `file`, `record`, `frames`, `in?`, `props?` | a new clip |
| `set` | `items`, `props?`, `fades?`, `color?`, `enabled?` | settings, in place |
| `delete` | `items` | no ripple |
| `join` | `items`, `same_grade?` | a through-edit (same file running on frame for frame) into one clip. Graded pieces need `same_grade`: how you know their grades match, since Resolve can't read a grade's values on the Edit page |
| `cut` | `ranges`, `fill` | ripple-deletes each `[s, e)` across every track, last first: clips crossing an edge are split, a track with nothing there gets `fill` (black, or silence, mono where the track is), and every later clip is checked to have moved by exactly `e - s` |

An op on the person's own tracks (named camera, screen or voice) needs `ok`: what asked for it. `props` are
Resolve's own values (`GetProperties` keys): Crop, Pan and Tilt in Resolve units, never pixels (see Units below).

- **Dry run** (`resolve.plan`): the ops compile against a model of the timeline into remove and lay
  steps. It refuses what wouldn't survive a re-lay: linked clips, Fusion comps, retimed clips, clip
  markers, titles and generators. It also refuses a spot that's taken (Resolve would return a clip and
  place nothing), a source running off either end of its file, and a dissolve with no handles. A
  frame-map diff shows the frames each op changes per lane. Any change outside what the op means to
  change is a problem, and a cut is compared shifted.
- **Units:** sources go to `AppendToTimeline` in the file's own frames. A 60 fps screen recording on a
  25 fps timeline moves `round(k × 2.4)` of its frames per `k` timeline frames, and is compared in
  seconds within one of its frames.
- **Apply** (`resolve.apply`) runs only once the timeline has been still for `wait` seconds, and only
  if a fresh dry run gives the same steps (its digest). Each op then:
  1. checks every clip it removes is where the plan says, and every file is in the media pool;
  2. lays a test clip past the timeline's end. For a graded clip that test clip is its stand-in, and
     `CopyGrades` puts the grade on it first. Nothing is removed until Resolve has taken a clip; a busy
     Resolve returns nothing, and it waits, then stops;
  3. removes, appends and reads every new clip back from its track (place, length, source);
  4. puts settings, fades, colour, grades and centred dissolves back.

  `applied.json` is written after every step. A stop says why and what was done. Apply never saves,
  switches timelines, adds tracks, moves the playhead or starts a background job.
- **Verify:** after applying, the live timeline is read and compared with the model, clip by clip.
- **Undo** (`resolve.undo`): everything the plan placed or moved goes, and the snapshot's clips come
  back where they were, grades copied across from what replaced them, markers moved back. A cut is
  opened again first, the last one made first, with the same rippling speed change `resolve.insert_gap`
  uses (`timeline/gap.py`), so everything after it moves back as it is. Only what the cut split or took
  out is laid again. Undo refuses to touch a clip that isn't the plan's own or the snapshot's.
- **Preview** (`resolve.preview`): the shadow render before and after each op.

- **Versions** (`resolve.version`, PFE-12): for a whole-cut change the person wants to compare, the plan
  goes onto a copy of their timeline (`DuplicateTimeline` keeps grades, Fusion, transitions and markers;
  an OTIO round trip would drop grades), never onto theirs. While it's made, Resolve shows the copy, so it
  needs `ok`: what asked for it. The person is then put back on their timeline, page and playhead. Their
  clip selection can't be set back, so a change to it is reported. They switch between the two to A/B.
  `resolve.drop_version` deletes a copy, but only when it isn't open and nobody has edited it. The marker
  watcher and the follow watcher skip copies (`versions.json`): a copy holds every @ marker of the original,
  and it isn't the person's cut.

Plans live in `~/.pointman-resolve/plans/<id>/`: `plan.json`, `compiled.json` (every step) and
`applied.json`. Every apply and undo is in the journal, so diffs and follow mode can tell them from the
person's own edits.

## Watching the cut

`resolve.watch` (tool `resolve_watch`), and the same checks by themselves (PFE-13). Once the person has paused
for 15 s and the cut has changed since the last check, the agents' tracks are checked against it:

| check | what it finds | fix in the plan |
|---|---|---|
| sync | a cutout or loft piece (`<clip>-f<start>…`) showing a camera frame 2+ off the voice under it, less that camera's offset | `slip` by the difference |
| no cutout | a desktop piece with no cutout over 8+ frames of it | none: it may be meant |
| orphan cue | a sound with no card or flip starting or ending near it | `delete` |
| scrap | a picture piece under 8 frames on the agents' tracks | `delete` |
| see-through | a cut below a full-frame card or flip while it fades (alpha read from the file), or just outside one | none: move the card |
| chapter | a chapter card (`c01-…`) that an edit took out | none: say so |

What's new since the last post goes to the project's agent in the project's thread, as a document, with a
dry-run plan for the fixes. The editor previews and applies it, since only the editor changes the timeline. A change
the person made to the agents' tracks is taken as meant: the checks read the timeline as it is, so nothing is moved
back. Only projects with a `follow` table in `routes.toml` are watched:

```toml
[projects."feat-flow-with-spacesheep".follow]
roles = { flips = "V7" }                  # role -> track; by default from the track names
offsets = { C0030 = -175.929 }            # voice - camera, seconds, per camera clip
```

Version timelines (copies made to A/B a plan) aren't watched. Turn it off with the `watch` setting.

## Guidance for agents

[SKILLS.md](SKILLS.md) goes into the prompt of every agent woken on a machine where this extension is on
(Pointman release 9 or later). It covers editing a person's timeline:
- their call, and don't move their playhead while they edit;
- check every change in Resolve's own frames;
- keep the timeline clean, and colour clips by block type;
- treat a look they set by hand as the reference;
- markers are requests: don't ask back.

[skills/resolve-rules.md](skills/resolve-rules.md) has the API details behind each rule.

## The timeline as data

`resolve.snapshot`, `resolve.diff` and `resolve.check` (`pointman_resolve/timeline/`) give agents the edit as
data, so they can follow it without watching your screen.

- **Read only, by construction:** the snapshot wraps every Resolve object in `ReadOnly`, which lets only
  `Get…`, `Is…` and `Export…` calls through. It can't save, switch project or timeline, move the playhead,
  add a marker or start a job. It runs on the "resolve" lane like everything else here.
- **A snapshot** holds the timeline (name, id, fps, start and end frame, size), its tracks, every item
  (id, kind, track, record frame, length, in-point, source time and frame, media fps and file, type,
  properties, fades, speed, enabled, colour, grade nodes, linked items, clip markers), the timeline
  markers, and our own edits since the previous snapshot. Snapshots live in
  `~/.pointman-resolve/snapshots/<project>/<timeline>/`. The commands take snapshots by file name,
  never by path.
- **A diff** matches items by Resolve's `GetUniqueId`, which survives ripples, trims and restarts. It
  sorts them into kept, changed, moved, trimmed, new and gone. A new item cut from an old one (a razor)
  names it as `parent`, by source range, not position. Markers are new, changed, gone or moved, and
  `ours` lists what our own tools did, from `~/.pointman-resolve/journal.jsonl`.
- **Units** (Resolve 21.1): `in` is `GetLeftOffset`, the in-point in timeline frames, and it matched
  Resolve's OTIO on every item. `src_start` is seconds on the media's own timecode. Pan, tilt and crops
  are in Resolve's units, not pixels. Pan and Tilt count across the clip's *fitted image*: 960 units across
  its width and 540 down its height. A 16:9 source on a 1920x1080 timeline fills the frame, so a unit is
  2 px; a 1728x1116 screen recording fits to 1672 px wide, so its Pan unit is 1.74 px (measured with
  `resolve.frame_check`). Crops are timeline pixels before the zoom, 2 px a unit whatever the source's shape:
  on that recording CropRight 471.7 put the edge at x 780, not the 960 that fitted shares would give
  (measured with `resolve.frame_check`). `resolve.calibrate` measures the timeline's
  own size, and a snapshot carries it as `timeline.px_per_unit` (`null` if not measured; 2 is assumed). OTIO stores them as units ÷ 960 (Pan,
  CropLeft/Right) and ÷ 540 (Tilt, CropTop/Bottom): for a crop that's a fraction of the timeline's width or
  height, for Pan and Tilt a fraction of the fitted image's.
- From a shell: `python -m pointman_resolve.timeline.cli snapshot | diff A B | check S`.
- **Grades** (`timeline/grades.py`): Resolve has `SetCDL` but no `GetCDL`, so a tool that sets a CDL also writes it
  to `~/.pointman-resolve/grades.json` under the clip's unique id: `{"items": {"<id>": {"cdl": {"Slope": "r g b",
  "Offset": …, "Power": …, "Saturation": …}, "by": "<tool>"}}}`. Snapshots attach it as `item.cdl`, and the shadow
  render, marker stills and frame checks draw it. A neutral CDL removes the entry.
- **Resolve's own frame** (`timeline/framecheck.py`): the shadow render is only as right as its model of
  Resolve. On Spacesheep v1 every cutout was invisible in Resolve for a day while our placer and renderer
  agreed with each other, both taking units for pixels. `resolve.frame_check` closes that gap after any
  change to position, zoom, crop, colour or transitions. Exporting Resolve's frame moves the playhead,
  so it reads `GetEndFrame` and `GetCurrentTimecode` twice, `wait` seconds apart, and retries later if
  either changed (edits, scrubbing, playback). Afterwards it puts the playhead back and says whether it's
  back (if not, `ok` is false and a `warning` comes first). It can only reach those calls: the project and
  timeline are wrapped like `MarkerWriter`. Its reads of the timeline stay in memory, so frame checks don't
  add snapshots to the store. Whole frame rates only for now (23.976 and 29.97 are refused).

## Status

`resolve.status`, every 10 s: `connected` (and why not), Resolve's version, the page it's on, the open
project and timeline (name, fps, duration, timecode, tracks and markers), and whether it's rendering. It
says `blocked` when Resolve answers scripts with nothing, which is what a modal dialog does.

`health.check` answers with one part, DaVinci Resolve: `ok`, `stopped` (not open) or `failing` (scripting
off, or blocked by a dialog), with its version and why. It reads the latest status; it never calls
Resolve itself.

## Tests

```
uv run --no-project --python 3.12 --with pytest python -m pytest tests
```

The timeline modules run against a fake Resolve (`tests/fake.py`). `tests/test_extension.py` runs
`python -m pointman_resolve` the way the node does and talks to it over JSON-RPC, with Resolve made to
look absent. It needs the node's `pointman_extension.py` (`$POINTMAN_EXTENSION_SDK`, or the one the node
installed).

## Settings

Per machine, in `~/.config/pointman/extensions/resolve/config.toml`, or
`pointman config set node.extensions.resolve.<key> <value>`:

| Setting | Default | |
|---|---|---|
| `preview_height` | 720 | Previews are rendered at this height (480, 720 or 1080), or the timeline's if it's smaller |
| `auto_render_idle_minutes` | 0 | Render a preview after the edit changes and the Mac has been idle this long. 0 is off |
| `markers` | `true` | Notes on a Resolve preview become markers on its timeline |
