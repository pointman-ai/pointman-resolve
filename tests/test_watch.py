"""Watching the cut (PFE-13 v1): the checks, on hand-built snapshots."""

from pointman_resolve.timeline import watch


def item(i, kind, track, record, frames, file, inn=0, **k):
    return {"id": i, "kind": kind, "track": track, "record": record, "frames": frames, "in": inn, "file": file,
            "type": kind, "props": {}, **k}


def snap(items, names=None):
    names = names or {("video", 1): "screen", ("video", 2): "camera", ("video", 3): "loft", ("video", 4): "desktop",
                      ("video", 5): "cutout", ("video", 6): "graphics", ("video", 7): "Video 7", ("audio", 1): "voice",
                      ("audio", 3): "sfx"}
    return {"timeline": {"fps": 25, "width": 1920, "height": 1080, "start_frame": 0, "end_frame": 1000},
            "tracks": [{"kind": k, "index": i, "name": n} for (k, i), n in names.items()], "items": items}


CFG = {"roles": {"flips": "V7"}, "offsets": {"C0030": -175.929}}


def test_roles_come_from_track_names_and_the_config():
    r = watch.roles_of(snap([]), CFG)
    assert r["cutout"] == ("video", 5) and r["voice"] == ("audio", 1) and r["flips"] == ("video", 7)


def test_a_cutout_off_the_voice_gets_a_slip():
    # voice in 1000 at record 100 → voice time 40 s; camera frame = (40 + 175.929) * 25 = 5398; file starts at 5000
    good = item("c1", "video", 5, 100, 50, "/x/C0030-f005000-alpha.mov", inn=398)
    off = item("c2", "video", 5, 150, 50, "/x/C0030-f005000-alpha.mov", inn=440)
    s = snap([item("v", "audio", 1, 0, 400, "/a/voice.wav", inn=900), good, off])
    found = watch.sync(s, watch.roles_of(s, CFG), CFG["offsets"])
    assert [(f["id"], f["fix"]["by"]) for f in found] == [("c2", 8)]            # 448 − 440


def test_desktop_without_cutout_orphan_cues_and_scraps():
    s = snap([item("d", "video", 4, 0, 100, "/s/desk.mp4"), item("c", "video", 5, 0, 60, "/x/C0030-f000000-alpha.mov"),
              item("card", "video", 6, 200, 50, "/g/s07-card.mov"), item("tick", "audio", 3, 205, 1, "/a/tick.wav"),
              item("whoosh", "audio", 3, 500, 10, "/a/whoosh.wav"), item("bit", "video", 6, 600, 3, "/g/x.mov")])
    r = watch.roles_of(s, CFG)
    gaps = watch.no_cutout(s, r)
    assert [(f["at"], f["to"]) for f in gaps] == [(60, 100)]
    found = {(f["check"], f["id"]) for f in watch.orphans_and_scraps(s, r)}
    assert found == {("orphan cue", "whoosh"), ("scrap", "bit")}                # the tick sits on its card; ticks aren't scraps


def test_split_screens_and_scraps_that_carry_on():
    s = snap([item("split", "video", 4, 0, 100, "/s/desk.mp4", props={"CropRight": 480}),
              item("d1", "video", 4, 200, 50, "/s/desk.mp4", inn=1000), item("d2", "video", 4, 250, 6, "/s/desk.mp4", inn=1050),
              item("d3", "video", 4, 300, 5, "/s/other.mp4"), item("c", "video", 5, 200, 106, "/x/C0030-f000000-alpha.mov"),
              item("cardbit", "video", 6, 400, 4, "/g/s01-card.mov")])
    r = watch.roles_of(s, CFG)
    assert watch.no_cutout(s, r) == []                     # the split screen (100 frames, no cutout) is left alone
    fixes = {f["id"]: f["fix"] for f in watch.orphans_and_scraps(s, r) if f["check"] == "scrap"}
    assert fixes == {"d2": {"op": "join", "items": ["d1", "d2"]}, "d3": None,
                     "cardbit": {"op": "delete", "items": ["cardbit"]}}


def test_accepted_findings_stay_out():
    good = item("c2", "video", 5, 150, 50, "/x/C0030-f005000-alpha.mov", inn=440)
    s = snap([item("v", "audio", 1, 0, 400, "/a/voice.wav", inn=900), good])
    assert [f["check"] for f in watch.run(s, CFG, transitions=False)["findings"]] == ["sync"]
    assert watch.run(s, {**CFG, "accept": ["sync:c2"]}, transitions=False)["findings"] == []


def test_a_chapter_card_that_went():
    s = snap([item("c3", "video", 6, 0, 50, "/g/c03-visual-docs.mov")])
    gone, now = watch.chapters_gone(s, ["c03-visual-docs", "c07-discuss"], watch.roles_of(s, CFG))
    assert [f["what"].split()[3] for f in gone] == ["c07-discuss"] and now == ["c03-visual-docs"]
    # c03 re-rendered in another style is the same chapter
    s2 = snap([item("c3", "video", 6, 0, 50, "/g/c03-visual-docs-paper-v2.mov")])
    assert watch.chapters_gone(s2, now, watch.roles_of(s2, CFG))[0] == []


def test_a_cut_showing_through_a_card_fading_in(monkeypatch):
    # a full-frame card on V6 fading in over 4 frames (alpha 0, .3, .6, .9, then opaque); camera cuts at frame 102
    s = snap([item("cam1", "video", 2, 0, 102, "/c/a.mp4"), item("cam2", "video", 2, 102, 100, "/c/b.mp4"),
              item("card", "video", 6, 100, 20, "/g/card.mov")])
    fade = [(0.0, 0.0), (0.3, 0.3), (0.6, 0.6), (0.9, 0.9)] + [(1.0, 1.0)] * 16
    watch_alpha = lambda path, start, frames, fps: fade[:frames] if "card" in path else []
    monkeypatch.setattr(watch.Path, "exists", lambda self: True)     # the fake files "exist"
    monkeypatch.setattr(watch.R, "probe", lambda f: {"alpha": "card" in f, "w": 1920, "h": 1080})
    found = watch.see_through(s, watch.roles_of(s, CFG), alpha=watch_alpha)
    assert [(f["at"], f["id"]) for f in found] == [(102, "card")] and "shows through" in found[0]["what"]
