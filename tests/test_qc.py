"""PFE-16's QC report: picture checks on a synthetic snapshot, a frozen recording against a live one, and the
mix's loudness and dead air, through real ffmpeg."""

import shutil
import subprocess
from pathlib import Path

import pytest

from pointman_resolve.timeline import qc

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="needs ffmpeg")


def item(iid, track, record, frames, file="/x.mov", kind="video", **kw):
    return {"id": iid, "kind": kind, "track": track, "record": record, "frames": frames, "in": 0, "file": file,
            "type": kind, "props": {}, "fades": None, "speed": None, "name": iid, **kw}


def snap(items, end=200, tracks=3):
    return {"project": {"name": "p"}, "timeline": {"name": "t", "fps": 25.0, "width": 1920, "height": 1080,
                                                   "start_frame": 0, "end_frame": end},
            "tracks": [{"kind": "video", "index": i, "enabled": True} for i in range(1, tracks + 1)]
                      + [{"kind": "audio", "index": 1, "enabled": True}],
            "items": items}


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("qc")
    def make(name, src, *extra):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", src, *extra, str(d / name)], check=True)
        return str(d / name)
    return {
        "live": make("live.mp4", "testsrc2=s=320x180:r=25:d=70", "-pix_fmt", "yuv420p"),
        "frozen": make("frozen.mp4", "color=c=gray:s=320x180:r=25:d=70", "-pix_fmt", "yuv420p"),
        # 2 s of tone, 3 s of silence, 2 s of tone
        "short": make("fill.mov", "color=c=black:s=320x180:r=25:d=20", "-pix_fmt", "yuv420p"),
        "voice": make("voice.wav", "aevalsrc='if(between(t\\,2\\,5)\\,0\\,0.3*sin(2*PI*220*t))':s=48000:d=7"),
    }


@needs_ffmpeg
def test_black_and_flash(media):
    s = snap([item("a", 1, 0, 100, media["live"]), item("b", 1, 101, 49, media["live"]),   # a 1-frame gap at 100
              item("blink", 2, 120, 2, media["live"]),                                    # a 2-frame clip on top
              item("c", 1, 160, 40, media["live"])], end=200)                             # 150–160: nothing at all
    found = qc.picture(s, 0, 200)
    kinds = [(i["check"], i["at"], i["to"]) for i in found]
    assert ("black", 100, 101) in kinds and ("black", 150, 160) in kinds
    assert ("flash", 100, 101) in kinds          # the gap between a and b on V1
    assert ("flash", 120, 122) in kinds          # the 2-frame clip on V2


@needs_ffmpeg
def test_a_frozen_recording_is_found_and_a_live_one_isnt(media):
    s = snap([item("f", 1, 0, 25 * 30, media["frozen"], src_start=0.0),
              item("l", 2, 0, 25 * 30, media["live"], src_start=0.0)], end=25 * 30)
    found = qc.frozen(s)
    assert [Path(f["file"]).name for f in found] == ["frozen.mp4"]
    assert "doesn't change" in found[0]["what"]
    # a fill or hold is still by design: short files are left alone
    s2 = snap([item("fill", 1, 0, 25 * 20, media["short"], src_start=0.0)], end=25 * 20)
    assert qc.frozen(s2) == []


@needs_ffmpeg
def test_the_mix_has_loudness_and_dead_air(media):
    s = snap([item("v", 1, 0, 175, media["voice"], kind="audio", props={"AudioVolume": 0.0})], end=175)
    stats, issues = qc.sound(s, 0, 175)
    assert stats["lufs"] is not None and stats["true_peak"] is not None
    dead = [i for i in issues if i["check"] == "dead air"]
    assert len(dead) == 1 and 45 <= dead[0]["at"] <= 55 and 120 <= dead[0]["to"] <= 130


def test_the_report_counts_and_reads(monkeypatch):
    monkeypatch.setattr(qc, "frozen", lambda s: [])
    s = snap([item("a", 1, 0, 100)], end=110)
    monkeypatch.setattr(qc.R, "plan", lambda snap, a, b: {"video": [], "audio": []})
    r = qc.report(s, audio=False)
    assert not r["ok"] and r["counts"] == {"black": 1} and r["issues"][0]["at"] == 100
    assert "1 black" in qc.markdown(r, "t")


@needs_ffmpeg
def test_a_gap_under_a_full_frame_clip_isnt_a_flash_and_disabled_clips_dont_cover(media):
    s = snap([item("a", 1, 0, 100, media["live"]), item("b", 1, 101, 99, media["live"]),
              item("cover", 2, 90, 30, media["live"]),                                     # opaque, full frame
              item("off", 3, 150, 50, media["live"], enabled=False)], end=200)
    found = [(i["check"], i["at"]) for i in qc.picture(s, 0, 200)]
    assert ("flash", 100) not in found and ("black", 100) not in found


def test_the_report_says_times_and_that_sound_is_an_estimate(monkeypatch):
    r = {"ok": False, "range": "0:00.00–0:10.00", "counts": {"black": 1}, "fps": 25.0,
         "sound": {"lufs": -15.8, "true_peak": -1.1, "target_lufs": -14.0, "estimate": qc.ESTIMATE},
         "issues": [{"check": "black", "at": 1512, "to": 1513, "what": "no video for 1 frames"}]}
    md = qc.markdown(r, "t")
    assert "| 1:00.48 | black |" in md and "clip levels only" in md and "about -15.8 LUFS" in md
