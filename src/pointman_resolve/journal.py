"""The journal of our own edits, so a diff can tell them from James's.

Every tool that changes a timeline (a placer, a re-lay, a slip) appends one line to
~/.pointman-resolve/journal.jsonl ($POINTMAN_RESOLVE_HOME overrides the folder):

    {"time": "2026-10-19T02:15:00.123456Z", "tool": "place_cutouts", "timeline": "<timeline id>",
     "removed": ["<item id>", ...], "added": ["<item id>", ...], "note": "8 desk ranges"}

`record()` writes one; `entries()` reads those in a time window. Times are UTC ISO 8601, so they
sort as strings.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("POINTMAN_RESOLVE_HOME", Path.home() / ".pointman-resolve"))


def path() -> Path:
    return home() / "journal.jsonl"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def record(tool: str, removed=(), added=(), *, timeline: str | None = None, note: str = "") -> dict:
    """Append one edit of ours. One write of one line, so concurrent tools don't interleave."""
    entry = {"time": now(), "tool": tool, "timeline": timeline, "removed": list(removed), "added": list(added),
             "note": note}
    path().parent.mkdir(parents=True, exist_ok=True)
    with path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def entries(after: str | None = None, until: str | None = None, *, timeline: str | None = None) -> list[dict]:
    """Our edits with after < time <= until (either end open), optionally for one timeline only."""
    if not path().exists():
        return []
    out = []
    for line in path().read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line cut short by a crash: skip it rather than lose the rest
        t = e.get("time", "")
        if after and t <= after or until and t > until:
            continue
        if timeline and e.get("timeline") not in (None, timeline):
            continue
        out.append(e)
    return out
