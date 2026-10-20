"""resolve.insert_gap: a gap opens (or closes) on every track through a temporary clip's rippling speed change,
the way Resolve 21.1 moved the Spacesheep cut (+15 frames at 11:30.44, 2026-10-20)."""

import pytest

from pointman_resolve.timeline import gap
from pointman_resolve.timeline.framecheck import Busy

import fake


class Clip(fake.Item):
    def __init__(self, *a, timeline=None, **k):
        super().__init__(*a, **k)
        self.timeline, self.source = timeline, self.frames

    def SetSpeed(self, opts):
        """What Resolve does with RippleTimeline: the clip's length is its source length over the speed (cut
        down to whole frames), and everything starting after the clip's start, on every track, moves by the
        difference; so do the timeline's markers. `skip` leaves one track behind, as a bad ripple would."""
        new = int(self.source * 100.0 / opts["Percentage"])
        d, start = new - self.frames, self.start
        self.frames = new
        if opts.get("RippleTimeline"):
            for key, items in self.timeline.tracks.items():
                if key == getattr(self.timeline, "skip", None):
                    continue
                for it in items:
                    if it is not self and it.start > start:
                        it.start += d
            self.timeline.markers = {(f + d if self.timeline.start + f > start else f): m
                                     for f, m in self.timeline.markers.items()}
        return True


class Timeline(fake.Timeline):
    def GetCurrentTimecode(self):
        return "01:00:00:00"

    def AddTrack(self, kind):
        self.tracks[(kind, self.GetTrackCount(kind) + 1)] = []
        return True

    def DeleteTrack(self, kind, i):
        assert not self.tracks.get((kind, i)), "the temporary track still has clips"
        del self.tracks[(kind, i)]
        return True

    def DeleteClips(self, items, ripple=False):
        for v in self.tracks.values():
            v[:] = [it for it in v if it not in items]
        return True


class Pool(fake.Obj):
    def __init__(self, tl):
        self.tl = tl

    def AppendToTimeline(self, infos):
        out = []
        for i in infos:
            c = Clip("temp", i["recordFrame"], i["endFrame"] - i["startFrame"], i["startFrame"], i["mediaPoolItem"],
                     timeline=self.tl)
            self.tl.tracks[("video", i["trackIndex"])].append(c)
            out.append(c)
        return out


def cut():
    cam, voice = fake.Media("/raw/C0031.MP4"), fake.Media("/audio/voice.wav", fps=0)
    t0 = 90000
    tl = Timeline({("video", 1): [fake.Item("a", t0, 100, 0, cam), fake.Item("b", t0 + 100, 50, 300, cam)],
                   ("video", 2): [fake.Item("card", t0 + 90, 30, 0, cam)],          # crosses frame 100
                   ("audio", 1): [fake.Item("va", t0, 100, 0, voice, type="audio"),
                                  fake.Item("vb", t0 + 100, 50, 300, voice, type="audio")]},
                  markers={40: {"name": "before"}, 100: {"name": "on the cut"}, 120: {"name": "after"}}, start=t0)
    return tl, Pool(tl)


def lanes(tl):
    return {f"{k[0][0].upper()}{k[1]}": [(it.uid, it.start - tl.start, it.frames) for it in v] for k, v in tl.tracks.items()}


def test_a_gap_opens_on_every_track_and_the_markers_move():
    tl, pool = cut()
    got = gap.insert(pool, tl, 100, 15, wait=0)
    assert got == {"at": 100, "frames": 15, "moved": 15}
    assert lanes(tl) == {"V1": [("a", 0, 100), ("b", 115, 50)], "V2": [("card", 90, 30)],
                         "A1": [("va", 0, 100), ("vb", 115, 50)]}           # the temporary track is gone
    assert sorted(tl.markers) == [40, 115, 135]


def test_closing_puts_it_back_and_refuses_a_gap_that_isnt_empty():
    tl, pool = cut()
    gap.insert(pool, tl, 100, 15, wait=0)
    with pytest.raises(ValueError):
        gap.insert(pool, tl, 110, -15, wait=0)                            # 'b' starts at 115, inside 110-125
    gap.insert(pool, tl, 100, -15, wait=0)
    assert lanes(tl)["V1"] == [("a", 0, 100), ("b", 100, 50)] and sorted(tl.markers) == [40, 100, 120]


def test_it_waits_for_a_still_timeline(monkeypatch):
    tl, pool = cut()
    monkeypatch.setattr(gap, "idle", lambda tl, wait: (_ for _ in ()).throw(Busy("the timeline is being worked on")))
    with pytest.raises(Busy):
        gap.insert(pool, tl, 100, 15, wait=0)
    assert lanes(tl)["V1"] == [("a", 0, 100), ("b", 100, 50)]


def test_a_length_resolve_would_round_down_is_nudged_until_it_lands():
    tl, pool = cut()
    for frames in (6, 11, 13, 23):                       # 100 * 1 / (1 + 6) etc. aren't exact in floating point
        tl, pool = cut()
        assert gap.insert(pool, tl, 100, frames, wait=0)["moved"] == frames
        assert lanes(tl)["V1"] == [("a", 0, 100), ("b", 100 + frames, 50)]


def test_a_ripple_that_leaves_a_track_behind_fails_and_says_how_far_it_moved():
    tl, pool = cut()
    tl.skip = ("audio", 1)
    with pytest.raises(gap.GapError) as e:
        gap.insert(pool, tl, 100, 15, wait=0)
    assert "1 of 5 clips ended up in the wrong place" in str(e.value)        # vb, on the track left behind
    assert "moved +15" in str(e.value) and "frames=-15" in str(e.value)
    assert ("video", 3) not in tl.tracks                 # the temporary track still came off


def test_closing_over_a_marker_or_before_the_first_frame_is_refused():
    tl, pool = cut()
    gap.insert(pool, tl, 100, 15, wait=0)
    tl.markers[105] = {"name": "an @editor note in the gap"}
    with pytest.raises(ValueError, match="markers"):
        gap.insert(pool, tl, 100, -15, wait=0)
    with pytest.raises(ValueError, match="first frame"):
        gap.insert(pool, tl, 0, 15, wait=0)
