"""The extension end to end: `python -m pointman_resolve` run as the node runs it, spoken to over
JSON-RPC on stdin/stdout by a stand-in node. Resolve is made to look absent (RESOLVE_APP points
nowhere), so nothing here reaches the real Resolve; the snapshots come from the fake one.

Needs the node's runtime module, pointman_extension.py: $POINTMAN_EXTENSION_SDK, or the one the
node installed under ~/Library/Application Support/message-board/agent/sdk/."""

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import fake
from pointman_resolve.timeline.readonly import ReadOnly
from pointman_resolve.timeline.snapshot import store_dir, write

ROOT = Path(__file__).parents[1]


def sdk() -> Path | None:
    given = os.environ.get("POINTMAN_EXTENSION_SDK")
    if given:
        return Path(given)
    found = sorted(Path.home().glob("Library/Application Support/message-board/agent/sdk/*/pointman_extension.py"),
                   key=lambda p: p.stat().st_mtime)
    return found[-1].parent if found else None


pytestmark = pytest.mark.skipif(sdk() is None, reason="no pointman_extension runtime on this machine")


class Node:
    """Just enough of a node: initialize, start commands, ask methods, collect what comes back."""

    def __init__(self, home: Path, tmp: Path):
        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "LANG", "LC_CTYPE")}
        # Resolve's scripting library resets the process's locale to plain C once it's connected, so
        # files read with the default encoding break on "·" in a timeline's name. An ASCII locale here
        # does the same without Resolve.
        env.update({"POINTMAN_RESOLVE_HOME": str(home), "RESOLVE_APP": str(tmp / "no-resolve.app"),
                    "LC_ALL": "en_US.US-ASCII", "PYTHONPATH": os.pathsep.join([str(sdk()), str(ROOT)])})
        self.p = subprocess.Popen([sys.executable, "-m", "pointman_resolve"], cwd=ROOT, env=env, text=True,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.msgs: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.n = 0

    def _read(self):
        for line in self.p.stdout:
            self.msgs.put(json.loads(line))

    def send(self, method, params, rid=True):
        self.n += 1
        msg = {"jsonrpc": "2.0", "method": method, "params": params, **({"id": f"n{self.n}"} if rid else {})}
        self.p.stdin.write(json.dumps(msg) + "\n")
        self.p.stdin.flush()
        return f"n{self.n}"

    def wait(self, match, timeout=20):
        """The first message `match` accepts; anything else (status events, the extension's own
        requests to core) is answered or set aside."""
        while True:
            m = self.msgs.get(timeout=timeout)
            if m.get("method") == "core.call" or (m.get("id") and m.get("method")):
                self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": m["id"], "error": {"message": "no method"}}) + "\n")
                self.p.stdin.flush()
                continue
            if match(m):
                return m

    def command(self, kind, params, cid):
        self.send("command.start", {"id": cid, "kind": kind, "params": params, "work": str(Path(self.work) / cid)})
        m = self.wait(lambda m: m.get("method") in ("done", "failed") and m["params"]["id"] == cid)
        return m["method"], m["params"]

    def close(self):
        self.send("shutdown", {})
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            self.p.kill()


@pytest.fixture
def node(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("POINTMAN_RESOLVE_HOME", str(home))
    # two snapshots of the fake timeline, the second after a ripple and a razor cut
    tl = fake.sample_timeline()
    folder = store_dir("feat-flow-with-spacesheep", "Stage 1 · silences out")
    write(ReadOnly(fake.Resolve(tl)), folder)
    cam = tl.tracks[("video", 2)]
    cam[0].frames = 60                                   # trimmed: the head keeps its id
    cam.insert(1, fake.Item("v2piece", 90060, 30, 570, cam[0].media))
    write(ReadOnly(fake.Resolve(tl)), folder)
    n = Node(home, tmp_path)
    n.work = tmp_path / "work"
    n.send("initialize", {"protocol": 1, "settings": {"markers": False}, "work_dir": str(n.work),
                          "state_dir": str(tmp_path / "state"), "roots": []})
    n.wait(lambda m: m.get("id") == "n1")
    yield n
    n.close()


def test_diff_the_latest_two(node):
    state, result = node.command("resolve.diff", {}, "c1")
    assert state == "done", result
    r = result["result"]
    assert r["counts"]["trimmed"] == 1 and r["counts"]["new"] == 1
    assert r["new"][0]["id"] == "v2piece" and r["new"][0]["parent"] == "v2a" and r["new"][0]["parent_kept"] is True
    assert Path(r["file"]).exists() and r["truncated"] is False


def test_check_the_latest(node):
    state, result = node.command("resolve.check", {}, "c2")
    assert state == "done", result
    assert result["result"]["ok"], result["result"]["problems"]


def test_snapshots_are_named_never_paths(node):
    state, result = node.command("resolve.check", {"snapshot": "../../etc/passwd"}, "c3")
    assert state == "failed" and "isn't a snapshot name" in result["error"]


def test_snapshot_waits_while_resolve_is_away(node):
    state, result = node.command("resolve.snapshot", {}, "c4")
    assert state == "failed" and result["retry"] is True and "installed" in result["error"]


def test_health_says_resolve_is_stopped(node):
    for _ in range(40):  # until the lane's first resolve.status has run
        rid = node.send("health.check", {})
        [part] = node.wait(lambda m: m.get("id") == rid)["result"]
        if part["state"] != "unknown":
            break
        time.sleep(0.25)
    assert part["id"] == "resolve" and part["state"] == "stopped" and "installed" in part["why"]
