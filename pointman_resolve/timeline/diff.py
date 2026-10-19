"""What changed between two snapshots of a timeline. Offline: no Resolve calls.

Items are matched by id (GetUniqueId). From watching James edit (PFE-9), ids survive ripples, trims
and Resolve restarts; a razor cut keeps the id on the piece with the clip's original head and gives
every piece after the cut a new id; a delete, or one of our own delete + re-append, makes ids go.

- kept: same id, same place, nothing else changed
- changed: same id and place; props, fades, speed, enabled, colour, grade nodes or clip markers changed
- moved: same id, length and in-point; new track or record frame (a ripple, a drag)
- trimmed: same id; length or in-point changed (a trim or a slip), maybe moved too
- new: ids only in B. `parent` is the item of A it was cut from, by source lineage: same kind and file,
  and its source range [in, in + frames) inside the parent's, preferring the same track. Ripple deletes
  between razor pieces shift them, so this matches on source, never on timeline position.
- gone: ids only in A. `pieces` lists the new items cut from it (a razor whose head was deleted).
- markers: timeline markers new / changed / gone / moved (same name, note, colour and custom data at a
  different frame).
- ours: ids our own tools added or removed between the two snapshots (from the journal), so follow
  mode can tell our re-lays from James's edits. new/gone entries carry `ours: true` when they're ours.
"""

from . import journal

PLACE = ("kind", "track", "record", "frames", "in")
ATTRS = ("props", "fades", "speed", "enabled", "color", "nodes", "markers")


def brief(it: dict) -> dict:
    return {"id": it["id"], **{k: it.get(k) for k in PLACE}, "file": it.get("file"), "name": it.get("name")}


def lane(it: dict) -> str:
    return f"{it['kind'][0].upper()}{it['track']}"


def parent_of(it: dict, before: list[dict]) -> dict | None:
    """The item of `before` this one was cut from: same kind and file, source range inside its range."""
    if not it.get("file") or it.get("in") is None or it.get("frames") is None:
        return None
    a, b = it["in"], it["in"] + it["frames"]
    found = [p for p in before if p["kind"] == it["kind"] and p.get("file") == it["file"] and p.get("in") is not None
             and p["in"] <= a and b <= p["in"] + (p.get("frames") or 0)]
    if not found:
        return None
    # the same track first, then the tightest source range (the most specific parent)
    return min(found, key=lambda p: (p["track"] != it["track"], p.get("frames") or 0))


def marker_key(m: dict) -> tuple:
    return (m.get("name"), m.get("note"), m.get("color"), m.get("custom"))


def diff_markers(a: list[dict], b: list[dict]) -> dict:
    fa, fb = {m["frame"]: m for m in a}, {m["frame"]: m for m in b}
    changed = [{"frame": f, "before": fa[f], "after": fb[f]} for f in sorted(fa.keys() & fb.keys()) if fa[f] != fb[f]]
    new = [fb[f] for f in sorted(fb.keys() - fa.keys())]
    gone = [fa[f] for f in sorted(fa.keys() - fb.keys())]
    moved = []
    for g in list(gone):  # the same marker at another frame (a ripple carried it)
        n = next((n for n in new if marker_key(n) == marker_key(g)), None)
        if n is not None:
            moved.append({"from": g["frame"], "to": n["frame"], **{k: n[k] for k in ("color", "name", "note")}})
            gone.remove(g)
            new.remove(n)
    return {"new": new, "changed": changed, "gone": gone, "moved": moved}


def ours_between(a: dict, b: dict, *, local: bool = True) -> dict:
    """Ids our tools added or removed after A was taken and up to when B was taken: the journal
    entries embedded in B, plus this machine's journal file (if A and B weren't consecutive)."""
    lo, hi, tid = a["taken"], b["taken"], b["timeline"].get("id")
    seen, entries = set(), []
    for e in list(b.get("journal") or []) + (journal.entries(after=lo, until=hi, timeline=tid) if local else []):
        key = (e.get("time"), e.get("tool"))
        if lo < e.get("time", "") <= hi and key not in seen:
            seen.add(key)
            entries.append(e)
    return {"added": sorted({i for e in entries for i in e.get("added") or []}),
            "removed": sorted({i for e in entries for i in e.get("removed") or []}),
            "entries": [{k: e.get(k) for k in ("time", "tool", "note")} for e in entries]}


def diff(a: dict, b: dict, *, local_journal: bool = True) -> dict:
    if a["timeline"].get("id") != b["timeline"].get("id"):
        note = f"different timelines: {a['timeline']['name']!r} vs {b['timeline']['name']!r}"
    else:
        note = None
    ia, ib = {i["id"]: i for i in a["items"]}, {i["id"]: i for i in b["items"]}
    ours = ours_between(a, b, local=local_journal)
    ours_ids = set(ours["added"]) | set(ours["removed"])
    kept, changed, moved, trimmed = [], [], [], []
    for uid in ia.keys() & ib.keys():
        x, y = ia[uid], ib[uid]
        attrs = [k for k in ATTRS if x.get(k) != y.get(k)]
        if all(x.get(k) == y.get(k) for k in PLACE):
            (changed.append({"id": uid, "fields": attrs, **brief(y)}) if attrs else kept.append(uid))
        elif x.get("frames") == y.get("frames") and x.get("in") == y.get("in"):
            moved.append({"id": uid, "from": {"track": x["track"], "record": x["record"]},
                          "to": {"track": y["track"], "record": y["record"]}, "also_changed": attrs, "lane": lane(y)})
        else:
            trimmed.append({"id": uid, "before": brief(x), "after": brief(y), "also_changed": attrs, "lane": lane(y)})
    before = list(ia.values())
    new = []
    for uid in sorted(ib.keys() - ia.keys(), key=lambda u: (ib[u]["kind"], ib[u]["track"], ib[u]["record"] or 0)):
        p = parent_of(ib[uid], before)
        new.append({**brief(ib[uid]), "lane": lane(ib[uid]), "parent": p["id"] if p else None,
                    "parent_kept": (p["id"] in ib) if p else None, "ours": uid in ours_ids})
    pieces: dict[str, list[str]] = {}
    for n in new:
        if n["parent"]:
            pieces.setdefault(n["parent"], []).append(n["id"])
    gone = [{**brief(ia[u]), "lane": lane(ia[u]), "pieces": pieces.get(u, []), "ours": u in ours_ids}
            for u in sorted(ia.keys() - ib.keys(), key=lambda u: (ia[u]["kind"], ia[u]["track"], ia[u]["record"] or 0))]
    out = {
        "a": {"taken": a["taken"], "timeline": a["timeline"]["name"], "items": len(ia)},
        "b": {"taken": b["taken"], "timeline": b["timeline"]["name"], "items": len(ib)},
        "note": note,
        "counts": {"kept": len(kept), "changed": len(changed), "moved": len(moved), "trimmed": len(trimmed),
                   "new": len(new), "gone": len(gone), "new_ours": sum(n["ours"] for n in new),
                   "gone_ours": sum(g["ours"] for g in gone)},
        "kept": sorted(kept), "changed": changed, "moved": moved, "trimmed": trimmed, "new": new, "gone": gone,
        "markers": diff_markers(a.get("markers") or [], b.get("markers") or []),
        "ours": ours,
        "timeline": {k: [a["timeline"].get(k), b["timeline"].get(k)] for k in ("name", "end_frame", "fps")
                     if a["timeline"].get(k) != b["timeline"].get(k)},
        "tracks": {"before": [t for t in a.get("tracks", []) if t not in b.get("tracks", [])],
                   "after": [t for t in b.get("tracks", []) if t not in a.get("tracks", [])]},
    }
    return out
