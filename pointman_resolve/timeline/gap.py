"""Open (or close) a gap on every track at once: the insert Resolve's API doesn't have.

Resolve 21.1 can't insert time, and DeleteClips(ripple) only takes time out. But a speed change with
`RippleTimeline` moves every item that starts after the changed clip, on every track, and the timeline's
markers with them (tried on the Spacesheep cut, 2026-10-20: +15 frames, then back, exactly). So:

  1. a temporary video track goes on top, and a 1-frame clip on it at `at - 1`;
  2. SetSpeed stretches that clip to 1 + `frames` with RippleTimeline, so everything starting at `at` or
     later moves `frames` later (a clip that only crosses `at` stays, and keeps playing);
  3. the clip and the track come off again, and the playhead goes back on the picture it was on: AppendToTimeline
     moves it to the end of the clip it lays, so it goes back where it was, `frames` later if it was at `at` or
     after (as Resolve's own ripple delete carries it back with the picture: both seen live, 2026-10-21).

A negative `frames` closes a gap the same way (a clip of 1 + |frames| sped up to 1 frame), but only if
nothing starts in [at, at + |frames|), nothing that moves back would land on a clip that stays, and no
timeline marker sits in the gap. It refuses to start unless the timeline has been still for `wait` seconds.
Afterwards every item is checked, not just the end: those before `at` unchanged, the rest moved by exactly
`frames`. Whatever goes wrong, the error says how far the timeline moved and how to undo it.
Untested: clips on locked tracks.
"""

from __future__ import annotations

from .framecheck import Busy, Only, idle, timecode  # noqa: F401 - Busy is what idle() raises

TL_CALLS = ("GetStartFrame", "GetEndFrame", "GetCurrentTimecode", "GetTrackCount", "GetItemListInTrack", "GetSetting",
            "GetMarkers", "AddTrack", "DeleteTrack", "DeleteClips",
            "SetCurrentTimecode")   # only to put the playhead back after AppendToTimeline moves it
POOL_CALLS = ("AppendToTimeline",)


class GapError(RuntimeError):
    """The gap didn't come out as asked; the message says what moved and how to put it back. `moved` is the
    shift that really happened (for the journal: follow mode mustn't take the ask for it)."""

    def __init__(self, message: str, moved: int):
        super().__init__(message)
        self.moved = moved


def _items(tl, kind):
    return [it for k in range(1, (tl.GetTrackCount(kind) or 0) + 1) for it in (tl.GetItemListInTrack(kind, k) or [])]


def _starts(tl, t0) -> dict:
    return {it.GetUniqueId(): it.GetStart() - t0 for kind in ("video", "audio") for it in _items(tl, kind)}


def _fps(tl) -> float:
    try:
        return float(tl.GetSetting("timelineFrameRate") or 25)
    except (AttributeError, ValueError):
        return 25.0


def _playhead(home: str, tl, t0: int, at: int, frames: int) -> str:
    """Where the playhead that was at `home` belongs once everything from `at` has moved `frames`: on the same
    picture. At a rate that isn't whole (drop-frame timecode) it just stays at `home` rather than guess."""
    fps = _fps(tl)
    try:
        h, m, s, f = (int(x) for x in home.replace(";", ":").split(":"))
        frame = ((h * 60 + m) * 60 + s) * round(fps) + f - t0
        if frame < at or abs(fps - round(fps)) > 1e-6:
            return home
        return timecode(max(at, frame + frames), fps, t0)   # a playhead inside a gap that closes goes to its start
    except ValueError:
        return home


def _filler(tl, n: int):
    """A media pool item already on the timeline, at the timeline's own rate and at least n frames long."""
    fps = _fps(tl)
    for it in _items(tl, "video"):
        mp = it.GetMediaPoolItem()
        if mp and abs(float(mp.GetClipProperty("FPS") or 0) - fps) < 0.01 \
                and int(float(mp.GetClipProperty("Frames") or n)) >= n:
            return mp
    raise RuntimeError(f"no clip on the timeline at its own frame rate and {n}+ frames long to lay the temporary clip from")


def _stretch(temp, n: int, want: int) -> bool:
    """Set the speed so the clip lasts `want` frames; Resolve may round a frame either way, so nudge and retry."""
    for nudge in (0.0, 0.25, -0.25, 0.45, -0.45):
        if not temp.SetSpeed({"Percentage": 100.0 * n / (want + nudge), "RippleTimeline": True}):
            return False
        if temp.GetDuration() == want:
            return True
    return False


def insert(pool, tl, at: int, frames: int, *, wait: float = 3.0) -> dict:
    """Move everything starting at timeline frame `at` (0 = the first frame) by `frames` (negative: back)."""
    if not frames:
        return {"at": at, "frames": 0, "moved": 0}
    if at < 1:
        raise ValueError("a gap can't open before the timeline's first frame: `at` must be 1 or later")
    why = "insert_gap only lays and stretches a temporary clip on a temporary track"
    pool, tl = Only(pool, *POOL_CALLS, why=why), Only(tl, *TL_CALLS, why=why)
    t0 = tl.GetStartFrame()
    if frames < 0:
        # nothing may start inside the gap, nothing that moves back may land on a clip that stays (a clip that only
        # crosses the gap, as a card left running over it does, is fine), and no marker may be lost in it
        for kind in ("video", "audio"):
            for k in range(1, (tl.GetTrackCount(kind) or 0) + 1):
                spans = [(it.GetStart() - t0, it.GetStart() - t0 + it.GetDuration()) for it in tl.GetItemListInTrack(kind, k) or []]
                stay = [b for a, b in spans if a < at]
                move = [a for a, b in spans if a >= at]
                if any(a < at - frames for a in move) or (stay and move and max(stay) > min(move) + frames):
                    raise ValueError(f"{kind} {k} has clips in {at}–{at - frames}: only an empty gap closes")
        marks = [f for f in (tl.GetMarkers() or {}) if at <= f < at - frames]
        if marks:
            raise ValueError(f"timeline markers in {at}–{at - frames} ({len(marks)}): move them before closing the gap")
    end, home = idle(tl, wait)
    before = _starts(tl, t0)                     # after idle(): an edit before then would make it stale
    n = 1 if frames > 0 else 1 - frames
    filler = _filler(tl, n)
    if not tl.AddTrack("video"):
        raise RuntimeError("couldn't add the temporary track")
    k = tl.GetTrackCount("video")
    temp, err = None, None
    try:
        got = pool.AppendToTimeline([{"mediaPoolItem": filler, "startFrame": 0, "endFrame": n, "mediaType": 1,
                                      "trackIndex": k, "recordFrame": t0 + at - 1}]) or []
        temp = got[0] if got else None
        if temp is None or temp.GetStart() - t0 != at - 1 or temp.GetDuration() != n:
            raise RuntimeError("couldn't lay the temporary clip")
        if not _stretch(temp, n, n + frames):
            raise RuntimeError(f"Resolve wouldn't make the temporary clip {n + frames} frames long")
    except Exception as e:  # noqa: BLE001 - reported below with what actually moved
        err = e
    finally:
        if temp is not None:
            tl.DeleteClips([temp], False)
        track_gone = bool(tl.DeleteTrack("video", k))
        home = _playhead(home, tl, t0, at, frames)
        if tl.GetCurrentTimecode() != home:
            tl.SetCurrentTimecode(home)
    after = _starts(tl, t0)
    moved = tl.GetEndFrame() - end
    wrong = [u for u, s in before.items() if after.get(u) != (s + frames if s >= at else s)]
    problems = ([str(err)] if err else []) + ([f"{len(wrong)} of {len(before)} clips ended up in the wrong place"] if wrong else []) + \
               ([f"the end moved by {moved}, not {frames}"] if moved != frames else []) + \
               ([f"left an empty video track {k}: delete it"] if not track_gone else [])
    if problems:
        undo = f"; undo with at={at}, frames={-moved}" if moved else ""
        raise GapError("; ".join(problems) + f" (the timeline moved {moved:+d}{undo})", moved)
    return {"at": at, "frames": frames, "moved": moved}
