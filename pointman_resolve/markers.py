"""Resolve markers that call an agent (Pointman for Editors, PFE-30).

A timeline marker whose name or note starts with `@pm`, `@pointman` or `@<handle>` becomes a post on
the board: in the thread the project is routed to (~/.pointman-resolve/routes.toml), mentioning that
handle, or the project's agent for @pm / @pointman. A project with no route posts to the fallback:
the thread and agent of the last marker posted on this machine, which an agent can change
(resolve.set_fallback), and the post says it came there. A renamed project (the same project or
timeline ID as a routed one) keeps its route. The post has the marker's timecode and note, the clips
under it, and a still of that frame drawn from the source files (timeline/render.py, never by Resolve).

Once it's posted, the marker turns cyan and its custom data becomes `pm:<post id>`. It turns green when
the agent calls resolve.marker_done, or, where the node lets the extension read posts, when the post gets
a ✅ from the agent it called or from James. Its name and note are never changed.
Markers whose custom data starts `dailies:` (board notes mirrored by resolve.marker) or `pm:` (ours)
are never picked up, so nothing loops.

Everything here reads through ReadOnly, except MarkerWriter, which can only tag, delete and re-add a
marker: the same kind of write resolve.marker already makes.
"""

import hashlib
import json
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .timeline import journal

TRIGGER = re.compile(r"^\s*@([A-Za-z0-9][A-Za-z0-9_.-]{0,31})(?![A-Za-z0-9_.-])")
DEFAULT_NAMES = {"pm", "pointman"}          # "the project's agent"
SKIP = ("dailies:", "pm:")                  # board notes mirrored as markers, and ours
POSTED, DONE = "Cyan", "Green"
DONE_EMOJI = ("✅", "white_check_mark", ":white_check_mark:")


@dataclass
class Call:
    """One marker that calls an agent."""
    frame: int
    marker: dict
    handle: str | None                      # None: the project's default agent

    @property
    def text(self) -> str:
        """What James wrote: the name and the note, the @handle left in."""
        name, note = (self.marker.get("name") or "").strip(), (self.marker.get("note") or "").strip()
        return "\n".join(t for t in (name, note) if t and not re.fullmatch(r"Marker \d+", t))

    @property
    def key(self) -> str:
        """Stays the same while James leaves the marker alone: its frame, name and note."""
        body = f"{self.marker.get('name') or ''}\n{self.marker.get('note') or ''}"
        return f"{self.frame}-{hashlib.sha1(body.encode()).hexdigest()[:12]}"


def calls(markers: dict) -> list[Call]:
    """The markers (GetMarkers: frame -> info) that call an agent, oldest frame first."""
    out = []
    for frame, m in sorted((markers or {}).items(), key=lambda kv: float(kv[0])):
        if str(m.get("customData") or "").startswith(SKIP):
            continue
        for field in (m.get("name"), m.get("note")):
            hit = TRIGGER.match(field or "")
            if hit:
                handle = hit[1].lower()
                out.append(Call(int(frame), m, None if handle in DEFAULT_NAMES else handle))
                break
    return out


# ------------------------------------------------------------ where a call goes


def routes_path() -> Path:
    return journal.home() / "routes.toml"


EXAMPLE_ROUTES = """\
# Where Resolve markers that call an agent (@pm, @pointman, @<handle>) are posted, by Resolve project.
# A project that isn't listed posts to the fallback: the thread and agent of the last marker posted on
# this machine (fallback.json beside this file; an agent moves it with resolve.set_fallback). A project
# renamed in Resolve keeps its route: it's known by its project and timeline IDs.
person = "james"
# The handles a marker may call. One that isn't here goes to the project's agent, with a note that
# it wasn't recognised (an extension can't ask the board who exists). Leave it out to trust any handle.
agents = ["editor", "james", "rtx-render", "sheepgfx", "thumbs", "scene", "camgrab", "resolvedoc", "patchbay"]

[projects."feat-flow-with-spacesheep"]
thread = 10399        # the board thread
agent = "editor"      # who @pm / @pointman means in this project
"""


def load_routes(path: Path | None = None) -> dict:
    path = path or routes_path()
    if not path.exists():
        return tomllib.loads(EXAMPLE_ROUTES)
    return tomllib.loads(path.read_text(encoding="utf-8"))


def route(routes: dict, project: str, handle: str | None, fallback: dict | None = None) -> dict:
    """{thread, mention, unknown, routed, agent}: a reply in the project's `thread`, or, for a project
    with no route (`routed` False), in the `fallback`'s; no thread: nowhere to post yet. `mention`: the
    handle called, else `agent` (who @pm means there). A handle that isn't in `agents` goes to `agent`
    instead, and comes back as `unknown`."""
    p = (routes.get("projects") or {}).get(project) or {}
    person = routes.get("person") or "james"
    agents = {str(a).lower().lstrip("@") for a in routes["agents"]} if routes.get("agents") else None
    unknown = None
    if handle and agents is not None and handle not in agents and handle != person:
        unknown, handle = handle, None
    if p.get("thread"):
        agent = p.get("agent") or person
        return {"thread": int(p["thread"]), "mention": handle or agent, "unknown": unknown, "routed": True,
                "agent": agent}
    fb = fallback or {}
    agent = fb.get("agent") or person
    return {"thread": int(fb["thread"]) if fb.get("thread") else None, "mention": handle or agent,
            "unknown": unknown, "routed": False, "agent": agent}


# ------------------------------------------------------------ the post


def timecode(frame: int, fps: float, start: str | None) -> str:
    """The marker's timecode as Resolve shows it (the timeline's start timecode plus its frame)."""
    fps_i = round(fps or 25)
    h, m, s, f = (int(x) for x in re.split(r"[:;]", start or "01:00:00:00"))
    total = ((h * 60 + m) * 60 + s) * fps_i + f + frame
    f = total % fps_i
    s = total // fps_i
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{f:02d}"


def clock(frame: int, fps: float) -> str:
    s = frame / (fps or 25)
    return f"{int(s // 60)}:{s % 60:05.2f}"


def under(snap: dict, frame: int) -> list[str]:
    """The clips under a frame, top video track first, then audio: 'V3 loft · C0030-f009844.mov at 4:01.20'."""
    fps = snap["timeline"].get("fps") or 25
    names = {(t["kind"], t["index"]): t.get("name") for t in snap.get("tracks", [])}
    hits = [i for i in snap["items"] if i.get("type") in ("video", "audio", "generator") and i.get("record") is not None
            and i["record"] <= frame < i["record"] + (i.get("frames") or 0)]
    hits.sort(key=lambda i: (i["kind"] != "video", -i["track"] if i["kind"] == "video" else i["track"]))
    out = []
    for i in hits:
        lane = f"{i['kind'][0].upper()}{i['track']}"
        track = names.get((i["kind"], i["track"]))
        what = Path(i["file"]).name if i.get("file") else (i.get("name") or i.get("type"))
        src = f" at {clock((i.get('in') or 0) + frame - i['record'], fps)}" if i.get("file") else ""
        out.append(f"{lane}{' ' + track if track and not re.fullmatch(r'(Video|Audio) \d+', track) else ''} · {what}{src}")
    return out


def compose(call: Call, snap: dict, mention: str, machine: str | None = None, *, ticks: bool = False,
            unknown: str | None = None, fallback: bool = False, renamed: tuple[str, str] | None = None) -> str:
    """The post: who it's for, where it is, what James wrote, what's under it, and how to say it's done."""
    tl = snap["timeline"]
    tc = timecode(call.frame, tl.get("fps") or 25, tl.get("start_timecode"))
    lines = [f"@{mention} Resolve marker at **{clock(call.frame, tl.get('fps') or 25)}** ({tc}), "
             f"{snap['project']['name']} › {tl['name']}:"]
    lines += ["> " + line for line in (call.text or "(no note)").splitlines()]
    if unknown:  # in backticks, so the board doesn't take it as a mention
        lines.append(f"\n_The marker calls `@{unknown}`, which I didn't recognise, so it's here for @{mention}._")
    on = f" on {machine}" if machine else ""
    if fallback:
        lines.append(f"\n_Project “{snap['project']['name']}” has no route, so it came to this thread (the fallback, "
                     f"where the last marker went). Route it in `~/.pointman-resolve/routes.toml`{on}, or move "
                     f"the fallback with `resolve.set_fallback`._")
    elif renamed:
        lines.append(f"\n_Project “{snap['project']['name']}” isn't in routes.toml, but {renamed[1]} “{renamed[0]}” "
                     f"(renamed?), so it came to that one's thread._")
    clips = under(snap, call.frame)
    if clips:
        lines.append("")
        lines.append("Under it: " + " · ".join(f"`{c}`" for c in clips) if len(clips) <= 2 else "Under it:")
        if len(clips) > 2:
            lines += [f"- {c}" for c in clips]
    lines.append("")
    call_it = (f"`resolve.marker_done` with `post` = this post's id (tool `resolve_marker_done`, or "
               f"`run_on_machine`{on})")
    lines.append(f"Done? React ✅, or call {call_it}: the marker turns green in Resolve." if ticks else
                 f"Done? {call_it} turns the marker green in Resolve.")
    return "\n".join(lines)


# ------------------------------------------------------------ what's been posted


class State:
    """markers.json beside the routes: each marker posted (by its key) and where it stands, so a marker
    is posted once even if tagging it in Resolve fails."""

    def __init__(self, path: Path | None = None):
        self.path = path or journal.home() / "markers.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def get(self, timeline_id: str, key: str) -> dict | None:
        return self.data.get(f"{timeline_id}/{key}")

    def put(self, timeline_id: str, key: str, entry: dict) -> None:
        self.data[f"{timeline_id}/{key}"] = entry
        self.save()

    def open(self) -> list[tuple[str, dict]]:
        """Posted, not yet done."""
        return [(k, v) for k, v in self.data.items() if v.get("status") != "done"]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)


class Projects:
    """projects.json beside the routes: each Resolve project seen, by name, with its ID and the
    timelines seen in it, so a project renamed in Resolve (same IDs, new name) keeps its route."""

    def __init__(self, path: Path | None = None, state: State | None = None):
        self.path = path or journal.home() / "projects.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
            for entry in (state.data.values() if state else ()):   # markers posted before 0.4.1
                if entry.get("project") and entry.get("timeline_id"):
                    self.see(entry["project"], None, entry["timeline_id"], save=False)
            if self.data:
                self.save()

    def see(self, name: str, project_id: str | None, timeline_id: str | None, *, save: bool = True) -> None:
        p = self.data.setdefault(name, {"id": None, "timelines": []})
        changed = False
        if project_id and p.get("id") != project_id:
            p["id"], changed = project_id, True
        if timeline_id and timeline_id not in p["timelines"]:
            p["timelines"].append(timeline_id)
            changed = True
        if changed and save:
            self.save()

    def renamed_from(self, routes: dict, name: str, project_id: str | None,
                     timeline_id: str | None) -> tuple[str, str] | None:
        """The routed project this unrouted one was, by its IDs: (its name, how it's known), or None. A
        shared timeline only counts while the routed project's own ID isn't known (markers from before
        0.4.1): a Duplicate or Save As keeps the timelines but gets a new project ID."""
        for old, r in (routes.get("projects") or {}).items():
            known = self.data.get(old)
            if old == name or not known or not (r or {}).get("thread"):
                continue
            if project_id and known.get("id") == project_id:
                return old, "it has the same project ID as"
            if project_id and known.get("id"):
                continue                                  # two projects' IDs, and they differ: a copy, not a rename
            if timeline_id and timeline_id in known.get("timelines", []):
                return old, "its open timeline was seen in"
        return None

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)


class Fallback:
    """fallback.json beside the routes: where a project with no route posts. The thread and agent of
    the last marker posted from a routed project on this machine, unless an agent has moved it since
    (resolve.set_fallback). With no file yet, it starts from the last marker in markers.json."""

    def __init__(self, path: Path | None = None, state: State | None = None, routes: dict | None = None):
        self.path = path or journal.home() / "fallback.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8")) or None
        except (OSError, ValueError):
            self.data = None
            posted = sorted((e for e in (state.data.values() if state else ()) if e.get("post") and e.get("project")),
                            key=lambda e: e.get("at") or 0)
            for entry in reversed(posted):
                r = ((routes or {}).get("projects") or {}).get(entry["project"]) or {}
                if r.get("thread"):
                    self.set(int(r["thread"]), r.get("agent") or (routes or {}).get("person") or "james",
                             by="marker", project=entry["project"])
                    break

    def get(self) -> dict | None:
        return self.data

    def set(self, thread: int, agent: str, *, by: str, project: str | None = None) -> dict | None:
        """The fallback now; the one before is returned."""
        was = self.data
        self.data = {"thread": int(thread), "agent": str(agent).lstrip("@"), "by": by, "project": project,
                     "at": time.time()}
        if not was or (was.get("thread"), was.get("agent")) != (self.data["thread"], self.data["agent"]):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
        else:
            self.data = was                             # the same place: leave who set it, and when
        return was


# ------------------------------------------------------------ the one write: the marker's colour and tag


class MarkerWriter:
    """Marker calls only: everything else on the timeline stays out of reach."""

    ALLOWED = ("GetMarkers", "GetMarkerCustomData", "UpdateMarkerCustomData", "DeleteMarkerAtFrame", "AddMarker")

    def __init__(self, timeline):
        self._tl = timeline

    def __getattr__(self, name):
        if name not in MarkerWriter.ALLOWED:
            raise PermissionError(f"MarkerWriter only touches markers: {name}() is not allowed")
        return getattr(self._tl, name)

    def find(self, custom: str) -> tuple[int, dict] | None:
        """Our marker by its tag, wherever James's edits have moved it since."""
        for f, m in (self.GetMarkers() or {}).items():
            if m.get("customData") == custom:
                return int(f), m
        return None

    def mark(self, frame: int, expect: dict, color: str, custom: str) -> str:
        """Tag the marker at `frame` with `custom` and give it `color`, if it's still the one we posted
        (same name and note). 'ok', 'gone' (moved or edited: left alone), or 'failed: …'."""
        now = (self.GetMarkers() or {}).get(frame) or (self.GetMarkers() or {}).get(float(frame))
        if not now or (now.get("name"), now.get("note")) != (expect.get("name"), expect.get("note")):
            return "gone"
        # 1. the tag first: no delete in it, so whatever happens next the marker isn't posted twice
        if now.get("customData") != custom and not self.UpdateMarkerCustomData(frame, custom):
            return "failed: Resolve wouldn't tag the marker"
        if now.get("color") == color:
            return "ok"
        # 2. a colour can only change by delete + add: same frame, name, note and duration
        name, note, duration = now.get("name") or "", now.get("note") or "", int(now.get("duration") or 1)
        if not self.DeleteMarkerAtFrame(frame):
            return "failed: Resolve wouldn't remove the marker to recolour it (it's tagged)"
        if self.AddMarker(frame, color, name, note, duration, custom):
            return "ok"
        # 3. never lose James's marker: put it back as it was, tagged
        self.AddMarker(frame, now.get("color") or "Blue", name, note, duration, custom)
        return "failed: Resolve wouldn't add the recoloured marker back (restored as it was)"


def done_by(reactions: list[dict], mention: str, people=("james",)) -> str | None:
    """Who gave the post its ✅, the agent it called or James, from core's reactions
    ([{emoji, count, handles}])."""
    for r in reactions or []:
        if str(r.get("emoji")) not in DONE_EMOJI:
            continue
        for h in r.get("handles") or [r.get("handle")]:
            if str(h or "").lstrip("@").lower() in {mention, *people}:
                return str(h).lstrip("@")
    return None
