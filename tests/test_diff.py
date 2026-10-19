import copy

import pytest

from pointman_resolve.timeline import journal
from pointman_resolve.timeline.diff import diff


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POINTMAN_RESOLVE_HOME", str(tmp_path / "home"))


def item(uid, track, record, frames, in_, file="/raw/C0030.MP4", kind="video", **kw):
    return {"id": uid, "kind": kind, "track": track, "record": record, "frames": frames, "in": in_, "file": file,
            "name": file.rsplit("/", 1)[-1], "type": kind, "props": kw.get("props", {}), "fades": None, "speed": None,
            "enabled": True, "color": "", "nodes": 1, "markers": []}


def snap(items, taken="2026-10-19T01:00:00.000000Z", markers=None, journal_entries=None):
    return {"taken": taken, "timeline": {"id": "tl-1", "name": "Stage 1", "end_frame": 1000, "fps": 25.0},
            "tracks": [], "items": items, "markers": markers or [], "journal": journal_entries or []}


A_ITEMS = [item("cam1", 2, 0, 331, 755), item("cam2", 2, 331, 119, 1108), item("cam3", 2, 450, 100, 2000),
           item("card", 6, 0, 131, 0, file="/graphics/o02.mov"), item("del", 3, 600, 50, 10, file="/loft.mov")]


def test_identical_snapshots_are_all_kept():
    a = snap(A_ITEMS)
    d = diff(a, copy.deepcopy(a))
    assert d["counts"] == {"kept": 5, "changed": 0, "moved": 0, "trimmed": 0, "new": 0, "gone": 0,
                           "new_ours": 0, "gone_ours": 0}


def test_james_razor_with_ripple_deletes():
    """What PFE-9 saw at 10:04: a clip cut in three with two small bits ripple-deleted. The head keeps its id,
    the pieces get new ids, and everything after shifts left."""
    a = snap(A_ITEMS)
    b = snap([item("cam1", 2, 0, 159, 755),                      # head kept, trimmed
              item("p1", 2, 159, 73, 929), item("p2", 2, 232, 70, 1016),  # pieces after the cuts
              item("cam2", 2, 302, 107, 1108),                   # ripple + tail trim
              item("cam3", 2, 409, 100, 2000),                   # ripple only
              item("card", 6, 0, 131, 0, file="/graphics/o02.mov"),
              item("del", 3, 600, 50, 10, file="/loft.mov")], taken="2026-10-19T01:05:00.000000Z")
    d = diff(a, b)
    assert d["counts"]["kept"] == 2 and d["counts"]["new"] == 2 and d["counts"]["gone"] == 0
    assert {t["id"] for t in d["trimmed"]} == {"cam1", "cam2"}
    assert [(m["id"], m["from"]["record"], m["to"]["record"]) for m in d["moved"]] == [("cam3", 450, 409)]
    assert [(n["id"], n["parent"], n["parent_kept"]) for n in d["new"]] == [("p1", "cam1", True), ("p2", "cam1", True)]


def test_head_deleted_then_cut_leaves_pieces_on_a_gone_parent():
    a = snap(A_ITEMS)
    b = snap([i for i in A_ITEMS if i["id"] != "card"] +
             [item("c1", 6, 0, 36, 12, file="/graphics/o02.mov"), item("c2", 6, 36, 83, 48, file="/graphics/o02.mov")])
    d = diff(a, b)
    gone = {g["id"]: g for g in d["gone"]}
    assert gone["card"]["pieces"] == ["c1", "c2"]
    assert all(n["parent"] == "card" and n["parent_kept"] is False for n in d["new"])


def test_a_new_clip_has_no_parent_and_a_delete_is_gone():
    a = snap(A_ITEMS)
    b = snap([i for i in A_ITEMS if i["id"] != "del"] + [item("fresh", 4, 0, 50, 0, file="/raw/screen.mp4")])
    d = diff(a, b)
    assert [(n["id"], n["parent"]) for n in d["new"]] == [("fresh", None)]
    assert [(g["id"], g["pieces"]) for g in d["gone"]] == [("del", [])]


def test_changed_props_in_place():
    a = snap(A_ITEMS)
    b = copy.deepcopy(a)
    b["items"][2]["props"] = {"ZoomX": 1.15}
    d = diff(a, b)
    assert [(c["id"], c["fields"]) for c in d["changed"]] == [("cam3", ["props"])]


def test_moving_to_another_track_is_a_move():
    a = snap(A_ITEMS)
    b = copy.deepcopy(a)
    b["items"][4]["track"] = 4
    d = diff(a, b)
    assert [(m["id"], m["from"]["track"], m["to"]["track"]) for m in d["moved"]] == [("del", 3, 4)]


def test_markers_new_changed_gone_moved():
    m = lambda f, name, note="": {"frame": f, "color": "Red", "name": name, "note": note, "duration": 1, "custom": ""}
    a = snap(A_ITEMS, markers=[m(10, "a"), m(20, "b"), m(30, "c"), m(40, "d")])
    b = snap(A_ITEMS, markers=[m(10, "a", "now with a note"), m(25, "b"), m(50, "@pm fix this")])
    d = diff(a, b)["markers"]
    assert [c["frame"] for c in d["changed"]] == [10]
    assert [(x["from"], x["to"]) for x in d["moved"]] == [(20, 25)]
    assert [x["name"] for x in d["new"]] == ["@pm fix this"]
    assert sorted(x["name"] for x in d["gone"]) == ["c", "d"]


def test_ours_from_the_journal_marks_our_relays():
    """Our gap-fix re-lay: delete + re-append gives new ids; the journal says they're ours."""
    a = snap(A_ITEMS, taken="2026-10-19T01:00:00.000000Z")
    embedded = [{"time": "2026-10-19T01:02:00.000000Z", "tool": "fix_gaps", "removed": ["cam3"], "added": ["cam3b"]}]
    b = snap([i for i in A_ITEMS if i["id"] != "cam3"] + [item("cam3b", 2, 450, 100, 2000)],
             taken="2026-10-19T01:05:00.000000Z", journal_entries=embedded)
    journal.record("place_cutouts", added=["outside-window"], timeline="tl-1")  # after B was taken: not counted
    d = diff(a, b)
    assert d["ours"]["added"] == ["cam3b"] and d["ours"]["removed"] == ["cam3"]
    assert [(n["id"], n["ours"]) for n in d["new"]] == [("cam3b", True)]
    assert [(g["id"], g["ours"]) for g in d["gone"]] == [("cam3", True)]
    assert d["counts"]["new_ours"] == 1 and d["counts"]["gone_ours"] == 1


def test_different_timelines_are_flagged():
    a, b = snap(A_ITEMS), snap(A_ITEMS)
    b["timeline"] = {"id": "tl-2", "name": "Stage 2", "end_frame": 1000, "fps": 25.0}
    assert "different timelines" in diff(a, b)["note"]
