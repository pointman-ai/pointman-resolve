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

Agents get `resolve_render`, `resolve_still`, `resolve_snapshot`, `resolve_diff` and `resolve_check` as tools, or call any command on the Mac with
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
  agent `@pm` means there. A project that isn't listed goes to the `[unmapped]` thread or channel,
  mentioning you. If neither is set, its markers wait.
- **Who:** `agents` in the same file lists the handles a marker may call (the extension can't ask the
  board who exists). A handle that isn't listed, like a typo, goes to the project's agent with a note
  that it wasn't recognised.
- **The marker answers:** once posted it turns **cyan**, with custom data `pm:<post id>`. It turns
  **green** when the agent calls `resolve.marker_done` (tool `resolve_marker_done`). Where the node lets
  extensions read posts, a ✅ from that agent or from you does it too. The name and note are never
  changed.
- **No loops:** markers with custom data `dailies:…` (board notes mirrored as markers) or `pm:…` (already
  posted) are never picked up. Each marker is posted once, remembered in `~/.pointman-resolve/markers.json`,
  even if tagging it in Resolve fails. At most three go out per 10 s round.
- **The only write:** `markers.MarkerWriter` can only tag, delete and re-add a marker, the same kind of
  write `resolve.marker` makes. It tags first, so the marker is never posted twice. A colour change is
  a delete and re-add, and if Resolve refuses the re-add, the marker is put back as it was. It leaves
  alone a marker you've edited since it was posted.
- Turn it off with the `marker_calls` setting.

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
  are in Resolve's units, not pixels: one unit is 2 px on a 1920x1080 timeline (measured against
  Resolve's own stills; other sizes aren't yet). OTIO stores them as a fraction of half the frame.
- From a shell: `python -m pointman_resolve.timeline.cli snapshot | diff A B | check S`.

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
