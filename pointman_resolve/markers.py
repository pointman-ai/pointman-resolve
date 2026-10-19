"""Resolve markers that call an agent (Pointman for Editors, PFE-30).

A timeline marker whose name or note starts with `@pm`, `@pointman` or `@<handle>` becomes a post on
the board: in the thread the project is routed to (~/.pointman-resolve/routes.toml), mentioning that
handle, or the project's agent for @pm / @pointman. A project with no route starts a thread in the
`unmapped` channel, mentioning James (an extension can't open a DM through the node). The post has the marker's timecode and note, the clips under it, and a still of that frame drawn from the
source files (timeline/render.py, never by Resolve).

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
# A project that isn't listed starts a thread in [unmapped]'s channel, mentioning `person` (no channel
# set: those markers wait, and resolve.status says so).
person = "james"

[unmapped]
# thread = 123       # a board thread to post in, or
# channel = 123      # a board channel to start a thread in

[projects."feat-flow-with-spacesheep"]
thread = 10399        # the board thread
agent = "editor"      # who @pm / @pointman means in this project
"""


def load_routes(path: Path | None = None) -> dict:
    path = path or routes_path()
    if not path.exists():
        return tomllib.loads(EXAMPLE_ROUTES)
    return tomllib.loads(path.read_text(encoding="utf-8"))


def route(routes: dict, project: str, handle: str | None) -> dict:
    """{thread, channel, mention}: a reply in the project's `thread`, or a new thread in the unmapped
    `channel`, mentioning `mention`. Neither: nowhere to post yet."""
    p = (routes.get("projects") or {}).get(project) or {}
    person = routes.get("person") or "james"
    if p.get("thread"):
        return {"thread": int(p["thread"]), "channel": None, "mention": handle or p.get("agent") or person}
    un = routes.get("unmapped") or {}
    return {"thread": int(un["thread"]) if un.get("thread") else None,
            "channel": int(un["channel"]) if un.get("channel") and not un.get("thread") else None,
            "mention": handle or person}


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


def compose(call: Call, snap: dict, mention: str, machine: str | None = None, *, ticks: bool = False) -> str:
    """The post: who it's for, where it is, what James wrote, what's under it, and how to say it's done."""
    tl = snap["timeline"]
    tc = timecode(call.frame, tl.get("fps") or 25, tl.get("start_timecode"))
    lines = [f"@{mention} Resolve marker at **{clock(call.frame, tl.get('fps') or 25)}** ({tc}), "
             f"{snap['project']['name']} › {tl['name']}:"]
    lines += ["> " + line for line in (call.text or "(no note)").splitlines()]
    clips = under(snap, call.frame)
    if clips:
        lines.append("")
        lines.append("Under it: " + " · ".join(f"`{c}`" for c in clips) if len(clips) <= 2 else "Under it:")
        if len(clips) > 2:
            lines += [f"- {c}" for c in clips]
    lines.append("")
    on = f" on {machine}" if machine else ""
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
