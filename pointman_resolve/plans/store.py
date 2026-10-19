"""Plans on disk: ~/.pointman-resolve/plans/<id>/plan.json (the ops, the snapshot they were compiled on,
the digest a dry run gave) and applied.json (apply.Record). A plan is only ever named by its id, never
a path, so nothing outside the store is read."""

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from ..timeline import journal

ID = re.compile(r"^p-\d{8}T\d{6}-[0-9a-f]{4}$")


def root() -> Path:
    return journal.home() / "plans"


def new_id() -> str:
    return f"p-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(2)}"


def folder(plan_id: str) -> Path:
    if not ID.match(plan_id or ""):
        raise ValueError(f"{plan_id!r} isn't a plan id (p-<utc>-<hex>, from resolve.plan)")
    path = root() / plan_id
    if not (path / "plan.json").exists():
        raise FileNotFoundError(f"no plan {plan_id}")
    return path


def save(plan: dict) -> Path:
    path = root() / plan["id"]
    path.mkdir(parents=True, exist_ok=True)
    tmp = path / "plan.json.tmp"
    tmp.write_text(json.dumps(plan, indent=1, default=str), encoding="utf-8")
    tmp.replace(path / "plan.json")
    return path


def load(plan_id: str) -> dict:
    return json.loads((folder(plan_id) / "plan.json").read_text(encoding="utf-8"))
