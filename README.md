# pointman-resolve

Read DaVinci Resolve timelines as data, so Pointman's editing agents can see the edit without
touching James's screen.

```bash
uv run pointman-resolve snapshot [--out DIR] [--no-otio]   # the open timeline -> snapshot-<utc>.json (+ .otio)
uv run pointman-resolve diff A.json B.json [--out FILE]    # what changed from A to B
uv run pointman-resolve check SNAPSHOT.json                # the snapshot against its own OTIO export
```

Each command prints one JSON object on stdout. Exit codes: 0 ok, 1 `check` found mismatches,
2 error (the JSON has `error`). Snapshots go to `~/.pointman-resolve/snapshots/<project>/<timeline>/`
unless `--out` says otherwise.

## Read-only, by construction

Every Resolve object is wrapped in `resolve.ReadOnly`. Only calls named `Get…`, `Is…` or `Export…`
(and upper-case constants such as `EXPORT_OTIO`) get through. Anything else (`Set…`, `Delete…`,
`Append…`, `Load…`, `Save…`, `Transcribe…`) raises `ReadOnlyError` before it reaches Resolve. So a
snapshot never saves, switches project or timeline, moves the playhead, or starts a background job.
The tests check that only reads reach a fake Resolve.

Cost, measured on the Spacesheep timeline (Resolve 21.1, ~1,300 items): about 0.1 ms per call, ~3 s per
snapshot, plus 0.2 s for the OTIO export.

## snapshot-<utc>.json

| Key | |
|---|---|
| `schema` | 1 |
| `taken` | UTC ISO time, e.g. `2026-10-19T02:15:00.123456Z` |
| `resolve`, `project` | version string; `{name, id}` |
| `timeline` | `{name, id, fps, start_frame, end_frame, start_timecode, width, height}` |
| `tracks` | `[{kind, index, name, enabled, locked, subtype}]`, kind `video` / `audio` / `subtitle` |
| `items` | one per timeline item, below |
| `markers` | timeline markers `[{frame, color, name, note, duration, custom}]`, frame from the timeline start |
| `journal` | our own edits since the previous snapshot of this timeline (see Journal) |
| `previous`, `journal_since` | that previous snapshot's file name and `taken` |
| `otio` | `{file, s}`: Resolve's OTIO export beside it, and how long it took |
| `errors` | calls that failed, counted by name (`{}` when clean) |
| `took_s` | seconds for the whole snapshot |

Each item:

| Key | Source | Unit |
|---|---|---|
| `id` | `GetUniqueId` | survives ripples, trims and Resolve restarts |
| `kind`, `track` | | `video`/`audio`/`subtitle`, track index from 1 |
| `record` | `GetStart` − timeline start | timeline frame, 0-based |
| `frames` | `GetDuration` | timeline frames |
| `in` | `GetLeftOffset` | in-point, timeline frames from the file start: **the one to trust** |
| `src_start` | `GetSourceStartTime` | seconds on the media's own timecode (C0030 starts at 4843.68 s) |
| `src_frame` | `GetSourceStartFrame` | for reference only: a frame off `in` on ~25% of items |
| `media_fps`, `file`, `media_id` | media pool item | `null` for transitions and generators |
| `name`, `type` | `GetName`, `GetType` | type `video` / `audio` / `transition` / `generator` |
| `props` | `GetProperties` | Pan, Tilt, crops in output pixels; Zoom 1.0 = fit; audio: AudioVolume dB, AudioPan… |
| `fades`, `speed` | `GetFades`, `GetSpeed` | `{FadeIn, FadeOut}` frames; `{Percentage, …}`; `null` on transitions |
| `enabled`, `color` | `GetClipEnabled`, `GetClipColor` | |
| `nodes` | `GetNodeGraph().GetNumNodes()` | video only; more than 1 means a grade |
| `linked` | `GetLinkedItems` | ids of linked items |
| `markers` | `GetMarkers` | clip markers, frame from the clip start |

Grades themselves aren't in the snapshot or in OTIO (Resolve's OTIO export drops CDL grades, PFE-9);
`nodes` only says whether there is one.

## diff

`{a, b, note, counts, kept, changed, moved, trimmed, new, gone, markers, ours, timeline, tracks}`:

- `kept`: ids with the same place and nothing else changed
- `changed`: same place; `fields` lists what changed (props, fades, speed, enabled, color, nodes, markers)
- `moved`: same length and in-point, new track or record frame (a ripple, a drag)
- `trimmed`: length or in-point changed (a trim or a slip), maybe moved too; `before` and `after`
- `new`: ids only in B, each with `parent`: the item of A it was cut from by **source lineage** (same
  kind and file, source range `[in, in + frames)` inside the parent's, same track preferred), or `null`.
  `parent_kept` says whether the parent's id survived. A razor keeps the id on the piece with the clip's
  original head, and gives every piece after the cut a new id.
- `gone`: ids only in A; `pieces` lists the new items cut from it (a razor whose head was deleted)
- `markers`: timeline markers `new`, `changed`, `gone`, and `moved` (same name, note, colour, custom
  data at another frame)
- `ours`: `{added, removed, entries}` from the journal between A and B; `new` and `gone` entries carry
  `ours: true` when they're ours, so follow mode can tell our re-lays from James's edits

## Journal

Every tool of ours that changes a timeline appends one line to `~/.pointman-resolve/journal.jsonl`
(`$POINTMAN_RESOLVE_HOME` overrides the folder):

```json
{"time": "2026-10-19T02:15:00.123456Z", "tool": "place_cutouts", "timeline": "<timeline id>",
 "removed": ["<item id>"], "added": ["<item id>"], "note": "8 desk ranges"}
```

From Python: `pointman_resolve.journal.record("place_cutouts", removed=[...], added=[...], timeline=tl_id)`.

## check

Compares a snapshot with the OTIO export written beside it: per track, every clip's record frame,
length, in-point and zoom/pan/tilt/crop (OTIO stores Pan and left/right crops ÷ half the width, Tilt and
top/bottom crops ÷ half the height), then transitions and timeline markers. `ok` is true when all agree.

## Tests

```bash
uv run pytest
```

They run against a fake Resolve (`tests/fake.py`), whose `Export` writes OTIO the way Resolve 21.1 does.
