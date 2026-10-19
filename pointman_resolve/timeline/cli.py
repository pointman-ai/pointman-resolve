"""The timeline tools from a shell. The extension runs the same code as resolve.snapshot, resolve.diff
and resolve.check.

    python -m pointman_resolve.timeline.cli snapshot [--out DIR] [--no-otio]   the open timeline -> snapshot-<utc>.json (+ .otio)
    python -m pointman_resolve.timeline.cli diff A.json B.json [--out FILE]    what changed from A to B
    python -m pointman_resolve.timeline.cli check SNAPSHOT.json                a snapshot against its own OTIO

Without --out, snapshots go to ~/.pointman-resolve/snapshots/<project>/<timeline>/. Each command
prints one JSON object on stdout. Read-only against Resolve: no saves, no switches, no edits.
"""

import argparse
import json
import os
import sys
from pathlib import Path



def cmd_snapshot(args) -> dict:
    from .readonly import connect
    from .snapshot import store_dir, write
    resolve = connect()
    if args.out:
        out = Path(args.out).expanduser()
    else:
        project = resolve.GetProjectManager().GetCurrentProject()
        tl = project.GetCurrentTimeline() if project else None
        if not project or not tl:
            raise SystemExit("no project or timeline open in Resolve")
        out = store_dir(project.GetName(), tl.GetName())
    path = write(resolve, out, otio=not args.no_otio)
    snap = json.loads(path.read_text(encoding="utf-8"))
    return {"snapshot": str(path), "otio": str(path.with_suffix(".otio")) if (snap.get("otio") or {}).get("file") else None,
            "items": len(snap["items"]), "markers": len(snap["markers"]), "journal": len(snap["journal"]),
            "errors": snap["errors"], "took_s": snap["took_s"], "previous": snap["previous"]}


def cmd_diff(args) -> dict:
    from .diff import diff
    a, b = (json.loads(Path(p).read_text(encoding="utf-8")) for p in (args.a, args.b))
    result = diff(a, b, local_journal=not args.no_local_journal)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
        return {"diff": args.out, "counts": result["counts"], "note": result["note"]}
    return result


def cmd_check(args) -> dict:
    from .check import check
    return check(Path(args.snapshot))


def run(argv=None) -> tuple[int, dict]:
    """Parse and run one command: (exit code, result)."""
    p = argparse.ArgumentParser(prog="python -m pointman_resolve.timeline.cli", description=__doc__.split("\n")[0])
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
