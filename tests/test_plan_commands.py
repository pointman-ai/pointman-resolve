"""Edit plans through the extension as the node runs it: resolve.plan, resolve.apply and resolve.undo on
the fake Resolve (writable here), behind the bridge's fake mode."""

import json
from pathlib import Path

import pytest

from test_extension import sdk
from test_marker_calls import start, until

pytestmark = pytest.mark.skipif(sdk() is None, reason="no pointman_extension runtime on this machine")

STANDIN = '''
import fake
_tl = fake.sample_timeline()
_tl.tracks[("video", 2)][0].markers = {}
_tl.tracks[("video", 5)][0].tools = {1: ["Primary Balance"]}
_tl.writable = True
_resolve = fake.Resolve(_tl)
def scriptapp(name):
    return _resolve
'''

SLIP = [{"op": "slip", "items": ["v2a"], "by": -3, "follow": ["V3", "V5"], "note": "re-sync C0030"}]


def lane(n, kind, track):
    state, r = n.command("resolve.snapshot", {"otio": False}, f"snap-{kind}{track}-{id(n)}-{len(n.published)}-{n.n}")
    assert state == "done", r
    snap = json.loads(Path(r["result"]["snapshot"]).read_text(encoding="utf-8"))
    return [(i["record"], i["frames"], i["in"], (i.get("grade") or {}).get("graded"))
            for i in snap["items"] if i["kind"] == kind and i["track"] == track]


def test_plan_apply_and_undo(tmp_path):
    n = start(tmp_path, read_posts=False, standin=STANDIN)
    try:
        state, r = n.command("resolve.plan", {"ops": SLIP, "note": "re-sync C0030"}, "plan-1")
        assert state == "done" and r["result"]["ok"], r
        res = r["result"]
        pid = res["plan"]
        assert res["counts"]["remove"] == 3 and res["counts"]["graded"] == 1
        assert res["frames_changed"] == {"op 1": {"V2": 100, "V3": 100, "V5": 60}}
        assert any("V2 0:00.00 C0030.MP4: source 500 → 497" == s.split(": ", 1)[1] for s in res["steps"]), res["steps"]
        assert lane(n, "video", 2)[0] == (0, 100, 500, False)
        state, r = n.command("resolve.apply", {"plan": pid, "wait": 0.1}, "apply-1")
        assert state == "done" and r["result"]["ok"], r
        assert r["result"]["verify"]["wrong"] == [] and r["result"]["verify"]["checked"] >= 3, r["result"]["verify"]
        assert lane(n, "video", 2)[0] == (0, 100, 497, False) and lane(n, "video", 5) == [(20, 60, 27, True)]
        state, r = n.command("resolve.apply", {"plan": pid, "wait": 0.1}, "apply-2")
        assert state == "done" and r["result"]["state"] == "applied" and "already" in r["result"]["note"]
        state, r = n.command("resolve.undo", {"plan": pid, "wait": 0.1}, "undo-1")
        assert state == "done" and r["result"]["ok"] and r["result"]["state"] == "undone", r
        assert lane(n, "video", 2)[0] == (0, 100, 500, False) and lane(n, "video", 5) == [(20, 60, 30, True)]
    finally:
        n.close()


def test_a_plan_with_problems_says_so_and_wont_apply(tmp_path):
    n = start(tmp_path, read_posts=False, standin=STANDIN)
    try:
        state, r = n.command("resolve.plan", {"ops": [{"op": "slip", "items": ["v3a"], "by": -50}]}, "plan-bad")
        assert state == "done" and not r["result"]["ok"] and "before the file does" in r["result"]["problems"][0]
        state, r = n.command("resolve.apply", {"plan": r["result"]["plan"], "wait": 0.1}, "apply-bad")
        assert state == "failed" and "problems" in json.dumps(r)
        assert lane(n, "video", 3)[0][2] == 10
    finally:
        n.close()


def test_watch_runs_the_checks_on_demand(tmp_path):
    n = start(tmp_path, read_posts=False, standin=STANDIN)
    try:
        (n.home / "routes.toml").write_text(
            '[projects."feat-flow-with-spacesheep"]\nthread = 10399\nagent = "editor"\n'
            '[projects."feat-flow-with-spacesheep".follow]\nroles = { cutout = "V5", graphics = "V6", voice = "A1" }\n',
            encoding="utf-8")
        state, r = n.command("resolve.watch", {"transitions": False}, "watch-1")
        assert state == "done" and r["result"]["configured"], r
        assert r["result"]["roles"]["cutout"] == "V5" and isinstance(r["result"]["findings"], list)
    finally:
        n.close()


@pytest.mark.parametrize("version", [False, True])
def test_the_watcher_leaves_a_version_alone(tmp_path, monkeypatch, version):
    monkeypatch.setenv("POINTMAN_RESOLVE_WATCH_EVERY", "0.3")
    monkeypatch.setenv("POINTMAN_RESOLVE_WATCH_IDLE", "0")
    home = {"versions.json": {"tl-1": {"name": "Stage 1 · v2", "of": "tl-0", "plan": "p1"}}} if version else {}
    n = start(tmp_path, read_posts=False, standin=STANDIN, home=home)
    try:
        (n.home / "routes.toml").write_text(
            '[projects."feat-flow-with-spacesheep"]\nthread = 10399\nagent = "editor"\n'
            '[projects."feat-flow-with-spacesheep".follow]\nroles = { cutout = "V5", graphics = "V6", voice = "A1" }\n',
            encoding="utf-8")
        checked = until(lambda: "tl-1" in json.loads((n.home / "watch.json").read_text(encoding="utf-8"))
                        if (n.home / "watch.json").exists() else False, timeout=6)
        assert checked is not version                     # the person's timeline is checked; a copy of it isn't
    finally:
        n.close()


def test_a_version_needs_ok_and_leaves_the_timeline_as_it_was(tmp_path):
    n = start(tmp_path, read_posts=False, standin=STANDIN)
    try:
        state, r = n.command("resolve.plan", {"ops": [{"op": "slip", "items": ["v3a"], "by": 2}]}, "plan-v")
        pid = r["result"]["plan"]
        state, r = n.command("resolve.version", {"plan": pid, "ok": "", "wait": 0.1}, "v-no")
        assert state == "failed" and "person's ok" in json.dumps(r)
        state, r = n.command("resolve.version", {"plan": pid, "ok": "James, post 15400", "wait": 0.1}, "v-1")
        assert state == "done" and r["result"]["ok"] and r["result"]["back"]["timeline"], r
        assert lane(n, "video", 3)[0][2] == 10                       # the open timeline (his) didn't change
        assert r["result"]["version"].startswith("Stage 1 · silences out · v2")
        state, r = n.command("resolve.version", {"plan": pid, "ok": "again", "wait": 0.1}, "v-2")
        assert state == "failed" and "already has a version" in json.dumps(r)
        state, r = n.command("resolve.undo", {"plan": pid, "wait": 0.1}, "v-undo")   # undo knows nothing was applied
        assert state == "done" and "nothing of this plan was applied" in r["result"]["note"], r
        state, r = n.command("resolve.drop_version", {"plan": pid}, "v-drop")
        assert state == "done" and r["result"]["ok"] and r["result"]["dropped"].startswith("Stage 1"), r
    finally:
        n.close()
