"""pointman-resolve: read DaVinci Resolve timelines as data.

    pointman-resolve snapshot [--out DIR] [--no-otio]   the open timeline -> DIR/snapshot-<utc>.json (+ .otio)
    pointman-resolve diff A.json B.json [--out FILE]    what changed from A to B, as JSON
    pointman-resolve check SNAPSHOT.json                the snapshot against its own OTIO, item by item

Without --out, snapshots go to ~/.pointman-resolve/snapshots/<project>/<timeline>/. Each command
prints one JSON object on stdout. Read-only against Resolve: no saves, no switches, no edits.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import journal


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "untitled"


def cmd_snapshot(args) -> dict:
    from .resolve import connect
    from .snapshot import write
    resolve = connect()
    if args.out:
        out = Path(args.out).expanduser()
    else:
        project = resolve.GetProjectManager().GetCurrentProject()
        tl = project.GetCurrentTimeline() if project else None
        if not project or not tl:
            raise SystemExit("no project or timeline open in Resolve")
        out = journal.home() / "snapshots" / slug(project.GetName()) / slug(tl.GetName())
    path = write(resolve, out, otio=not args.no_otio)
    snap = json.loads(path.read_text())
    return {"snapshot": str(path), "otio": str(path.with_suffix(".otio")) if (snap.get("otio") or {}).get("file") else None,
            "items": len(snap["items"]), "markers": len(snap["markers"]), "journal": len(snap["journal"]),
            "errors": snap["errors"], "took_s": snap["took_s"], "previous": snap["previous"]}


def cmd_diff(args) -> dict:
    from .diff import diff
    a, b = (json.loads(Path(p).read_text()) for p in (args.a, args.b))
    result = diff(a, b, local_journal=not args.no_local_journal)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1))
        return {"diff": args.out, "counts": result["counts"], "note": result["note"]}
    return result


def cmd_check(args) -> dict:
    from .check import check
    return check(Path(args.snapshot))


def run(argv=None) -> tuple[int, dict]:
    """Parse and run one command: (exit code, result)."""
    p = argparse.ArgumentParser(prog="pointman-resolve", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="snapshot the timeline open in Resolve (read-only)")
    s.add_argument("--out", help="folder for snapshot-<utc>.json and .otio")
    s.add_argument("--no-otio", action="store_true", help="skip Resolve's OTIO export")
    d = sub.add_parser("diff", help="what changed between two snapshots")
    d.add_argument("a")
    d.add_argument("b")
    d.add_argument("--out", help="write the diff here instead of printing it")
    d.add_argument("--no-local-journal", action="store_true", help="use only the journal embedded in B")
    c = sub.add_parser("check", help="a snapshot against its own OTIO export")
    c.add_argument("snapshot")
    args = p.parse_args(argv)
    try:
        result = {"snapshot": cmd_snapshot, "diff": cmd_diff, "check": cmd_check}[args.cmd](args)
        return (1 if args.cmd == "check" and not result.get("ok") else 0), result
    except SystemExit as e:
        return 2, {"error": str(e)}
    except Exception as e:  # report as JSON; callers parse stdout
        return 2, {"error": f"{type(e).__name__}: {e}"}


def main(argv=None):
    code, result = run(argv)
    sys.stdout.write(json.dumps(result, indent=1) + "\n")
    sys.stdout.flush()
    # Resolve's scripting library can crash the interpreter on exit (exit code 139); leave directly so
    # callers see our exit code, not the crash.
    os._exit(code)


if __name__ == "__main__":
    main()
