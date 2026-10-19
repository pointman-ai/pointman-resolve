"""Resolve's own frame against the shadow render (PFE-36): geometry is checked in Resolve, not only in our
model of it. On Spacesheep v1 every cutout was invisible in Resolve for a day while the renderer and the placer
agreed with each other, because both took Crop/Pan/Tilt for pixels (they're 2 px units on 1080p).

- grab(): the frame at a timeline frame, exported by Resolve (ExportCurrentFrameAsStill). It moves the playhead,
  so it first checks nobody is working (GetEndFrame and GetCurrentTimecode read twice, `wait` seconds apart: the
  first catches edits, the second scrubbing and playback) and puts the playhead back afterwards.
- compare(): PSNR between two images and the bounding box of where they differ, with ffmpeg only.
- suspects(): the clips drawn at that frame whose rectangle overlaps the difference, the closest fit (IoU) first.
- calibrate(): the px-per-unit (Crop/Pan/Tilt) whose shadow render matches Resolve's frame best. It only reads:
  no probe value is ever set on the person's clips.
"""

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import journal
from . import render as R

UNITS = (0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0)
MOVED = 20.0  # units of Pan/Tilt/crop a clip needs before it can tell units apart


class Busy(RuntimeError):
    """Someone is editing or playing: the playhead isn't ours to move."""


def timecode(frame: int, fps: float, start_frame: int) -> str:
    """HH:MM:SS:FF of timeline frame `frame` (0 = the timeline's first frame), non-drop."""
    f = int(start_frame) + int(frame)
    n = round(fps)
    return f"{f // (3600 * n):02d}:{f // (60 * n) % 60:02d}:{f // n % 60:02d}:{f % n:02d}"


def idle(tl, wait: float = 3.0) -> tuple[int, str]:
    """(end frame, timecode) when both stay the same over `wait` seconds; Busy otherwise."""
    first = (tl.GetEndFrame(), tl.GetCurrentTimecode())
    time.sleep(wait)
    second = (tl.GetEndFrame(), tl.GetCurrentTimecode())
    if first != second:
        raise Busy(f"the timeline is being worked on (end {first[0]} → {second[0]}, playhead {first[1]} → {second[1]})")
    return second


def grab(project, tl, frame: int, fps: float, start_frame: int, out: Path, *, wait: float = 3.0) -> dict:
    """Resolve's picture at timeline frame `frame`, as a PNG at `out`; the playhead goes back where it was."""
    _, home = idle(tl, wait)
    out.parent.mkdir(parents=True, exist_ok=True)
    tc = timecode(frame, fps, start_frame)
    try:
        if not tl.SetCurrentTimecode(tc):
            raise RuntimeError(f"Resolve wouldn't move the playhead to {tc}")
        if not project.ExportCurrentFrameAsStill(str(out)) or not out.exists():
            raise RuntimeError("Resolve couldn't export the frame (it exports from the Edit or Color page)")
    finally:
        tl.SetCurrentTimecode(home)
    back = tl.GetCurrentTimecode()
    return {"timecode": tc, "playhead": home, "restored": back == home}


def _ffmpeg(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-y", *args], capture_output=True, text=True)


def compare(a: Path, b: Path, *, width: int = 960, threshold: int = 24, side_by_side: Path | None = None) -> dict:
    """PSNR (dB, higher is closer) of a against b at `width`, and the box (in a's own pixels) where they differ.
    The box is found at 480 px wide on lightly blurred grey images, so text that a scaler draws a pixel apart
    doesn't count, while a clip that's missing, moved or cropped does. With side_by_side: a | b | the
    difference, amplified."""
    w = width - width % 2
    scale = f"scale={w}:-2:flags=area,format=yuv444p"
    run = _ffmpeg("-i", str(a), "-i", str(b), "-lavfi", f"[0]{scale}[x];[1]{scale}[y];[x][y]psnr", "-f", "null", "-")
    m = re.search(r"average:([0-9.]+|inf)", run.stderr)
    psnr = None if not m else (99.0 if m.group(1) == "inf" else float(m.group(1)))
    small = "scale=480:-2:flags=area,format=gray,gblur=sigma=1.5"
    run = _ffmpeg("-i", str(a), "-i", str(b), "-lavfi",
                  f"[0]{small}[x];[1]{small}[y];[x][y]blend=all_mode=difference,"
                  f"lut=y='if(gt(val,{threshold}),255,0)',bbox=min_val=128", "-f", "null", "-")
    boxes = re.findall(r"x1:(\d+) x2:(\d+) y1:(\d+) y2:(\d+)", run.stderr)
    box = None
    if boxes:
        x1 = min(int(v[0]) for v in boxes); x2 = max(int(v[1]) for v in boxes)
        y1 = min(int(v[2]) for v in boxes); y2 = max(int(v[3]) for v in boxes)
        k = (_size(a)[0] or 480) / 480
        box = [round(x1 * k), round(y1 * k), round((x2 - x1 + 1) * k), round((y2 - y1 + 1) * k)]
    if side_by_side:
        rgb = f"scale={w}:-2:flags=area,format=gbrp"
        _ffmpeg("-i", str(a), "-i", str(b), "-lavfi",
                f"[0]{rgb},split[x][x2];[1]{rgb},split[y][y2];[x2][y2]blend=all_mode=difference,"
                f"lutrgb=r='min(val*4,255)':g='min(val*4,255)':b='min(val*4,255)'[d];[x][y][d]hstack=3,format=yuv420p",
                "-frames:v", "1", str(side_by_side))
    return {"psnr": round(psnr, 2) if psnr is not None else None, "diff_box": box}


def _size(path: Path) -> tuple[int, int]:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout.strip()
    try:
        w, h = (int(v) for v in out.split(",")[:2])
        return w, h
    except ValueError:
        return 0, 0


def suspects(plan: dict, box: list | None, *, limit: int = 5) -> list[dict]:
    """Clips drawn at the frame whose rectangle overlaps the difference box, the closest fit (IoU) first; `covers`
    is how much of the box each one covers."""
    if not box:
        return []
    bx, by, bw, bh = box
    out = []
    for v in plan["video"]:
        x, y, w, h = v["geo"]["rect"]
        ix = max(0.0, min(x + w, bx + bw) - max(x, bx))
        iy = max(0.0, min(y + h, by + bh) - max(y, by))
        if ix * iy > 0:
            inter = ix * iy
            out.append({"id": v["id"], "lane": f"V{v['track']}", "name": v.get("name"),
                        "iou": round(inter / max(w * h + bw * bh - inter, 1), 3), "covers": round(inter / max(bw * bh, 1), 3),
                        "rect": [round(c) for c in (x, y, w, h)],
                        "props": {k: v["props"][k] for k in ("ZoomX", "Pan", "Tilt", "CropLeft", "CropRight", "CropTop",
                                                              "CropBottom") if k in v["props"]}})
    return sorted(out, key=lambda s: (-s["iou"], -int(s["lane"][1:])))[:limit]


def probe_frame(snap: dict) -> int | None:
    """A frame where a visible clip is moved or cropped by at least MOVED units, so units can be told apart:
    the middle of the biggest such clip, top tracks first."""
    tl = snap["timeline"]
    best = None
    for it in snap["items"]:
        if it.get("kind") != "video" or not it.get("file") or it.get("type") in ("transition", "generator", "title"):
            continue
        p = it.get("props") or {}
        moved = max(abs(float(p.get(k) or 0)) for k in ("Pan", "Tilt", "CropLeft", "CropRight", "CropTop", "CropBottom"))
        if moved < MOVED or (it.get("frames") or 0) < 3:
            continue
        mid = it["record"] + it["frames"] // 2
        hidden = any(o is not it and o.get("kind") == "video" and o["track"] > it["track"] and o.get("file")
                     and o["record"] <= mid < o["record"] + (o.get("frames") or 0)
                     and (g := R.geometry(o, tl["width"], tl["height"])) and R.covers_frame(g, tl["width"], tl["height"])
                     for o in snap["items"])
        if hidden:
            continue
        key = (it["track"], moved)
        if best is None or key > best[0]:
            best = (key, mid)
    return best[1] if best else None


def calibrate(snap: dict, frame: int, resolve_png: Path, work: Path, *, units=UNITS) -> dict:
    """Render the frame at each candidate px-per-unit and keep the one closest to Resolve's own frame."""
    tl = snap["timeline"]
    scores = {}
    for u in units:
        trial = {**snap, "timeline": {**tl, "px_per_unit": u}}
        out = work / f"shadow-u{u}.png"
        r = R.still(trial, frame, out, width=tl["width"] or 1920)
        if r["ok"]:
            scores[u] = compare(resolve_png, out)["psnr"]
    scored = {u: s for u, s in scores.items() if s is not None}
    if not scored:
        return {"ok": False, "error": "no shadow render came out", "scores": scores}
    best = max(scored, key=scored.get)
    ranked = sorted(scored.values(), reverse=True)
    margin = round(ranked[0] - ranked[1], 2) if len(ranked) > 1 else None
    return {"ok": margin is None or margin >= 0.5, "px_per_unit": best, "psnr": scored[best], "margin_db": margin,
            "scores": scores, "resolution": f"{tl['width']}x{tl['height']}"}


def calibration_file() -> Path:
    return journal.home() / "calibration.json"


def stored_units(width: int | None, height: int | None) -> float | None:
    """The px-per-unit measured for this timeline resolution, if any."""
    try:
        data = json.loads(calibration_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entry = data.get(f"{width}x{height}") or {}
    return entry.get("px_per_unit")


def store(result: dict, *, project: str, timeline: str, frame: int) -> Path:
    path = calibration_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[result["resolution"]] = {"px_per_unit": result["px_per_unit"], "psnr": result["psnr"],
                                  "margin_db": result["margin_db"], "project": project, "timeline": timeline,
                                  "frame": frame, "measured": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return path
