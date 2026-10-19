"""PFE-36: Resolve's own frame against the shadow render. The playhead guard and restore with a fake
timeline; compare(), suspects() and calibrate() on synthetic media through real ffmpeg."""

import shutil
import subprocess
from pathlib import Path

import pytest

from pointman_resolve.timeline import framecheck as F
from pointman_resolve.timeline import render as R

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")


class Timeline:
    """Reads give `ends` and `tcs` in turn (someone working), then hold; the playhead follows SetCurrentTimecode."""
    def __init__(self, ends=(1000,), tcs=("01:00:10:00",)):
        self.ends, self.reads, self.moves = list(ends), list(tcs), []
        self.end, self.tc = ends[0], tcs[0]

    def GetEndFrame(self):
        if self.ends:
            self.end = self.ends.pop(0)
        return self.end

    def GetCurrentTimecode(self):
        if self.reads:
            self.tc = self.reads.pop(0)
        return self.tc

    def SetCurrentTimecode(self, tc):
        self.moves.append(tc)
        self.tc = tc
        return True


class Project:
    def __init__(self, ok=True):
        self.ok = ok

    def ExportCurrentFrameAsStill(self, path):
        if self.ok:
            Path(path).write_bytes(b"png")
        return self.ok


def test_timecode():
    assert F.timecode(0, 25, 90000) == "01:00:00:00"
    assert F.timecode(5877, 25.0, 90000) == "01:03:55:02"


def test_busy_timeline_is_left_alone():
    with pytest.raises(F.Busy):
        F.idle(Timeline(ends=(1000, 990)), wait=0)
    tl = Timeline(tcs=("01:00:10:00", "01:00:12:05"))
    with pytest.raises(F.Busy):
        F.grab(Project(), tl, 50, 25, 90000, Path("/nonexistent/x.png"), wait=0)
    assert tl.moves == []  # never moved


def test_grab_puts_the_playhead_back(tmp_path):
    tl = Timeline()
    got = F.grab(Project(), tl, 250, 25, 90000, tmp_path / "f.png", wait=0)
    assert tl.moves == ["01:00:10:00", "01:00:10:00"]  # frame 250 is 01:00:10:00, then back home
    assert got["timecode"] == "01:00:10:00" and got["restored"]
    tl = Timeline()
    with pytest.raises(RuntimeError):
        F.grab(Project(ok=False), tl, 300, 25, 90000, tmp_path / "g.png", wait=0)
    assert tl.moves == ["01:00:12:00", "01:00:10:00"]  # put back even when the export fails


def make(path: Path, src: str, *extra: str) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, *extra, "-frames:v", "1", str(path)], check=True)
    return path


@needs_ffmpeg
def test_compare_finds_where_they_differ(tmp_path):
    a = make(tmp_path / "a.png", "color=c=gray:s=1920x1080")
    b = make(tmp_path / "b.png", "color=c=gray:s=1920x1080", "-vf", "drawbox=x=1400:y=780:w=480:h=300:c=red:t=fill")
    same = F.compare(a, a)
    assert same["psnr"] >= 60 and same["diff_box"] is None
    diff = F.compare(a, b, side_by_side=tmp_path / "side.png")
    x, y, w, h = diff["diff_box"]
    assert abs(x - 1400) <= 16 and abs(y - 780) <= 16 and abs(w - 480) <= 32 and abs(h - 300) <= 32
    assert diff["psnr"] < 40 and (tmp_path / "side.png").exists()


def test_suspects_rank_by_overlap():
    plan = {"video": [
        {"id": "desk", "track": 4, "name": "screen", "props": {}, "geo": {"rect": (0, 0, 1920, 1080)}},
        {"id": "cut", "track": 5, "name": "cutout", "props": {"Pan": 339}, "geo": {"rect": (1396, 783, 484, 297)}},
    ]}
    s = F.suspects(plan, [1400, 780, 480, 300])
    assert [x["id"] for x in s] == ["cut", "desk"]  # both cover the box; the cutout fits it
    assert s[0]["props"] == {"Pan": 339}
    assert F.suspects(plan, None) == []


@pytest.fixture(scope="module")
def cutout(tmp_path_factory):
    d = tmp_path_factory.mktemp("cal")
    path = d / "cutout.mov"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=red:s=1920x1080:r=25:d=2,format=yuva444p,"
                    "geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':a='if(between(X,640,1279)*between(Y,200,879),255,0)'",
                    "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", str(path)], check=True)
    return str(path)


def snap(items):
    return {"project": {"name": "p"}, "timeline": {"name": "t", "fps": 25.0, "width": 1920, "height": 1080,
                                                   "start_frame": 90000},
            "tracks": [{"kind": "video", "index": i, "enabled": True} for i in range(1, 6)], "items": items}


def item(iid, track, props, file):
    return {"id": iid, "kind": "video", "track": track, "record": 0, "frames": 25, "in": 0, "file": file,
            "type": "video", "props": props, "fades": None, "speed": None}


@needs_ffmpeg
def test_calibrate_finds_two_px_per_unit(tmp_path, cutout):
    s = snap([item("c", 5, {"ZoomX": 0.55, "Pan": 339.0, "Tilt": -206.5, "CropLeft": 40}, cutout)])
    assert F.probe_frame(s) == 12
    truth = tmp_path / "resolve.png"  # what Resolve shows, made with the measured 2 px per unit
    assert R.still({**s, "timeline": {**s["timeline"], "px_per_unit": 2.0}}, 12, truth, width=1920)["ok"]
    r = F.calibrate(s, 12, truth, tmp_path, units=(1.0, 2.0, 3.0))
    assert r["ok"] and r["px_per_unit"] == 2.0 and r["resolution"] == "1920x1080" and r["margin_db"] >= 0.5


def test_probe_frame_needs_a_moved_clip():
    assert F.probe_frame(snap([item("c", 5, {"Pan": 3}, "/x.mov")])) is None


def test_calibration_is_stored_per_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("POINTMAN_RESOLVE_HOME", str(tmp_path))
    assert F.stored_units(1920, 1080) is None
    F.store({"resolution": "1920x1080", "px_per_unit": 2.0, "psnr": 41.2, "margin_db": 6.1},
            project="p", timeline="t", frame=12)
    assert F.stored_units(1920, 1080) == 2.0 and F.stored_units(3840, 2160) is None
