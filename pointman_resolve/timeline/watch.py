"""Watching the cut (PFE-13, v1): the checks that catch what the person's edits did to the agents' tracks,
run on a snapshot while they pause, and proposed to the project's agent as findings and a plan.

Nothing here writes: findings name what's wrong and, where a fix is mechanical, the plan op for it. Only the
editor agent changes the timeline (SKILLS.md), so the watcher proposes and the editor applies.

Tracks are found by role (`roles`: role -> "V3" / "A1" / a track name), by default from the track names:
screen, camera, voice, loft, desktop, cutout, graphics, flips, sfx. The checks:

  sync        a cutout or loft piece (file `<clip>-f<start>…`, rendered from camera frame <start>) showing a camera
              frame 2+ frames off the voice under it, less that camera's offset (`offsets`: clip -> voice − camera,
              seconds; edit/sync_activity.py). Fix: a slip by the difference.
  no cutout   a desktop piece with no cutout over part of it (8+ frames); not a split screen's (cropped left or
              right: the person is on the other side, in the loft).
  orphan cue  a sound on the sfx track with no card or flip starting or ending near it. Fix: delete it.
  scrap       a picture piece under 8 frames on the agents' tracks (sounds are short by design). Fix: a join, if it
              carries on from its neighbour (same file and settings, the source running on); a delete, if it's a card
              or flip; otherwise none (deleting a desktop scrap would leave a hole).
  see-through a cut on a lower track that shows through a full-frame card or flip while it fades or wipes (its
              alpha from the file), or just before or after one (transition_cover.py's rule).
  chapter     a chapter card (c01-…) that was there last time and isn't now, by card id: a re-render in another
              style (-schematic / -paper / -signal, -vN) is the same card.
Findings whose key starts with one of the config's `accept` entries (e.g. "sync:<item id>") are left out: what
the person or the editor made that way on purpose.
A change the person made to the agents' tracks is taken as meant: the checks look at the timeline as it is,
not at what changed, so nothing is ever moved back to where it was.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from . import journal
from . import render as R

ROLES = ("screen", "camera", "voice", "loft", "desktop", "cutout", "graphics", "flips", "sfx")
OURS = ("loft", "desktop", "cutout", "graphics", "flips", "sfx")
SCRAP, SYNC_OFF, CUE_NEAR = 8, 2, 12
OPAQUE, CLEAR, NEAR = 0.99, 0.02, 6
W, H = 480, 270
STEM = re.compile(r"^(?P<clip>[A-Za-z]+\d+)-f(?P<start>\d+)")
CHAPTER = re.compile(r"^c\d{2}-")
STYLE = re.compile(r"(-(schematic|signal|paper))?(-v\d+)?$")


def card_id(file: str) -> str:
    """A card whatever style or version it's rendered in: c07-discuss-the-options-paper -> c07-discuss-the-options."""
    return STYLE.sub("", Path(file).stem)


def clock(f: int, fps: float) -> str:
    s = f / (fps or 25)
    return f"{int(s // 60)}:{s % 60:05.2f}"


def roles_of(snap: dict, config: dict | None) -> dict:
    """role -> (kind, index), from `roles` in the config, else the track names."""
    tracks = {(t["kind"], t["index"]): (t.get("name") or "").strip().lower() for t in snap.get("tracks", [])}
    out = {}
    for role in ROLES:
        hits = [k for k, n in tracks.items() if n == role]
        if len(hits) == 1:
            out[role] = hits[0]
    for role, where in ((config or {}).get("roles") or {}).items():
        w = str(where).strip()
        if len(w) > 1 and w[0].upper() in "VA" and w[1:].isdigit():
            out[role] = ("video" if w[0].upper() == "V" else "audio", int(w[1:]))
        else:
            hits = [k for k, n in tracks.items() if n == w.lower()]
            if len(hits) == 1:
                out[role] = hits[0]
    return out


def lane_items(snap: dict, key) -> list[dict]:
    if not key:
        return []
    return sorted((i for i in snap["items"] if (i.get("kind"), i.get("track")) == tuple(key) and i.get("type") != "transition"
                   and i.get("record") is not None), key=lambda i: i["record"])


def lane(key) -> str:
    return f"{key[0][0].upper()}{key[1]}"


def name(it: dict) -> str:
    return Path(it.get("file") or it.get("name") or "").name


# ---------------------------------------------------------------- the checks


def sync(snap: dict, roles: dict, offsets: dict) -> list[dict]:
    fps = snap["timeline"].get("fps") or 25
    voice = lane_items(snap, roles.get("voice"))
    out = []
    for role in ("cutout", "loft"):
        for it in lane_items(snap, roles.get(role)):
            m = STEM.match(Path(it.get("file") or "").stem)
            if not m or m["clip"] not in offsets:
                continue
            s = it["record"]
            v = next((x for x in voice if x["record"] <= s < x["record"] + x["frames"]), None)
            if v is None:
                continue
            want = round(((v["in"] + s - v["record"]) / fps - float(offsets[m["clip"]])) * fps) - int(m["start"])
            off = want - (it.get("in") or 0)
            if abs(off) >= SYNC_OFF:
                out.append({"check": "sync", "at": s, "to": s + it["frames"], "lane": lane(roles[role]), "id": it["id"],
                            "what": f"{lane(roles[role])} {name(it)} is {off:+d} frames off the voice under it",
                            "fix": {"op": "slip", "items": [it["id"]], "by": off}, "key": f"sync:{it['id']}:{off}"})
    return out


def no_cutout(snap: dict, roles: dict) -> list[dict]:
    cut = lane_items(snap, roles.get("cutout"))
    out = []
    for d in lane_items(snap, roles.get("desktop")):
        props = d.get("props") or {}
        if float(props.get("CropLeft") or 0) or float(props.get("CropRight") or 0):
            continue                                       # a split screen: he's in the loft beside it, by design
        a, b = d["record"], d["record"] + d["frames"]
        covered = sorted((max(a, c["record"]), min(b, c["record"] + c["frames"])) for c in cut
                         if c["record"] < b and c["record"] + c["frames"] > a)
        at, gaps = a, []
        for x, y in covered + [(b, b)]:
            if x - at >= SCRAP:
                gaps.append((at, x))
            at = max(at, y)
        for x, y in gaps:
            out.append({"check": "no cutout", "at": x, "to": y, "lane": lane(roles["desktop"]), "id": d["id"],
                        "what": f"{lane(roles['desktop'])} {name(d)} has no cutout over it for {y - x} frames",
                        "fix": None, "key": f"nocutout:{d['id']}:{x}:{y}"})
    return out


def orphans_and_scraps(snap: dict, roles: dict) -> list[dict]:
    cards = lane_items(snap, roles.get("graphics")) + lane_items(snap, roles.get("flips"))
    out = []
    for s in lane_items(snap, roles.get("sfx")):
        a, b = s["record"], s["record"] + s["frames"]
        near = any(abs(c["record"] - a) <= CUE_NEAR or abs(c["record"] + c["frames"] - a) <= CUE_NEAR
                   or (c["record"] < b and a < c["record"] + c["frames"]) for c in cards)
        if not near:
            out.append({"check": "orphan cue", "at": a, "to": b, "lane": lane(roles["sfx"]), "id": s["id"],
                        "what": f"{lane(roles['sfx'])} {name(s)} has no card or flip near it",
                        "fix": {"op": "delete", "items": [s["id"]]}, "key": f"orphan:{s['id']}"})
    for role in OURS:
        if role == "sfx":
            continue                                       # ticks and fills are short by design
        items = lane_items(snap, roles.get(role))
        for n, it in enumerate(items):
            if (it.get("frames") or 0) >= SCRAP:
                continue
            nb = [x for x in (items[n - 1] if n else None, items[n + 1] if n + 1 < len(items) else None)
                  if x and continues(*sorted((x, it), key=lambda y: y["record"]))]
            if nb:
                fix, how = {"op": "join", "items": [nb[0]["id"], it["id"]]}, "joins its neighbour"
            elif role in ("graphics", "flips"):
                fix, how = {"op": "delete", "items": [it["id"]]}, "a card scrap: delete it"
            else:
                fix, how = None, "doesn't carry on from a neighbour: deleting it would leave a hole"
            out.append({"check": "scrap", "at": it["record"], "to": it["record"] + it["frames"],
                        "lane": lane(roles[role]), "id": it["id"],
                        "what": f"{lane(roles[role])} {name(it)} is {it['frames']} frames long ({how})",
                        "fix": fix, "key": f"scrap:{it['id']}"})
    return out


def continues(a: dict, b: dict) -> bool:
    """b carries straight on from a: back to back, same file and settings, the source running on (within a frame
    for media at another rate)."""
    return (a["record"] + a["frames"] == b["record"] and a.get("file") and a.get("file") == b.get("file")
            and (a.get("props") or {}) == (b.get("props") or {}) and a.get("color") == b.get("color")
            and abs((a.get("in") or 0) + a["frames"] - (b.get("in") or 0)) <= 1)


def chapters_gone(snap: dict, previous: list[str] | None, roles: dict) -> tuple[list[dict], list[str]]:
    """Chapter cards (by card id) there last time and not now; and the ones there now, for next time."""
    now = sorted({card_id(name(i)) for i in snap["items"] if CHAPTER.match(name(i))})
    out = []
    for c in sorted(set(previous or []) - set(now)):
        out.append({"check": "chapter", "at": 0, "to": 0, "lane": lane(roles["graphics"]) if roles.get("graphics") else "",
                    "id": None, "what": f"the chapter card {c} isn't on the timeline any more (in any style): an edit "
                                        "took it out",
                    "fix": None, "key": f"chapter:{c}"})
    return out, now


# ---------------------------------------------------------------- see-through: a cut showing through a transition


def opacity_cache() -> Path:
    return journal.home() / "cache" / "opacity.json"


def alpha_range(path: str, start: int, frames: int, fps: float) -> list[tuple[float, float]]:
    """(min, max) alpha, 0-1, of each of `frames` frames from the file's frame `start`; cached by file and range."""
    try:
        cache = json.loads(opacity_cache().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    try:
        stamp = int(os.stat(path).st_mtime)
    except OSError:
        return []
    key = f"{path}|{stamp}|{start}|{frames}"
    if key in cache:
        return [tuple(x) for x in cache[key]]
    run = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{start / fps:.4f}", "-i", path, "-frames:v",
                          str(frames), "-vf", f"extractplanes=a,scale={W}:{H}:flags=area", "-f", "rawvideo", "-pix_fmt",
                          "gray", "-"], capture_output=True, preexec_fn=lambda: os.nice(10))
    raw, size = run.stdout, W * H
    out = [(min(raw[i:i + size]) / 255, max(raw[i:i + size]) / 255) for i in range(0, len(raw) - size + 1, size)]
    cache[key] = out
    opacity_cache().parent.mkdir(parents=True, exist_ok=True)
    opacity_cache().write_text(json.dumps(cache), encoding="utf-8")
    return out


def see_through(snap: dict, roles: dict, *, alpha=alpha_range) -> list[dict]:
    tl = snap["timeline"]
    fps, Wt, Ht = tl.get("fps") or 25, tl.get("width") or 1920, tl.get("height") or 1080
    unit = float(tl.get("px_per_unit") or R.UNIT)
    video = {}
    for it in snap["items"]:
        if it.get("kind") == "video" and it.get("type") != "transition" and it.get("record") is not None:
            video.setdefault(it["track"], []).append(it)
    for v in video.values():
        v.sort(key=lambda i: i["record"])
    is_alpha = {}

    def alpha_file(f):
        if f not in is_alpha:
            is_alpha[f] = bool(f and Path(f).exists() and R.probe(f).get("alpha"))
        return is_alpha[f]

    def opaque_full(it):
        if not it.get("file") or alpha_file(it["file"]) or float((it.get("props") or {}).get("Opacity", 100) or 100) < 100:
            return False
        g = R.geometry(it, Wt, Ht, unit) if Path(it["file"]).exists() else None
        return bool(g) and R.covers_frame(g, Wt, Ht)

    def cuts(k):
        out, items = [], video.get(k, [])
        for n, it in enumerate(items):
            a, b = it["record"], it["record"] + it["frames"]
            prev = items[n - 1] if n else None
            if not (prev and prev["record"] + prev["frames"] == a and prev.get("file") == it.get("file")
                    and (prev.get("in") or 0) + prev["frames"] == it.get("in")):
                out.append((a, f"V{k} {name(it)} in"))
            nxt = items[n + 1] if n + 1 < len(items) else None
            if not (nxt and nxt["record"] == b):
                out.append((b, f"V{k} {name(it)} out"))
        return out

    card_tracks = [roles[r][1] for r in ("graphics", "flips") if roles.get(r) and roles[r][0] == "video"]
    opaque = {k: [(i["record"], i["record"] + i["frames"]) for i in items if opaque_full(i)] for k, items in video.items()}
    found = []
    for k in card_tracks:
        below = [c for j in video if j < k for c in cuts(j)]
        for it in video.get(k, []):
            if not alpha_file(it.get("file")):
                continue
            a, b = it["record"], it["record"] + it["frames"]
            am = alpha(it["file"], it.get("in") or 0, b - a, fps)
            if len(am) < b - a or max(x[0] for x in am) < OPAQUE:
                continue                                   # never opaque over the whole frame: an overlay, not a transition
            seen = [n for n, x in enumerate(am) if x[1] >= CLEAR]
            first, last = a + seen[0], a + seen[-1] + 1
            for f, what in below:
                if not first - NEAR <= f <= last + NEAR:
                    continue
                if f < first or f > last:
                    edge = am[first - a] if f < first else am[last - a - 1]
                    if edge[0] >= OPAQUE:
                        continue                           # it cuts in or out hard: no transition there
                    kind, lo, hi = "near", (0, 0), (0, 0)
                else:
                    lo = am[f - a - 1] if f > a else (0, 0)
                    hi = am[f - a] if f < b else (0, 0)
                    if lo[0] >= OPAQUE or hi[0] >= OPAQUE or max(lo[1], hi[1]) < CLEAR:
                        continue                           # covered on one side, or the card isn't in yet
                    kind = "through"
                track = int(what[1:what.index(" ")])
                if any(x <= f - 1 and f + 1 <= y for j in range(track + 1, k) for x, y in opaque.get(j, [])):
                    continue                               # hidden under an opaque clip lower down
                found.append({"check": "see-through", "at": f, "to": f + 1, "lane": f"V{k}", "id": it["id"],
                              "what": (f"{what} at {clock(f, fps)} shows through {name(it)} (V{k}, opacity "
                                       f"{round(lo[0], 2)}→{round(hi[0], 2)})" if kind == "through" else
                                       f"{what} at {clock(f, fps)} is just outside {name(it)} (V{k})'s fade"),
                              "fix": None, "key": f"see:{it['id']}:{f}:{kind}"})
    return found


# ---------------------------------------------------------------- all of it


def run(snap: dict, config: dict | None, *, previous_chapters: list[str] | None = None, alpha=alpha_range,
        transitions: bool = True) -> dict:
    roles = roles_of(snap, config)
    offsets = (config or {}).get("offsets") or {}
    findings = sync(snap, roles, offsets) + no_cutout(snap, roles) + orphans_and_scraps(snap, roles)
    if transitions:
        findings += see_through(snap, roles, alpha=alpha)
    gone, chapters = chapters_gone(snap, previous_chapters, roles)
    findings += gone
    accept = [str(a) for a in (config or {}).get("accept") or []]
    findings = [f for f in findings if not any(f["key"].startswith(a) for a in accept)]
    findings.sort(key=lambda f: (f["at"], f["check"]))
    return {"findings": findings, "chapters": chapters, "roles": {r: lane(k) for r, k in roles.items()},
            "fixes": [f["fix"] for f in findings if f.get("fix")]}


def markdown(report: dict, title: str, fps: float, plan: str | None) -> str:
    counts: dict = {}
    for f in report["findings"]:
        counts[f["check"]] = counts.get(f["check"], 0) + 1
    lines = [f"# Watch: {title}", "", ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "Nothing found.", ""]
    if plan:
        lines += [f"Plan for the mechanical fixes (sync slips, deletes): `{plan}`. Dry-run it, preview it, then "
                  "`resolve_apply`.", ""]
    lines += ["| At | Check | What |", "|---|---|---|"]
    lines += [f"| {clock(f['at'], fps)} | {f['check']} | {f['what']} |" for f in report["findings"][:150]]
    return "\n".join(lines) + "\n"
