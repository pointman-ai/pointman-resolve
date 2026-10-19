"""Edit plans (PFE-11): compiled against snapshots of the fake timeline, then applied to it and undone."""

import pytest

import fake
from pointman_resolve.plans import apply as A
from pointman_resolve.plans.compile import compile_plan
from pointman_resolve.timeline.readonly import ReadOnly
from pointman_resolve.timeline.snapshot import take


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POINTMAN_RESOLVE_HOME", str(tmp_path / "home"))


def snap_of(tl):
    return take(ReadOnly(fake.Resolve(tl)))[0]


def sample():
    """The sample timeline without the clip marker on V2's first clip (clip markers aren't carried yet)."""
    tl = fake.sample_timeline()
    tl.tracks[("video", 2)][0].markers = {}
    return tl


def lanes(tl):
    """Each lane as (record, frames, in, file name) from the start, transitions as ("T", record, frames)."""
    out = {}
    for (kind, k), items in tl.tracks.items():
        out[f"{kind[0].upper()}{k}"] = [("T", i.start - tl.start, i.frames) if i.type == "transition" else
                                        (i.start - tl.start, i.frames, i.left, i.media.path.rsplit("/", 1)[-1])
                                        for i in sorted(items, key=lambda x: (x.start, x.type == "transition"))]
    return out


def run(tl, plan, tmp_path, **kw):
    snap = snap_of(tl)
    tl.writable = True
    rec = A.Record(tmp_path / "applied.json")
    out = A.apply(fake.Resolve(tl), snap, plan, rec, sleep=lambda s: None, **kw)
    return snap, rec, out


def resolve_of(tl):
    r = fake.Resolve(tl)
    r.pm.project.tl = tl
    return r


# ---------------------------------------------------------------- compiling


def test_a_slip_takes_the_pieces_stacked_on_it():
    r = compile_plan(snap_of(sample()), {"ops": [{"op": "slip", "items": ["v2a"], "by": -3,
                                                                 "follow": ["V3", "V5"]}]})
    assert not r["problems"], r["problems"]
    g = r["groups"][0]
    assert sorted(p["id"] for p in g["remove"]) == ["v2a", "v3a", "v5a"]
    lays = {p["from"]: p for p in g["lay"]}
    assert (lays["v2a"]["record"], lays["v2a"]["in"], lays["v3a"]["in"], lays["v5a"]["in"]) == (0, 497, 7, 27)
    assert lays["v3a"]["props"]["ZoomX"] == 1.15 and lays["v5a"]["props"]["CropLeft"] == 520.0
    assert {d["lane"]: (d["changed"], d["outside"]) for d in r["frames"]} == {"V2": (100, 0), "V3": (100, 0),
                                                                               "V5": (60, 0)}


def test_the_persons_own_tracks_need_an_ok():
    tl = sample()
    tl.names = {("video", 2): "camera"}
    plan = {"ops": [{"op": "slip", "items": ["v2a"], "by": 2}]}
    r = compile_plan(snap_of(tl), plan)
    assert "V2 camera" in r["problems"][0] and "\"ok\"" in r["problems"][0]
    plan["ops"][0]["ok"] = "marker 99"
    assert not compile_plan(snap_of(tl), plan)["problems"]


def test_what_cant_be_done_is_said_before_anything_happens():
    snap = snap_of(sample())
    bad = {
        "the source would start 10 frames before the file does": {"op": "slip", "items": ["v3a"], "by": -20},
        "lands on o02-transformed.mov": {"op": "move", "items": ["v5a"], "to": 0, "track": "V6"},
        "no item nope": {"op": "delete", "items": ["nope"]},
        "needs 4 frames of handle": {"op": "slip", "items": ["v6a"], "by": 2},   # the dissolve to v6b
        "no op 'ripple'": {"op": "ripple"},
    }
    snap_marked = snap_of(fake.sample_timeline())
    assert "has clip markers" in compile_plan(snap_marked, {"ops": [{"op": "slip", "items": ["v2a"], "by": 1}]})["problems"][0]
    for why, op in bad.items():
        problems = compile_plan(snap, {"ops": [op]})["problems"]
        assert problems and why in problems[0], (why, problems)


def test_a_dissolve_goes_back_on_the_same_cut():
    tl = sample()
    tl.tracks[("video", 6)][2].left = 20                    # v6b has a handle now
    r = compile_plan(snap_of(tl), {"ops": [{"op": "slip", "items": ["v6a"], "by": 2}]})
    assert not r["problems"], r["problems"]
    t = r["groups"][0]["transitions"]
    assert len(t) == 1 and t[0]["cut"] == 50 and t[0]["name"] == "Cross Dissolve" and t[0]["right"] == "v6b"


# ---------------------------------------------------------------- applying and undoing


def test_slip_applied_keeps_settings_and_grades_then_undo_puts_it_all_back(tmp_path):
    tl = sample()
    tl.tracks[("video", 5)][0].tools = {1: ["Primary Balance"]}   # a CDL on the cutout
    before = lanes(tl)
    snap, rec, out = run(tl, {"ops": [{"op": "slip", "items": ["v2a"], "by": -3, "follow": ["V3", "V5"]}]}, tmp_path)
    now = lanes(tl)
    assert now["V2"][0] == (0, 100, 497, "C0030.MP4") and now["V3"][0] == (0, 100, 7, "C0030-f009844.mov")
    assert now["V5"] == [(20, 60, 27, "C0030-f009844-alpha.mov")]          # one clip: the stand-in is gone
    v5 = tl.tracks[("video", 5)][0]
    assert v5.uid != "v5a" and v5.props["CropLeft"] == 520.0 and v5.tools == {1: ["Primary Balance"]}
    assert tl.tracks[("video", 3)][0].props["ZoomX"] == 1.15
    assert tl.GetEndFrame() == tl.start + 150 and rec.data["state"] == "applied" and not rec.data["warnings"]
    A.undo(fake.Resolve(tl), snap, {"ops": [{"op": "slip", "items": ["v2a"], "by": -3, "follow": ["V3", "V5"]}]}, rec,
           sleep=lambda s: None)
    assert lanes(tl) == before
    assert tl.tracks[("video", 5)][0].tools == {1: ["Primary Balance"]} and rec.data["state"] == "undone"


def test_nothing_is_removed_while_resolve_takes_nothing(tmp_path):
    tl = sample()
    before = lanes(tl)
    tl.busy = 99
    with pytest.raises(A.Stop, match="nothing was changed"):
        run(tl, {"ops": [{"op": "slip", "items": ["v2a"], "by": 1}]}, tmp_path)
    assert lanes(tl) == before


def test_an_edit_since_the_plan_stops_it(tmp_path):
    tl = sample()
    snap = snap_of(tl)
    tl.tracks[("video", 3)][0].left = 12                    # James slipped the loft himself
    tl.writable = True
    with pytest.raises(A.Busy, match="isn't where the plan has it"):
        A.apply(fake.Resolve(tl), snap, {"ops": [{"op": "slip", "items": ["v2a"], "by": 1, "follow": ["V3"]}]},
                A.Record(tmp_path / "a.json"), sleep=lambda s: None)


def test_undo_wont_remove_what_james_changed_since(tmp_path):
    tl = sample()
    plan = {"ops": [{"op": "slip", "items": ["v2a"], "by": 2}]}
    snap, rec, _ = run(tl, plan, tmp_path)
    tl.tracks[("video", 2)][0].frames = 90                  # he trimmed the re-laid clip
    with pytest.raises(A.Stop, match="isn't this plan's"):
        A.undo(fake.Resolve(tl), snap, plan, rec, sleep=lambda s: None)


def strip():
    """60 fps screen on V1, a camera with a gap on V2, voice on A1, and markers."""
    screen = fake.Media("/raw/screen.mp4", fps=60.0)
    cam = fake.Media("/raw/cam.mp4")
    voice = fake.Media("/audio/voice.wav", fps=0)
    s1 = fake.Item("s1", 90000, 100, 0, screen)
    s2 = fake.Item("s2", 90100, 100, 125, screen)
    s2.src = 300
    tracks = {("video", 1): [s1, s2],
              ("video", 2): [fake.Item("c1", 90000, 40, 500, cam), fake.Item("c2", 90080, 120, 700, cam)],
              ("audio", 1): [fake.Item("a1", 90000, 200, 0, voice, type="audio", nodes=None)]}
    marks = {f: {"color": "Blue", "name": f"m{f}", "note": "", "duration": 1, "customData": ""} for f in (10, 45, 120)}
    tl = fake.Timeline(tracks, marks, names={("video", 1): "screen", ("video", 2): "camera", ("audio", 1): "voice"})
    tl.writable = False
    return tl


FILL = {"video": "/gfx/black.mov", "audio": "/audio/silence.wav"}


def test_a_cut_ripples_every_track_and_undo_puts_the_time_back(tmp_path):
    tl = strip()
    before = lanes(tl)
    plan = {"ops": [{"op": "cut", "ranges": [[30, 60]], "fill": FILL, "ok": "James, post 14563"}]}
    media = {FILL["video"]: {"fps": 25.0, "frames": 100000}, FILL["audio"]: {"fps": 25.0, "frames": None}}
    tl.tracks[("video", 1)][0].tl.writable = True
    resolve = fake.Resolve(tl)
    resolve.pm.project.pool.extra = [fake.Media(FILL["video"]), fake.Media(FILL["audio"], fps=0)]
    tl.writable = False
    snap = snap_of(tl)
    r = compile_plan(snap, plan, media=media)
    assert not r["problems"], r["problems"]
    assert [lay["file"] for lay in r["groups"][0]["lay"] if lay["file"] == FILL["video"]] == [FILL["video"]]  # V2's gap
    tl.writable = True
    rec = A.Record(tmp_path / "applied.json")
    A.apply(resolve, snap, plan, rec, media=media, sleep=lambda s: None)
    now = lanes(tl)
    assert now["V1"] == [(0, 30, 0, "screen.mp4"), (30, 40, 60, "screen.mp4"), (70, 100, 125, "screen.mp4")]
    assert tl.tracks[("video", 1)][2].uid == "s2" and tl.tracks[("video", 1)][1].src == 144   # 60 * 2.4
    assert now["V2"] == [(0, 30, 500, "cam.mp4"), (50, 120, 700, "cam.mp4")]
    assert now["A1"] == [(0, 30, 0, "voice.wav"), (30, 140, 60, "voice.wav")]
    assert sorted(tl.markers) == [10, 90]                        # 120 moved back by the cut, 45 went with it
    A.undo(resolve, snap, plan, rec, media=media, sleep=lambda s: None)
    assert lanes(tl) == before and sorted(tl.markers) == [10, 45, 120]


def test_a_swap_moves_the_blocks_and_their_markers(tmp_path):
    tl = strip()
    plan = {"ops": [{"op": "swap", "a": 0, "b": 100, "c": 200, "stay": ["A1", "V2"], "ok": "James"}]}
    snap, rec, _ = run(tl, plan, tmp_path)
    assert lanes(tl)["V1"] == [(0, 100, 125, "screen.mp4"), (100, 100, 0, "screen.mp4")]
    assert sorted(tl.markers) == [20, 110, 145]


def test_set_changes_in_place_and_undo_sets_it_back(tmp_path):
    tl = sample()
    plan = {"ops": [{"op": "set", "items": ["v3a"], "props": {"ZoomX": 1.3}, "color": "Orange"}]}
    snap, rec, _ = run(tl, plan, tmp_path)
    v3 = tl.tracks[("video", 3)][0]
    assert v3.uid == "v3a" and v3.props["ZoomX"] == 1.3 and v3.color == "Orange"
    A.undo(fake.Resolve(tl), snap, plan, rec, sleep=lambda s: None)
    assert v3.props["ZoomX"] == 1.15


def test_join_makes_one_clip_of_a_through_edit(tmp_path):
    cam = fake.Media("/raw/cam.mp4")
    tl = fake.Timeline({("video", 1): [fake.Item("j1", 90000, 50, 100, cam), fake.Item("j2", 90050, 30, 150, cam)]})
    snap, rec, _ = run(tl, {"ops": [{"op": "join", "items": ["j1", "j2"]}]}, tmp_path)
    assert lanes(tl)["V1"] == [(0, 80, 100, "cam.mp4")]
    r = compile_plan(snap, {"ops": [{"op": "join", "items": ["j1", "j2"]}]})
    assert r["clutter"]["V1"] == {"clips": [2, 1], "through_edits": [1, 0]}


def test_the_writer_only_makes_the_placers_writes():
    w = A.PlanWriter(fake.Resolve(sample()))
    project = w.GetProjectManager().GetCurrentProject()
    for name in ("SaveProject", "SetCurrentTimeline", "ImportTimelineFromFile"):
        with pytest.raises(PermissionError):
            getattr(project, name)
    tl = project.GetCurrentTimeline()
    for name in ("AddTrack", "DeleteTrack", "SetCurrentTimecode", "SetName"):
        with pytest.raises(PermissionError):
            getattr(tl, name)
    assert tl.GetName() == "Stage 1 · silences out"


def test_clip_markers_go_with_a_moved_clip_and_come_back_on_undo(tmp_path):
    tl = fake.sample_timeline()                             # v2a has a clip marker at 12
    plan = {"ops": [{"op": "move", "items": ["v2a"], "track": "V4"}]}
    snap, rec, _ = run(tl, plan, tmp_path)
    moved = tl.tracks[("video", 4)][0]
    assert moved.uid != "v2a" and moved.markers[12]["name"] == "check lip sync"
    A.undo(fake.Resolve(tl), snap, plan, rec, sleep=lambda s: None)
    back = tl.tracks[("video", 2)][0]
    assert back.start == tl.start and back.markers[12]["name"] == "check lip sync" and not tl.tracks[("video", 4)]


def test_a_cut_whose_undo_would_relay_too_much_needs_saying_so():
    snap = snap_of(strip())
    op = {"op": "cut", "ranges": [[30, 60]], "fill": FILL, "ok": "James", "max_relay": 3}
    media = {FILL["video"]: {"fps": 25.0, "frames": 100000}, FILL["audio"]: {"fps": 25.0, "frames": None}}
    problems = compile_plan(snap, {"ops": [op]}, media=media)["problems"]
    assert problems and "re-lay 5 clips" in problems[0] and "\"max_relay\": 5" in problems[0]
    op["max_relay"] = 5
    assert not compile_plan(snap, {"ops": [op]}, media=media)["problems"]


def test_graded_pieces_join_only_when_the_plan_says_their_grades_match(tmp_path):
    cam = fake.Media("/raw/cut-alpha.mov")
    a, b = fake.Item("g1", 90000, 50, 100, cam), fake.Item("g2", 90050, 30, 150, cam)
    a.tools = b.tools = {1: ["Primary Balance"]}             # same tools: the values can't be read
    tl = fake.Timeline({("video", 1): [a, b]})
    plan = {"ops": [{"op": "join", "items": ["g1", "g2"]}]}
    assert "say how in \"same_grade\"" in compile_plan(snap_of(tl), plan)["problems"][0]
    plan["ops"][0]["same_grade"] = "cutout_look.py set one CDL on every piece"
    snap, rec, _ = run(tl, plan, tmp_path)
    assert lanes(tl)["V1"] == [(0, 80, 100, "cut-alpha.mov")] and tl.tracks[("video", 1)][0].tools == {1: ["Primary Balance"]}


def test_recorded_cdls_follow_a_relaid_clip_and_decide_joins(tmp_path):
    from pointman_resolve.timeline import grades
    cdl = {"Slope": "0.88 1.03 1.13", "Offset": "0 0 0", "Power": "1 1 1", "Saturation": "1"}
    tl = sample()
    tl.tracks[("video", 5)][0].tools = {1: ["Primary Balance"]}
    grades.record("v5a", cdl, by="cutout_look.py")
    plan = {"ops": [{"op": "slip", "items": ["v5a"], "by": 2}]}
    snap, rec, _ = run(tl, plan, tmp_path)
    new = tl.tracks[("video", 5)][0].uid
    assert grades.load()[new]["cdl"]["Slope"] == "0.88 1.03 1.13" and "re-laid" in grades.load()[new]["by"]
    A.undo(fake.Resolve(tl), snap, plan, rec, sleep=lambda s: None)
    assert grades.load()[tl.tracks[("video", 5)][0].uid]["cdl"]["Slope"] == "0.88 1.03 1.13"

    cut = fake.Media("/raw/cut-alpha.mov")
    a, b = fake.Item("k1", 90000, 50, 100, cut), fake.Item("k2", 90050, 30, 150, cut)
    a.tools = b.tools = {1: ["Primary Balance"]}
    tl2 = fake.Timeline({("video", 1): [a, b]})
    grades.record("k1", cdl, by="cutout_look.py")
    grades.record("k2", cdl, by="cutout_look.py")
    assert not compile_plan(snap_of(tl2), {"ops": [{"op": "join", "items": ["k1", "k2"]}]})["problems"]
    grades.record("k2", {**cdl, "Slope": "0.9 1 1.1"}, by="by hand")
    assert "recorded for its pieces differ" in compile_plan(snap_of(tl2), {"ops": [{"op": "join", "items": ["k1", "k2"]}]})["problems"][0]
