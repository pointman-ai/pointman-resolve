"""QC report on a timeline (PFE-16): what a viewer would notice that an edit can introduce, found from a snapshot
and the source files, without Resolve or the playhead.

Picture (from the snapshot):
- black: frames where no enabled video track has a clip;
- flash: a visible clip shorter than 3 frames, or a 1-2 frame gap on a track with clips either side, where
  what's below shows for a blink (60 fps rounding and razor slips leave these);
- frozen: a video source (not an alpha cutout or graphic) whose frames don't change across the stretches the cut
  uses. On Spacesheep v1, three OBS recordings were one frozen frame for 31 minutes of the cut.
Sound (the mix of a range, drawn like the shadow render, up to AUDIO_MAX_S):
- loudness (integrated LUFS) and true peak, against a target (YouTube: -14 LUFS, peaks under -1 dBTP);
- dead air: stretches of the mix quieter than -50 dB for 1.5 s or more.
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from . import render as R

FLASH = 3                 # frames: shorter than this, a clip or a gap is a blink
FROZEN_DB = 50.0          # PSNR between frames far apart above this: the source isn't changing
FROZEN_MIN_S = 20         # only sources the cut uses for this long are worth decoding
AUDIO_MAX_S = 900         # the mix is rendered in one go: 15 minutes at most per report
TARGET_LUFS, MAX_PEAK = -14.0, -1.0
DEAD_DB, DEAD_S = -50, 1.5


def niced():
    os.nice(10)


def tc(frame: int, fps: float) -> str:
    s = frame / fps
    return f"{int(s // 60)}:{s % 60:05.2f}"


def _spans(frames: list[int]) -> list[tuple[int, int]]:
    out = []
    for f in sorted(frames):
        if out and f == out[-1][1]:
            out[-1][1] = f + 1
        else:
            out.append([f, f + 1])
    return [tuple(s) for s in out]


def picture(snap: dict, start: int, end: int) -> list[dict]:
    """Black frames, flash clips and flash gaps in [start, end)."""
    tl = snap["timeline"]
    fps = tl.get("fps") or 25.0
    enabled = {t["index"] for t in snap["tracks"] if t["kind"] == "video" and t.get("enabled") is not False}
    video = [i for i in snap["items"] if i.get("kind") == "video" and i.get("track") in enabled
             and i.get("type") != "transition" and i.get("record") is not None]
    issues = []
    covered = set()
    for it in video:
        a, b = max(start, it["record"]), min(end, it["record"] + (it.get("frames") or 0))
        covered.update(range(a, b))
    for a, b in _spans([f for f in range(start, end) if f not in covered]):
        issues.append({"check": "black", "at": a, "to": b, "what": f"no video for {b - a} frames ({tc(a, fps)})"})
    p = R.plan(snap, start, end)                      # what's drawn, with hidden clips already left out
    shown = {v["id"] for v in p["video"]}
    by_track: dict[int, list] = {}
    for it in video:
        by_track.setdefault(it["track"], []).append(it)
    for track, items in sorted(by_track.items()):
        items.sort(key=lambda i: i["record"])
        for it in items:
            n = it.get("frames") or 0
            if n < FLASH and it["id"] in shown and start <= it["record"] < end:
                issues.append({"check": "flash", "at": it["record"], "to": it["record"] + n, "lane": f"V{track}",
                               "what": f"V{track} {it.get('name')} is {n} frame{'s' * (n != 1)} long"})
        for x, y in zip(items, items[1:]):
            gap_a, gap_b = x["record"] + (x.get("frames") or 0), y["record"]
            if 0 < gap_b - gap_a < FLASH and start <= gap_a < end:
                issues.append({"check": "flash", "at": gap_a, "to": gap_b, "lane": f"V{track}",
                               "what": f"a {gap_b - gap_a}-frame gap on V{track} between {x.get('name')} and {y.get('name')}"})
    return issues


def frozen(snap: dict, *, samples: int = 4) -> list[dict]:
    """Video sources the cut uses whose frames are the same from end to end (a capture that froze)."""
    fps = snap["timeline"].get("fps") or 25.0
    uses: dict[str, list] = {}
    for it in snap["items"]:
        f = it.get("file")
        if it.get("kind") != "video" or not f or it.get("type") == "transition" or not (it.get("frames") or 0):
            continue
        uses.setdefault(f, []).append(it)
    out = []
    for f, items in sorted(uses.items()):
        total = sum(i["frames"] for i in items) / fps
        m = R.probe(f)
        if total < FROZEN_MIN_S or m["alpha"] or not Path(f).exists():
            continue
        # across the whole file: a capture that froze is the same from end to end, while a page that just sits
        # still for the minute the cut uses is fine (Spacesheep's 10-32-22)
        dur = duration(f)
        if not dur:
            continue
        lo, hi = 0.0, dur
        times = [lo + (hi - lo) * (k + 0.5) / samples for k in range(samples)]
        pngs = []
        with tempfile.TemporaryDirectory() as d:
            for k, t in enumerate(times):
                png = Path(d) / f"{k}.png"
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{max(t - 1, 0):.3f}", "-i", f,
                                "-ss", "1", "-frames:v", "1", "-vf", "scale=320:-2", str(png)],
                               capture_output=True, preexec_fn=niced)
                if png.exists():
                    pngs.append(png)
            scores = [psnr(pngs[0], q) for q in pngs[1:]]
        if len(pngs) >= 3 and all(s is not None and s >= FROZEN_DB for s in scores):
            used = [i for i in items if i.get("record") is not None]
            a = min(i["record"] for i in used)
            b = max(i["record"] + i["frames"] for i in used)
            out.append({"check": "frozen", "at": a, "to": b, "file": f,
                        "what": f"{Path(f).name} doesn't change: {len(pngs)} frames spread over its {hi / 60:.0f} min "
                                f"are the same (PSNR ≥ {FROZEN_DB:.0f} dB). Used for {total / 60:.1f} min of the cut, "
                                f"{tc(a, fps)}–{tc(b, fps)}"})
    return out


def duration(path: str) -> float | None:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                         capture_output=True, text=True).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return None


def psnr(a: Path, b: Path) -> float | None:
    run = subprocess.run(["ffmpeg", "-nostdin", "-i", str(a), "-i", str(b), "-lavfi", "psnr", "-f", "null", "-"],
                         capture_output=True, text=True)
    m = re.search(r"average:([0-9.]+|inf)", run.stderr)
    return None if not m else (99.0 if m.group(1) == "inf" else float(m.group(1)))


def mix(snap: dict, start: int, end: int, out: Path) -> dict:
    """The audio of [start, end) mixed as the shadow render mixes it, as a 48 kHz stereo WAV."""
    p = R.plan(snap, start, end)
    if not p["audio"]:
        return {"ok": False, "error": "no audio in the range"}
    args, chains, labels = ["ffmpeg", "-nostdin", "-v", "error", "-y"], [], []
    for k, a in enumerate(p["audio"]):
        args += ["-ss", f"{a['src']:.4f}", "-t", f"{a['dur']:.4f}", "-i", a["file"]]
        vol = float(a["props"].get("AudioVolume") or 0)
        f = [f"[{k}:a]aresample=48000", "aformat=channel_layouts=stereo", f"volume={vol:.2f}dB",
             *R.fade_filters(a, p["fps"], audio=True), f"adelay=delays={round(a['on'] * 1000)}:all=1"]
        chains.append(",".join(f) + f"[a{k}]")
        labels.append(f"[a{k}]")
    chains.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:duration=longest,apad,atrim=0:{p['dur']:.3f}[m]")
    run = subprocess.run(args + ["-filter_complex", ";".join(chains), "-map", "[m]", "-c:a", "pcm_s16le", str(out)],
                         capture_output=True, text=True, preexec_fn=niced)
    return {"ok": run.returncode == 0 and out.exists(), "error": run.stderr.strip()[-400:] or None, "clips": len(labels)}


def sound(snap: dict, start: int, end: int, *, target: float = TARGET_LUFS) -> tuple[dict, list[dict]]:
    """Loudness, true peak and dead air of the mix of [start, end)."""
    fps = snap["timeline"].get("fps") or 25.0
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / "mix.wav"
        m = mix(snap, start, end, wav)
        if not m["ok"]:
            return {"error": m["error"]}, []
        run = subprocess.run(["ffmpeg", "-nostdin", "-i", str(wav), "-af",
                              f"ebur128=peak=true,silencedetect=noise={DEAD_DB}dB:d={DEAD_S}", "-f", "null", "-"],
                             capture_output=True, text=True, preexec_fn=niced)
    err = run.stderr
    lufs = re.findall(r"I:\s+(-?[0-9.]+) LUFS", err)
    peak = re.findall(r"Peak:\s+(-?[0-9.]+|-inf) dBFS", err)
    stats = {"lufs": float(lufs[-1]) if lufs else None, "true_peak": float(peak[-1]) if peak and peak[-1] != "-inf" else None,
             "target_lufs": target, "clips": m["clips"]}
    issues = []
    if stats["lufs"] is not None and abs(stats["lufs"] - target) > 1.0:
        issues.append({"check": "loudness", "at": start, "to": end,
                       "what": f"{stats['lufs']:.1f} LUFS, target {target:.0f} (raise by {target - stats['lufs']:+.1f} dB)"})
    if stats["true_peak"] is not None and stats["true_peak"] > MAX_PEAK:
        issues.append({"check": "peak", "at": start, "to": end, "what": f"true peak {stats['true_peak']:.1f} dBTP, over {MAX_PEAK:.0f}"})
    starts = [float(x) for x in re.findall(r"silence_start: (-?[0-9.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", err)]
    for s, e in zip(starts, ends + [ (end - start) / fps ] * (len(starts) - len(ends))):
        a, b = start + round(max(s, 0) * fps), start + round(e * fps)
        issues.append({"check": "dead air", "at": a, "to": b, "what": f"{e - max(s, 0):.1f} s of near-silence at {tc(a, fps)}"})
    return stats, issues


def report(snap: dict, start: int | None = None, end: int | None = None, *, audio: bool = True,
           sources: bool = True) -> dict:
    tl = snap["timeline"]
    fps = tl.get("fps") or 25.0
    full = int(tl["end_frame"] - tl["start_frame"])
    start, end = max(0, start or 0), min(full, end or full)
    issues = picture(snap, start, end)
    if sources:
        issues += frozen(snap)
    stats = None
    if audio:
        if (end - start) / fps <= AUDIO_MAX_S:
            stats, more = sound(snap, start, end)
            issues += more
        else:
            stats = {"skipped": f"the range is over {AUDIO_MAX_S // 60} min: give from/to for the sound checks"}
    issues.sort(key=lambda i: (i["at"], i["check"]))
    counts = {}
    for i in issues:
        counts[i["check"]] = counts.get(i["check"], 0) + 1
    return {"ok": not issues, "from": start, "to": end, "range": f"{tc(start, fps)}–{tc(end, fps)}", "counts": counts,
            "sound": stats, "issues": issues}


def markdown(r: dict, title: str) -> str:
    lines = [f"# QC: {title}, {r['range']}", ""]
    if r["ok"]:
        lines.append("Nothing found.")
    else:
        lines.append(", ".join(f"{n} {k}" for k, n in sorted(r["counts"].items())))
    s = r.get("sound") or {}
    if s.get("lufs") is not None:
        lines.append(f"\nSound: {s['lufs']:.1f} LUFS (target {s['target_lufs']:.0f}), true peak {s.get('true_peak')} dBTP.")
    elif s.get("skipped"):
        lines.append(f"\nSound: {s['skipped']}.")
    if r["issues"]:
        lines += ["", "| At | Check | What |", "|---|---|---|"]
        lines += [f"| {i['at']} | {i['check']} | {i['what']} |" for i in r["issues"][:200]]
    return "\n".join(lines) + "\n"
