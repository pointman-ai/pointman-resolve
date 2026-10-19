"""Edit plans compiled against a snapshot: ops -> groups of steps on a Model, with the problems that stop
it, a frame-map diff and the change in clutter. Offline: no Resolve calls, so a dry run can't change
anything, and apply compiles again on a fresh snapshot to check nothing moved since.

A plan:

    {"note": "re-sync C0030", "ops": [
      {"op": "slip",    "items": [id, ...], "by": -3, "follow": ["V3", "V5"]},
      {"op": "swap",    "a": A, "b": B, "c": C, "stay": ["A2"]},
      {"op": "move",    "items": [id, ...], "to": frame, "track": "V6"},
      {"op": "replace", "item": id, "file": path, "in": frame},
      {"op": "place",   "track": "V6", "file": path, "record": frame, "frames": n, "in": 0, "props": {...}},
      {"op": "set",     "items": [id, ...], "props": {...}, "fades": {...}, "color": "Orange", "enabled": true},
      {"op": "delete",  "items": [id, ...]},
      {"op": "join",    "items": [id, id, ...]},
      {"op": "cut",     "ranges": [[s, e], ...], "fill": {"video": path, "audio": path, "audio_mono": path}}
    ]}

Frames are timeline frames from the timeline's start; ids are GetUniqueId from a snapshot. An op that
touches the person's own tracks (named camera, screen or voice) needs "ok": what asked for it (a
marker, a post). Each op compiles to one group (a cut, one per range, last range first), and apply
runs a group as: remove, lay, put settings and grades back, ripple, transitions, markers, sets.
"""

import hashlib
import json
from pathlib import Path

from .model import Model, PlanError, clock, lane, same_frame

OWN_TRACKS = ("camera", "screen", "voice")
TRANSITIONS = {"video": ("Cross Dissolve",), "audio": ("Cross Fade +3 dB", "Cross Fade 0 dB", "Cross Fade -3 dB")}
PLACE = ("kind", "track", "record", "frames", "in", "src_frame", "file")


class Group:
    """One op's steps (a cut range's, for cut)."""

    def __init__(self, i: int, op: dict, model: Model):
        self.i, self.op, self.m = i, op, model
        self.remove: list[dict] = []
        self.lay: list[dict] = []
        self.set: list[dict] = []
        self.ripple: list | None = None
        self.transitions: list[dict] = []      # to put back after the lays
        self.dropped: list[dict] = []          # transitions taken with a clip and not put back
        self.markers: list[list] = []          # [from, to] timeline marker moves
        self.spans: dict[tuple, list] = {}     # lane -> [[s, e], ...] it means to change
        self.warnings: list[str] = []
        self.before = {k: list(v) for k, v in model.tracks.items()}
        self.end_before = model.track_end()

    def span(self, key: tuple, s: int, e: int) -> None:
        self.spans.setdefault(key, []).append([int(s), int(e)])

    def take(self, p: dict, *, relay: bool = True) -> None:
        """p comes off the timeline (to be laid again, if relay)."""
        m, key = self.m, self.m.key(p)
        if relay:
            why = []
            if not p.get("file") or p.get("type") not in ("video", "audio"):
                why.append("it isn't a media clip (a title, generator or compound clip)")
            if p.get("linked"):
                why.append("it's linked to another clip")
            if p.get("fusion"):
                why.append("it has a Fusion comp")
            pct = (p.get("speed") or {}).get("Percentage") if isinstance(p.get("speed"), dict) else None
            if pct not in (None, 100, 100.0):
                why.append(f"it's retimed ({pct}%)")
            if why:
                raise PlanError(f"{m.name(key)} {clock(p['record'], m.fps)} {p.get('name')}: can't re-lay it: "
                                + "; ".join(why))
        for t in m.transitions_at(key, p):
            if not any(x["id"] == t.get("id") for x in self.transitions + self.dropped):
                c = cut_under(m, key, t)
                self.transitions.append({"id": t.get("id"), "key": list(key), "name": t.get("name"),
                                         "frames": t["frames"], "record": t["record"], "cut": c,
                                         "left": left_of(m, key, c), "right": right_of(m, key, c)})
        self.remove.append(p)
        self.span(key, p["record"], p["record"] + p["frames"])
        m.remove(p)

    def put(self, p: dict, source: str | None, *, grade: bool = True, inside: bool = False) -> dict:
        """p goes onto the timeline (from `source`'s id, whose grade it gets if it has one)."""
        m = self.m
        if p.get("file") and p.get("media_fps") is not None:
            m.check_source(p)
        src = next((r for r in self.remove if r.get("id") == source), None)
        if src and src.get("markers"):
            # Resolve doesn't say whether a clip marker's frame counts from the clip's start or its source's, so
            # they're carried only where both give the same frame: same source start and length
            if m.src_first(p) != m.src_first(src) or p["frames"] != src["frames"]:
                raise PlanError(f"{m.name(m.key(src))} {clock(src['record'], m.fps)} {src.get('name')} has clip markers, "
                                "which are only carried when its source and length stay the same (a move, not a slip "
                                "or a split): move them to the timeline first")
            p = {**p, "markers": src["markers"]}
        elif source is not None:
            p = {**p, "markers": []}
        q = m.lay(p, self.i, source)
        q["grade_from"] = source if grade and src and (src.get("grade") or {}).get("graded") else None
        q["inside"] = inside
        self.lay.append(q)
        self.span(m.key(q), q["record"], q["record"] + q["frames"])
        return q

    def finish(self) -> None:
        """Transitions taken with a clip go back where the same two clips (or pieces of them) still meet."""
        m, keep = self.m, []
        for t in self.transitions:
            key = tuple(t["key"])
            found = rejoined(m, key, t)
            ok_name = t["name"] in TRANSITIONS[key[0]]
            centred = abs(t["record"] + t["frames"] / 2 - t["cut"]) <= 0.5 if t["cut"] is not None else False
            if found and ok_name and centred:
                left, right, c = found
                need = (t["frames"] + 1) // 2
                if m.src_first(right) < need or (left.get("media_frames") and
                                                m.src_first(left) + m.src_span(left) + need > left["media_frames"]):
                    raise PlanError(f"{m.name(key)} {clock(c, m.fps)}: the {t['name']} needs {need} frames of handle "
                                    "on both clips, and one doesn't have them")
                keep.append({**t, "cut": c, "left": left["id"], "right": right["id"]})
                continue
            why = ("its two clips no longer meet" if not found else
                   f"only centred {' / '.join(TRANSITIONS[key[0]])} can be put back" if not centred or not ok_name else "")
            if self.op.get("drop_transitions"):
                self.dropped.append(t)
                self.warnings.append(f"{m.name(key)} {clock(t['record'], m.fps)}: the {t['name']} is dropped ({why})")
            else:
                raise PlanError(f"{m.name(key)} {clock(t['record'], m.fps)}: the {t['name']} would be lost ({why}); "
                                "add \"drop_transitions\": true to drop it")
        self.transitions = keep
        m.transitions_put(keep)

    def as_dict(self) -> dict:
        keys = sorted({*self.spans}, key=lambda k: (k[0] != "video", k[1]))
        return {"op": self.i, "kind": self.op["op"], "lanes": [lane(*k) for k in keys],
                "remove": [brief(p) for p in self.remove], "lay": [brief(p, full=True) for p in self.lay],
                "set": self.set, "ripple": self.ripple, "transitions": self.transitions,
                "dropped": [{k: t[k] for k in ("key", "name", "record", "frames")} for t in self.dropped],
                "markers": self.markers, "spans": {lane(*k): v for k, v in self.spans.items()},
                "end_before": self.end_before, "end_after": self.m.track_end()}


def brief(p: dict, *, full: bool = False) -> dict:
    keys = PLACE + ("id", "name", "media_fps", "media_frames", "src_start") + (
        ("props", "fades", "color", "enabled", "markers", "from", "grade_from", "inside") if full else ("grade",))
    return {k: p.get(k) for k in keys if k in p}


# ---------------------------------------------------------------- transitions


def cut_under(m: Model, key: tuple, t: dict) -> int | None:
    """The cut a transition sits on: where one clip ends and the next starts, inside it."""
    a, b = t["record"], t["record"] + t["frames"]
    ends = {p["record"] + p["frames"] for p in m.tracks.get(key, [])}
    starts = {p["record"] for p in m.tracks.get(key, [])}
    both = sorted(x for x in ends & starts if a <= x <= b)
    return both[0] if both else None


def left_of(m: Model, key: tuple, c) -> str | None:
    return next((p.get("id") for p in m.tracks.get(key, []) if p["record"] + p["frames"] == c), None)


def right_of(m: Model, key: tuple, c) -> str | None:
    return next((p.get("id") for p in m.tracks.get(key, []) if p["record"] == c), None)


def rejoined(m: Model, key: tuple, t: dict):
    """(left, right, cut) where the clips either side of the transition (or pieces of them) meet now."""
    lefts = [t["left"]] + m.descendants(t["left"] or "")
    rights = [t["right"]] + m.descendants(t["right"] or "")
    pieces = m.tracks.get(key, [])
    for left in pieces:
        if left.get("id") not in lefts:
            continue
        c = left["record"] + left["frames"]
        right = next((p for p in pieces if p["record"] == c and p.get("id") in rights), None)
        if right:
            return left, right, c
    return None


def _transitions_put(self: Model, kept: list[dict]) -> None:
    for t in kept:
        key = tuple(t["key"])
        self.transitions.setdefault(key, []).append(
            {"id": t.get("id"), "kind": key[0], "track": key[1], "type": "transition", "name": t["name"],
             "record": t["cut"] - t["frames"] // 2, "frames": t["frames"]})


Model.transitions_put = _transitions_put


# ---------------------------------------------------------------- the ops


def piece_of(m: Model, op: dict, p: dict, x: int, y: int) -> dict:
    """The part of p on [x, y): its source moved along, the fades kept only at p's own ends."""
    q = m.moved_source(p, x - p["record"])
    q["record"], q["frames"] = x, y - x
    fades = dict(p.get("fades") or {})
    if fades:
        if x != p["record"]:
            fades["FadeIn"] = 0.0
        if y != p["record"] + p["frames"]:
            fades["FadeOut"] = 0.0
        q["fades"] = fades
    return q


def items(m: Model, op: dict, field: str = "items") -> list[dict]:
    ids = op.get(field)
    if isinstance(ids, str):
        ids = [ids]
    if not ids:
        raise PlanError(f"needs {field!r}: item ids from a snapshot")
    out = []
    for pid in ids:
        p = m.piece(str(pid))
        if p not in out:
            out.append(p)
    return out


def op_slip(m: Model, op: dict, g: Group) -> None:
    by = int(op.get("by") or 0)
    if not by:
        raise PlanError("needs \"by\": the frames to slip, + later in the source, - earlier")
    targets = items(m, op)
    follow = [m.lane_of(x) for x in op.get("follow") or []]
    todo = list(targets)
    for t in targets:
        for key in follow:
            for p in m.overlapping(key, t["record"], t["record"] + t["frames"]):
                if t["record"] <= p["record"] and p["record"] + p["frames"] <= t["record"] + t["frames"] and p not in todo:
                    todo.append(p)
    for p in todo:
        g.take(p)
        g.put(m.moved_source(p, by), p.get("id"))


def op_swap(m: Model, op: dict, g: Group) -> None:
    a, b, c = (int(op[k]) for k in ("a", "b", "c"))
    if not a < b < c:
        raise PlanError("needs a < b < c: [a, b) swaps places with [b, c)")
    stay = {m.lane_of(x) for x in op.get("stay") or []}
    later, earlier = c - b, -(b - a)
    moves = []
    for key in m.lanes():
        if key in stay:
            continue
        for p in list(m.overlapping(key, a, c)):
            x, y = p["record"], p["record"] + p["frames"]
            if x < a < y or x < b < y:
                raise PlanError(f"{m.name(key)} {p.get('name')} at {clock(x, m.fps)} crosses {'a' if x < a < y else 'b'} "
                                f"({clock(a if x < a < y else b, m.fps)}): move the range, or trim the clip first")
            if y <= b:
                moves.append((p, [(x, y, later)]))
            elif y <= c:
                moves.append((p, [(x, y, earlier)]))
            else:                                       # crosses c: the part before c moves, the rest stays
                moves.append((p, [(x, c, earlier), (c, y, 0)]))
    for p, _ in moves:
        g.take(p)
    for p, parts in moves:
        for x, y, d in parts:
            q = piece_of(m, op, p, x, y) if (x, y) != (p["record"], p["record"] + p["frames"]) else dict(p)
            q["record"] = x + d
            g.put(q, p.get("id"))
    for f in sorted(m.markers):
        if a <= f < c:
            g.markers.append([f, f + (later if f < b else earlier)])
    moved = {f: m.markers.pop(f) for f, _ in g.markers}
    for f, to in g.markers:
        m.markers[to] = moved[f]
    for key in m.lanes():
        if key not in stay:
            g.span(key, a, c)


def op_move(m: Model, op: dict, g: Group) -> None:
    ps = items(m, op)
    lo = min(p["record"] for p in ps)
    shift = int(op.get("to", lo)) - lo
    key2 = m.lane_of(op["track"]) if op.get("track") else None
    if key2 and any(p["kind"] != key2[0] for p in ps):
        raise PlanError(f"can't move {ps[0]['kind']} onto {lane(*key2)}")
    if not shift and not key2:
        raise PlanError("needs \"to\" (a frame) or \"track\"")
    for p in ps:
        g.take(p)
    for p in ps:
        q = dict(p)
        q["record"] = p["record"] + shift
        if key2:
            q["kind"], q["track"] = key2
        g.put(q, p.get("id"))


def media_info(ctx: dict, path: str) -> dict:
    info = (ctx.get("media") or {}).get(path)
    if info is None and not Path(path).exists():
        raise PlanError(f"no file {path}")
    return info or {}


def op_replace(m: Model, op: dict, g: Group) -> None:
    p = items(m, op, "item")[0]
    path = str(op.get("file") or p.get("file"))
    info = media_info(g.ctx, path)
    q = dict(p)
    q.update({"file": path, "name": Path(path).name, "media_fps": info.get("fps", p.get("media_fps")),
              "media_frames": info.get("frames"), "src_start": None})
    q["in"] = int(op.get("in", p.get("in") or 0))
    q["src_frame"] = round(q["in"] * m.rate(q) / m.fps) if m.fast(q) else q["in"]
    if path == p.get("file") and q["in"] == p.get("in"):
        raise PlanError("replace with the same file and in-point changes nothing")
    if info.get("fps") is None and path != p.get("file"):
        g.warnings.append(f"{Path(path).name}: not in Resolve's media pool yet, so its rate and length are checked at apply")
    g.take(p)
    g.put(q, p.get("id"), grade=op.get("keep_grade", True))


def op_place(m: Model, op: dict, g: Group) -> None:
    key = m.lane_of(op.get("track") or "")
    path = str(op.get("file") or "")
    info = media_info(g.ctx, path)
    record, frames, first = int(op["record"]), int(op["frames"]), int(op.get("in") or 0)
    q = {"kind": key[0], "track": key[1], "record": record, "frames": frames, "in": first, "file": path,
         "name": Path(path).name, "type": key[0], "media_fps": info.get("fps"), "media_frames": info.get("frames"),
         "props": dict(op.get("props") or {}), "fades": dict(op.get("fades") or {}),
         "color": op.get("color") or "", "enabled": op.get("enabled", True), "grade": None}
    q["src_frame"] = round(first * m.rate(q) / m.fps) if m.fast(q) else first
    g.put(q, None)


def op_set(m: Model, op: dict, g: Group) -> None:
    change = {k: op[k] for k in ("props", "fades", "color", "enabled") if k in op}
    if not change:
        raise PlanError("needs props, fades, color or enabled")
    for p in items(m, op):
        unknown = [k for k in (change.get("props") or {}) if p.get("props") and k not in p["props"]]
        if unknown:
            g.warnings.append(f"{m.name(m.key(p))} {p.get('name')}: Resolve didn't list {', '.join(unknown)} for it")
        g.set.append({"id": p.get("id"), "key": list(m.key(p)), "record": p["record"], **change})
        q = dict(p)
        for k, v in change.items():
            q[k] = {**(p.get(k) or {}), **v} if isinstance(v, dict) else v
        items_ = m.tracks[m.key(p)]
        items_[items_.index(p)] = q
        g.span(m.key(p), p["record"], p["record"] + p["frames"])


def op_delete(m: Model, op: dict, g: Group) -> None:
    for p in items(m, op):
        g.take(p, relay=False)
    for t in g.transitions:
        g.dropped.append(t)
        g.warnings.append(f"{lane(*t['key'])} {clock(t['record'], m.fps)}: the {t['name']} goes with the clip")
    g.transitions = []


def op_join(m: Model, op: dict, g: Group) -> None:
    ps = sorted(items(m, op), key=lambda p: p["record"])
    if len(ps) < 2:
        raise PlanError("join needs two or more pieces")
    key = m.key(ps[0])
    for a, b in zip(ps, ps[1:]):
        why = None
        if m.key(b) != key:
            why = "they're on different tracks"
        elif a["record"] + a["frames"] != b["record"]:
            why = f"there's a gap or overlap at {clock(b['record'], m.fps)}"
        elif a.get("file") != b.get("file"):
            why = "they're different files"
        elif abs(m.source_time(b, b["record"]) - m.source_time(a, b["record"])) * m.rate(a) >= 0.999:
            why = f"the source jumps at {clock(b['record'], m.fps)}"
        elif (a.get("props"), a.get("color"), a.get("enabled")) != (b.get("props"), b.get("color"), b.get("enabled")):
            why = f"their settings differ at {clock(b['record'], m.fps)}"
        elif (a.get("grade") or {}).get("tools") != (b.get("grade") or {}).get("tools"):
            why = f"their grades differ at {clock(b['record'], m.fps)}"
        elif m.transitions_at(key, b):
            why = f"there's a transition at {clock(b['record'], m.fps)}"
        if why:
            raise PlanError(f"can't join {m.name(key)} {a.get('name')}: {why}")
    marked = [p for p in ps[1:] if p.get("markers")]
    if marked or ps[0].get("markers"):
        raise PlanError(f"{m.name(key)} {(marked or ps)[0].get('name')} has clip markers, which a join can't carry yet "
                        "(their frame reference is unmeasured): move them to the timeline first")
    for p in ps:
        g.take(p)
    q = dict(ps[0])
    q["frames"] = sum(p["frames"] for p in ps)
    fades = dict(ps[0].get("fades") or {})
    if fades or ps[-1].get("fades"):
        fades["FadeOut"] = (ps[-1].get("fades") or {}).get("FadeOut", 0.0)
        q["fades"] = fades
    g.put(q, ps[0].get("id"))


def op_cut(m: Model, op: dict, groups: list[Group], i: int, ctx: dict) -> None:
    ranges = sorted({(int(s), int(e)) for s, e in op.get("ranges") or []}, reverse=True)
    if not ranges or any(s >= e for s, e in ranges):
        raise PlanError("needs \"ranges\": [[start, end], ...], each start before its end")
    for (s1, e1), (s2, e2) in zip(ranges, ranges[1:]):
        if e2 > s1:
            raise PlanError(f"ranges overlap at {clock(s1, m.fps)}")
    fill = op.get("fill") or {}
    first = min(s for s, _ in ranges)
    relay = sum(1 for key in m.lanes() for p in m.tracks[key] if p["record"] + p["frames"] > first)
    limit = int(op.get("max_relay", 400))
    if relay > limit:
        raise PlanError(f"undoing this cut would re-lay {relay} clips on every track from {clock(first, m.fps)} on "
                        f"(Resolve can't ripple-insert), the person's own included: add \"max_relay\": {relay} to "
                        "allow it")
    for s, e in ranges:                                   # the last first, so the earlier ones keep their place
        g = Group(i, op, m)
        g.ctx = ctx
        for key in m.lanes():
            for t in m.transitions.get(key, []):
                if t["record"] < e and t["record"] + t["frames"] > s:
                    raise PlanError(f"{m.name(key)}: a {t.get('name')} at {clock(t['record'], m.fps)} is in or on the edge "
                                    "of the cut: remove it first")
            for p in list(m.overlapping(key, s, e)):
                x, y = p["record"], p["record"] + p["frames"]
                if s <= x and y <= e:
                    continue                              # wholly inside: the ripple takes it
                g.take(p)
                for a, b in ((x, s), (max(x, s), min(y, e)), (e, y)):
                    if b > a and (a, b) != (x, x):
                        g.put(piece_of(m, op, p, a, b), p.get("id"), inside=(s <= a and b <= e))
            if any(p["record"] >= e for p in m.tracks[key]):  # this lane moves: [s, e) must be all clip
                at, cover = s, sorted(m.overlapping(key, s, e), key=lambda p: p["record"])
                for p in cover + [None]:
                    nxt = p["record"] if p else e
                    if nxt > at:
                        which = "video" if key[0] == "video" else (
                            "audio_mono" if m.subtypes.get(key) == "mono" else "audio")
                        path = fill.get(which) or (fill.get("audio") if which == "audio_mono" else None)
                        if not path:
                            raise PlanError(f"{m.name(key)} has nothing in {clock(at, m.fps)}–{clock(nxt, m.fps)}: a cut "
                                            f"needs \"fill\": {{\"{which}\": <a black or silent file>}} to close the gap there")
                        info = media_info(ctx, path)
                        g.put({"kind": key[0], "track": key[1], "record": at, "frames": nxt - at, "in": 0, "src_frame": 0,
                               "file": path, "name": Path(path).name, "type": key[0], "media_fps": info.get("fps"),
                               "media_frames": info.get("frames"), "props": {}, "fades": {}, "color": "", "enabled": True,
                               "grade": None}, None, inside=True)
                    if p:
                        at = max(at, p["record"] + p["frames"])
        inside = [p.get("id") for key in m.lanes() for p in m.overlapping(key, s, e)]
        g.ripple = {"range": [s, e], "inside": inside, "markers_inside": [f for f in m.markers if s <= f < e]}
        if g.ripple["markers_inside"]:
            g.warnings.append(f"{len(g.ripple['markers_inside'])} timeline markers are inside {clock(s, m.fps)}–"
                              f"{clock(e, m.fps)} and go with it")
        g.finish()
        m.ripple(s, e)
        for key in m.lanes():
            g.span(key, s, m.end + (e - s))
        groups.append(g)


OPS = {"slip": op_slip, "swap": op_swap, "move": op_move, "replace": op_replace, "place": op_place,
       "set": op_set, "delete": op_delete, "join": op_join}


# ---------------------------------------------------------------- the whole plan


def compile_plan(snap: dict, plan: dict, *, media: dict | None = None, own=OWN_TRACKS, upto: int | None = None) -> dict:
    """The plan's groups, problems and warnings, its frame-map diff and clutter, and a digest that
    apply checks again. `media`: path -> {fps, frames} for files that aren't on the timeline yet.
    `upto`: stop after that many groups (undo rebuilds the state a stopped apply left)."""
    m = Model(snap)
    ctx = {"media": media or {}}
    groups: list[Group] = []
    problems: list[str] = []
    ops = plan.get("ops") or []
    if not ops:
        problems.append("the plan has no ops")
    for i, op in enumerate(ops):
        if upto is not None and len(groups) >= upto:
            break
        kind = (op or {}).get("op")
        try:
            if kind == "cut":
                own_lanes = [k for k in m.lanes() if (m.names.get(k) or "").lower() in own]
                if own_lanes and not op.get("ok"):
                    raise PlanError(f"a cut changes {', '.join(m.name(k) for k in own_lanes)}: say what asked for it "
                                    "in \"ok\" (a marker or a post)")
                op_cut(m, op, groups, i, ctx)
                continue
            if kind not in OPS:
                raise PlanError(f"no op {kind!r} (one of {', '.join(sorted([*OPS, 'cut']))})")
            g = Group(i, op, m)
            g.ctx = ctx
            OPS[kind](m, op, g)
            g.finish()
            own_hit = [k for k in g.spans if (m.names.get(k) or "").lower() in own]
            if own_hit and not op.get("ok"):
                raise PlanError(f"it changes {', '.join(m.name(k) for k in own_hit)}, the person's own: say what asked "
                                "for it in \"ok\" (a marker or a post)")
            groups.append(g)
        except PlanError as e:
            problems.append(f"op {i + 1} ({kind}): {e}")
            break                                         # later ops were planned on this one's result
        except (KeyError, TypeError, ValueError) as e:
            problems.append(f"op {i + 1} ({kind}): {type(e).__name__}: {e}")
            break
    out = [g.as_dict() for g in groups]
    diff = frame_diff(groups, m)
    for d in diff:
        if d["outside"]:
            problems.append(f"op {d['op'] + 1}: changes {d['outside']} frames of {d['lane']} outside what it means to "
                            "change (a bug in the plan or the placer: not applied)")
    warnings = [w for g in groups for w in g.warnings]
    body = json.dumps([{k: g[k] for k in ("op", "remove", "lay", "set", "ripple", "transitions", "markers")} for g in out],
                      sort_keys=True, default=str)
    lays = sum(len(g["lay"]) for g in out)
    return {"groups": out, "problems": problems, "warnings": warnings, "frames": diff,
            "clutter": clutter(groups, m), "digest": hashlib.sha256(body.encode()).hexdigest()[:16],
            "counts": {"ops": len(ops), "groups": len(out), "remove": sum(len(g["remove"]) for g in out), "lay": lays,
                       "set": sum(len(g["set"]) for g in out), "ripples": sum(1 for g in out if g["ripple"]),
                       "transitions": sum(len(g["transitions"]) for g in out),
                       "graded": sum(1 for g in out for p in g["lay"] if p.get("grade_from"))},
            "model": m}


def frame_diff(groups: list[Group], end: Model) -> list[dict]:
    """Per group and lane: frames whose picture or sound changes, inside and outside its spans. A ripple
    is compared shifted: after it, frame f shows what f + d showed before."""
    out = []
    for gi, g in enumerate(groups):
        after = groups[gi + 1].before if gi + 1 < len(groups) else end.tracks
        for key, spans in g.spans.items():
            lo = min(s for s, _ in spans)
            hi = max(e for _, e in spans)
            if g.ripple:
                s, e = g.ripple["range"]
                d = e - s
                hi = min(hi, s + 25 * 60)               # a minute after the cut is enough to see the shift
                was = view(end, g.before.get(key, []), lo, hi + d)
                now = view(end, after.get(key, []), lo, hi)
                changed = sum(1 for f in range(lo, hi) if not same_frame(now[f - lo], was[(f if f < s else f + d) - lo]))
                out.append({"op": g.i, "lane": lane(*key), "changed": changed, "outside": 0, "ripple": d})
                continue
            was = view(end, g.before.get(key, []), lo, hi)
            now = view(end, after.get(key, []), lo, hi)
            inside = changed = 0
            for f in range(lo, hi):
                if not same_frame(now[f - lo], was[f - lo]):
                    changed += 1
                    inside += any(s <= f < e for s, e in spans)
            out.append({"op": g.i, "lane": lane(*key), "changed": changed, "outside": changed - inside})
    return out


def view(m: Model, pieces: list[dict], s: int, e: int) -> list:
    saved = m.tracks
    try:
        m.tracks = {("_",): pieces}
        return m.frame_map(("_",), s, e)
    finally:
        m.tracks = saved


def clutter(groups: list[Group], m: Model) -> dict:
    """Clips and through-edits on each lane the plan touches, before and after."""
    keys = {k for g in groups for k in g.spans}
    if not groups:
        return {}
    first = groups[0].before
    out = {}
    for key in sorted(keys, key=lambda k: (k[0] != "video", k[1])):
        saved = m.tracks
        try:
            m.tracks = {**saved, key: first.get(key, [])}
            before = (len(first.get(key, [])), m.through_edits(key))
        finally:
            m.tracks = saved
        out[lane(*key)] = {"clips": [before[0], len(m.tracks.get(key, []))],
                           "through_edits": [before[1], m.through_edits(key)]}
    return out
