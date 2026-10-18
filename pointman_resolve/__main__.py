"""The Resolve extension's process: `python -m pointman_resolve`, started and spoken to by the node.

What the Python node did around Resolve (agent/daemon.py's Resolve thread, publisher.py's
publish_render and publish_still), so the Rust node only runs it (MB-354):

- resolve.render and resolve.still: Resolve renders the timeline or grabs the frame under the
  playhead, and the proxy, poster and thumbnail are built here and published through the node
  (files.publish) as before: a `timeline` asset at resolve/<project>/<timeline> (a still at
  .../frame), in the board project named like the Resolve one, else "Resolve", posted where the
  board's request asked. A node that doesn't publish for extensions (the Python node's
  ResolveViaExtension) gets the file as the command's output and publishes it itself.
- Notes at a timecode on a Resolve preview become markers on its timeline by themselves: purple while
  open, green once resolved, gone once deleted. Each is marked synced in core; one whose project
  isn't open in Resolve waits until it is.
- A preview is rendered by itself once the edit has changed and the Mac has been idle for
  auto_render_idle_minutes.
- resolve.status every 10 s: the node offers the commands only while it says `connected`.

Every Resolve call happens on the "resolve" lane, one thread. Core is reached only through the node
(core.call): the extension holds no token.
"""

import json
import shutil
import tempfile
import time
from pathlib import Path

try:
    from pointman_extension import Cancelled, Extension, Retry
except ImportError:                 # a node from before the rename (thread 2255 [3561])
    from mb_extension import Cancelled, Extension, Retry  # type: ignore[no-redef]

from . import media
from .bridge import ResolveBridge, ResolveUnavailable, hid_idle_seconds, snapshot_digest

ext = Extension()
bridge = ResolveBridge()
# the open timeline's digest is a full read of its clips, so it's refreshed at most once a minute,
# and only when something wants it (auto renders)
last_digest: dict = {"at": 0.0, "key": None, "digest": None}
MIN_FREE_GB = 10                    # the Python node's min_free_gb: a render never fills the disk
PUBLISH_WAIT = 6 * 3600             # a long preview over a slow line: the node uploads it in parts


class State:
    """resolve.json in the extension's state folder: the notes cursor, notes waiting for their
    project to be open, the markers placed, and each timeline's last rendered digest."""

    def __init__(self):
        self.path: Path | None = None
        self.data = {"notes_since": 0.0, "pending": {}, "applied": {}, "rendered": {}}

    def _load(self) -> None:
        if self.path is None:          # read once the node has said where its state folder is
            self.path = Path(ext.node.get("state_dir") or ".") / "resolve.json"
            try:
                self.data.update(json.loads(self.path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    def __getitem__(self, key: str):
        self._load()
        return self.data[key]

    def __setitem__(self, key: str, value) -> None:
        self._load()
        self.data[key] = value

    def save(self) -> None:
        if self.path is not None:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(self.path)


state = State()
seen: dict[str, tuple[str, float]] = {}        # project/timeline -> (its digest, when first seen)
node: dict = {"publishes": None}               # whether the node publishes for us (publishes())


def poll_status() -> None:
    try:
        st = bridge.status()
    except Exception as e:  # noqa: BLE001 - a status is never worth crashing over
        st = {"connected": False, "reason": str(e)}
    tl = st.get("timeline")
    if tl and (ext.settings.get("auto_render_idle_minutes") or 0) > 0:
        key = f"{st.get('project')}/{tl['name']}"
        if key != last_digest["key"] or time.monotonic() - last_digest["at"] > 60:
            sig = bridge.signature()
            last_digest.update(at=time.monotonic(), key=key, digest=sig[2] if sig else None)
        tl["digest"] = last_digest["digest"]
    ext.event("resolve.status", st)


def unavailable(e: ResolveUnavailable) -> Retry:
    """Resolve went away: the node hears so first (and stops offering the commands), then that the job
    is worth asking again once it's back."""
    poll_status()
    return Retry(str(e))


# ------------------------------------------------------------ core, through the node


def core(method: str, path: str, body=None):
    """A call to core through the node: its answer's body, or RuntimeError."""
    params = {"method": method, "path": path, **({"body": body} if body is not None else {})}
    r = ext.request("core.call", params, 60) or {}
    if int(r.get("status") or 0) >= 400:
        raise RuntimeError(f"{method} {path} -> {r.get('status')}: {str(r.get('body'))[:300]}")
    return r.get("body")


def publishes() -> bool:
    """Whether the node publishes for us and passes calls to core (the Rust node does; the Python
    node's host has neither, and does what's here itself). Asked once."""
    if node["publishes"] is None:
        try:
            core("GET", f"/v1/node/notes?since={time.time():.0f}&limit=1")
            node["publishes"] = True
        except RuntimeError as e:
            if str(e).startswith("no method"):
                node["publishes"] = False
            else:
                return True             # core's away for now; the node is there
    return node["publishes"]


def publish(build: media.Build, *, project: dict, asset_path: str, title: str, caption: str,
            kind: str | None = None, post: dict | None = None) -> dict:
    """Uploaded and published by the node, as the Python node's upload_and_publish did."""
    files = [{"role": f.role, "file": str(f.path), "name": f.name, "mime": f.mime, "size": f.path.stat().st_size,
              "width": f.width, "height": f.height, "duration": f.duration} for f in build.files]
    body = {"project": project, "asset": {"path": asset_path, "title": title, "kind": kind or build.kind},
            "caption": caption, "meta": build.meta, "files": files, "by": "resolve", "post": post}
    return ext.request("files.publish", {"body": body}, PUBLISH_WAIT) or {}


def project_for(resolve_project: str) -> dict:
    """Resolve projects are usually named after the video's folder, in the dailies folder the node
    gives us (its roots)."""
    target = resolve_project.strip().lower().replace(" ", "-")
    for root in ext.node.get("roots") or []:
        try:
            for child in Path(root).expanduser().iterdir():
                if child.is_dir() and child.name.lower() == target:
                    return {"slug": child.name, "name": child.name}
        except OSError:
            continue
    return {"slug": "resolve", "name": "Resolve"}


def posted_for(job) -> dict | None:
    """Where core posts it: the board's request (the node gives its id with the apps' kinds, else
    it's in the command's id, `req-<n>`), as the Python node did."""
    rid = job.params.get("request_id")
    if not isinstance(rid, int) and job.id.startswith("req-") and job.id[4:].isdigit():
        rid = int(job.id[4:])
    return {"request_id": rid} if isinstance(rid, int) else None


def check_disk(work: Path) -> None:
    free = shutil.disk_usage(work).free / 1e9
    if free < MIN_FREE_GB:
        raise RuntimeError(f"only {free:.0f} GB free (min_free_gb = {MIN_FREE_GB}); skipping so the disk doesn't fill")


def emptied(folder: Path) -> None:
    """A job's files, once it's over where the node publishes for us: the node keeps nothing of them
    (the Python node publishes the outputs itself, then removes them)."""
    if not node["publishes"]:
        return
    for p in folder.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)


# ------------------------------------------------------------ commands


def render_and_publish(work: Path, *, timeline: str | None, height: int, caption: str | None, post: dict | None,
                       progress=lambda f: None, cancelled=lambda: False) -> tuple[dict, dict | None, Path]:
    """(the timeline's snapshot, core's answer or None where the node doesn't publish, the render)."""
    check_disk(work)
    path, snap = bridge.render(work, timeline=timeline, height=height, progress=progress, cancelled=cancelled)
    published = None
    if publishes():
        build = media.build_video(path, Path(tempfile.mkdtemp(dir=work)), lambda stage, f: None, height=720,
                                  bitrate="2500k", name=f"{snap['name']}.mp4")
        build.meta.update(timeline=snap, resolve_project=snap["project"], rendered_at=time.time())
        published = publish(build, project=project_for(snap["project"]), kind="timeline",
                            asset_path=f"resolve/{snap['project']}/{snap['name']}", title=snap["name"],
                            caption=caption or f"Preview render of {snap['name']}", post=post)
    # this edit is rendered: an auto render won't do it again
    state["rendered"][f"{snap['project']}/{snap['name']}"] = snapshot_digest(snap)
    state.save()
    return snap, published, path


@ext.command("resolve.render")
def render(job):
    height = int(job.params.get("height") or ext.settings.get("preview_height") or 720)
    try:
        snap, published, path = render_and_publish(
            job.work, timeline=job.params.get("timeline"), height=height, caption=job.params.get("caption"),
            post=posted_for(job), progress=lambda f: job.progress(f, "render"), cancelled=lambda: job.cancelled)
    except ResolveUnavailable as e:
        raise unavailable(e) from None
    except RuntimeError:
        if job.cancelled:
            raise Cancelled("cancelled") from None
        raise
    finally:
        emptied(job.work)
    poll_status()
    out = {"project": snap["project"], "timeline": snap["name"], "digest": snapshot_digest(snap)}
    if published is None:
        job.output(path, kind="video", meta={"timeline": snap})
        return out
    return {**published, **out}


@ext.command("resolve.still")
def still(job):
    try:
        path, info = bridge.still(job.work)
    except ResolveUnavailable as e:
        raise unavailable(e) from None
    if not publishes():
        job.output(path, kind="image", meta=info)
        return info
    try:
        build = media.build_image(path, Path(tempfile.mkdtemp(dir=job.work)), lambda stage, f: None)
        build.meta.update(info)
        published = publish(build, project=project_for(info["project"]),
                            asset_path=f"resolve/{info['project']}/{info['timeline']}/frame",
                            title=f"{info['timeline']} frame", caption=f"Frame at {info['timecode']}",
                            post=posted_for(job))
    finally:
        emptied(job.work)
    return {**published, **info}


@ext.command("resolve.marker")
def marker(job):
    """The note mirrored as a marker; null when its project isn't the one open (try later)."""
    if not ext.settings.get("markers", True):
        return {"marker": "off"}
    p = job.params
    try:
        return bridge.apply_note(p["note"], p["project"], p["timeline"])
    except ResolveUnavailable as e:
        raise unavailable(e) from None


# ------------------------------------------------------------ by itself: markers and auto renders


def sync_markers() -> None:
    """Notes at a timecode on a Resolve preview, as markers (the Python node's sync_notes and
    apply_markers, every 15 s)."""
    if not ext.settings.get("markers", True) or not publishes():
        return
    since = float(state["notes_since"] or 0)
    try:
        # a little overlap: core's timestamps are taken before commit, so a note can land with a time
        # just below a cursor already moved past
        changed = core("GET", f"/v1/node/notes?since={max(0.0, since - 30)}&include_deleted=true&limit=5000") or []
    except RuntimeError as e:
        ext.log(f"couldn't read the notes: {e}")
        return
    pending, applied = state["pending"], state["applied"]
    for n in changed:
        since = max(since, float(n.get("updated_at") or 0))
        if (n.get("asset") or {}).get("kind") != "timeline" or n.get("timecode") is None:
            continue
        if applied.get(str(n["id"])) == n["updated_at"] or (n.get("synced_at") or 0) >= n["updated_at"]:
            continue                    # already its marker (placed here, or before the switch)
        pending[str(n["id"])] = n
    state["notes_since"] = since
    for nid, note in list(pending.items()):
        parts = ((note.get("asset") or {}).get("path") or "").split("/", 2)
        if len(parts) < 3:
            pending.pop(nid)
            continue
        try:
            info = bridge.apply_note(note, parts[1], parts[2])
        except ResolveUnavailable:
            break                       # Resolve isn't there: they all wait
        except Exception as e:  # noqa: BLE001
            info = {"marker": "failed", "error": str(e)}
        if info is None:
            continue                    # its project isn't open in Resolve; try again later
        pending.pop(nid)
        applied[nid] = note["updated_at"]
        try:
            core("POST", f"/v1/node/notes/{nid}/synced", {"info": info})
        except RuntimeError as e:
            ext.log(f"couldn't mark note {nid} synced: {e}")
    state.save()


def auto_render() -> None:
    """A preview once the open timeline has changed since its last render and neither it nor the
    Mac's keyboard and mouse have been touched for auto_render_idle_minutes (the Python node's
    maybe_auto_render, every minute)."""
    minutes = int(ext.settings.get("auto_render_idle_minutes") or 0)
    if minutes <= 0 or not publishes():
        return
    try:
        sig = bridge.signature()
    except ResolveUnavailable:
        return
    if sig is None:
        return
    project, timeline, digest = sig
    key, now = f"{project}/{timeline}", time.time()
    if key not in seen or seen[key][0] != digest:
        seen[key] = (digest, now)
        return
    idle = minutes * 60
    if state["rendered"].get(key) == digest or now - seen[key][1] < idle or hid_idle_seconds() < idle:
        return
    ext.log(f"auto render: {key} changed and the Mac has been idle")
    root = Path(ext.node.get("work_dir") or tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="auto-", dir=root))
    try:
        render_and_publish(work, timeline=timeline, height=int(ext.settings.get("preview_height") or 720), post=None,
                           caption=f"Automatic preview: the edit changed and the Mac was idle for {minutes}+ min")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    poll_status()


ext.every(10, poll_status, lane="resolve")
ext.every(15, sync_markers, lane="resolve")
ext.every(60, auto_render, lane="resolve")

if __name__ == "__main__":
    ext.run()
