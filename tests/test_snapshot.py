import json

import pytest

import fake
from pointman_resolve.timeline import journal
from pointman_resolve.timeline.check import check
from pointman_resolve.timeline.readonly import ReadOnly, ReadOnlyError
from pointman_resolve.timeline.snapshot import take, write

ALLOWED = ("Get", "Is", "Export")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POINTMAN_RESOLVE_HOME", str(tmp_path / "home"))
    fake.CALLS.clear()
    return tmp_path / "home"


def resolve():
    return ReadOnly(fake.Resolve(fake.sample_timeline()))


def test_guard_blocks_writes_at_every_level():
    r = resolve()
    tl = r.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    item = tl.GetItemListInTrack("video", 2)[0]
    for obj, name in ((item, "SetProperty"), (tl, "DeleteClips"), (r.GetProjectManager(), "LoadProject"),
                      (r.GetProjectManager().GetCurrentProject(), "SaveProject"), (item.GetMediaPoolItem(), "ReplaceClip")):
        with pytest.raises(ReadOnlyError):
            getattr(obj, name)
    assert r.EXPORT_OTIO == 31  # constants pass
    assert item.GetDuration() == 100  # reads pass
    with pytest.raises(AttributeError):
        item.__deepcopy__  # Python's probes are plain attribute errors, not Resolve calls


def test_snapshot_has_the_interface_fields():
    snap, _ = take(resolve())
    assert snap["schema"] == 1 and snap["project"]["name"] == "feat-flow-with-spacesheep"
    assert snap["timeline"] == {"name": "Stage 1 · silences out", "id": "tl-1", "fps": 25.0, "start_frame": 90000,
                                "end_frame": 90150, "start_timecode": "01:00:00:00", "width": 1920, "height": 1080}
    assert [(t["kind"], t["index"]) for t in snap["tracks"]] == [("video", i) for i in range(1, 7)] + [("audio", 1)]
    by = {i["id"]: i for i in snap["items"]}
    cam = by["v2a"]
    for key in ("id", "kind", "track", "record", "frames", "in", "src_start", "src_frame", "media_fps", "file", "name",
                "type", "props", "fades", "speed", "enabled", "color", "nodes", "markers"):
        assert key in cam, key
    assert (cam["kind"], cam["track"], cam["record"], cam["frames"], cam["in"]) == ("video", 2, 0, 100, 500)
    assert cam["src_start"] == (121092 + 500) / 25 and cam["media_fps"] == 25.0 and cam["file"] == "/raw/C0030.MP4"
    assert cam["markers"] == [{"frame": 12, "color": "Red", "name": "check lip sync", "note": "", "duration": 1, "custom": ""}]
    assert by["v5a"]["props"]["CropLeft"] == 520.0 and by["v3a"]["nodes"] == 1
    assert by["v6t"]["type"] == "transition" and by["v6t"]["fades"] is None and by["v6t"]["file"] is None
    assert by["a1a"]["nodes"] is None and by["a1a"]["speed"]["PitchCorrection"] is True
    assert [m["frame"] for m in snap["markers"]] == [0, 136]
    assert snap["errors"] == {}


def test_only_reads_reach_resolve(tmp_path):
    write(resolve(), tmp_path / "snaps")
    assert fake.CALLS and all(c.startswith(ALLOWED) for c in fake.CALLS), sorted(set(fake.CALLS))


def test_write_then_check_round_trips(tmp_path):
    path = write(resolve(), tmp_path / "snaps")
    snap = json.loads(path.read_text())
    assert path.name.startswith("snapshot-") and snap["otio"]["file"] == path.with_suffix(".otio").name
    result = check(path)
    assert result["ok"], result["problems"]
    assert result["items"] == 8 and result["props_checked"] > 0 and result["transitions"] == 1


def test_check_catches_a_mismatch(tmp_path):
    path = write(resolve(), tmp_path / "snaps")
    otio_path = path.with_suffix(".otio")
    otio = json.loads(otio_path.read_text())
    clip = next(c for t in otio["tracks"]["children"] for c in t["children"] if c["OTIO_SCHEMA"] == "Clip.2")
    clip["source_range"]["start_time"]["value"] += 1  # an in-point one frame off
    otio_path.write_text(json.dumps(otio))
    result = check(path)
    assert not result["ok"] and result["in_ok"] == result["items"] - 1


def test_journal_since_the_previous_snapshot(tmp_path):
    out = tmp_path / "snaps"
    first = json.loads(write(resolve(), out).read_text())
    assert first["previous"] is None
    journal.record("place_cutouts", removed=[], added=["v5a"], timeline="tl-1", note="8 desk ranges")
    journal.record("other_timeline", added=["x"], timeline="tl-2")
    second = json.loads(write(resolve(), out).read_text())
    assert second["previous"].startswith("snapshot-") and second["journal_since"] == first["taken"]
    assert [e["tool"] for e in second["journal"]] == ["place_cutouts"]
