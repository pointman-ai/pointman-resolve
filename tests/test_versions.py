"""Versions (PFE-12): a plan applied to a copy of the timeline for the person to A/B, theirs left as it was."""

import pytest

import fake
from pointman_resolve.plans import apply as A
from pointman_resolve.plans import versions as V
from pointman_resolve.plans.model import PlanError
from pointman_resolve.timeline.readonly import ReadOnly
from pointman_resolve.timeline.snapshot import take
from test_plans import lanes, sample


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POINTMAN_RESOLVE_HOME", str(tmp_path / "home"))


def made(tmp_path, plan):
    tl = sample()
    snap = take(ReadOnly(fake.Resolve(tl)))[0]
    tl.writable = True
    resolve = fake.Resolve(tl)
    rec = A.Record(tmp_path / "version.json")
    out = V.make(resolve, snap, plan, "Stage 1 · v2", rec, sleep=lambda s: None)
    return tl, resolve, rec, out


def test_a_version_gets_the_change_and_the_persons_timeline_doesnt(tmp_path):
    before = lanes(sample())
    tl, resolve, rec, out = made(tmp_path, {"ops": [{"op": "slip", "items": ["v3a"], "by": 2}]})
    project = resolve.pm.project
    assert project.GetCurrentTimeline() is tl and out["back"]["timeline"] and out["back"]["playhead"]
    assert lanes(tl) == before                                    # theirs: untouched
    copy = next(t for t in project.timelines if t is not tl)
    assert copy.name == "Stage 1 · v2" and lanes(copy)["V3"][0] == (0, 100, 12, "C0030-f009844.mov")
    assert V.is_version(copy.uid) and not V.is_version(tl.uid)    # the marker watcher skips the copy
    assert rec.data["version"]["id"] == copy.uid


def test_an_id_with_no_single_match_in_the_copy_refuses_the_version():
    snap = {"items": [{"id": "a", "kind": "video", "track": 1, "record": 0, "frames": 10, "in": 0, "file": "x", "type": "video"}]}
    dup = {"items": [dict(snap["items"][0], id="b1"), dict(snap["items"][0], id="b2")]}
    assert V.match_ids(snap, dup) == {}
    with pytest.raises(PlanError, match="no single match"):
        V.rewrite({"ops": [{"op": "delete", "items": ["a"]}]}, V.match_ids(snap, dup))


def test_drop_only_an_unedited_version_that_isnt_open(tmp_path):
    tl, resolve, rec, out = made(tmp_path, {"ops": [{"op": "set", "items": ["v3a"], "color": "Orange"}]})
    project = resolve.pm.project
    copy = next(t for t in project.timelines if t is not tl)
    project.SetCurrentTimeline(copy)
    with pytest.raises(A.Stop, match="is open"):
        V.drop(resolve, copy.uid)
    project.SetCurrentTimeline(tl)
    copy.tracks[("video", 3)][0].frames = 90                      # James trims something in the version
    with pytest.raises(A.Stop, match="edited since"):
        V.drop(resolve, copy.uid)
    copy.tracks[("video", 3)][0].frames = 100
    assert V.drop(resolve, copy.uid)["dropped"] == "Stage 1 · v2"
    assert copy not in project.timelines and not V.is_version(copy.uid)


def test_a_taken_name_gets_a_number(tmp_path):
    tl, resolve, rec, out = made(tmp_path, {"ops": [{"op": "set", "items": ["v3a"], "color": "Orange"}]})
    assert V.free_name(resolve.pm.project, "Stage 1 · v2") == "Stage 1 · v2 (2)"


def test_spans_map_onto_the_timeline_before_the_plan_and_after_it():
    from pointman_resolve.plans.compile import to_base, to_final
    ripples = [(0, 150, 160), (1, 30, 60)]                    # a cut's ranges, the last one first
    # group 1 (30–60) saw 150–160 already cut, so its frame 100 was 100 then too (before the 150 cut)
    assert to_base(100, 1, ripples) == 100 and to_base(170, 1, ripples) == 180
    assert to_base(150, 1, ripples, end=True) == 150          # an end at the cut stays before its gap
    # group 0 (150–160) left frame 170; the later cut at 30–60 pulls it 30 earlier
    assert to_final(170, 0, ripples) == 140 and to_final(45, 0, ripples) == 30 and to_final(10, 0, ripples) == 10
    assert to_final(170, 1, ripples) == 170                   # nothing was cut after group 1
