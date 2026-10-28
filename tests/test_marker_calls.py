"""Markers that call an agent, end to end: the extension run as the node runs it, with the fake Resolve
behind the bridge's fake mode (a stand-in DaVinciResolveScript) and a stand-in node that answers
files.publish (the post) and core.call (reading the post back, or refusing as today's node does)."""

import json
import time
from pathlib import Path

import pytest

from test_extension import Node as BaseNode
from test_extension import sdk

if sdk() is not None:
    import pointman_extension_types as types

pytestmark = pytest.mark.skipif(sdk() is None, reason="no pointman_extension runtime on this machine")

STANDIN = '''
import fake
_tl = fake.sample_timeline()
_tl.markers[300] = {"color": "Red", "name": "@editor this cut jumps", "note": "James wrote this", "duration": 1, "customData": ""}
_tl.markers[400] = {"color": "Blue", "name": "check lip sync", "note": "", "duration": 1, "customData": ""}
_resolve = fake.Resolve(_tl)
def scriptapp(name):
    return _resolve
'''


class Node(BaseNode):
    """The stand-in node with a fake Resolve behind the bridge's fake mode, answering files.publish (the post) and
    core.call (reading the post back, or refusing as today's node does)."""

    def __init__(self, tmp: Path, read_posts: bool, project: str | None = None, home: dict | None = None,
                 extra: str = "", standin: str = STANDIN):
        api = tmp / "api"
        (api / "Modules").mkdir(parents=True)
        rename = f"fake.Project.GetName = lambda self: {project!r}\n" if project else ""
        (api / "Modules" / "DaVinciResolveScript.py").write_text(standin + rename + extra, encoding="utf-8")
        (tmp / "Resolve.app").mkdir()
        (tmp / "fusionscript.so").write_text("")
        self.home = tmp / "home"
        self.home.mkdir()
        for name, data in (home or {}).items():                  # what the extension remembers already
            (self.home / name).write_text(json.dumps(data), encoding="utf-8")
        self.read_posts, self.published, self.ticked = read_posts, [], False
        super().__init__(tmp, {"POINTMAN_RESOLVE_HOME": str(self.home), "MB_RESOLVE_FAKE": "1",
                               "RESOLVE_SCRIPT_API": str(api), "RESOLVE_SCRIPT_LIB": str(tmp / "fusionscript.so"),
                               "RESOLVE_APP": str(tmp / "Resolve.app"), "POINTMAN_RESOLVE_MARK_EVERY": "0.5",
                               "POINTMAN_RESOLVE_MARK_POLL": "0", "POINTMAN_RESOLVE_HEAL_IDLE": "0"},
                         methods={"files.publish": self.files_publish, "core.call": self.core_call})

    def files_publish(self, req):
        self.published.append(req.body)
        return types.FilesPublishResponse(file=types.FileRef(id="file_1"), version=types.FileVersionRef(id="ver_1"),
                                          post=types.PostRef(id=f"post_{776 + len(self.published)}"))

    def core_call(self, req):
        if req.path.startswith("/v1/node/messages/"):
            if not self.read_posts:                    # today's node: not on the extension allowlist
                raise RuntimeError(f"resolve may not call GET {req.path} on core")
            if req.path != "/v1/node/messages/777":
                return types.CoreCallResponse(status=404, body={"detail": "no such message"})
            emoji = [{"emoji": "✅", "count": 1, "handles": ["editor"]}] if self.ticked else []
            return types.CoreCallResponse(status=200, body={"id": 777, "reactions": emoji})
        return types.CoreCallResponse(status=200, body=[])        # the notes the old sync asks for: none

    def marker(self, frame):
        """The timeline marker at `frame`, as the fake Resolve has it now (read through a snapshot)."""
        state, r = self.command("resolve.snapshot", {"otio": False}, f"snap-{time.time_ns()}")
        assert state == "done", r
        snap = json.loads(Path(r["result"]["snapshot"]).read_text(encoding="utf-8"))
        return next(m for m in snap["markers"] if m["frame"] == frame)


def start(tmp_path, read_posts, **kw):
    return Node(tmp_path, read_posts, **kw)


def until(check, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return True
        time.sleep(0.2)
    return False


@pytest.fixture
def node_refusing(tmp_path):
    n = start(tmp_path, read_posts=False)
    yield n
    n.close()


@pytest.fixture
def node_reading(tmp_path):
    n = start(tmp_path, read_posts=True)
    yield n
    n.close()


def test_a_marker_that_calls_an_agent_is_posted_once_and_turns_cyan(node_refusing):
    n = node_refusing
    assert until(lambda: n.published), "nothing was posted"
    body = n.published[0]
    assert body["post"]["thread_id"] == 10399 and body["post"]["text"].startswith("@editor Resolve marker at **0:12.00**")
    assert "> @editor this cut jumps\n> James wrote this" in body["post"]["text"]
    assert "React ✅" not in body["post"]["text"] and "resolve.marker_done" in body["post"]["text"]
    assert body["asset"]["path"].startswith("resolve/feat-flow-with-spacesheep/Stage 1 · silences out/markers/300-")
    m = n.marker(300)
    assert (m["color"], m["custom"], m["name"], m["note"]) == ("Cyan", "pm:777", "@editor this cut jumps", "James wrote this")
    assert n.marker(400)["color"] == "Blue"                      # not a call: untouched
    time.sleep(1.5)                                               # a few more rounds
    assert len(n.published) == 1                                 # posted once
    fb = json.loads((n.home / "fallback.json").read_text(encoding="utf-8"))
    assert (fb["thread"], fb["agent"], fb["by"]) == (10399, "editor", "marker")   # where unrouted ones go next


def test_marker_done_turns_it_green_where_posts_cant_be_read(node_refusing):
    n = node_refusing
    assert until(lambda: n.published)
    until(lambda: n.marker(300)["color"] == "Cyan")
    state, r = n.command("resolve.marker_done", {"post": 777, "by": "editor"}, "done-1")
    assert state == "done" and r["result"]["marker"] == "ok", r
    m = n.marker(300)
    assert (m["color"], m["custom"], m["note"]) == ("Green", "pm:777", "James wrote this")


def test_a_tick_from_the_agent_turns_it_green_where_posts_can_be_read(node_reading):
    n = node_reading
    assert until(lambda: n.published)
    # no post of ours to ask about yet, so the first one says marker_done; polling settles it after
    assert "resolve.marker_done" in n.published[0]["post"]["text"]
    assert until(lambda: n.marker(300)["color"] == "Cyan")
    n.ticked = True                                               # @editor reacts ✅
    assert until(lambda: n.marker(300)["color"] == "Green"), "the ✅ didn't turn it green"


def test_an_unrouted_project_waits_for_a_fallback_then_posts_there(tmp_path):
    n = start(tmp_path, read_posts=False, project="scratch")
    try:
        time.sleep(2)                                             # a few rounds: no route, no fallback
        assert not n.published and n.marker(300)["custom"] == "" and n.marker(300)["color"] == "Red"
        state, r = n.command("resolve.set_fallback", {}, "fb-0")
        assert state == "done" and r["result"]["fallback"] is None, r
        state, r = n.command("resolve.set_fallback", {"thread": 13661, "agent": "@editor", "by": "editor"}, "fb-1")
        assert state == "done" and r["result"]["fallback"]["thread"] == 13661 and r["result"]["was"] is None, r
        assert until(lambda: n.published), "the fallback didn't let it post"
        post = n.published[0]["post"]
        assert post["thread_id"] == 13661 and post["text"].startswith("@editor Resolve marker at")
        assert "Project “scratch” has no route, so it came to this thread" in post["text"]
        assert until(lambda: n.marker(300)["color"] == "Cyan")
        for i, bad in enumerate(({"thread": "x"}, {"thread": 0}, {"agent": "edi tor"})):
            state, r = n.command("resolve.set_fallback", bad, f"fb-bad-{i}")
            assert state == "failed", (bad, r)
        assert json.loads((n.home / "fallback.json").read_text(encoding="utf-8"))["agent"] == "editor"
    finally:
        n.close()


def test_a_renamed_project_keeps_its_route(tmp_path):
    n = start(tmp_path, read_posts=False, project="Spacesheep v1 (renamed)",
              home={"projects.json": {"feat-flow-with-spacesheep": {"id": "pr-1", "timelines": []}},
                    "fallback.json": {"thread": 13661, "agent": "rtx-render"}})
    try:
        assert until(lambda: n.published)
        post = n.published[0]["post"]
        assert post["thread_id"] == 10399 and post["text"].startswith("@editor Resolve marker at")
        assert "it has the same project ID as “feat-flow-with-spacesheep” (renamed?)" in post["text"]
        assert until(lambda: json.loads((n.home / "fallback.json").read_text(encoding="utf-8"))["thread"] == 10399)
    finally:
        n.close()


# James's ripple moves the marker while its post is out (a Wi-Fi drop made the post slow): marker 59
MOVES_WHILE_POSTING = """
_update = fake.Timeline.UpdateMarkerCustomData
def _moving(self, frame, custom):
    ok = _update(self, frame, custom)
    if ok and custom.startswith("pm:claim-") and int(frame) == 300:
        self.markers[200] = self.markers.pop(300)
    return ok
fake.Timeline.UpdateMarkerCustomData = _moving
"""


def test_a_marker_moved_while_its_post_is_out_is_posted_once(tmp_path):
    n = start(tmp_path, read_posts=False, extra=MOVES_WHILE_POSTING)
    try:
        assert until(lambda: n.published), "nothing was posted"
        assert until(lambda: n.marker(200)["color"] == "Cyan"), "not tagged where it moved to"
        m = n.marker(200)
        assert (m["custom"], m["name"]) == ("pm:777", "@editor this cut jumps")
        time.sleep(1.5)                                           # a few more rounds
        assert len(n.published) == 1, f"posted {len(n.published)} times"
    finally:
        n.close()


# an undo in Resolve takes back our delete + add: the marker is tagged but back at its own colour
UNDONE_ONCE = """
_add = fake.Timeline.AddMarker
_undone = []
def _undo_first_cyan(self, frame, color, name, note, duration, custom=""):
    if color == "Cyan" and not _undone:
        _undone.append(frame)
        return _add(self, frame, "Red", name, note, duration, custom)
    return _add(self, frame, color, name, note, duration, custom)
fake.Timeline.AddMarker = _undo_first_cyan
"""


def test_a_marker_back_at_its_own_colour_gets_ours_again(tmp_path):
    n = start(tmp_path, read_posts=False, extra=UNDONE_ONCE)
    try:
        assert until(lambda: n.published)
        assert until(lambda: n.marker(300)["color"] == "Cyan"), "the reverted colour wasn't healed"
        assert n.marker(300)["custom"] == "pm:777" and len(n.published) == 1
    finally:
        n.close()


