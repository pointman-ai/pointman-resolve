"""A snapshot of the timeline open in Resolve: every track, item, property and marker, as JSON, plus
Resolve's own OTIO export of the same timeline beside it.

Read-only (see resolve.ReadOnly). ~20 calls per item at ~0.1 ms each: about 3 s for 1,300 items.

Units, as measured in PFE-9 (Resolve 21.1):
- record, frames, in: timeline frames. `in` is GetLeftOffset, the in-point counted in timeline frames
  from the start of the file. It's the one to trust: GetSourceStartFrame is a frame off on ~25% of
  items, even 25 fps ones (kept as `src_frame` for reference only).
- src_start: GetSourceStartTime, seconds on the media's own timecode (C0030 starts at 4843.68 s),
  sub-frame for 60 fps media.
- props: GetProperties as Resolve gives them: Pan, Tilt and crops in output pixels, Zoom 1.0 = fit.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA, journal

KINDS = ("video", "audio", "subtitle")


class SnapshotError(RuntimeError):
    pass


class Reader:
    """Calls that can fail one item at a time (an odd item shouldn't sink the snapshot): failures
    are counted by call name and reported in the snapshot's `errors`."""

    def __init__(self):
        self.errors: dict[str, int] = {}

    def __call__(self, obj, name, *args, default=None):
        try:
            return getattr(obj, name)(*args)
        except Exception:
            self.errors[name] = self.errors.get(name, 0) + 1
            return default


def utc_stamp(t: datetime) -> str:
    return t.strftime("%Y%m%dT%H%M%S") + f"{t.microsecond // 1000:03d}Z"  # 20261019T021500123Z: sorts as text


def markers_list(raw) -> list[dict]:
    return [{"frame": int(f), "color": m.get("color"), "name": m.get("name"), "note": m.get("note"),
             "duration": m.get("duration"), "custom": m.get("customData", "")}
            for f, m in sorted((raw or {}).items(), key=lambda kv: float(kv[0]))]


def number(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def item_record(get: Reader, it, kind: str, track: int, t0: int) -> dict:
    start = get(it, "GetStart")
    mp = get(it, "GetMediaPoolItem")
    graph = get(it, "GetNodeGraph") if kind == "video" else None
    rec = {
        "id": get(it, "GetUniqueId"), "kind": kind, "track": track,
        "record": start - t0 if isinstance(start, (int, float)) else None,
        "frames": get(it, "GetDuration"), "in": get(it, "GetLeftOffset"),
        "src_start": get(it, "GetSourceStartTime"), "src_frame": get(it, "GetSourceStartFrame"),
        "media_fps": number(get(mp, "GetClipProperty", "FPS")) if mp else None,
        "file": (get(mp, "GetClipProperty", "File Path") or None) if mp else None,
        "media_id": get(mp, "GetUniqueId") if mp else None,
        "name": get(it, "GetName"), "type": get(it, "GetType"),
        "props": get(it, "GetProperties", default={}) or {},
        "fades": get(it, "GetFades"), "speed": get(it, "GetSpeed"),
        "enabled": get(it, "GetClipEnabled"), "color": get(it, "GetClipColor") or "",
        "nodes": get(graph, "GetNumNodes") if graph else None,
        "linked": [x for x in (get(l, "GetUniqueId") for l in get(it, "GetLinkedItems", default=[]) or [] if l) if x],
        "markers": markers_list(get(it, "GetMarkers")),
    }
    if rec["type"] == "transition":  # transitions have no fades/speed of their own (Resolve gives None/False)
        rec["fades"], rec["speed"] = None, None
    return rec


def take(resolve) -> tuple[dict, object]:
    """The snapshot of the open timeline, and the (read-only) timeline object for the OTIO export."""
    t_start, taken = time.perf_counter(), datetime.now(timezone.utc)
    get = Reader()
    pm = resolve.GetProjectManager()
    project = pm.GetCurrentProject() if pm else None
    tl = project.GetCurrentTimeline() if project else None
    if not project or not tl:
        raise SnapshotError("no project or timeline open in Resolve (or a dialog is blocking scripting)")
    t0 = tl.GetStartFrame()
    settings = get(tl, "GetSettings", default={}) or {}
    timeline = {"name": tl.GetName(), "id": get(tl, "GetUniqueId"),
                "fps": number(settings.get("timelineFrameRate")), "start_frame": t0, "end_frame": tl.GetEndFrame(),
                "start_timecode": get(tl, "GetStartTimecode"),
                "width": int(number(settings.get("timelineResolutionWidth")) or 0) or None,
                "height": int(number(settings.get("timelineResolutionHeight")) or 0) or None}
    tracks, items = [], []
    for kind in KINDS:
        for i in range(1, (get(tl, "GetTrackCount", kind) or 0) + 1):
            tracks.append({"kind": kind, "index": i, "name": get(tl, "GetTrackName", kind, i),
                           "enabled": get(tl, "GetIsTrackEnabled", kind, i), "locked": get(tl, "GetIsTrackLocked", kind, i),
                           "subtype": get(tl, "GetTrackSubType", kind, i) or None})
            for it in get(tl, "GetItemListInTrack", kind, i, default=[]) or []:
                items.append(item_record(get, it, kind, i, t0))
    snap = {
        "schema": SCHEMA, "taken": taken.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "resolve": get(resolve, "GetVersionString"),
        "project": {"name": project.GetName(), "id": get(project, "GetUniqueId")},
        "timeline": timeline, "tracks": tracks, "items": items,
        "markers": markers_list(get(tl, "GetMarkers")),
        "previous": None, "journal_since": None, "journal": [],
        "errors": get.errors,
        "took_s": round(time.perf_counter() - t_start, 3),
    }
    return snap, tl


def latest(out_dir: Path, timeline_id: str | None = None, *, look_back: int = 50) -> dict | None:
    """The newest snapshot in out_dir (of this timeline, if given), with its file name, or None."""
    for f in sorted(out_dir.glob("snapshot-*.json"), reverse=True)[:look_back]:
        snap = json.loads(f.read_text())
        if timeline_id is None or snap.get("timeline", {}).get("id") == timeline_id:
            snap["file"] = f.name
            return snap
    return None


def attach_journal(snap: dict, previous: dict | None) -> dict:
    """Our own edits (journal.py) since the previous snapshot of the same timeline."""
    since = previous["taken"] if previous else None
    snap["previous"] = previous.get("file") if previous else None
    snap["journal_since"] = since
    snap["journal"] = journal.entries(after=since, until=snap["taken"], timeline=snap["timeline"]["id"])
    return snap


def write(resolve, out_dir: Path, *, otio: bool = True) -> Path:
    """Take a snapshot, write snapshot-<utc>.json (+ .otio) into out_dir, return the JSON's path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    snap, tl = take(resolve)
    attach_journal(snap, latest(out_dir, snap["timeline"]["id"]))
    stem = f"snapshot-{utc_stamp(datetime.strptime(snap['taken'], '%Y-%m-%dT%H:%M:%S.%fZ'))}"
    if otio:
        path = out_dir / f"{stem}.otio"
        t = time.perf_counter()
        ok = tl.Export(str(path), resolve.EXPORT_OTIO, resolve.EXPORT_NONE)
        snap["otio"] = {"file": path.name if ok and path.exists() else None, "s": round(time.perf_counter() - t, 3)}
    target = out_dir / f"{stem}.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snap, indent=1, default=str))
    os.replace(tmp, target)  # readers never see half a snapshot
    return target
