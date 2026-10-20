"""Open (or close) a gap on every track at once: the insert Resolve's API doesn't have.

Resolve 21.1 can't insert time, and DeleteClips(ripple) only takes time out. But a speed change with
`RippleTimeline` moves every item that starts after the changed clip, on every track, and the timeline's
markers with them (tried on the Spacesheep cut, 2026-10-20: +15 frames, then back, exactly). So:

  1. a temporary video track goes on top, and a 1-frame clip on it at `at - 1`;
  2. SetSpeed stretches that clip to 1 + `frames` with RippleTimeline, so everything starting at `at` or
     later moves `frames` later (a clip that only crosses `at` stays, and keeps playing);
  3. the clip and the track come off again.

A negative `frames` closes a gap the same way (a clip of 1 + |frames| sped up to 1 frame), but only if
nothing starts in [at, at + |frames|) and nothing that moves back would land on a clip that stays. It
refuses to start unless the timeline has been still for `wait` seconds, and checks the end moved by
exactly `frames`.
"""

from __future__ import annotations

from .framecheck import Busy, idle


def _items(tl, kind):
    return [it for k in range(1, (tl.GetTrackCount(kind) or 0) + 1) for it in (tl.GetItemListInTrack(kind, k) or [])]


def _filler(tl):
    """A media pool item already on the timeline, at the timeline's own rate, to lay the temporary clip from."""
    fps = float(tl.GetSetting("timelineFrameRate") or 25) if hasattr(tl, "GetSetting") else 25.0
    for it in _items(tl, "video"):
        mp = it.GetMediaPoolItem()
        if mp and abs(float(mp.GetClipProperty("FPS") or 0) - fps) < 0.01:
            return mp
    raise RuntimeError("no clip on the timeline at its own frame rate to lay the temporary clip from")


def insert(pool, tl, at: int, frames: int, *, wait: float = 3.0) -> dict:
    """Move everything starting at timeline frame `at` (0 = the first frame) by `frames` (negative: back)."""
    if not frames:
        return {"at": at, "frames": 0, "moved": 0}
    t0 = tl.GetStartFrame()
    if frames < 0:
        # nothing may start inside the gap, and nothing that moves back may land on a clip that stays
        # (a clip that only crosses the gap, as a card left running over it does, is fine)
        for kind in ("video", "audio"):
            for k in range(1, (tl.GetTrackCount(kind) or 0) + 1):
                spans = [(it.GetStart() - t0, it.GetStart() - t0 + it.GetDuration()) for it in tl.GetItemListInTrack(kind, k) or []]
                stay = [b for a, b in spans if a < at]
                move = [a for a, b in spans if a >= at]
                if any(a < at - frames for a in move) or (stay and move and max(stay) > min(move) + frames):
                    raise ValueError(f"{kind} {k} has clips in {at}–{at - frames}: only an empty gap closes")
    end, _ = idle(tl, wait)
    n = 1 if frames > 0 else 1 - frames
    if not tl.AddTrack("video"):
        raise RuntimeError("couldn't add the temporary track")
    k = tl.GetTrackCount("video")
    temp = None
    try:
        got = pool.AppendToTimeline([{"mediaPoolItem": _filler(tl), "startFrame": 0, "endFrame": n, "mediaType": 1,
                                      "trackIndex": k, "recordFrame": t0 + at - 1}]) or []
        temp = got[0] if got else None
        if temp is None or temp.GetStart() - t0 != at - 1 or temp.GetDuration() != n:
            raise RuntimeError("couldn't lay the temporary clip")
        if not temp.SetSpeed({"Percentage": 100.0 * n / (n + frames), "RippleTimeline": True}):
            raise RuntimeError("Resolve refused the speed change")
    finally:
        if temp is not None:
            tl.DeleteClips([temp], False)
        tl.DeleteTrack("video", k)
    moved = tl.GetEndFrame() - end
    if moved != frames:
        raise Busy(f"the end moved by {moved}, not {frames}: check the timeline")
    return {"at": at, "frames": frames, "moved": moved}
