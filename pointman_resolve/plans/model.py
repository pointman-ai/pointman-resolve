"""A model of a timeline, built from a snapshot, that edit plans are compiled against. Offline.

Pieces are the snapshot's items (dicts). Units, as measured in PFE-9:
- record, frames, in: timeline frames. `in` (GetLeftOffset) is the in-point counted in timeline frames
  from the start of the file, exact for media at the timeline's rate.
- For media at another rate (a 60 fps screen recording on a 25 fps timeline: "fast"), AppendToTimeline
  takes the file's own frames: `src_frame` (GetSourceStartFrame) is exact there, and `in` is rounded.
  Moving such a source by k timeline frames moves it round(k * 60 / 25) of its own frames, so a split
  can shift it by up to 0.4 of a timeline frame: sources are compared in seconds, within one of the
  file's frames.
"""

import math

TOLERANCE = 0.999            # source frames: what "the same source frame" allows for fast media


def lane(kind: str, track: int) -> str:
    return f"{kind[0].upper()}{track}"


def clock(frame: float, fps: float) -> str:
    s = frame / (fps or 25)
    return f"{int(s // 60)}:{s % 60:05.2f}"


class PlanError(ValueError):
    """A plan that can't be applied as it stands: says why, in words for the agent."""


class Model:
    def __init__(self, snap: dict):
        tl = snap["timeline"]
        self.fps = float(tl.get("fps") or 25)
        self.end = int(tl["end_frame"] - tl["start_frame"])
        self.names = {(t["kind"], t["index"]): (t.get("name") or "") for t in snap.get("tracks", [])}
        self.subtypes = {(t["kind"], t["index"]): t.get("subtype") for t in snap.get("tracks", [])}
        self.tracks: dict[tuple, list[dict]] = {k: [] for k in self.names}
        self.transitions: dict[tuple, list[dict]] = {k: [] for k in self.names}
        for it in snap.get("items", []):
            if it.get("record") is None or it.get("kind") not in ("video", "audio"):
                continue
            key = (it["kind"], it["track"])
            (self.transitions if it.get("type") == "transition" else self.tracks).setdefault(key, []).append(dict(it))
        for k in self.tracks:
            self.tracks[k].sort(key=lambda p: p["record"])
        self.markers = {int(m["frame"]): dict(m) for m in snap.get("markers", [])}
        self.lineage: dict[str, list[str]] = {}   # an id that went -> the pieces laid from it
        self.serial = 0

    # ---------------------------------------------------------------- finding things

    def lanes(self) -> list[tuple]:
        return sorted(self.tracks, key=lambda k: (k[0] != "video", k[1]))

    def lane_of(self, text) -> tuple:
        """("video", 3) from "V3", "A1" or a track's name ("loft"), any case."""
        if isinstance(text, (list, tuple)):
            return (str(text[0]), int(text[1]))
        s = str(text).strip()
        if len(s) > 1 and s[0].upper() in "VA" and s[1:].isdigit():
            key = ("video" if s[0].upper() == "V" else "audio", int(s[1:]))
            if key in self.tracks:
                return key
        hits = [k for k, n in self.names.items() if n.lower() == s.lower()]
        if len(hits) == 1:
            return hits[0]
        raise PlanError(f"no track {s!r} (V1, A2, or a track's name)" if not hits else
                        f"{len(hits)} tracks are named {s!r}: say which, as V<n> or A<n>")

    def name(self, key: tuple) -> str:
        n = self.names.get(key) or ""
        return f"{lane(*key)}" + (f" {n}" if n and not n.lower().startswith(("video", "audio")) else "")

    def piece(self, pid: str) -> dict:
        """A piece by id: a snapshot item, or (after an earlier op re-laid it) the one piece laid from it."""
        for items in self.tracks.values():
            for p in items:
                if p.get("id") == pid:
                    return p
        later = self.descendants(pid)
        if len(later) == 1:
            return self.piece(later[0])
        if later:
            raise PlanError(f"{pid} was split into {len(later)} pieces by an earlier op: name the piece")
        raise PlanError(f"no item {pid} on the timeline (take a fresh snapshot: James may have deleted it)")

    def descendants(self, pid: str) -> list[str]:
        out = []
        for c in self.lineage.get(pid, []):
            out += [c] if not self.lineage.get(c) else self.descendants(c)
        return out

    def key(self, p: dict) -> tuple:
        return (p["kind"], p["track"])

    def overlapping(self, key: tuple, s: int, e: int, *, exclude=()) -> list[dict]:
        return [p for p in self.tracks.get(key, []) if p["record"] < e and p["record"] + p["frames"] > s
                and p.get("id") not in exclude]

    def transitions_at(self, key: tuple, p: dict) -> list[dict]:
        """Transitions on p's head or tail (Resolve deletes them with the clip)."""
        a, b = p["record"], p["record"] + p["frames"]
        return [t for t in self.transitions.get(key, []) if t["record"] <= a < t["record"] + t["frames"]
                or t["record"] < b <= t["record"] + t["frames"]]

    def track_end(self) -> int:
        return max((p["record"] + p["frames"] for items in self.tracks.values() for p in items), default=0)

    # ---------------------------------------------------------------- sources

    def fast(self, p: dict) -> bool:
        f = p.get("media_fps") or 0
        return p["kind"] == "video" and f > 0 and abs(f - self.fps) > 0.01

    def rate(self, p: dict) -> float:
        return float(p["media_fps"]) if self.fast(p) else self.fps

    def src_first(self, p: dict) -> int:
        """AppendToTimeline's startFrame: the file's own frame."""
        return int(p["src_frame"]) if self.fast(p) else int(p["in"])

    def src_span(self, p: dict) -> int:
        """AppendToTimeline's endFrame - startFrame: whole source frames, the end rounded up (Resolve
        drops fractions, and a short end left 1-frame gaps on 53 clips once)."""
        return math.ceil(p["frames"] * self.rate(p) / self.fps - 1e-9) if self.fast(p) else int(p["frames"])

    def source_time(self, p: dict, f: int) -> float:
        """Seconds into the file shown at timeline frame f."""
        if self.fast(p):
            return p["src_frame"] / self.rate(p) + (f - p["record"]) / self.fps
        return (p["in"] + f - p["record"]) / self.fps

    def moved_source(self, p: dict, k: int) -> dict:
        """p with its source moved by k timeline frames (a slip, or the piece of a split k frames in)."""
        q = dict(p)
        q["in"] = (p.get("in") or 0) + k
        if self.fast(p):
            q["src_frame"] = int(p["src_frame"]) + round(k * self.rate(p) / self.fps)
        elif p.get("src_frame") is not None:
            q["src_frame"] = int(p["src_frame"]) + k
        if p.get("src_start") is not None:
            q["src_start"] = p["src_start"] + k / self.fps
        return q

    def check_source(self, p: dict) -> None:
        first, span, total = self.src_first(p), self.src_span(p), p.get("media_frames")
        if first < 0:
            raise PlanError(f"{self.name(self.key(p))} {clock(p['record'], self.fps)} {p.get('name')}: the source would "
                            f"start {-first} frames before the file does")
        if total and first + span > total:
            raise PlanError(f"{self.name(self.key(p))} {clock(p['record'], self.fps)} {p.get('name')}: the source would "
                            f"run {first + span - int(total)} frames past the end of the file")

    # ---------------------------------------------------------------- changes

    def new_id(self, op: int) -> str:
        self.serial += 1
        return f"new-{op}-{self.serial}"

    def remove(self, p: dict) -> None:
        self.tracks[self.key(p)].remove(p)
        for t in self.transitions_at(self.key(p), p):
            self.transitions[self.key(p)].remove(t)

    def lay(self, p: dict, op: int, source: str | None) -> dict:
        key = self.key(p)
        if key not in self.tracks:
            raise PlanError(f"there's no track {lane(*key)}: plans don't add tracks")
        if p["frames"] <= 0:
            raise PlanError(f"a piece of {p['frames']} frames at {clock(p['record'], self.fps)}")
        if p["record"] < 0:
            raise PlanError(f"{self.name(key)}: a piece would start before the timeline does")
        hit = self.overlapping(key, p["record"], p["record"] + p["frames"])
        if hit:
            h = hit[0]
            raise PlanError(f"{self.name(key)} {clock(p['record'], self.fps)}: lands on {h.get('name')} at "
                            f"{clock(h['record'], self.fps)} (Resolve would return a clip and place nothing)")
        q = dict(p)
        q["id"] = self.new_id(op)
        q["from"] = source
        if source:
            self.lineage.setdefault(source, []).append(q["id"])
        self.tracks[key].append(q)
        self.tracks[key].sort(key=lambda x: x["record"])
        return q

    def ripple(self, s: int, e: int) -> None:
        """Every lane loses [s, e): what's inside goes, everything after moves d earlier, markers too."""
        d = e - s
        for key in self.tracks:
            keep = []
            for p in self.tracks[key]:
                if p["record"] >= e:
                    keep.append({**p, "record": p["record"] - d})
                elif p["record"] + p["frames"] <= s:
                    keep.append(p)
                elif not (s <= p["record"] and p["record"] + p["frames"] <= e):
                    raise PlanError(f"{self.name(key)} {p.get('name')} crosses the cut at {clock(s, self.fps)}–"
                                    f"{clock(e, self.fps)}: split it first")
            self.tracks[key] = keep
            self.transitions[key] = [({**t, "record": t["record"] - d} if t["record"] >= e else t)
                                     for t in self.transitions[key] if not (s <= t["record"] < e)]
        self.markers = {(f - d if f >= e else f): m for f, m in self.markers.items() if not (s <= f < e)}
        self.end -= d

    # ---------------------------------------------------------------- reading it back

    def frame_map(self, key: tuple, s: int, e: int) -> list:
        """(file, seconds into it) shown at each frame of [s, e) on one lane; None where it's empty."""
        out: list = [None] * max(0, e - s)
        for p in self.overlapping(key, s, e):
            for f in range(max(s, p["record"]), min(e, p["record"] + p["frames"])):
                out[f - s] = (p.get("file") or p.get("name"), self.source_time(p, f), self.rate(p))
        return out

    def through_edits(self, key: tuple) -> int:
        """Cuts on a lane where the same file carries on frame for frame with the same settings."""
        items, n = self.tracks.get(key, []), 0
        for a, b in zip(items, items[1:]):
            if (a["record"] + a["frames"] == b["record"] and a.get("file") and a.get("file") == b.get("file")
                    and a.get("props") == b.get("props") and a.get("color") == b.get("color")
                    and a.get("enabled") == b.get("enabled") and not self.transitions_at(key, b)
                    and abs(self.source_time(b, b["record"]) - self.source_time(a, b["record"])) * self.rate(a) < TOLERANCE):
                n += 1
        return n


def same_frame(x, y) -> bool:
    """Two frame-map entries show the same thing: same file, sources within one of its frames."""
    if x is None or y is None:
        return x is y
    return x[0] == y[0] and abs(x[1] - y[1]) * max(x[2], y[2]) < TOLERANCE
