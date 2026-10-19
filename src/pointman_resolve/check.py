"""Round-trip check: does a snapshot's JSON agree, item by item, with Resolve's OTIO export of the
same timeline taken at the same moment? Offline: reads the two files, no Resolve calls.

Compares, per track: the clips in order (transitions apart), and for each clip its record frame, length,
in-point and transform/crop values, then the transitions and the timeline markers. OTIO stores Pan and
CropLeft/Right as a fraction of half the width, Tilt and CropTop/Bottom of half the height (PFE-9);
the snapshot keeps Resolve's pixels, so those are scaled here.
"""

import json
from pathlib import Path

PROPS = {"ZoomX": ("Transform", "transformationZoomX"), "ZoomY": ("Transform", "transformationZoomY"),
         "Pan": ("Transform", "transformationPan"), "Tilt": ("Transform", "transformationTilt"),
         "CropLeft": ("Cropping", "cropLeft"), "CropRight": ("Cropping", "cropRight"),
         "CropTop": ("Cropping", "cropTop"), "CropBottom": ("Cropping", "cropBottom")}
DEFAULT = {"ZoomX": 1.0, "ZoomY": 1.0}


def scale(prop: str, width: int, height: int) -> float:
    if prop in ("Pan", "CropLeft", "CropRight"):
        return width / 2
    if prop in ("Tilt", "CropTop", "CropBottom"):
        return height / 2
    return 1.0


def otio_track(track: dict, fps: float) -> tuple[list[dict], list[dict]]:
    """(clips, transitions) with record frames; gaps and clips take time, transitions don't."""
    at, clips, transitions = 0, [], []
    for c in track["children"]:
        schema = c["OTIO_SCHEMA"].split(".")[0]
        if schema == "Transition":
            transitions.append({"record": round(at - c["in_offset"]["value"]),
                                "frames": round(c["in_offset"]["value"] + c["out_offset"]["value"]), "name": c.get("name")})
            continue
        sr = c["source_range"]
        n = round(sr["duration"]["value"] * fps / sr["duration"]["rate"])
        if schema == "Clip":
            ref = (c.get("media_references") or {}).get(c.get("active_media_reference_key", "DEFAULT_MEDIA"), {})
            avail = (ref.get("available_range") or {}).get("start_time", {}).get("value", 0)
            params = {}
            for e in c.get("effects") or []:
                m = (e.get("metadata") or {}).get("Resolve_OTIO", {})
                for p in m.get("Parameters", []):
                    params[(m.get("Effect Name"), p["Parameter ID"])] = p.get("Parameter Value")
            clips.append({"record": at, "frames": n, "in": round(sr["start_time"]["value"] - avail), "params": params})
        at += n
    return clips, transitions


def check(snapshot_path: Path) -> dict:
    snap = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    otio_file = (snap.get("otio") or {}).get("file")
    if not otio_file:
        return {"ok": False, "error": "this snapshot has no OTIO export beside it"}
    otio = json.loads((Path(snapshot_path).parent / otio_file).read_text(encoding="utf-8"))
    tl = snap["timeline"]
    fps, w, h = tl.get("fps") or 25.0, tl.get("width") or 1920, tl.get("height") or 1080
    by_kind = {"Video": [], "Audio": []}
    for t in otio["tracks"]["children"]:
        by_kind.setdefault(t.get("kind"), []).append(t)
    totals = dict.fromkeys(("items", "place_ok", "length_ok", "in_ok", "props_checked", "props_ok",
                            "transitions", "transitions_ok"), 0)
    problems, tracks = [], {}
    for kind, otio_kind in (("video", "Video"), ("audio", "Audio")):
        for idx, track in enumerate(by_kind.get(otio_kind, []), 1):
            items = sorted((i for i in snap["items"] if i["kind"] == kind and i["track"] == idx),
                           key=lambda i: i["record"])
            api = [i for i in items if i.get("type") != "transition"]
            api_tr = [i for i in items if i.get("type") == "transition"]
            clips, trans = otio_track(track, fps)
            lane = f"{kind[0].upper()}{idx}"
            if len(api) != len(clips):
                problems.append(f"{lane}: {len(api)} items in the snapshot, {len(clips)} clips in the OTIO")
            r = dict.fromkeys(("items", "place_ok", "length_ok", "in_ok", "props_checked", "props_ok"), 0)
            for a, o in zip(api, clips):
                r["items"] += 1
                r["place_ok"] += a["record"] == o["record"]
                r["length_ok"] += a["frames"] == o["frames"]
                r["in_ok"] += a["in"] == o["in"]
                for prop, key in PROPS.items():
                    if kind != "video" or prop not in (a.get("props") or {}):
                        continue
                    want = float(a["props"][prop])
                    got = float(o["params"].get(key, DEFAULT.get(prop, 0.0))) * scale(prop, w, h)
                    r["props_checked"] += 1
                    if abs(got - want) < 1e-3:
                        r["props_ok"] += 1
                    elif len(problems) < 20:
                        problems.append(f"{lane} @{a['record']}: {prop} {want} in the snapshot, {got:.4f} in the OTIO")
                if (a["record"], a["frames"], a["in"]) != (o["record"], o["frames"], o["in"]) and len(problems) < 20:
                    problems.append(f"{lane}: snapshot {a['record']}/{a['frames']}/{a['in']} vs OTIO "
                                    f"{o['record']}/{o['frames']}/{o['in']} (record/frames/in)")
            for a, o in zip(api_tr, trans):
                totals["transitions"] += 1
                totals["transitions_ok"] += (a["record"], a["frames"]) == (o["record"], o["frames"])
            if len(api_tr) != len(trans):
                problems.append(f"{lane}: {len(api_tr)} transitions in the snapshot, {len(trans)} in the OTIO")
            for k in r:
                totals[k] += r[k]
            tracks[lane] = r
    markers_otio = len(otio["tracks"].get("markers") or [])
    markers_ok = markers_otio == len(snap.get("markers") or [])
    if not markers_ok:
        problems.append(f"{len(snap.get('markers') or [])} timeline markers in the snapshot, {markers_otio} in the OTIO")
    clips_total = sum(1 for i in snap["items"] if i.get("type") != "transition" and i["kind"] in ("video", "audio"))
    ok = (not problems and totals["items"] == clips_total and
          totals["place_ok"] == totals["length_ok"] == totals["in_ok"] == totals["items"] and
          totals["props_ok"] == totals["props_checked"] and totals["transitions_ok"] == totals["transitions"])
    return {"ok": ok, "snapshot": Path(snapshot_path).name, "otio": otio_file, **totals,
            "markers": {"snapshot": len(snap.get("markers") or []), "otio": markers_otio},
            "tracks": tracks, "problems": problems}
