"""PFE-42: grades our tools set are recorded by clip, attached to snapshots and drawn by the shadow renderer."""

import shutil
import subprocess
from pathlib import Path

import pytest

from pointman_resolve.timeline import grades
from pointman_resolve.timeline import render as R

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")


def test_recorded_by_clip_and_attached(tmp_path, monkeypatch):
    monkeypatch.setenv("POINTMAN_RESOLVE_HOME", str(tmp_path))
    grades.record("uid-1", {"NodeIndex": "1", "Slope": "0.6 0.6 0.6", "Offset": "0 0 0", "Power": "1 1 1",
                            "Saturation": "1.0"}, by="desktop_dim")
    items = [{"id": "uid-1"}, {"id": "uid-2"}]
    assert grades.attach(items) == 1 and items[0]["cdl"]["Slope"] == "0.6 0.6 0.6" and "cdl" not in items[1]
    grades.record("uid-1", grades.NEUTRAL, by="desktop_dim --undo")     # back to neutral: forgotten
    assert grades.load() == {}


def pixel(png: Path, x: int, y: int) -> tuple:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(png), "-vf", f"crop=1:1:{x}:{y},format=rgb24", "-f", "rawvideo",
                          "-"], capture_output=True).stdout
    return tuple(raw[:3])


@needs_ffmpeg
@pytest.mark.parametrize("cdl, check", [
    ({"Slope": "0.6 0.6 0.6", "Saturation": "1"}, lambda p: all(70 <= c <= 84 for c in p)),       # the 60% desktop dim
    ({"Slope": "1 1 1", "Saturation": "0"}, lambda p: max(p) - min(p) <= 3),                       # no colour left
    ({"Slope": "0.904 1.023 1.107", "Saturation": "0.95"}, lambda p: p[2] > p[0]),                 # the cutout's cooler white
])
def test_the_renderer_draws_a_cdl(tmp_path, cdl, check):
    src = tmp_path / "gray.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x808080:s=1920x1080:r=25:d=1",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    if cdl.get("Saturation") == "0":
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0xC04030:s=1920x1080:r=25:d=1",
                        "-pix_fmt", "yuv420p", str(src)], check=True)
    item = {"id": "a", "kind": "video", "track": 1, "record": 0, "frames": 25, "in": 0, "file": str(src), "type": "video",
            "props": {}, "cdl": cdl}
    snap = {"timeline": {"fps": 25.0, "width": 1920, "height": 1080, "start_frame": 0},
            "tracks": [{"kind": "video", "index": 1, "enabled": True}], "items": [item]}
    out = tmp_path / "f.png"
    assert R.still(snap, 5, out, width=480)["ok"]
    assert check(pixel(out, 240, 135)), pixel(out, 240, 135)
