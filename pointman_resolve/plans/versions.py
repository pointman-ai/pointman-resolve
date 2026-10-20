"""Whole-cut changes as a version the person can A/B (PFE-12): the plan is applied to a copy of their
timeline, not to it, and they switch between the two in Resolve to compare.

    make()  DuplicateTimeline (keeps grades, Fusion, transitions, markers: an OTIO round trip drops grades),
            switch to the copy, map the plan's item ids onto it, apply the plan there with the same placer and
            checks as resolve.apply, then switch back to the person's timeline, page and playhead, checked.
    drop()  delete a version we made, only if it isn't open and nobody has edited it since.

Switching timelines is something the person sees, so a version is only made when they asked for one (`ok`).
The versions are listed in versions.json, which the marker watcher skips: a copy holds every @ marker of the
original, and posting them again would wake every agent they called.
"""

import json
import time
from pathlib import Path

from ..timeline import journal
from ..timeline.readonly import ReadOnly
from ..timeline.snapshot import take
from . import apply as plan_apply
from .model import PlanError

PLACE = ("kind", "track", "record", "frames", "in", "file", "type")


def registry() -> Path:
    return journal.home() / "versions.json"


def load() -> dict:
    try:
        return json.loads(registry().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: dict) -> None:
    p = registry()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)


def is_version(timeline_id: str | None) -> bool:
    """A copy we made that the person hasn't adopted: the marker watcher leaves its markers alone."""
    v = load().get(timeline_id or "")
    return bool(v) and not v.get("adopted")


def match_ids(src: dict, dup: dict) -> dict:
    """The copy's id for each of the original's items: same lane, place, length, in-point, file and type.
    An item with no single match is left out, and an op that names it is refused."""
    by: dict = {}
    for it in dup["items"]:
        by.setdefault(tuple(it.get(k) for k in PLACE), []).append(it["id"])
    out = {}
    for it in src["items"]:
        found = by.get(tuple(it.get(k) for k in PLACE)) or []
        if len(found) == 1:
            out[it["id"]] = found[0]
    return out


def rewrite(plan: dict, ids: dict) -> dict:
    """The plan with every item id it names swapped for the copy's."""
    ops = []
    for op in plan.get("ops") or []:
        op = dict(op)
        for field in ("items", "item"):
            if field not in op:
                continue
            names = op[field] if isinstance(op[field], list) else [op[field]]
            missing = [x for x in names if x not in ids]
            if missing:
                raise PlanError(f"{missing[0]} has no single match in the copy (same place, length, in-point and "
                                "file): the version wasn't made")
            mapped = [ids[x] for x in names]
            op[field] = mapped if isinstance(op[field], list) else mapped[0]
        ops.append(op)
    return {**plan, "ops": ops}


def timelines(project) -> list:
    return [t for t in (project.GetTimelineByIndex(i) for i in range(1, (project.GetTimelineCount() or 0) + 1)) if t]


def free_name(project, name: str) -> str:
    taken = {t.GetName() for t in timelines(project)}
    if name not in taken:
        return name
    n = 2
    while f"{name} ({n})" in taken:
        n += 1
    return f"{name} ({n})"


def make(resolve, snap: dict, plan: dict, name: str, record: plan_apply.Record, *, media=None, sleep=time.sleep,
         wait: float = 2.0) -> dict:
    """The plan applied to a copy of the open timeline, which stays as it was; the person is put back on it."""
    project = resolve.GetProjectManager().GetCurrentProject()
    original = project.GetCurrentTimeline()
    oid, playhead = original.GetUniqueId(), original.GetCurrentTimecode()
    page = resolve.GetCurrentPage() if hasattr(resolve, "GetCurrentPage") else None
    selected = selection(original)
    if oid != snap["timeline"]["id"]:
        raise plan_apply.Stop(f"Resolve has {original.GetName()!r} open; the plan is for {snap['timeline']['name']!r}")
    name = free_name(project, name)
    dup = original.DuplicateTimeline(name)
    if not dup:
        raise plan_apply.Stop("Resolve wouldn't copy the timeline: nothing was changed")
    reg = load()
    reg[dup.GetUniqueId()] = {"name": name, "of": oid, "plan": record.data.get("plan") or plan.get("id"),
                              "made": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    save(reg)
    record.data["version"] = {"id": dup.GetUniqueId(), "name": name, "of": oid}
    record.save()
    out, err, back = {}, None, {}
    try:
        if project.GetCurrentTimeline().GetUniqueId() != dup.GetUniqueId() and not project.SetCurrentTimeline(dup):
            raise plan_apply.Stop("Resolve wouldn't open the copy: it's there, unchanged, as " + repr(name))
        copy_snap, _ = take(ReadOnly(resolve))
        version_plan = rewrite(plan, match_ids(snap, copy_snap))
        out = plan_apply.apply(resolve, copy_snap, version_plan, record, media=media, sleep=sleep, wait=wait)
        copy_after, _ = take(ReadOnly(resolve))          # the copy as it is: the A/B's bottom row is drawn from this
        left = sorted((it["id"], it["record"], it["frames"]) for it in copy_after["items"])
        out["copy_snapshot"] = copy_after
        reg = load()
        reg[dup.GetUniqueId()]["left"] = left            # what we left it as: drop() refuses once it's been edited
        save(reg)
    except Exception as e:  # noqa: BLE001 - always back to the person's timeline first, then report
        err = e
    finally:
        back = put_back(resolve, project, original, oid, playhead, page)
        if selection(original) != selected:               # there's no setter: say so instead
            back["selection"] = "changed: re-select what you had"
    if err:
        raise err
    return {**out, "version": name, "version_id": dup.GetUniqueId(), "back": back}


def selection(tl) -> list:
    """The ids of the clips selected in the timeline (21.1 can read a selection but not set one)."""
    try:
        return sorted(c.GetUniqueId() for c in (tl.GetSelectedClips() or []) if c)
    except (AttributeError, TypeError):
        return []


def put_back(resolve, project, original, oid: str, playhead: str, page) -> dict:
    """The person's timeline, playhead and page, as they left them; says what didn't come back."""
    ok = project.SetCurrentTimeline(original)
    now = project.GetCurrentTimeline()
    on = bool(now) and now.GetUniqueId() == oid
    if on and original.GetCurrentTimecode() != playhead:
        original.SetCurrentTimecode(playhead)
    if page and hasattr(resolve, "OpenPage") and resolve.GetCurrentPage() != page:
        resolve.OpenPage(page)
    return {"timeline": on and bool(ok), "playhead": on and original.GetCurrentTimecode() == playhead,
            "page": (resolve.GetCurrentPage() == page) if page else None}


def drop(resolve, version_id: str) -> dict:
    """Delete a version we made: not if it's open, and not once anyone has edited it since we left it."""
    reg = load()
    v = reg.get(version_id)
    if not v:
        raise plan_apply.Stop("that timeline isn't one of our versions")
    project = resolve.GetProjectManager().GetCurrentProject()
    t = next((x for x in timelines(project) if x.GetUniqueId() == version_id), None)
    if t is None:
        reg.pop(version_id, None)
        save(reg)
        return {"dropped": v["name"], "note": "it was already gone"}
    if project.GetCurrentTimeline().GetUniqueId() == version_id:
        raise plan_apply.Stop(f"{v['name']!r} is open in Resolve: switch away from it first")
    if v.get("left") is not None:
        now = sorted((it.GetUniqueId(), it.GetStart() - t.GetStartFrame(), it.GetDuration())
                     for kind in ("video", "audio") for k in range(1, (t.GetTrackCount(kind) or 0) + 1)
                     for it in t.GetItemListInTrack(kind, k) or [])
        left = sorted(tuple(x) for x in v["left"])
        if [tuple(x) for x in now] != left:
            raise plan_apply.Stop(f"{v['name']!r} has been edited since it was made: it stays (delete it by hand)")
    if not project.GetMediaPool().DeleteTimelines([t]):
        raise plan_apply.Stop("Resolve wouldn't delete it")
    reg.pop(version_id, None)
    save(reg)
    return {"dropped": v["name"]}
