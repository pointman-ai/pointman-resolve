"""The extension end to end: `python -m pointman_resolve` run as the node runs it, by the SDK's stand-in node
(pointman_extension_testing), speaking the typed protocol. Resolve is made to look absent (RESOLVE_APP points
nowhere), so nothing here reaches the real Resolve; the snapshots come from the fake one.

Needs the node's Python SDK (pointman_extension and its stand-in): $POINTMAN_EXTENSION_SDK, or the one the
node installed under ~/Library/Application Support/message-board/agent/sdk/."""

import os
import sys
import time
from pathlib import Path

import pytest

import fake
from pointman_resolve.timeline.readonly import ReadOnly
from pointman_resolve.timeline.snapshot import store_dir, write

ROOT = Path(__file__).parents[1]


def sdk() -> Path | None:
    """The SDK's folder, if it speaks the typed protocol (it has pointman_extension_types); an older node's is skipped."""
    given = os.environ.get("POINTMAN_EXTENSION_SDK") or os.environ.get("POINTMAN_EXTENSIONS_SDK")
    if given:
        return Path(given) if (Path(given) / "pointman_extension_types.py").exists() else None
    found = sorted(Path.home().glob("Library/Application Support/message-board/agent/sdk/*/pointman_extension_types.py"),
                   key=lambda p: p.stat().st_mtime)
    return found[-1].parent if found else None


if sdk() is not None:
    sys.path.insert(0, str(sdk()))
    import pointman_extension_types as types  # noqa: E402
    from pointman_extension_testing import ExtensionFailed, StandInNode  # noqa: E402

pytestmark = pytest.mark.skipif(sdk() is None, reason="no pointman_extension runtime on this machine")


class Node:
    """The SDK's stand-in node, with what these tests ask of it: a command as (state, what came back)."""

    def __init__(self, tmp: Path, env: dict, methods: dict | None = None, settings: dict | None = None):
        # Resolve's scripting library resets the process's locale to plain C once it's connected, so
        # files read with the default encoding break on "·" in a timeline's name. An ASCII locale here
        # does the same without Resolve.
        env = {**{k: v for k, v in os.environ.items() if k.startswith("POINTMAN_RESOLVE_")},   # a test's own
               "LC_ALL": "en_US.US-ASCII", "LANG": "C", "PYTHONUTF8": "0",
               "PYTHONPATH": os.pathsep.join([str(ROOT), str(sdk()), str(ROOT / "tests")]), **env}
        self.standin = StandInNode(ROOT, settings=settings or {"markers": False}, state_dir=tmp / "node", env=env,
                                   methods=methods or {})
        self.standin.start()
        self.n = 0

    def command(self, kind: str, params: dict, cid: str, timeout: float = 60) -> tuple[str, dict]:
        self.n += 1
        try:
            done = self.standin.run(kind, params, cid=cid, timeout=timeout)
        except ExtensionFailed as e:
            return "failed", {"error": str(e), "retry": e.retry}
        return "done", {"result": done.result}

    def close(self):
        self.standin.stop()


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
    n = Node(tmp_path, {"POINTMAN_RESOLVE_HOME": str(home), "RESOLVE_APP": str(tmp_path / "no-resolve.app")})
    yield n
    n.close()


def test_notes_are_read_as_it_starts_then_only_when_the_node_says_they_changed(tmp_path):
    """Core pushes its notes (node 0.4.6): the markers' rounds read them once, then again only after notes.changed."""
    reads = []

    def core_call(req):
        if req.method == "GET" and "include_deleted" in req.path:      # the markers' read, not publishes()'s
            reads.append(req.path)
        return types.CoreCallResponse(status=200, body=[])

    n = Node(tmp_path, {"POINTMAN_RESOLVE_HOME": str(tmp_path / "home"), "RESOLVE_APP": str(tmp_path / "no-resolve.app"),
                        "POINTMAN_RESOLVE_NOTES_EVERY": "0.2"},
             methods={"core.call": core_call}, settings={"markers": True})
    try:
        for _ in range(100):
            if reads:
                break
            time.sleep(0.1)
        time.sleep(1.5)                                    # rounds every 0.2 s: none of them reads again
        assert len(reads) == 1, reads
        n.standin.notify(types.NotesChangedRequest())
        for _ in range(100):
            if len(reads) > 1:
                break
            time.sleep(0.1)
        time.sleep(0.5)
        assert len(reads) == 2, reads
    finally:
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


def test_shadow_and_frame_checks_wait_while_resolve_is_away(node):
    for i, (kind, params) in enumerate((("resolve.shadow", {"from": "1:00", "to": "1:10"}),
                                        ("resolve.frame_check", {"frame": 100}), ("resolve.calibrate", {}))):
        state, result = node.command(kind, params, f"w{i}")
        assert state == "failed" and result["retry"] is True and "installed" in result["error"], (kind, result)


def test_health_says_resolve_is_stopped(node):
    for _ in range(40):  # until the lane's first resolve.status has run
        [part] = node.standin.call(types.HealthCheckRequest()).parts
        if part.state != types.Health.Part.State.STATE_UNSPECIFIED:
            break
        time.sleep(0.25)
    assert part.id == "resolve" and part.state == types.Health.Part.State.STATE_STOPPED and "installed" in part.why
