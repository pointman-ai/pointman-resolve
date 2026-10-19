"""The shadow renderer against synthetic media: geometry as Resolve places clips, the 60 fps source
mapping, alpha cutouts, hidden-clip skipping, and a real ffmpeg still checked pixel by pixel."""

import shutil
import subprocess
from pathlib import Path

import pytest

from pointman_resolve.timeline import render as R

FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")


def make(path: Path, src: str, *extra: str) -> str:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, *extra, str(path)], check=True)
    return str(path)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    return {
        # a 4K 25 fps "camera" that is pure blue, and a 1728x1116 60 fps "screen" that is pure green
        "cam": make(d / "cam.mp4", "color=c=blue:s=3840x2160:r=25:d=4", "-pix_fmt", "yuv420p"),
        "screen": make(d / "screen.mp4", "color=c=green:s=1728x1116:r=60:d=4", "-pix_fmt", "yuv420p"),
        # a 1080p cutout with alpha: opaque red in its middle third, transparent elsewhere
        "cutout": make(d / "cutout.mov", "color=c=red:s=1920x1080:r=25:d=4,format=yuva444p,"
                       "geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':a='if(between(X,640,1279),255,0)'",
                       "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le"),
    }


def item(iid, track, record, frames, file, *, inp=0, props=None, kind="video"):
    return {"id": iid, "kind": kind, "track": track, "record": record, "frames": frames, "in": inp,
            "file": file, "type": kind, "props": props or {}, "fades": None, "speed": None}


def snap(items, tracks=6):
    return {"timeline": {"fps": 25.0, "width": 1920, "height": 1080, "start_frame": 90000},
            "tracks": [{"kind": "video", "index": i, "enabled": True} for i in range(1, tracks + 1)],
            "items": items}


def test_geometry_matches_resolve(media):
    # the cutout corner placement editor verified in Resolve: crop L/R 520, T 60, B 480, zoom 0.55
    props = {"CropLeft": 520, "CropRight": 520, "CropTop": 60, "CropBottom": 480, "ZoomX": 0.55,
             "Pan": 678.0, "Tilt": -507.0}
    g = R.geometry(item("c", 5, 0, 25, media["cutout"], props=props), 1920, 1080)
    assert [round(v) for v in g["rect"]] == [1396, 783, 484, 297]
    # a 1728x1116 screen fits to 1080 high (pillarboxed); zoom 1.148 fills the width, and Tilt up
    # moves it up: y = 540 + 1.148·(0 − 540) − 25.56
    s = R.geometry(item("s", 4, 0, 25, media["screen"], props={"ZoomX": 1.1481481481, "Tilt": 25.5556}), 1920, 1080)
    assert [round(v) for v in s["rect"]] == [0, -106, 1920, 1240]


def test_hidden_clips_are_not_decoded(media):
    s = snap([item("scr", 1, 0, 50, media["screen"]), item("cam", 2, 0, 50, media["cam"])])
    p = R.plan(s, 0, 50)
    assert [v["id"] for v in p["video"]] == ["cam"] and p["hidden"] == 1


def test_sixty_fps_source_time_is_in_timeline_frames(media):
    s = snap([item("scr", 1, 100, 50, media["screen"], inp=40)])
    p = R.plan(s, 110, 120)
    assert p["video"][0]["src"] == pytest.approx((40 + 10) / 25)


def test_still_composites_in_order(media, tmp_path):
    cut = {"CropLeft": 520, "CropRight": 520, "CropTop": 60, "CropBottom": 480, "ZoomX": 0.55,
           "Pan": 678.0, "Tilt": -507.0}
    s = snap([item("cam", 2, 0, 50, media["cam"]), item("cut", 5, 0, 50, media["cutout"], props=cut)])
    out = tmp_path / "still.png"
    r = R.still(s, 10, out, width=1920)
    assert r["ok"], r["error"]
    rgb = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True).stdout

    def px(x, y):
        i = (y * 1920 + x) * 3
        return tuple(rgb[i:i + 3])

    r_, g_, b_ = px(100, 100)              # the camera: blue
    assert b_ > 200 and r_ < 60
    r_, g_, b_ = px(1396 + 242, 783 + 150)  # inside the cutout's opaque middle: red over the camera
    assert r_ > 200 and b_ < 60
    r_, g_, b_ = px(1396 + 20, 783 + 150)   # the cutout's transparent left third: the camera shows
    assert b_ > 200 and r_ < 60


def test_skips_what_it_cannot_draw(media):
    gen = {**item("gen", 6, 0, 50, None), "type": "generator", "name": "Solid Color"}
    s = snap([item("cam", 2, 0, 50, media["cam"]), gen])
    p = R.plan(s, 0, 50)
    assert [k["id"] for k in p["skipped"]] == ["gen"]
