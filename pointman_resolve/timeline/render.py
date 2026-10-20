"""Shadow render (PFE-15): any range of a snapshot, composited from the source files with ffmpeg, so an agent
can watch and listen to the edit without touching Resolve or James's screen.

Video tracks are stacked bottom to top, each clip placed the way Resolve places it (measured on the
Spacesheep timeline, Resolve 21.1, "scale to fit"):
  1. fit: the source is scaled to fit the timeline frame, centred (letter/pillarbox),
  2. crop: CropLeft/Right/Top/Bottom cut the fitted image,
  3. zoom about the frame centre, then Pan right and Tilt up.
Crop/Pan/Tilt are in Resolve units, not pixels. Pan and Tilt are shares of the *fitted image*: 960 units
across its width, 540 down its height. Crop isn't: it's timeline pixels before the zoom, `u` px a unit
whatever the source's shape (measured 2026-10-20 with resolve.frame_check on a 1728x1116 recording at zoom
1.478: CropRight 471.7 put Resolve's edge at x 780, where shares of the fitted width would put it at 960;
410.8 puts it at 960). For a source that fits by height the two agree vertically.
A 16:9 source on a 1920x1080 timeline fills it, so a unit is 2 px both ways (UNIT; measured 2026-10-19 against
ExportCurrentFrameAsStill: Pan 100 moved a clip 200 px, CropRight 260 cut 520 px). A 1728x1116 screen
recording fits to 1672x1080, so its Pan unit is 2·1672/1920 = 1.74 px and its Tilt unit 2 px (measured with
resolve.frame_check: Pan −9 and −12 put Resolve's desktop 2 and 3 px right of a 2 px/unit render, and 0 px
off with this rule). A snapshot can say `timeline.px_per_unit` (resolve.calibrate) for other timeline sizes.
So a cropped image whose fitted top-left is (x0, y0) lands at
  (W/2 + zoom·(x0 − W/2) + ux·Pan,  H/2 + zoom·(y0 − H/2) − uy·Tilt),  zoom·(its size),
  ux = u·(fitted width / W),  uy = u·(fitted height / H),  u = px per unit (and per Crop unit).
Opacity and fades (GetFades) apply to alpha. Alpha media (ProRes 4444 cutouts, graphics) keep theirs.
Audio items are mixed at their AudioVolume (dB) with their fades.

Grades are drawn where our tools recorded the CDL they set (grades.py: Resolve has no GetCDL).
Not drawn yet (listed in the result's `skipped`): generators and titles (Resolve doesn't expose
their parameters), transitions, speed changes, other grades, flips and rotation.

Clips fully hidden under an opaque full-frame clip above them for their whole span are not decoded.
"""

import json
import math
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from . import grades

UNIT = 2.0  # px per Crop/Pan/Tilt unit on a 1920x1080 timeline
ALPHA_FMTS = ("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya")


@lru_cache(maxsize=None)
def probe(path: str) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,pix_fmt", "-of", "json", path], capture_output=True, text=True)
    s = (json.loads(out.stdout or "{}").get("streams") or [{}])[0]
    return {"w": s.get("width"), "h": s.get("height"), "alpha": any(f in (s.get("pix_fmt") or "") for f in ALPHA_FMTS)}


def parse_time(v: str | int | float, fps: float) -> int:
    """A timeline frame (0-based) from 'mm:ss.xx', 'hh:mm:ss.xx', seconds ('12.5s') or frames ('300')."""
    v = str(v).strip()
    if ":" in v:
        secs = 0.0
        for part in v.split(":"):
            secs = secs * 60 + float(part)
        return round(secs * fps)
    if v.endswith("s"):
        return round(float(v[:-1]) * fps)
    return int(float(v))


def geometry(item: dict, W: int, H: int, unit: float = UNIT) -> dict | None:
    """Where Resolve draws this video item in a W×H frame: crop box in fitted px, output rect, zoom."""
    m = probe(item["file"])
    if not m["w"] or not m["h"]:
        return None
    p = item.get("props") or {}
    fit = min(W / m["w"], H / m["h"])
    wf, hf = m["w"] * fit, m["h"] * fit
    ox, oy = (W - wf) / 2, (H - hf) / 2
    ux, uy = unit * wf / W, unit * hf / H  # Pan/Tilt units are shares of the fitted image (see the module's note)
    cl, cr = (unit * float(p.get(k) or 0) for k in ("CropLeft", "CropRight"))     # Crop units aren't: u px
    ct, cb = (unit * float(p.get(k) or 0) for k in ("CropTop", "CropBottom"))
    z = float(p.get("ZoomX") or 1.0)
    cw, ch = max(wf - cl - cr, 1), max(hf - ct - cb, 1)
    x = W / 2 + z * (ox + cl - W / 2) + ux * float(p.get("Pan") or 0)
    y = H / 2 + z * (oy + ct - H / 2) - uy * float(p.get("Tilt") or 0)
    return {"fit": fit, "zoom": z, "crop": (cl, ct, cw, ch), "rect": (x, y, z * cw, z * ch), "alpha": m["alpha"],
            "opacity": float(p.get("Opacity", 100) or 100), "fitted": (wf, hf)}


def covers_frame(g: dict, W: int, H: int) -> bool:
    x, y, w, h = g["rect"]
    return (not g["alpha"]) and g["opacity"] >= 100 and x <= 0.5 and y <= 0.5 and x + w >= W - 0.5 and y + h >= H - 0.5


def plan(snap: dict, start: int, end: int) -> dict:
    """Which clips draw (and hear) in [start, end), with their source times and geometry."""
    tl = snap["timeline"]
    fps, W, H = tl["fps"], tl["width"], tl["height"]
    unit = float(tl.get("px_per_unit") or UNIT)
    enabled = {(t["kind"], t["index"]) for t in snap["tracks"] if t.get("enabled") is not False}
    video, audio, skipped = [], [], []
    for it in snap["items"]:
        a, n = it.get("record"), it.get("frames") or 0
        if a is None or a + n <= start or a >= end or (it["kind"], it["track"]) not in enabled:
            continue
        s0, s1 = max(start, a), min(end, a + n)
        seg = {"id": it["id"], "track": it["track"], "file": it.get("file"), "name": it.get("name"),
               "on": (s0 - start) / fps, "dur": (s1 - s0) / fps,
               "src": ((it.get("in") or 0) + s0 - a) / fps, "fades": it.get("fades") or {},
               "head": s0 == a, "tail": s1 == a + n, "frames": n, "props": it.get("props") or {}, "cdl": it.get("cdl")}
        why = None
        if it.get("type") in ("transition", "generator", "title") or not it.get("file"):
            why = it.get("type") or "no file"
        elif (it.get("speed") or {}).get("Percentage") not in (None, 100, 100.0):
            why = f"speed {(it.get('speed') or {}).get('Percentage')}%"
        if why:
            skipped.append({"id": it["id"], "lane": f"{it['kind'][0].upper()}{it['track']}", "at": s0, "why": why,
                            "name": it.get("name")})
            continue
        if it["kind"] == "video":
            g = geometry(it, W, H, unit)
            if g is None:
                skipped.append({"id": it["id"], "lane": f"V{it['track']}", "at": s0, "why": "unreadable media"})
                continue
            video.append({**seg, "geo": g, "range": (s0, s1)})
        elif it["kind"] == "audio":
            audio.append(seg)
    # don't decode what an opaque full-frame clip above hides for the whole span
    hidden = 0
    keep = []
    for v in video:
        a, b = v["range"]
        if any(o["track"] > v["track"] and o["range"][0] <= a and b <= o["range"][1] and covers_frame(o["geo"], W, H)
               for o in video):
            hidden += 1
            continue
        keep.append(v)
    keep.sort(key=lambda v: (v["track"], v["on"]))
    return {"fps": fps, "W": W, "H": H, "video": keep, "audio": audio, "skipped": skipped, "hidden": hidden,
            "dur": (end - start) / fps}


def fade_filters(seg: dict, fps: float, audio: bool) -> list[str]:
    out = []
    fin, fout = float(seg["fades"].get("FadeIn") or 0), float(seg["fades"].get("FadeOut") or 0)
    if fin and seg["head"]:
        out.append(f"afade=t=in:st=0:d={fin / fps:.3f}" if audio else f"fade=t=in:st=0:d={fin / fps:.3f}:alpha=1")
    if fout and seg["tail"]:
        st = max(seg["dur"] - fout / fps, 0)
        out.append(f"afade=t=out:st={st:.3f}:d={fout / fps:.3f}" if audio else f"fade=t=out:st={st:.3f}:d={fout / fps:.3f}:alpha=1")
    return out


def even(x: float) -> int:
    return max(2, int(round(x / 2)) * 2)


def command(p: dict, out: Path, *, width: int = 960, still: bool = False) -> list[str]:
    fps, W, H, dur = p["fps"], p["W"], p["H"], p["dur"]
    s = width / W
    ow, oh = even(W * s), even(H * s)
    args, chains = ["ffmpeg", "-nostdin", "-v", "error", "-y"], []
    chains.append(f"color=black:s={ow}x{oh}:r={fps}:d={dur:.3f},format=yuva444p[base]")
    last, k = "base", 0
    for v in p["video"]:
        args += ["-ss", f"{v['src']:.4f}", "-t", f"{v['dur']:.4f}", "-i", v["file"]]
        g = v["geo"]
        z = g["zoom"] * s
        wf, hf = g["fitted"]
        cl, ct, cw, ch = g["crop"]
        x, y, _, _ = g["rect"]
        sw, sh = even(wf * z), even(hf * z)
        cx, cy = min(round(cl * z), sw - 2), min(round(ct * z), sh - 2)  # rounding must stay inside the scaled frame
        f = [f"[{k}:v]fps={fps}", f"scale={sw}:{sh}", "format=yuva444p",
             f"crop={max(2, min(round(cw * z), sw - cx))}:{max(2, min(round(ch * z), sh - cy))}:{cx}:{cy}"]
        f += grades.filters(v.get("cdl"))                # the grade our tools set (PFE-42)
        if g["opacity"] < 100:
            f.append(f"colorchannelmixer=aa={g['opacity'] / 100:.3f}")
        f += fade_filters(v, fps, audio=False)
        f.append(f"setpts=PTS-STARTPTS+{v['on']:.4f}/TB")
        chains.append(",".join(f) + f"[c{k}]")
        chains.append(f"[{last}][c{k}]overlay=x={round(x * s)}:y={round(y * s)}:eof_action=pass:format=auto[o{k}]")
        last, k = f"o{k}", k + 1
    chains.append(f"[{last}]format=yuv420p[vout]")
    maps = ["-map", "[vout]"]
    if not still and p["audio"]:
        labels = []
        for a in p["audio"]:
            args += ["-ss", f"{a['src']:.4f}", "-t", f"{a['dur']:.4f}", "-i", a["file"]]
            vol = float(a["props"].get("AudioVolume") or 0)
            f = [f"[{k}:a]aresample=48000", "aformat=channel_layouts=stereo", f"volume={vol:.2f}dB"]
            f += fade_filters(a, fps, audio=True)
            f.append(f"adelay=delays={round(a['on'] * 1000)}:all=1")
            chains.append(",".join(f) + f"[a{k}]")
            labels.append(f"[a{k}]")
            k += 1
        chains.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:duration=longest,"
                      f"apad,atrim=0:{dur:.3f}[aout]")
        maps += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k"]
    args += ["-filter_complex", ";".join(chains), *maps]
    if still:
        args += ["-frames:v", "1"]
    else:
        args += ["-t", f"{dur:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-movflags", "+faststart"]
    return args + [str(out)]


def render(snap: dict, start: int, end: int, out: Path, *, width: int = 960, still: bool = False) -> dict:
    """Render timeline frames [start, end) of the snapshot to `out` (an .mp4, or a .png with still)."""
    if still:
        end = start + 1
    p = plan(snap, start, end)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = command(p, out, width=width, still=still)
    # niced: the person may be playing back in Resolve on the same Mac (resolvedoc, PR 10)
    run = subprocess.run(cmd, capture_output=True, text=True, preexec_fn=lambda: os.nice(10))
    return {"ok": run.returncode == 0 and out.exists(), "out": str(out), "from": start, "to": end,
            "seconds": round(p["dur"], 3), "video_clips": len(p["video"]), "audio_clips": len(p["audio"]),
            "hidden": p["hidden"], "skipped": p["skipped"],
            "error": run.stderr.strip()[-800:] if run.returncode else None}


def still(snap: dict, frame: int, out: Path, *, width: int = 1280) -> dict:
    """One timeline frame of the snapshot as a PNG, drawn from the source files (never by Resolve, so
    James's playhead and page don't move). For PFE-30's marker posts."""
    r = render(snap, frame, frame + 1, Path(out), width=width, still=True)
    return {k: r[k] for k in ("ok", "out", "error", "skipped")}
