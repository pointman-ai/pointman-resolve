"""The CDL grades our tools set, by clip (PFE-42). Resolve has SetCDL but no GetCDL, so a snapshot can't read a
grade back, and the shadow render drew every graded clip ungraded: Spacesheep's first marker post showed James
orange, before the cutout's white-balance fix. Any tool that calls SetCDL also writes it here, under the clip's
GetUniqueId (which survives ripples, trims and restarts); snapshots attach it as `item.cdl`, and the renderer
draws it.

~/.pointman-resolve/grades.json:
  {"items": {"<unique id>": {"cdl": {"Slope": "r g b", "Offset": "r g b", "Power": "r g b", "Saturation": "s"},
                             "by": "<tool>", "at": "<utc>"}}}
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import journal

NEUTRAL = {"Slope": "1 1 1", "Offset": "0 0 0", "Power": "1 1 1", "Saturation": "1"}


def path() -> Path:
    return journal.home() / "grades.json"


def load() -> dict:
    try:
        return json.loads(path().read_text(encoding="utf-8")).get("items") or {}
    except (OSError, ValueError):
        return {}


def record(uid: str, cdl: dict | None, by: str) -> None:
    """Note the CDL just set on a clip (None or a neutral CDL: it's ungraded again)."""
    items = load()
    if cdl is None or parse(cdl) == parse(NEUTRAL):
        items.pop(uid, None)
    else:
        items[uid] = {"cdl": {k: cdl[k] for k in ("Slope", "Offset", "Power", "Saturation") if k in cdl}, "by": by,
                      "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def parse(cdl: dict) -> tuple:
    """((slope r g b), (offset r g b), (power r g b), saturation) as floats."""
    def three(v, d):
        xs = [float(x) for x in str(v if v is not None else d).split()]
        return tuple((xs * 3)[:3]) if len(xs) == 1 else tuple(xs[:3])
    return (three(cdl.get("Slope"), "1 1 1"), three(cdl.get("Offset"), "0 0 0"), three(cdl.get("Power"), "1 1 1"),
            float(cdl.get("Saturation") if cdl.get("Saturation") is not None else 1))


def attach(items: list[dict]) -> int:
    """Put each recorded grade on its snapshot item as `cdl`; how many."""
    known = load()
    n = 0
    for it in items:
        g = known.get(it.get("id"))
        if g:
            it["cdl"] = g["cdl"]
            n += 1
    return n


def filters(cdl: dict | None) -> list[str]:
    """ffmpeg filters drawing a CDL the way Resolve applies it to display-referred RGB: out = clamp(in·slope +
    offset)^power per channel, then saturation around Rec.709 luma. Alpha is kept."""
    if not cdl:
        return []
    (sr, sg, sb), (orr, og, ob), (pr, pg, pb), sat = parse(cdl)
    if ((sr, sg, sb), (orr, og, ob), (pr, pg, pb), sat) == parse(NEUTRAL):
        return []
    ch = lambda s, o, p: f"'255*pow(clip(val/255*{s}+{o}\\,0\\,1)\\,{p})'"
    out = ["format=gbrap", f"lutrgb=r={ch(sr, orr, pr)}:g={ch(sg, og, pg)}:b={ch(sb, ob, pb)}"]
    if abs(sat - 1) > 1e-6:
        lr, lg, lb, k = 0.2126, 0.7152, 0.0722, 1 - sat
        out.append("colorchannelmixer="
                   f"rr={lr * k + sat:.5f}:rg={lg * k:.5f}:rb={lb * k:.5f}:"
                   f"gr={lr * k:.5f}:gg={lg * k + sat:.5f}:gb={lb * k:.5f}:"
                   f"br={lr * k:.5f}:bg={lg * k:.5f}:bb={lb * k + sat:.5f}")
    out.append("format=yuva444p")
    return out
