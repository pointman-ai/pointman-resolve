import pytest

import fake
from pointman_resolve import markers as M
from pointman_resolve.timeline.readonly import ReadOnly
from pointman_resolve.timeline.snapshot import take


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POINTMAN_RESOLVE_HOME", str(tmp_path / "home"))


def mk(name, note="", custom="", color="Blue"):
    return {"color": color, "name": name, "note": note, "duration": 1, "customData": custom}


def test_which_markers_call_an_agent():
    found = M.calls({
        10.0: mk("@pm the cut here jumps"),
        20.0: mk("Marker 3", "@editor lip sync is off"),
        30.0: mk("@Pointman look"),
        40.0: mk("check lip sync"),                              # no @: not a call
        50.0: mk("@editor again", custom="pm:14200"),            # ours, already posted
        60.0: mk("Review: @editor", custom="dailies:991"),       # a board note mirrored as a marker
        70.0: mk("email me at x@y.com"),                         # an @ that isn't at the start
        80.0: mk("@rtx-render relight this"),
    })
    assert [(c.frame, c.handle) for c in found] == [(10, None), (20, "editor"), (30, None), (80, "rtx-render")]
    assert found[1].text == "@editor lip sync is off"            # "Marker 3" is Resolve's default name: left out


def test_routes_thread_default_agent_and_dm():
    routes = M.load_routes()                                     # no routes.toml yet: the example
    assert M.route(routes, "feat-flow-with-spacesheep", None) == {"thread": 10399, "channel": None, "mention": "editor"}
    assert M.route(routes, "feat-flow-with-spacesheep", "rtx-render")["mention"] == "rtx-render"
    # no unmapped channel set yet: nowhere to post
    assert M.route(routes, "some-other-project", None) == {"thread": None, "channel": None, "mention": "james"}
    routes["unmapped"] = {"channel": 42}
    assert M.route(routes, "some-other-project", "editor") == {"thread": None, "channel": 42, "mention": "editor"}
    routes["unmapped"] = {"thread": 13661, "channel": 42}       # a thread wins
    assert M.route(routes, "some-other-project", None) == {"thread": 13661, "channel": None, "mention": "james"}


def test_timecode_and_the_post():
    snap, _ = take(ReadOnly(fake.Resolve(fake.sample_timeline())))
    assert M.timecode(0, 25, "01:00:00:00") == "01:00:00:00"
    assert M.timecode(25 * 61 + 3, 25, "01:00:00:00") == "01:01:01:03"
    clips = M.under(snap, 25)
    assert clips[0].startswith("V6") and any(c.startswith("V2") and "C0030.MP4 at 0:21.00" in c for c in clips)
    assert clips[-1].startswith("A1")
    text = M.compose(M.Call(25, mk("@editor too dark", "the window light"), "editor"), snap, "editor")
    assert text.startswith("@editor Resolve marker at **0:01.00** (01:00:01:00), feat-flow-with-spacesheep")
    assert "> @editor too dark\n> the window light" in text and "Under it:" in text
    assert text.endswith("turns the marker green in Resolve.") and "resolve.marker_done" in text
    assert "React ✅" not in text and "React ✅" in M.compose(M.Call(25, mk("@editor x"), "editor"), snap, "editor", ticks=True)


def test_writer_tags_and_recolours_without_touching_the_note():
    tl = fake.sample_timeline()
    tl.markers[300] = mk("@editor fix", "the note James wrote", color="Red")
    w = M.MarkerWriter(tl)
    assert w.mark(300, mk("@editor fix", "the note James wrote"), M.POSTED, "pm:14200") == "ok"
    assert tl.markers[300] == {"color": "Cyan", "name": "@editor fix", "note": "the note James wrote", "duration": 1,
                               "customData": "pm:14200"}
    assert w.mark(300, tl.markers[300], M.DONE, "pm:14200") == "ok" and tl.markers[300]["color"] == "Green"


def test_writer_restores_the_marker_if_resolve_refuses_the_new_one():
    tl = fake.sample_timeline()
    tl.markers[300] = mk("@editor fix", "keep me", color="Red")
    tl.fail_add = 1
    result = M.MarkerWriter(tl).mark(300, mk("@editor fix", "keep me"), M.POSTED, "pm:1")
    assert result.startswith("failed") and tl.markers[300]["color"] == "Red" and tl.markers[300]["note"] == "keep me"
    assert tl.markers[300]["customData"] == "pm:1"               # tagged, so it won't be posted again


def test_writer_leaves_an_edited_marker_alone():
    tl = fake.sample_timeline()
    tl.markers[300] = mk("@editor fix", "James changed this since")
    assert M.MarkerWriter(tl).mark(300, mk("@editor fix", "the note we posted"), M.POSTED, "pm:1") == "gone"
    assert tl.markers[300]["customData"] == "" and tl.markers[300]["color"] == "Blue"


def test_writer_can_only_touch_markers():
    w = M.MarkerWriter(fake.sample_timeline())
    for name in ("DeleteClips", "GetItemListInTrack", "SetTrackEnable"):
        with pytest.raises(PermissionError):
            getattr(w, name)


def test_state_remembers_what_was_posted(tmp_path):
    s = M.State()
    s.put("tl-1", "300-abc", {"post": 14200, "status": "posted"})
    again = M.State()
    assert again.get("tl-1", "300-abc")["post"] == 14200 and len(again.open()) == 1


def test_done_needs_a_tick_from_the_agent_or_james():
    assert M.done_by([{"emoji": "✅", "count": 1, "handles": ["editor"]}], "editor") == "editor"
    assert M.done_by([{"emoji": "✅", "count": 2, "handles": ["cairn", "james"]}], "editor") == "james"
    assert M.done_by([{"emoji": "✅", "count": 1, "handles": ["shipwright"]},
                      {"emoji": "👀", "count": 1, "handles": ["editor"]}], "editor") is None


def test_find_our_marker_after_a_ripple_moved_it():
    tl = fake.sample_timeline()
    tl.markers[420] = mk("@editor fix", "n", custom="pm:14200", color="Cyan")   # was at 300 when posted
    w = M.MarkerWriter(tl)
    frame, m = w.find("pm:14200")
    assert frame == 420 and w.mark(frame, m, M.DONE, "pm:14200") == "ok" and tl.markers[420]["color"] == "Green"
    assert w.find("pm:1") is None
