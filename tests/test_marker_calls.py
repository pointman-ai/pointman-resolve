"""Markers that call an agent, end to end: the extension run as the node runs it, with the fake Resolve
behind the bridge's fake mode (a stand-in DaVinciResolveScript) and a stand-in node that answers
files.publish (the post) and core.call (reading the post back, or refusing as today's node does)."""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from test_extension import ROOT, sdk

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


class Node:
    def __init__(self, tmp: Path, read_posts: bool):
        api = tmp / "api"
        (api / "Modules").mkdir(parents=True)
        (api / "Modules" / "DaVinciResolveScript.py").write_text(STANDIN, encoding="utf-8")
        (tmp / "Resolve.app").mkdir()
        (tmp / "fusionscript.so").write_text("")
        self.home = tmp / "home"
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "LANG", "LC_CTYPE")}
        env.update({"POINTMAN_RESOLVE_HOME": str(self.home), "MB_RESOLVE_FAKE": "1", "RESOLVE_SCRIPT_API": str(api),
                    "RESOLVE_SCRIPT_LIB": str(tmp / "fusionscript.so"), "RESOLVE_APP": str(tmp / "Resolve.app"),
                    "POINTMAN_RESOLVE_MARK_EVERY": "0.5", "POINTMAN_RESOLVE_MARK_POLL": "0",
                    "LC_ALL": "en_US.US-ASCII",
                    "PYTHONPATH": os.pathsep.join([str(sdk()), str(ROOT), str(ROOT / "tests")])})
        self.p = subprocess.Popen([sys.executable, "-m", "pointman_resolve"], cwd=ROOT, env=env, text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.read_posts, self.published, self.ticked = read_posts, [], False
        self.msgs: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.n = 0

    def _answer(self, m):
        method, rid, p = m["method"], m["id"], m.get("params") or {}
        if method == "files.publish":
            self.published.append(p["body"])
            result = {"asset_id": 1, "version_id": 1, "number": 1, "deduped": False, "message_id": 777}
        elif method == "core.call" and str(p.get("path")).startswith("/v1/node/messages/"):
            if not self.read_posts:                    # today's node: not on the extension allowlist
                return {"jsonrpc": "2.0", "id": rid, "error": {"message": f"resolve may not call GET {p['path']} on core"}}
            if p["path"] != "/v1/node/messages/777":
                return {"jsonrpc": "2.0", "id": rid, "result": {"status": 404, "body": {"detail": "no such message"}}}
            emoji = [{"emoji": "✅", "count": 1, "handles": ["editor"]}] if self.ticked else []
            result = {"status": 200, "body": {"id": 777, "reactions": emoji}}
        elif method == "core.call":
            result = {"status": 200, "body": []}       # the notes the old sync asks for: none
        else:
            return {"jsonrpc": "2.0", "id": rid, "error": {"message": f"no method {method}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def _read(self):
        for line in self.p.stdout:
            m = json.loads(line)
            if m.get("method") and m.get("id"):        # the extension asking the node something
                self.p.stdin.write(json.dumps(self._answer(m)) + "\n")
                self.p.stdin.flush()
            else:
                self.msgs.put(m)

    def send(self, method, params):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": f"n{self.n}", "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        return f"n{self.n}"

    def wait(self, match, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                m = self.msgs.get(timeout=0.5)
            except queue.Empty:
                continue
            if match(m):
                return m
        raise TimeoutError

    def command(self, kind, params, cid):
        self.send("command.start", {"id": cid, "kind": kind, "params": params, "work": str(self.work / cid)})
        m = self.wait(lambda m: m.get("method") in ("done", "failed") and m["params"]["id"] == cid)
        return m["method"], m["params"]

    def marker(self, frame):
        """The timeline marker at `frame`, as the fake Resolve has it now (read through a snapshot)."""
        state, r = self.command("resolve.snapshot", {"otio": False}, f"snap-{time.time()}")
        assert state == "done", r
        snap = json.loads(Path(r["result"]["snapshot"]).read_text(encoding="utf-8"))
        return next(m for m in snap["markers"] if m["frame"] == frame)

    def close(self):
        self.send("shutdown", {})
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()


def start(tmp_path, read_posts):
    n = Node(tmp_path, read_posts)
    n.work = tmp_path / "work"
    n.send("initialize", {"protocol": 1, "settings": {"markers": False}, "work_dir": str(n.work),
                          "state_dir": str(tmp_path / "state"), "roots": []})
    return n


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
    assert "React ✅" in n.published[0]["post"]["text"]
    assert until(lambda: n.marker(300)["color"] == "Cyan")
    n.ticked = True                                               # @editor reacts ✅
    assert until(lambda: n.marker(300)["color"] == "Green"), "the ✅ didn't turn it green"
