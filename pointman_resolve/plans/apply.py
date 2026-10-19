"""A compiled plan applied to Resolve, checked as it goes, and undone (PFE-11).

Everything goes through PlanWriter: reads, plus the placer's few writes (AppendToTimeline, DeleteClips,
SetProperties, SetFades, SetClipColor, SetClipEnabled, CopyGrades, AddTransition and timeline markers).
It can't save, load, switch or rename a timeline, add or delete a track, move the playhead, import, or
start a background job.

A group runs as:
  1. check: every clip it removes is where the plan says, every spot it lays on is free, every file is
     in the media pool, and the timeline's length is what the plan expects (James may be editing);
  2. a test clip, laid past the timeline's end, shows Resolve is taking clips. For a graded clip that
     test clip is its stand-in: CopyGrades puts the grade on it before the clip goes. Nothing is
     removed until Resolve has taken one;
  3. DeleteClips, then one AppendToTimeline, each new clip read back from its track: place, length and
     source (GetLeftOffset, or GetSourceStartFrame for media at another rate). An append onto a taken
     spot returns a clip and places nothing, and a busy Resolve returns nothing: both are caught, retried,
     then stopped on;
  4. settings back (props that differ, fades, colour, enabled), grades from the stand-ins, stand-ins gone;
  5. a cut's ripple delete, every later clip checked to have moved by exactly its length;
  6. transitions and timeline markers back where the plan puts them, then the plan's own sets.
The record (applied.json) is written after every step, so a stop leaves an exact account, and undo works
from it: every clip the plan placed or moved goes, the snapshot's clips come back, grades copied across.
"""

import json
import math
import time
from pathlib import Path

from ..timeline import grades as grade_records
from ..timeline import journal
from .compile import compile_plan
from .model import Model, clock, lane

WRITES = ("AppendToTimeline", "DeleteClips", "SetProperties", "SetFades", "SetClipColor", "SetClipEnabled",
          "CopyGrades", "AddTransition", "AddMarker", "DeleteMarkerAtFrame")
SKIP_PROPS = {"ZoomGang"}
PLAIN = (str, int, float, bool, bytes, dict, type(None))


class Stop(RuntimeError):
    """Applying stopped: why, in words. What was done is in the record, and undo puts it back."""


class Busy(Stop):
    """James is working in Resolve (the timeline or the playhead moved): apply again to carry on."""


class PlanWriter:
    """A Resolve object that reads, and makes only the placer's writes. Objects it returns are wrapped too,
    and objects passed back into Resolve are unwrapped."""

    __slots__ = ("_obj",)

    def __init__(self, obj):
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        target = object.__getattribute__(self, "_obj")
        if name.isupper():
            return getattr(target, name)
        if not (name.startswith(("Get", "Is")) or name in WRITES):
            raise PermissionError(f"edit plans don't call {name}()")
        fn = getattr(target, name)

        def call(*args):
            return wrap(fn(*[unwrap(a) for a in args]))
        return call

    def __setattr__(self, name, value):
        raise PermissionError("edit plans don't set attributes on Resolve objects")

    def __bool__(self):
        return bool(object.__getattribute__(self, "_obj"))

    def __eq__(self, other):
        return unwrap(self) is unwrap(other) or unwrap(self) == unwrap(other)

    def __hash__(self):
        return id(unwrap(self))


def wrap(v):
    if isinstance(v, PLAIN):
        return v
    if isinstance(v, (list, tuple)):
        return [wrap(x) for x in v]
    return PlanWriter(v)


def unwrap(v):
    if isinstance(v, PlanWriter):
        return object.__getattribute__(v, "_obj")
    if isinstance(v, list):
        return [unwrap(x) for x in v]
    if isinstance(v, dict):
        return {k: unwrap(x) for k, x in v.items()}
    return v


# ---------------------------------------------------------------- the record


class Record:
    """applied.json in the plan's folder: what's been done, written after every step."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"state": "new", "groups_done": 0, "ids": {}, "partial": None, "log": [], "warnings": []}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, default=str), encoding="utf-8")
        tmp.replace(self.path)

    def log(self, text: str) -> None:
        self.data["log"].append(f"{time.strftime('%H:%M:%S')} {text}")
        self.save()


# ---------------------------------------------------------------- the placer


class Runner:
    def __init__(self, resolve, record: Record, *, sleep=time.sleep, retries: int = 3, wait: float = 2.0):
        self.w = PlanWriter(resolve)
        pm = self.w.GetProjectManager()
        self.project = pm.GetCurrentProject()
        self.tl = self.project.GetCurrentTimeline() if self.project else None
        if not self.tl:
            raise Stop("no timeline open in Resolve")
        self.pool = self.project.GetMediaPool()
        self.t0 = int(self.tl.GetStartFrame())
        self.rec, self.sleep, self.retries, self.wait = record, sleep, retries, wait
        self.ids: dict[str, str] = record.data.setdefault("ids", {})
        self.media_items: dict[str, object] | None = None
        self.playhead = self.tl.GetCurrentTimecode()

    # -- reading Resolve

    def real(self, pid: str) -> str:
        return self.ids.get(pid, pid)

    def items(self, key) -> list:
        return sorted(self.tl.GetItemListInTrack(key[0], key[1]) or [], key=lambda i: i.GetStart())

    def by_id(self, key) -> dict:
        return {i.GetUniqueId(): i for i in self.items(key)}

    def media(self, path: str):
        if self.media_items is None:
            self.media_items = {}
            stack = [self.pool.GetRootFolder()]
            while stack:
                folder = stack.pop()
                for c in folder.GetClipList() or []:
                    p = c.GetClipProperty("File Path")
                    if p:
                        self.media_items.setdefault(p, c)
                stack += folder.GetSubFolderList() or []
        mp = self.media_items.get(path)
        if mp is None or mp.GetClipProperty("File Path") != path:
            self.media_items = None                      # gone from the pool since (it happens): look again once
            if not getattr(self, "_looked_again", False):
                self._looked_again = True
                return self.media(path)
            raise Stop(f"{Path(path).name} isn't in Resolve's media pool: import it, then apply again")
        self._looked_again = False
        return mp

    def matches(self, it, p: dict, m: Model) -> bool:
        if it.GetStart() - self.t0 != p["record"] or it.GetDuration() != p["frames"]:
            return False
        if m.fast(p):
            return abs((it.GetSourceStartFrame() or 0) - m.src_first(p)) <= 1
        return it.GetLeftOffset() == p.get("in")

    def where(self, it) -> str:
        return f"{clock(it.GetStart() - self.t0, 25)}"

    # -- guards

    def guard(self, end_expected: int | None = None) -> None:
        """Stops while the playhead is moving (playback, scrubbing): a jump to a new spot is fine, since every
        clip is checked before it's touched. `end_expected`: the timeline's length the plan was made on."""
        now = self.tl.GetCurrentTimecode()
        if now != self.playhead:
            self.sleep(self.wait)
            again = self.tl.GetCurrentTimecode()
            if again != now:
                raise Busy(f"the playhead is moving ({now} → {again}): James is working in Resolve. Apply again to "
                           "carry on")
            self.rec.log(f"the playhead is at {again} now (was {self.playhead})")
            self.playhead = again
        if end_expected is not None and self.tl.GetEndFrame() - self.t0 != end_expected:
            raise Busy(f"the timeline is {self.tl.GetEndFrame() - self.t0} frames long, not {end_expected}: it changed "
                       "since the plan was made. Plan again")

    # -- writing

    def append(self, infos: list[dict]) -> list:
        for attempt in range(self.retries + 1):
            got = [x for x in (self.pool.AppendToTimeline(infos) or []) if x]
            if got:
                return got
            if attempt < self.retries:
                self.rec.log(f"Resolve placed nothing ({len(infos)} clips): waiting {self.wait:.0f} s")
                self.sleep(self.wait)
        return []

    def info(self, p: dict, m: Model, *, record: int | None = None, frames: int | None = None) -> dict:
        q = dict(p)
        if frames is not None:
            q["frames"] = frames
        first = m.src_first(q)
        return {"mediaPoolItem": self.media(q["file"]), "startFrame": first, "endFrame": first + m.src_span(q),
                "mediaType": 1 if q["kind"] == "video" else 2, "trackIndex": q["track"],
                "recordFrame": self.t0 + (q["record"] if record is None else record)}

    def park(self, lays: list[dict], sources: dict, m: Model) -> dict:
        """Test clips past the end: one per graded source (its stand-in, with its grade copied on), or
        one plain one. Returns {source id: stand-in}. Nothing has been removed yet if this fails."""
        far = self.tl.GetEndFrame() - self.t0 + 250
        stands, n = {}, 0
        need = [pid for pid in dict.fromkeys(q.get("grade_from") for q in lays) if pid]
        tests = [(pid, sources[pid][1]) for pid in need] or ([(None, lays[0])] if lays else [])
        for pid, p in tests:
            at = far + 10 * n
            n += 1
            got = self.append([self.info(p, m, record=at, frames=1)])
            placed = [g for g in got if g.GetStart() - self.t0 == at]
            if not placed:
                for g in got:
                    self.tl.DeleteClips([g], False)
                self.cleanup(stands)
                raise Stop("Resolve isn't taking clips right now (busy, or a dialog is open): nothing was changed "
                           "in this step. Apply again to carry on")
            if pid:
                if not sources[pid][0].CopyGrades([placed[0]]):
                    self.cleanup({**stands, "_": placed[0]})
                    raise Stop(f"Resolve wouldn't copy the grade of {sources[pid][1].get('name')}: nothing was changed")
                stands[pid] = placed[0]
            else:
                self.tl.DeleteClips(placed, False)
        return stands

    def cleanup(self, stands: dict) -> None:
        if stands:
            self.tl.DeleteClips(list(stands.values()), False)

    def restore(self, it, p: dict) -> list[str]:
        """The piece's settings back on a clip that was just laid; what Resolve refused."""
        bad = []
        want = {k: v for k, v in (p.get("props") or {}).items() if k not in SKIP_PROPS and v is not None}
        have = it.GetProperties() or {}
        diff = {k: v for k, v in want.items() if have.get(k) != v}
        if diff and not it.SetProperties(diff):
            bad.append(f"props {sorted(diff)}")
        fades = {k: float(v or 0) for k, v in (p.get("fades") or {}).items() if k in ("FadeIn", "FadeOut")}
        now = it.GetFades() or {}
        if fades and any(float(now.get(k) or 0) != v for k, v in fades.items()):
            if not it.SetFades({"FadeIn": fades.get("FadeIn", float(now.get("FadeIn") or 0)),
                                "FadeOut": fades.get("FadeOut", float(now.get("FadeOut") or 0))}):
                bad.append("fades")
        if p.get("color") and it.GetClipColor() != p["color"] and not it.SetClipColor(p["color"]):
            bad.append("colour")
        if p.get("enabled") is not None and it.GetClipEnabled() != p["enabled"] and not it.SetClipEnabled(p["enabled"]):
            bad.append("enabled" if p["enabled"] else "disabled")
        have_marks = {int(f) for f in (it.GetMarkers() or {})} if p.get("markers") else set()
        for mk in p.get("markers") or []:
            if int(mk["frame"]) not in have_marks and not it.AddMarker(
                    int(mk["frame"]), mk.get("color") or "Blue", mk.get("name") or "", mk.get("note") or "",
                    int(mk.get("duration") or 1), mk.get("custom") or ""):
                bad.append(f"clip marker {mk.get('name')!r}")
        return bad

    def run_group(self, g: dict, m_after: Model, m_before: Model) -> None:
        """One group of steps. The models are for units and sources (fps, which files are 60 fps) and the
        list of lanes: every piece the group touches is in the group itself."""
        kinds = {(p["kind"], p["track"]) for p in g["remove"] + g["lay"]}
        live = {k: self.by_id(k) for k in kinds}
        self.guard(g.get("end_before") if g.get("first") else None)
        # 1. what goes is where the plan says; where things go is free; the files are in the pool
        going, sources = [], {}
        for p in g["remove"]:
            key = (p["kind"], p["track"])
            it = live[key].get(self.real(p["id"]))
            if it is None or not self.matches(it, p, m_before):
                raise Busy(f"{lane(*key)} {p.get('name')} at {clock(p['record'], m_before.fps)} isn't where the plan "
                           "has it any more: take a fresh snapshot and plan again")
            going.append(it)
            sources[p["id"]] = (it, p)
        gone = {i.GetUniqueId() for i in going}
        edges = {((p["kind"], p["track"]), x) for p in g["remove"] for x in (p["record"], p["record"] + p["frames"])}
        for q in g["lay"]:
            key = (q["kind"], q["track"])
            for it in live[key].values():
                a = it.GetStart() - self.t0
                if not (a < q["record"] + q["frames"] and q["record"] < a + it.GetDuration()) or it.GetUniqueId() in gone:
                    continue
                if it.GetType() == "transition" and any(k == key and a <= x <= a + it.GetDuration() for k, x in edges):
                    continue                            # goes with the clip it's on
                raise Busy(f"{lane(*key)} {clock(q['record'], m_after.fps)} is taken by {it.GetName()}: plan again")
            self.media(q["file"])
        # 2. Resolve is taking clips (and the grades are safe on stand-ins)
        stands = self.park(g["lay"], sources, m_after) if g["lay"] else {}
        self.rec.data["partial"] = {"group": g["index"], "removed": [], "laid": []}
        try:
            # 3. out with the old, in with the new
            if going:
                if not self.tl.DeleteClips(going, False):
                    raise Stop("Resolve wouldn't remove the clips: nothing was changed in this step")
                self.rec.data["partial"]["removed"] = [p["id"] for p in g["remove"]]
                self.rec.save()
            if g["lay"]:
                self.lay(g, m_after, stands)
        finally:
            self.cleanup(stands)
        # 4. a cut's ripple
        if g.get("ripple"):
            self.ripple(g, m_before)
        # 5. transitions, markers, sets
        for t in g["transitions"]:
            key = tuple(t["key"])
            left = self.by_id(key).get(self.real(t["left"]))
            ok = left and left.AddTransition({"type": t["name"], "category": "simple" if key[0] == "video" else "audio",
                                              "position": "end", "alignment": "center", "duration": int(t["frames"])})
            if not ok:
                self.rec.data["warnings"].append(f"{lane(*key)} {clock(t['cut'], m_after.fps)}: Resolve wouldn't put the "
                                                 f"{t['name']} back: add it by hand")
        self.move_markers(g["markers"])
        for s in g["set"]:
            it = self.by_id(tuple(s["key"])).get(self.real(s["id"]))
            if it is None:
                raise Busy(f"{s['key']} {s['id']} isn't there any more")
            bad = self.restore(it, {"props": s.get("props"), "fades": s.get("fades"), "color": s.get("color"),
                                    "enabled": s.get("enabled")})
            if bad:
                self.rec.data["warnings"].append(f"{lane(*s['key'])} {it.GetName()}: Resolve wouldn't set {', '.join(bad)}")
        self.rec.data["partial"] = None
        journal.record("resolve.apply", removed=[self.real(p["id"]) for p in g["remove"]],
                       added=[self.ids[q["id"]] for q in g["lay"] if q["id"] in self.ids],
                       timeline=self.tl.GetUniqueId(), note=f"{self.rec.data.get('plan')} op {g['op'] + 1} ({g['kind']})")

    def lay(self, g: dict, m: Model, stands: dict) -> None:
        todo = list(g["lay"])
        for attempt in range(2):
            infos = [self.info(q, m) for q in todo]
            got = self.append(infos)
            if not got:
                raise Stop(f"removed {len(g['remove'])} clips, but Resolve placed none of the {len(todo)} new ones "
                           "(busy?): resolve.undo puts them back")
            for q in todo:
                key = (q["kind"], q["track"])
                it = next((i for i in self.items(key) if self.matches(i, q, m)), None)
                if it is not None:
                    self.ids[q["id"]] = it.GetUniqueId()
                    self.rec.data["partial"]["laid"].append(q["id"])
            self.rec.save()
            todo = [q for q in todo if q["id"] not in self.ids]
            if not todo:
                break
            # placed somewhere else, or not at all: take away strays before trying the rest again
            for g_it in got:
                if g_it.GetUniqueId() not in self.ids.values():
                    self.tl.DeleteClips([g_it], False)
            self.sleep(self.wait)
        if todo:
            q = todo[0]
            raise Stop(f"{len(todo)} clips didn't land where the plan puts them, e.g. {lane(q['kind'], q['track'])} "
                       f"{clock(q['record'], m.fps)} {q.get('name')}: resolve.undo puts back what was there")
        bad = []
        recorded = grade_records.load()
        for q in g["lay"]:
            it = self.by_id((q["kind"], q["track"])).get(self.ids[q["id"]])
            bad += [f"{q.get('name')}: {b}" for b in self.restore(it, q)]
            if q.get("grade_from") in stands:
                if not stands[q["grade_from"]].CopyGrades([it]):
                    bad.append(f"{q.get('name')}: grade")
                else:                                   # the CDL our tools recorded goes with the grade (PFE-42)
                    was = recorded.get(self.real(q["grade_from"]))
                    if was:
                        grade_records.record(self.ids[q["id"]], was["cdl"], by=f"{was.get('by')} (re-laid by "
                                             f"{self.rec.data.get('plan')})")
        if bad:
            self.rec.data["warnings"].append("Resolve refused some settings: " + "; ".join(bad[:10]))
            self.rec.save()

    def ripple(self, g: dict, m_before: Model) -> None:
        s, e = g["ripple"]["range"]
        d = e - s
        inside, tail = [], {}
        for key in m_before.lanes():
            for it in self.items(key):
                a = it.GetStart() - self.t0
                if s <= a and a + it.GetDuration() <= e:
                    inside.append(it)
                elif a >= e:
                    tail[it.GetUniqueId()] = (key, a, it.GetDuration(), source(it, key))
        want = {self.real(x) for x in g["ripple"]["inside"]}
        have = {i.GetUniqueId() for i in inside}
        if want != have:
            raise Busy(f"what's inside {clock(s, 25)}–{clock(e, 25)} isn't what the plan expects "
                       f"({len(have - want)} more, {len(want - have)} missing): plan again")
        markers = {int(f): mk for f, mk in (self.tl.GetMarkers() or {}).items() if f >= e}
        if not self.tl.DeleteClips(inside, True):
            raise Stop(f"Resolve wouldn't ripple-delete {clock(s, 25)}–{clock(e, 25)}")
        after = {}
        for key in m_before.lanes():
            for it in self.items(key):
                after[it.GetUniqueId()] = (key, it.GetStart() - self.t0, it.GetDuration(), source(it, key))
        bad = [u for u, (key, a, n, left) in tail.items() if after.get(u) != (key, a - d, n, left)]
        if bad:
            raise Stop(f"after the cut at {clock(s, 25)}, {len(bad)} later clips aren't exactly {d} frames earlier: "
                       "check the timeline, then resolve.undo")
        now = {int(f) for f in (self.tl.GetMarkers() or {})}
        if not all(f - d in now for f in markers):
            self.move_markers([[f, f - d] for f in sorted(markers) if f in now and f - d not in now])

    def move_markers(self, moves: list) -> None:
        if not moves:
            return
        have = {int(f): m for f, m in (self.tl.GetMarkers() or {}).items()}
        taken = [(f, to, have[f]) for f, to in moves if f in have]
        for f, _, _ in taken:
            self.tl.DeleteMarkerAtFrame(f)
        for f, to, mk in taken:
            if not self.tl.AddMarker(to, mk.get("color"), mk.get("name"), mk.get("note"), mk.get("duration") or 1,
                                     mk.get("customData") or ""):
                self.rec.data["warnings"].append(f"the marker {mk.get('name')!r} couldn't go to {clock(to, 25)}")


def source(it, key) -> int:
    """A clip's source start as cut_ranges.py checks it: the file's own frame on video, the in-point on audio."""
    return it.GetSourceStartFrame() if key[0] == "video" else it.GetLeftOffset()


# ---------------------------------------------------------------- apply and undo


def apply(resolve, snap: dict, plan: dict, record: Record, *, media=None, sleep=time.sleep, wait: float = 2.0) -> dict:
    """Run the plan's groups from where the record left off. `snap` is the base it was compiled on."""
    compiled = compile_plan(snap, plan, media=media)
    if compiled["problems"]:
        raise Stop("the plan has problems: " + "; ".join(compiled["problems"]))
    groups = compiled["groups"]
    for i, g in enumerate(groups):
        g["index"] = i
    if groups:                                         # Resolve's own end frame, as the snapshot read it just now
        groups[0]["end_before"] = int(snap["timeline"]["end_frame"] - snap["timeline"]["start_frame"])
    run = Runner(resolve, record, sleep=sleep, wait=wait)
    record.data["state"] = "applying"
    record.data["plan"] = plan.get("id")
    record.save()
    helper = Model(snap)                                # units and sources only: every group carries its own pieces
    start = record.data.get("groups_done", 0)
    if record.data.get("partial"):
        raise Stop("the last apply stopped part-way through a step: resolve.undo first, then plan again")
    for i in range(start, len(groups)):
        g = groups[i]
        g["first"] = i == 0
        record.log(f"op {g['op'] + 1} ({g['kind']}): {len(g['remove'])} out, {len(g['lay'])} in"
                   + (f", cut {g['ripple']['range']}" if g.get("ripple") else ""))
        run.run_group(g, helper, helper)
        record.data["groups_done"] = i + 1
        record.save()
    record.data["state"] = "applied"
    record.save()
    return {"groups": len(groups), "ids": len(run.ids), "warnings": record.data["warnings"]}


def undo(resolve, snap: dict, plan: dict, record: Record, *, media=None, sleep=time.sleep, wait: float = 2.0) -> dict:
    """Put back what the plan changed: every clip it placed or moved goes, the snapshot's clips come back
    (grades copied across from what replaced them). Refuses if anything in the way isn't the plan's own."""
    done = record.data.get("groups_done", 0) + (1 if record.data.get("partial") else 0)
    if not done:
        raise Stop("nothing of this plan has been applied")
    base = Model(snap)
    compiled = compile_plan(snap, plan, media=media, upto=done)
    after = compiled["model"]
    run = Runner(resolve, record, sleep=sleep, wait=wait)
    ids = record.data.get("ids", {})
    ours = {real: temp for temp, real in ids.items()}
    # the region each lane's restore covers: what the groups touched, a cut's from its start to the end
    regions: dict[tuple, list] = {}
    for g in compiled["groups"]:
        for name, spans in g["spans"].items():
            key = base.lane_of(name)
            for s, e in spans:
                regions.setdefault(key, []).append([s, 10 ** 9 if g.get("ripple") else e])
    for key, spans in regions.items():                 # whole clips of the snapshot's at either edge
        for sp in spans:
            for p in base.overlapping(key, sp[0], sp[1]):
                sp[0], sp[1] = min(sp[0], p["record"]), max(sp[1], p["record"] + p["frames"])
    base_by_id = {p["id"]: p for items in base.tracks.values() for p in items if p.get("id")}
    after_by_id = {}
    for items in after.tracks.values():
        for p in items:
            after_by_id[ids.get(p.get("id"), p.get("id"))] = p
    remove, keep_ids, lays, sources = [], set(), [], {}
    for key, spans in regions.items():
        for it in run.items(key):
            if it.GetType() == "transition":
                continue
            uid, a, n = it.GetUniqueId(), it.GetStart() - run.t0, it.GetDuration()
            if not any(s < a + n and a < e for s, e in spans):
                continue
            b = base_by_id.get(uid)
            if b and (b["kind"], b["track"]) == key and run.matches(it, b, base):
                keep_ids.add(uid)                       # the snapshot's, untouched
                continue
            exp = after_by_id.get(uid)
            if exp is None or (exp["kind"], exp["track"]) != key or not run.matches(it, exp, after):
                raise Stop(f"{lane(*key)} {it.GetName()} at {clock(a, base.fps)} isn't this plan's (James's edit since?): "
                           "undo would remove it, so it stops. Put it back by hand, or plan the change you want")
            remove.append({"kind": key[0], "track": key[1], "record": a, "frames": n, "id": uid,
                           "in": exp.get("in"), "src_frame": exp.get("src_frame"), "media_fps": exp.get("media_fps"),
                           "name": it.GetName(), "file": exp.get("file")})
            sources[uid] = exp
        for b in base.tracks.get(key, []):
            if b.get("id") in keep_ids or not any(s < b["record"] + b["frames"] and b["record"] < e for s, e in spans):
                continue
            q = dict(b)
            q["grade_from"] = None
            if (b.get("grade") or {}).get("graded"):
                temp_kids = [ids[t] for t in after.descendants(b["id"]) if t in ids]
                src = b["id"] if b["id"] in sources else next((k for k in temp_kids if k in sources), None)
                q["grade_from"] = src
                if src is None:
                    record.data["warnings"].append(f"{lane(*key)} {clock(b['record'], base.fps)} {b.get('name')}: its grade "
                                                   "has nowhere to be copied back from: re-grade it by hand")
            q["id"] = f"undo-{b['id']}"
            lays.append(q)
    markers, lost = [], []
    base_marks = {(m.get("name"), m.get("note"), m.get("color"), m.get("custom")): f for f, m in base.markers.items()}
    after_keys = set()
    for f, m in after.markers.items():
        k = (m.get("name"), m.get("note"), m.get("color"), m.get("custom"))
        after_keys.add(k)
        if k in base_marks and base_marks[k] != f:
            markers.append([f, base_marks[k]])
    for k, f in base_marks.items():                   # inside a cut: Resolve took them with it
        if k not in after_keys:
            lost.append({"frame": f, **base.markers[f]})
    transitions = []
    for key, items in base.transitions.items():
        for t in items:
            if key in regions and any(s <= t["record"] < e for s, e in regions[key]):
                c = next((p["record"] + p["frames"] for p in base.tracks.get(key, [])
                          if t["record"] <= p["record"] + p["frames"] <= t["record"] + t["frames"]), None)
                left = next((p["id"] for p in base.tracks.get(key, []) if p["record"] + p["frames"] == c), None)
                if left:
                    transitions.append({"key": list(key), "name": t["name"], "frames": t["frames"], "cut": c,
                                        "left": f"undo-{left}"})
    sets = []
    for g in compiled["groups"]:
        for s in g["set"]:
            b = base_by_id.get(s["id"])
            if b:
                sets.append({"id": s["id"], "key": s["key"],
                             **{k: ({x: (b.get(k) or {}).get(x) for x in s[k]} if isinstance(s[k], dict) else b.get(k))
                                for k in ("props", "fades", "color", "enabled") if k in s}})
    group = {"index": -1, "op": -1, "kind": "undo", "remove": remove, "lay": lays, "set": sets, "ripple": None,
             "transitions": transitions, "markers": markers, "lost_markers": lost, "end_before": None}
    run.ids.update({k: k for k in sources})            # the live ids stand for themselves
    record.data["state"] = "undoing"
    record.data["partial"] = None
    record.save()
    run.run_group_undo(group, base, after, sources)
    record.data["undo_ids"] = {k: v for k, v in run.ids.items() if k.startswith("undo-")}
    record.data["ids"] = {}
    record.data["state"] = "undone"
    record.data["groups_done"] = 0
    record.save()
    return {"removed": len(remove), "laid": len(lays), "markers": len(markers), "transitions": len(transitions),
            "warnings": record.data["warnings"]}


def _run_group_undo(self: Runner, g: dict, base: Model, after: Model, sources: dict) -> None:
    """The undo group: its removes are live clips (by their own ids), checked against `after`, and its lays
    are the snapshot's clips, checked against `base`."""
    going = []
    for p in g["remove"]:
        key = (p["kind"], p["track"])
        it = self.by_id(key).get(p["id"])
        if it is None:
            raise Busy(f"{lane(*key)} {p.get('name')} went while undoing: check the timeline")
        going.append(it)
    stand_src = {pid: (self.by_id((sources[pid]["kind"], sources[pid]["track"]))[pid], sources[pid]) for pid in
                 dict.fromkeys(q["grade_from"] for q in g["lay"] if q.get("grade_from"))}
    stands = self.park(g["lay"], stand_src, base) if g["lay"] else {}
    try:
        if going and not self.tl.DeleteClips(going, False):
            raise Stop("Resolve wouldn't remove the plan's clips: nothing was changed")
        chunks = [g["lay"][i:i + 200] for i in range(0, len(g["lay"]), 200)]
        for chunk in chunks:
            self.rec.data["partial"] = {"group": "undo", "removed": [], "laid": []}
            self.lay({**g, "lay": chunk, "remove": []}, base, stands)
    finally:
        self.cleanup(stands)
    for t in g["transitions"]:
        key = tuple(t["key"])
        left = self.by_id(key).get(self.ids.get(t["left"]))
        if not (left and left.AddTransition({"type": t["name"], "category": "simple" if key[0] == "video" else "audio",
                                             "position": "end", "alignment": "center", "duration": int(t["frames"])})):
            self.rec.data["warnings"].append(f"{lane(*key)} {clock(t['cut'], base.fps)}: the {t['name']} didn't come back")
    self.move_markers(g["markers"])
    for mk in g.get("lost_markers") or []:
        if not self.tl.AddMarker(mk["frame"], mk.get("color"), mk.get("name"), mk.get("note"), mk.get("duration") or 1,
                                 mk.get("custom") or ""):
            self.rec.data["warnings"].append(f"the marker {mk.get('name')!r} couldn't come back at {clock(mk['frame'], 25)}")
    for s in g["set"]:
        it = self.by_id(tuple(s["key"])).get(self.ids.get(s["id"], s["id"]))
        if it is not None:
            self.restore(it, s)
    self.rec.data["partial"] = None
    journal.record("resolve.undo", removed=[p["id"] for p in g["remove"]],
                   added=[self.ids[q["id"]] for q in g["lay"] if q["id"] in self.ids],
                   timeline=self.tl.GetUniqueId(), note=f"undo {self.rec.data.get('plan')}")


Runner.run_group_undo = _run_group_undo
