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

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pointman_extension_types as types
from pointman_extension import Cancelled, Extension, Retry

from . import markers, media
from .plans import apply as plan_apply
from .plans import store as plan_store
from .plans import versions as plan_versions
from .plans.compile import compile_plan, ripples_of, to_base, to_final
from .plans.model import Model, clock
from .bridge import ResolveBridge, ResolveUnavailable, hid_idle_seconds, snapshot_digest
from .timeline import check as timeline_check
from .timeline import framecheck
from .timeline import gap as timeline_gap
from .timeline import journal
from .timeline import qc as timeline_qc
from .timeline import diff as timeline_diff
from .timeline import render as timeline_render
from .timeline import snapshot as timeline_snapshot
from .timeline import watch as timeline_watch
from .timeline.readonly import ReadOnly

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
last_status: dict = {}                         # poll_status's latest, for health.check (off the lane)
marks: dict = {"state": None, "projects": None, "fallback": None, "read_posts": None, "polled": 0.0,
              "unrouted": set()}   # PFE-30's; read_posts: unknown yet
MARK_EVERY = float(os.environ.get("POINTMAN_RESOLVE_MARK_EVERY") or 10)     # tests make these short
MARK_POLL = float(os.environ.get("POINTMAN_RESOLVE_MARK_POLL") or 30)
# a marker back at its own colour usually means James is undoing: recolour only once he's been idle this long
HEAL_IDLE = float(os.environ.get("POINTMAN_RESOLVE_HEAL_IDLE") or 10)   # James: 10 s is fine (13661)
WATCH_EVERY = float(os.environ.get("POINTMAN_RESOLVE_WATCH_EVERY") or 10)
WATCH_IDLE = float(os.environ.get("POINTMAN_RESOLVE_WATCH_IDLE") or 15)   # he edits in bursts: check in the pauses
watching: dict = {"end": None}


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
    if st.get("connected") and st.get("page") is None:
        # what a modal dialog does (Resolve's Software Update one, 16 Oct): scripting answers, but with
        # nothing; saves and project loads fail until it's dealt with
        st["blocked"] = "Resolve answers scripts with nothing: a dialog is probably open in it"
    last_status.clear()
    last_status.update(st)
    ext.event("resolve.status", st)


def unavailable(e: ResolveUnavailable) -> Retry:
    """Resolve went away: the node hears so first (and stops offering the commands), then that the job
    is worth asking again once it's back."""
    poll_status()
    return Retry(str(e))


# ------------------------------------------------------------ core, through the node


def core(method: str, path: str, body=None):
    """A call to core through the node: its answer's body, or RuntimeError."""
    r = ext.request(types.CoreCallRequest(method=method, path=path, body=body), 60)
    if r.status >= 400:
        raise RuntimeError(f"{method} {path} -> {r.status}: {str(r.body)[:300]}")
    return r.body


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
    got = ext.request(types.FilesPublishRequest(body=body), PUBLISH_WAIT)
    # the file, its version and the post the node made, by their numbers (file_2636 is 2636), as kept before
    return {"asset_id": _number(got.file), "version_id": _number(got.version), "message_id": _number(got.post)}


def _number(ref) -> int | None:
    tail = (ref.value if ref is not None else "").rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else None


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


# ------------------------------------------------------------ the timeline as data: read only


def stored(timeline: str | None) -> Path:
    """The snapshots folder of the timeline named, or of the one snapshotted last."""
    folder = timeline_snapshot.timeline_folder(timeline)
    if folder is None:
        raise RuntimeError("no snapshots" + (f" of {timeline!r}" if timeline else "") + " yet: take one with resolve.snapshot")
    return folder


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@ext.command("resolve.snapshot")
def snapshot_cmd(job):
    """Every track, item, property and marker of the open timeline as JSON, with Resolve's OTIO export
    beside it, in ~/.pointman-resolve/snapshots/<project>/<timeline>/. ReadOnly lets only Get, Is and
    Export calls through, so it can't save, switch, move the playhead or start a job."""
    try:
        resolve = ReadOnly(bridge.connect())
    except ResolveUnavailable as e:
        raise unavailable(e) from None
    project = resolve.GetProjectManager().GetCurrentProject()
    tl = project.GetCurrentTimeline() if project else None
    if not project or not tl:
        raise Retry("no project or timeline open in Resolve")
    path = timeline_snapshot.write(resolve, timeline_snapshot.store_dir(project.GetName(), tl.GetName()),
                                   otio=job.params.get("otio", True))
    snap = read(path)
    return {"snapshot": str(path), "otio": str(path.with_suffix(".otio")) if (snap.get("otio") or {}).get("file") else None,
            "project": snap["project"]["name"], "timeline": snap["timeline"]["name"], "items": len(snap["items"]),
            "markers": len(snap["markers"]), "journal": len(snap["journal"]), "previous": snap["previous"],
            "errors": snap["errors"], "took_s": snap["took_s"]}


@ext.command("resolve.diff")
def diff_cmd(job):
    """What changed between two snapshots of a timeline: by default its latest and the one before.
    Snapshots are named, never given as paths, so nothing outside the store is read."""
    p = job.params
    folder = stored(p.get("timeline"))
    names = [s.name for s in timeline_snapshot.snapshots(folder)]
    b = timeline_snapshot.named(folder, p.get("b") or names[-1])
    if p.get("a"):
        a = timeline_snapshot.named(folder, p["a"])
    else:
        previous = read(b).get("previous") or (names[-2] if len(names) > 1 else None)
        if not previous:
            raise RuntimeError("only one snapshot of this timeline so far: take another to compare")
        a = timeline_snapshot.named(folder, previous)
    result = timeline_diff.diff(read(a), read(b))
    out = folder / f"diff-{a.stem.removeprefix('snapshot-')}-{b.stem.removeprefix('snapshot-')}.json"
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    if p.get("full"):
        return {**result, "file": str(out)}
    # the lists can run to thousands (a ripple moves everything after it): the first 50 of each, and the file
    cut = {k: result[k][:50] for k in ("changed", "moved", "trimmed", "new", "gone")}
    return {**{k: result[k] for k in ("a", "b", "note", "counts", "markers", "ours", "timeline", "tracks")}, **cut,
            "truncated": any(len(result[k]) > 50 for k in cut), "file": str(out)}


@ext.command("resolve.check")
def check_cmd(job):
    """A snapshot (by default the latest) against the OTIO export taken with it, item by item."""
    folder = stored(job.params.get("timeline"))
    name = job.params.get("snapshot") or timeline_snapshot.snapshots(folder)[-1].name
    return timeline_check.check(timeline_snapshot.named(folder, name))


# ------------------------------------------------------------ Resolve's own frame (PFE-36)


def live_snapshot(resolve) -> dict:
    """The open timeline as it is now, read through ReadOnly and kept in memory: not written to the store, so
    frame checks don't become the `latest` that resolve.diff compares."""
    ro = ReadOnly(resolve)
    project = ro.GetProjectManager().GetCurrentProject()
    if not project or not project.GetCurrentTimeline():
        raise Retry("no project or timeline open in Resolve")
    return timeline_snapshot.take(ro)[0]


def grab_frame(job, frame_of) -> tuple[dict, int, dict, Path]:
    """Snapshot, pick the frame (frame_of(snap)), and export Resolve's picture of it with the playhead put back."""
    try:
        resolve = bridge.connect()
    except ResolveUnavailable as e:
        raise unavailable(e) from None
    snap = live_snapshot(resolve)
    frame = frame_of(snap)
    project = resolve.GetProjectManager().GetCurrentProject()  # framecheck.grab() narrows it to the playhead + still
    t = snap["timeline"]
    png = Path(job.work) / "resolve.png"
    try:
        got = framecheck.grab(project, project.GetCurrentTimeline(), frame, t["fps"] or 25.0, t["start_frame"], png,
                              wait=float(job.params.get("wait", 3)))
    except framecheck.Busy as e:
        raise Retry(str(e)) from None
    return snap, frame, got, png


def post_image(job, path: Path, meta: dict, snap: dict, *, what: str, caption: str) -> dict:
    """The comparison image, posted where the board's request asked (as resolve.still posts its frame), or
    handed back as the command's output by a node that doesn't publish for extensions."""
    if not publishes():
        job.output(path, kind="image", meta=meta)
        return {}
    project, timeline = snap["project"]["name"], snap["timeline"]["name"]
    build = media.build_image(path, Path(tempfile.mkdtemp(dir=job.work)), lambda stage, f: None)
    build.meta.update({k: v for k, v in meta.items() if k in ("frame", "timecode", "psnr", "px_per_unit", "ok")})
    return publish(build, project=project_for(project), asset_path=f"resolve/{project}/{timeline}/{what.replace(' ', '-')}",
                   title=f"{timeline} {what}", caption=caption, post=posted_for(job))


def restored_warning(got: dict) -> dict:
    """First in the result when the playhead didn't go back: the one thing the person would notice."""
    if got.get("restored"):
        return {}
    return {"warning": f"the playhead was NOT put back: it should be at {got.get('playhead')}"}


def frame_of_param(job):
    return lambda snap: timeline_render.parse_time(job.params["frame"], snap["timeline"]["fps"] or 25.0)


SHADOW_MAX_S = 180   # holds the resolve lane (markers, status) while it renders: 3 min a request, about 75 s


@ext.command("resolve.shadow")
def shadow(job):
    """Any range of the open timeline drawn from the source files (PFE-15): every track composited by ffmpeg,
    with the audio mixed, posted as a video. No Resolve render, and the playhead and page don't move, so it
    can run while the person edits. What it can't draw yet (grades, transitions, speed, titles) is listed."""
    p = job.params
    try:
        resolve = bridge.connect()
    except ResolveUnavailable as e:
        raise unavailable(e) from None
    snap = live_snapshot(resolve)
    fps = snap["timeline"]["fps"] or 25.0
    a, b = timeline_render.parse_time(p["from"], fps), timeline_render.parse_time(p["to"], fps)
    if not 0 <= a < b:
        raise ValueError(f"`from` must come before `to` ({a} → {b})")
    if (b - a) / fps > SHADOW_MAX_S:
        raise ValueError(f"{(b - a) / fps:.0f} s is more than {SHADOW_MAX_S} s: render it in parts")
    check_disk(Path(job.work))
    out = Path(job.work) / "shadow.mp4"
    r = timeline_render.render(snap, a, b, out, width=int(p.get("width") or 960))
    if not r["ok"]:
        raise RuntimeError(f"the shadow render failed: {r['error']}")
    result = {k: r[k] for k in ("from", "to", "seconds", "video_clips", "audio_clips", "hidden")}
    # grades our tools recorded are drawn (PFE-42); others, and what the renderer skipped, aren't
    result["not_drawn"] = ["grades not set by our tools"] + sorted({s["why"] for s in r["skipped"]})
    project, timeline = snap["project"]["name"], snap["timeline"]["name"]
    if not publishes():
        job.output(out, kind="video", meta=result)
        return result
    try:
        build = media.build_video(out, Path(tempfile.mkdtemp(dir=job.work)), lambda stage, f: None, height=540,
                                  bitrate="1500k", name=f"{timeline}-shadow.mp4")
        build.meta.update(result)
        tc = lambda f: f"{int(f // fps // 60)}:{int(f // fps % 60):02d}"
        published = publish(build, project=project_for(project), asset_path=f"resolve/{project}/{timeline}/shadow",
                            title=f"{timeline} shadow {tc(a)}–{tc(b)}",
                            caption=f"Shadow render {tc(a)}–{tc(b)}, drawn from the sources"
                                    + f" (not drawn: {', '.join(result['not_drawn'])})",
                            post=posted_for(job))
    finally:
        emptied(job.work)
    return {**published, **result}


@ext.command("resolve.qc")
def qc_cmd(job):
    """A QC report on the open timeline (PFE-16): black, flash frames and gaps, frozen screen recordings, and
    for a range up to 15 min the mix's loudness, true peak and dead air. Reads the timeline in memory and the
    source files; Resolve and the playhead aren't touched. Posts the report as a document."""
    p = job.params
    try:
        resolve = bridge.connect()
    except ResolveUnavailable as e:
        raise unavailable(e) from None
    snap = live_snapshot(resolve)
    fps = snap["timeline"]["fps"] or 25.0
    a = timeline_render.parse_time(p["from"], fps) if p.get("from") is not None else None
    b = timeline_render.parse_time(p["to"], fps) if p.get("to") is not None else None
    r = timeline_qc.report(snap, a, b, audio=p.get("sound", True), sources=p.get("sources", True))
    project, timeline = snap["project"]["name"], snap["timeline"]["name"]
    doc = Path(job.work) / "qc.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(timeline_qc.markdown(r, timeline), encoding="utf-8")
    short = {k: r[k] for k in ("ok", "range", "counts", "sound")} | {"issues": r["issues"][:50],
                                                                     "truncated": len(r["issues"]) > 50}
    if not publishes():
        job.output(doc, kind="document", meta=short)
        return short
    try:
        build = media.Build("document", [media.OutFile("document", doc, doc.name, "text/markdown")])
        summary = ", ".join(f"{n} {k}" for k, n in sorted(r["counts"].items())) or "nothing found"
        published = publish(build, project=project_for(project), asset_path=f"resolve/{project}/{timeline}/qc",
                            title=f"{timeline} QC", caption=f"QC {r['range']}: {summary}", post=posted_for(job))
    finally:
        emptied(job.work)
    return {**published, **short}


@ext.command("resolve.frame_check")
def frame_check(job):
    """Resolve's own picture of one frame against the shadow render of it: PSNR, where they differ, and the
    clips there. Moves the playhead only while nobody is working, and always puts it back."""
    snap, frame, got, png = grab_frame(job, frame_of_param(job))
    t = snap["timeline"]
    shadow = timeline_render.still(snap, frame, Path(job.work) / "shadow.png", width=t["width"] or 1920)
    if not shadow["ok"]:
        raise RuntimeError(f"the shadow render failed: {shadow['error']}")
    side = Path(job.work) / "frame-check.png"
    cmp = framecheck.compare(png, Path(shadow["out"]), side_by_side=side)
    plan = timeline_render.plan(snap, frame, frame + 1)
    floor = float(job.params.get("min_psnr", 30))
    result = {**restored_warning(got), "ok": got["restored"] and (cmp["psnr"] or 0) >= floor, "frame": frame, **got, **cmp,
              "min_psnr": floor,
              "px_per_unit": t.get("px_per_unit") or timeline_render.UNIT,
              "suspects": framecheck.suspects(plan, cmp["diff_box"]), "not_drawn": shadow["skipped"],
              "image": "Resolve | shadow render | difference ×4"}
    if side.exists():
        result["posted"] = post_image(job, side, result, snap, what="frame check", caption=(
            f"Frame check at {got['timecode']}: Resolve | shadow render | difference. PSNR {cmp['psnr']} dB"
            + (f", differs in {cmp['diff_box']}" if cmp["diff_box"] else "")))
    return result


@ext.command("resolve.insert_gap")
def insert_gap(job):
    """Open a gap of `frames` at `at` on every track: everything starting there or later, and the timeline's
    markers, move later (a negative `frames` closes a gap). Resolve has no insert; a temporary clip's rippling
    speed change does it (timeline/gap.py). Waits for the timeline to be still."""
    try:
        raw = bridge.connect()
    except ResolveUnavailable as e:
        raise unavailable(e) from None
    project = raw.GetProjectManager().GetCurrentProject()
    tl = project.GetCurrentTimeline() if project else None
    if tl is None:
        raise Retry("no timeline open in Resolve")
    fps = float(tl.GetSetting("timelineFrameRate") or 25)
    at, frames = timeline_render.parse_time(job.params["at"], fps), int(job.params["frames"])
    try:
        got = timeline_gap.insert(project.GetMediaPool(), tl, at, frames, wait=float(job.params.get("wait", 3)))
    except framecheck.Busy as e:
        raise Retry(str(e)) from None
    except timeline_gap.GapError as e:  # the timeline may have moved: say so, and journal it, before failing
        journal.record("resolve.insert_gap", timeline=tl.GetUniqueId(), note=f"FAILED: {e}", at=at, frames=e.moved)
        raise
    # every clip from `at` on moved because of us: follow mode (PFE-13) and diffs read at/frames from here
    journal.record("resolve.insert_gap", timeline=tl.GetUniqueId(), note=f"{frames:+d} frames at {at}", at=at, frames=frames)
    project.SaveProject()
    return {**got, "undo": {"at": at, "frames": -frames}}


@ext.command("resolve.calibrate")
def calibrate(job):
    """Crop/Pan/Tilt px per unit for the open timeline's resolution, measured on Resolve's own frame: the
    candidate whose shadow render matches it best. Read-only (nothing is set on any clip); stored in
    ~/.pointman-resolve/calibration.json, and later snapshots carry it as timeline.px_per_unit."""
    def pick(snap):
        if job.params.get("frame") is not None:
            return frame_of_param(job)(snap)
        f = framecheck.probe_frame(snap)
        if f is None:
            raise RuntimeError(f"no visible clip is moved or cropped by {framecheck.MOVED:.0f}+ units: pass a `frame`")
        return f
    snap, frame, got, png = grab_frame(job, pick)
    result = {**restored_warning(got), **framecheck.calibrate(snap, frame, png, Path(job.work))}
    result.update(frame=frame, **got)
    if not got["restored"]:
        result["ok"] = False
    if result["ok"] and not job.params.get("dry_run"):
        result["stored"] = str(framecheck.store(result, project=snap["project"]["name"],
                                                timeline=snap["timeline"]["name"], frame=frame))
    best = Path(job.work) / f"shadow-u{result.get('px_per_unit')}.png"
    side = Path(job.work) / "calibrate.png"
    if best.exists():
        framecheck.compare(png, best, side_by_side=side)
        if side.exists():
            result["posted"] = post_image(job, side, result, snap, what="calibration", caption=(
                f"Calibration at {got['timecode']}: {result.get('px_per_unit')} px per unit "
                f"({result.get('psnr')} dB, {result.get('margin_db')} dB clear). Resolve | shadow | difference"))
    return result


# ------------------------------------------------------------ edit plans (PFE-11)


def open_timeline():
    """(the raw Resolve, a ReadOnly view, project, timeline), or Retry."""
    try:
        raw = bridge.connect()
    except ResolveUnavailable as e:
        raise unavailable(e) from None
    ro = ReadOnly(raw)
    project = ro.GetProjectManager().GetCurrentProject()
    tl = project.GetCurrentTimeline() if project else None
    if not project or not tl:
        raise Retry("no project or timeline open in Resolve")
    return raw, ro, project, tl


def plan_files(plan: dict, snap: dict) -> set[str]:
    """Files the plan lays that aren't on the timeline yet (their rate and length come from the pool)."""
    have = {i.get("file") for i in snap["items"]}
    out = set()
    for op in plan.get("ops") or []:
        out |= {str(op[k]) for k in ("file",) if op.get(k)}
        out |= {str(v) for v in (op.get("fill") or {}).values() if v}
    return out - have


def pool_media(ro, paths: set[str]) -> dict:
    """path -> {fps, frames} from Resolve's media pool, read only."""
    if not paths:
        return {}
    found, stack = {}, [ro.GetProjectManager().GetCurrentProject().GetMediaPool().GetRootFolder()]
    while stack and len(found) < len(paths):
        folder = stack.pop()
        for c in folder.GetClipList() or []:
            path = c.GetClipProperty("File Path")
            if path in paths:
                frames = c.GetClipProperty("Frames")
                found[path] = {"fps": float(c.GetClipProperty("FPS") or 0) or None,
                               "frames": int(frames) if str(frames).isdigit() else None}
        stack += folder.GetSubFolderList() or []
    return found


def steps_text(compiled: dict, fps: float, limit: int = 40) -> list[str]:
    """The groups as lines an agent can read back to James."""
    out = []
    for g in compiled["groups"]:
        head = f"op {g['op'] + 1} {g['kind']}"
        if g.get("ripple"):
            s, e = g["ripple"]["range"]
            out.append(f"{head}: cut {clock(s, fps)}–{clock(e, fps)} ({(e - s) / fps:.2f} s) on every track, "
                       f"{len(g['remove'])} clips split, {sum(1 for x in g['lay'] if x.get('inside'))} pieces cut out")
            continue
        for r in g["remove"]:
            new = [x for x in g["lay"] if x.get("from") == r["id"]]
            lane = f"{r['kind'][0].upper()}{r['track']}"
            if not new:
                out.append(f"{head}: {lane} {clock(r['record'], fps)} {r.get('name')}: removed")
            for x in new:
                what = []
                if (x["kind"], x["track"]) != (r["kind"], r["track"]):
                    what.append(f"to {x['kind'][0].upper()}{x['track']}")
                if x["record"] != r["record"]:
                    what.append(f"at {clock(x['record'], fps)}")
                if x.get("in") != r.get("in") or x.get("file") != r.get("file"):
                    what.append(f"source {r.get('in')} → {x.get('in')}" if x.get("file") == r.get("file") else
                                f"now {Path(x['file']).name} from {x.get('in')}")
                if x["frames"] != r["frames"]:
                    what.append(f"{x['frames']} frames")
                out.append(f"{head}: {lane} {clock(r['record'], fps)} {r.get('name')}: {', '.join(what) or 're-laid'}"
                           + (" (grade copied)" if x.get("grade_from") else ""))
        for x in g["lay"]:
            if not x.get("from"):
                out.append(f"{head}: {x['kind'][0].upper()}{x['track']} {clock(x['record'], fps)} {Path(x['file']).name} "
                           f"placed, {x['frames']} frames")
        for s in g["set"]:
            out.append(f"{head}: {s['key'][0][0].upper()}{s['key'][1]} {clock(s['record'], fps)}: set "
                       + ", ".join(k for k in ("props", "fades", "color", "enabled") if k in s))
        for t in g["transitions"]:
            out.append(f"{head}: {t['name']} put back at {clock(t['cut'], fps)}")
    return out[:limit] + ([f"… and {len(out) - limit} more (compiled.json)"] if len(out) > limit else [])


def make_plan(ro, project, tl, plan: dict) -> dict:
    """The dry run: a fresh snapshot, the plan compiled on it, saved under a new id."""
    path = timeline_snapshot.write(ro, timeline_snapshot.store_dir(project.GetName(), tl.GetName()), otio=False)
    snap = read(path)
    media_info = pool_media(ro, plan_files(plan, snap))
    compiled = compile_plan(snap, plan, media=media_info)
    model: Model = compiled.pop("model")
    fps = snap["timeline"].get("fps") or 25
    pid = plan_store.new_id()
    ripples = [g["ripple"]["range"] for g in compiled["groups"] if g.get("ripple")]
    saved = {"id": pid, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "note": plan.get("note") or "",
             "ops": plan["ops"], "snapshot": {"folder": str(path.parent), "file": path.name},
             "timeline": {"name": snap["timeline"]["name"], "id": snap["timeline"]["id"], "project": snap["project"]["name"]},
             "digest": compiled["digest"], "media": media_info, "problems": compiled["problems"]}
    folder = plan_store.save(saved)
    (folder / "compiled.json").write_text(json.dumps(compiled, indent=1, default=str), encoding="utf-8")
    by_op: dict = {}
    for d in compiled["frames"]:
        by_op.setdefault(f"op {d['op'] + 1}", {})[d["lane"]] = d["changed"] if not d["outside"] else \
            f"{d['changed']} ({d['outside']} outside)"
    return {"plan": pid, "ok": not compiled["problems"], "problems": compiled["problems"],
            "warnings": compiled["warnings"], "counts": compiled["counts"], "frames_changed": by_op,
            "clutter": compiled["clutter"], "steps": steps_text(compiled, fps), "end": [model.end, clock(model.end, fps)],
            "undo": (f"resolve.undo opens the {len(ripples)} cut{'s' * (len(ripples) != 1)} again with a ripple and lays "
                     "back only what they split or took out" if ripples else "resolve.undo puts back every clip it changes"),
            "file": str(folder / "compiled.json")}


@ext.command("resolve.plan")
def plan_cmd(job):
    """The dry run of an edit plan: compiled on a fresh snapshot, with the problems that stop it, the frames
    it changes per track, the clutter before and after, and its steps. Changes nothing in Resolve."""
    p = job.params
    plan = p.get("plan") if isinstance(p.get("plan"), dict) else {"ops": p.get("ops") or [], "note": p.get("note") or ""}
    _, ro, project, tl = open_timeline()
    return make_plan(ro, project, tl, plan)


def after_snapshot(base: dict, model: Model) -> dict:
    """The base snapshot with the model's clips: what the shadow renderer draws for "after"."""
    items = [p for v in model.tracks.values() for p in v] + [t for v in model.transitions.values() for t in v]
    tl = {**base["timeline"], "end_frame": base["timeline"]["start_frame"] + model.end}
    return {**base, "items": items, "timeline": tl, "markers": [{**m, "frame": f} for f, m in sorted(model.markers.items())]}


def preview_span(g: dict, fps: float, pad: int) -> tuple[list, list]:
    """[start, end) to render before and after a group: its spans with `pad` frames either side; a cut's
    edges only (before: across the cut; after: across where it closed)."""
    if g.get("ripple"):
        s, e = g["ripple"]["range"]
        return [max(0, s - pad), e + pad], [max(0, s - pad), s + pad]
    spans = [x for v in g["spans"].values() for x in v]
    lo, hi = min(s for s, _ in spans), max(e for _, e in spans)
    hi = min(hi, lo + int(fps * 20))                     # 20 s at most
    return [max(0, lo - pad), hi + pad], [max(0, lo - pad), hi + pad]


@ext.command("resolve.preview")
def preview_cmd(job):
    """A plan's change, drawn by the shadow renderer from the source files: before above, after below, for
    each op (the first `limit`). Nothing in Resolve moves."""
    pid = str(job.params.get("plan") or "")
    saved = plan_store.load(pid)
    base = read(Path(saved["snapshot"]["folder"]) / saved["snapshot"]["file"])
    out, groups = render_previews(job, pid, base, saved, limit=int(job.params.get("limit", 4)),
                                  pad=float(job.params.get("pad", 1.0)))
    return {"plan": pid, "previews": out, "groups": groups}


def render_previews(job, pid: str, base: dict, saved: dict, *, limit: int = 4, pad: float = 1.0,
                    version: str | None = None, copy: dict | None = None) -> tuple[list, int]:
    """Each op's span drawn from the sources, before above after, posted where the request asked. With
    `version` and `copy` (the version's own snapshot): the person's timeline above, the version as it really
    is below, each span mapped onto that timeline (the A/B of resolve.version)."""
    plan = {"id": pid, "ops": saved["ops"]}
    compiled = compile_plan(base, plan, media=saved.get("media") or {})
    if compiled["problems"]:
        raise RuntimeError("the plan has problems: " + "; ".join(compiled["problems"]))
    fps = base["timeline"].get("fps") or 25
    pad = int(pad * fps)
    work = Path(job.work)
    out = []
    ripples = ripples_of(compiled["groups"])
    for i, g in enumerate(compiled["groups"][:limit]):
        (b0, b1), (a0, a1) = preview_span(g, fps, pad)
        if copy is not None:                             # both rows from real timelines, not the plan's model
            before_snap, after_snap = base, copy
            b0, b1 = to_base(b0, i, ripples), to_base(b1, i, ripples, end=True)
            a0, a1 = to_final(a0, i, ripples), to_final(a1, i, ripples)
            if a1 <= a0:
                a1 = a0 + 1
        else:
            upto = compile_plan(base, plan, media=saved.get("media") or {}, upto=i + 1)["model"]
            before_snap = base if i == 0 else after_snapshot(base, compile_plan(base, plan, media=saved.get("media") or {},
                                                                                 upto=i)["model"])
            after_snap = after_snapshot(base, upto)
        rb = timeline_render.render(before_snap, b0, b1, work / f"op{i + 1}-before.mp4", width=640)
        ra = timeline_render.render(after_snap, a0, a1, work / f"op{i + 1}-after.mp4", width=640)
        if not (rb["ok"] and ra["ok"]):
            raise RuntimeError(f"the shadow render failed: {rb.get('error') or ra.get('error')}")
        both = work / f"op{i + 1}.mp4"
        extra = max(0, (b1 - b0) - (a1 - a0))
        run = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-y", "-i", rb["out"], "-i", ra["out"],
                              "-filter_complex", f"[1:v]tpad=stop_mode=clone:stop={extra}[a];[0:v][a]vstack=inputs=2[v]",
                              "-map", "[v]", "-map", "1:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "23",
                              "-c:a", "aac", str(both)], capture_output=True, text=True)
        if run.returncode or not both.exists():
            raise RuntimeError(f"couldn't stack before and after: {run.stderr.strip()[-300:]}")
        caption = (f"Plan {pid}, op {g['op'] + 1} ({g['kind']}): "
                   + (f"{base['timeline']['name']} (top) and the version {version!r} (bottom)" if version else
                      "before (top) and after (bottom)")
                   + f", {clock(a0, fps)}–{clock(a1, fps)}. Shadow render from the source files: grades not set by "
                   "our tools and some effects aren't drawn.")
        posted = {}
        if publishes():
            build = media.build_video(both, Path(tempfile.mkdtemp(dir=work)), lambda stage, f: None, height=720)
            posted = publish(build, project=project_for(base["project"]["name"]),
                             asset_path=f"resolve/{base['project']['name']}/{base['timeline']['name']}/plans/{pid}-op{i + 1}",
                             title=f"Plan {pid} op {i + 1}", caption=caption, post=posted_for(job))
        if not posted:
            job.output(both, kind="video", meta={"plan": pid, "op": g["op"] + 1})
        out.append({"op": g["op"] + 1, "before": [b0, b1], "after": [a0, a1], "file": str(both),
                    "posted": posted.get("message_id") if posted else None,
                    "not_drawn": sorted({str(s) for s in (rb.get("skipped") or []) + (ra.get("skipped") or [])})[:10]})
    return out, len(compiled["groups"])


def verify(ro, base: dict, plan: dict, record: plan_apply.Record, media_info: dict) -> dict:
    """The timeline now against the model of the plan applied: every clip the plan placed, moved or kept on
    the lanes it touched, by place, length and source."""
    done = record.data.get("groups_done", 0)
    if not done:
        return {"checked": 0, "wrong": []}
    compiled = compile_plan(base, plan, media=media_info, upto=done)
    model = compiled["model"]
    live, _ = timeline_snapshot.take(ro)
    ids = record.data.get("ids", {})
    by = {}
    for it in live["items"]:
        if it.get("type") != "transition" and it.get("record") is not None:
            by.setdefault((it["kind"], it["track"]), []).append(it)
    touched = {model.lane_of(x) for g in compiled["groups"] for x in g["lanes"]}
    wrong, checked = [], 0
    for key in touched:
        want = [(ids.get(p.get("id"), p.get("id")), p["record"], p["frames"]) for p in model.tracks.get(key, [])]
        have = [(it["id"], it["record"], it["frames"]) for it in sorted(by.get(key, []), key=lambda x: x["record"])]
        checked += len(want)
        if want != have:
            w, h = set(want), set(have)
            wrong.append({"lane": f"{key[0][0].upper()}{key[1]}", "missing": sorted(w - h, key=lambda x: x[1])[:5],
                          "unexpected": sorted(h - w, key=lambda x: x[1])[:5]})
    return {"checked": checked, "wrong": wrong}


@ext.command("resolve.apply")
def apply_cmd(job):
    """Apply a plan from resolve.plan: only while the timeline is still, only if a fresh dry run gives the
    same steps, checked as it goes and verified after. Never saves, switches or starts a job."""
    pid = str(job.params.get("plan") or "")
    saved = plan_store.load(pid)
    folder = plan_store.folder(pid)
    if saved.get("problems"):
        raise RuntimeError("the plan has problems; fix them and plan again: " + "; ".join(saved["problems"]))
    record = plan_apply.Record(folder / "applied.json")
    if record.data["state"] == "applied":
        return {"plan": pid, "ok": True, "state": "applied", "note": "already applied; resolve.undo puts it back"}
    raw, ro, project, tl = open_timeline()
    if tl.GetUniqueId() != saved["timeline"]["id"]:
        raise RuntimeError(f"Resolve has {tl.GetName()!r} open; the plan is for {saved['timeline']['name']!r}")
    try:
        framecheck.idle(tl, float(job.params.get("wait", 3)))
    except framecheck.Busy as e:
        raise Retry(str(e)) from None
    plan = {"id": pid, "ops": saved["ops"], "note": saved.get("note")}
    media_info = saved.get("media") or {}
    if not record.data.get("groups_done") and not record.data.get("partial"):
        path = timeline_snapshot.write(ro, timeline_snapshot.store_dir(project.GetName(), tl.GetName()), otio=False)
        base = read(path)
        fresh = compile_plan(base, plan, media=media_info)
        if fresh["digest"] != saved["digest"] or fresh["problems"]:
            again = make_plan(ro, project, tl, {"ops": saved["ops"], "note": saved.get("note")})
            return {"plan": pid, "ok": False, "stopped": "the timeline changed since the dry run, so the steps aren't the "
                    "ones checked: here's a new dry run to look at and apply", "replan": again}
        record.data["base"] = {"folder": str(path.parent), "file": path.name}
        record.save()
    else:
        base = read(Path(record.data["base"]["folder"]) / record.data["base"]["file"])
    stopped = None
    try:
        out = plan_apply.apply(raw, base, plan, record, media=media_info)
    except plan_apply.Stop as e:
        record.data["state"] = "stopped"
        record.data["stopped"] = str(e)
        record.save()
        stopped, out = str(e), {}
    result = {"plan": pid, "ok": stopped is None, "state": record.data["state"], "groups_done": record.data["groups_done"],
              "warnings": record.data.get("warnings", []), "verify": verify(ro, base, plan, record, media_info),
              "undo": f"resolve.undo with plan {pid}", "log": record.data.get("log", [])[-10:]}
    if stopped:
        result["stopped"] = stopped
    return {**result, **{k: v for k, v in out.items() if k not in result}}


@ext.command("resolve.version")
def version_cmd(job):
    """A plan from resolve.plan applied to a copy of the open timeline, for the person to A/B: theirs stays as
    it was, and they're put back on it (timeline, page, playhead). Only when they asked for it (`ok`)."""
    p = job.params
    if not str(p.get("ok") or "").strip():
        raise RuntimeError("a version switches timelines while it's made, so it needs the person's ok: say what "
                           "asked for it in `ok`")
    pid = str(p.get("plan") or "")
    saved = plan_store.load(pid)
    if saved.get("problems"):
        raise RuntimeError("the plan has problems: " + "; ".join(saved["problems"]))
    raw, ro, project, tl = open_timeline()
    if tl.GetUniqueId() != saved["timeline"]["id"]:
        raise RuntimeError(f"Resolve has {tl.GetName()!r} open; the plan is for {saved['timeline']['name']!r}")
    try:
        framecheck.idle(tl, float(p.get("wait", 3)))
    except framecheck.Busy as e:
        raise Retry(str(e)) from None
    # a version's own record: the plan's applied.json (what resolve.apply and resolve.undo use) is never touched,
    # so undo can't mistake a version for a change to the person's timeline
    record = plan_apply.Record(plan_store.folder(pid) / "version.json")
    if (record.data.get("version") or {}).get("id") and plan_versions.load().get(record.data["version"]["id"]):
        raise RuntimeError(f"this plan already has a version, {record.data['version']['name']!r}: drop it "
                           "(resolve.drop_version) or plan again")
    record.data = {"state": "new", "groups_done": 0, "ids": {}, "partial": None, "log": [], "warnings": [], "plan": pid}
    base = read(timeline_snapshot.write(ro, timeline_snapshot.store_dir(project.GetName(), tl.GetName()), otio=False))
    plan = {"id": pid, "ops": saved["ops"], "note": saved.get("note")}
    n = 2 + sum(1 for v in plan_versions.load().values() if v.get("of") == tl.GetUniqueId())
    name = str(p.get("name") or f"{tl.GetName()} · v{n} · {(saved.get('note') or pid)[:40]}")
    try:
        out = plan_versions.make(raw, base, plan, name, record, media=saved.get("media") or {})
    except (plan_apply.Stop, ValueError) as e:
        return {"plan": pid, "ok": False, "stopped": str(e), "version": record.data.get("version"),
                "log": record.data.get("log", [])[-10:]}
    copy = out.pop("copy_snapshot", None)
    try:                                                  # the A/B: what changed, his above, the version below
        ab, _ = render_previews(job, pid, base, saved, limit=int(p.get("limit", 4)), version=out["version"], copy=copy)
    except Exception as e:  # noqa: BLE001 - the version is made either way
        ab = [{"error": f"the A/B render failed: {e}"}]
    return {"plan": pid, "ok": True, **out, "ab": ab, "warnings": record.data.get("warnings", []),
            "compare": "switch between the two timelines in Resolve; resolve.drop_version deletes the copy"}


@ext.command("resolve.drop_version")
def drop_version_cmd(job):
    """Delete a version made by resolve.version: not while it's open, and not once anyone has edited it."""
    pid = str(job.params.get("plan") or "")
    record = plan_apply.Record(plan_store.folder(pid) / "version.json")
    v = record.data.get("version")
    if not v:
        return {"plan": pid, "ok": True, "note": "no version of this plan was made"}
    raw, ro, project, tl = open_timeline()
    try:
        return {"plan": pid, "ok": True, **plan_versions.drop(raw, v["id"])}
    except plan_apply.Stop as e:
        return {"plan": pid, "ok": False, "stopped": str(e)}


@ext.command("resolve.undo")
def undo_cmd(job):
    """Put back what a plan changed: everything it placed or moved goes, the snapshot's clips come back with
    their grades. Stops, changing nothing, if anything in the way isn't the plan's own."""
    pid = str(job.params.get("plan") or "")
    saved = plan_store.load(pid)
    record = plan_apply.Record(plan_store.folder(pid) / "applied.json")
    if not record.data.get("base"):
        return {"plan": pid, "ok": True, "note": "nothing of this plan was applied"}
    raw, ro, project, tl = open_timeline()
    if tl.GetUniqueId() != saved["timeline"]["id"]:
        raise RuntimeError(f"Resolve has {tl.GetName()!r} open; the plan is for {saved['timeline']['name']!r}")
    try:
        framecheck.idle(tl, float(job.params.get("wait", 3)))
    except framecheck.Busy as e:
        raise Retry(str(e)) from None
    base = read(Path(record.data["base"]["folder"]) / record.data["base"]["file"])
    plan = {"id": pid, "ops": saved["ops"], "note": saved.get("note")}
    try:
        out = plan_apply.undo(raw, base, plan, record, media=saved.get("media") or {},
                              gap_wait=float(job.params.get("wait", 3)))
    except plan_apply.Stop as e:
        return {"plan": pid, "ok": False, "stopped": str(e), "state": record.data["state"]}
    return {"plan": pid, "ok": True, **out, "state": record.data["state"]}


@ext.method("health.check")
def health(req: types.HealthCheckRequest) -> types.HealthCheckResponse:
    """One part, DaVinci Resolve, from the latest resolve.status. Methods run off the resolve lane, so
    this never calls Resolve itself."""
    State = types.Health.Part.State
    st = dict(last_status)
    if not st:
        state, why = State.STATE_UNSPECIFIED, "no status from Resolve yet"
    elif not st.get("connected"):
        reason = st.get("reason") or "not connected"
        state, why = (State.STATE_FAILING if "not accepting scripts" in reason else State.STATE_STOPPED), reason
    elif st.get("blocked"):
        state, why = State.STATE_FAILING, st["blocked"]
    else:
        state, why = State.STATE_OK, None
    return types.HealthCheckResponse(parts=[types.Health.Part(
        id="resolve", kind="app", name="DaVinci Resolve", version=st.get("version"), state=state, why=why)])


# ------------------------------------------------------------ markers that call an agent (PFE-30)


def marker_state() -> markers.State:
    if marks["state"] is None:
        marks["state"] = markers.State()
    return marks["state"]


def marker_projects() -> markers.Projects:
    if marks["projects"] is None:
        marks["projects"] = markers.Projects(state=marker_state())
    return marks["projects"]


# ------------------------------------------------------------ watching the cut (PFE-13 v1)


def watch_file() -> Path:
    return timeline_snapshot.journal.home() / "watch.json"


def watch_load() -> dict:
    try:
        return json.loads(watch_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def watch_save(data: dict) -> None:
    p = watch_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)


def edit_digest(snap: dict) -> str:
    """Changes when a clip does: place, length, source, file. Markers don't count."""
    rows = sorted((str(i.get("id")), i.get("kind"), i.get("track"), i.get("record"), i.get("frames"), i.get("in"),
                   i.get("file")) for i in snap["items"])
    return hashlib.sha1(json.dumps(rows, default=str).encode()).hexdigest()


def watch_config(project_name: str) -> dict | None:
    """The project's `follow` table in routes.toml: none, and the project isn't watched."""
    p = (markers.load_routes().get("projects") or {}).get(project_name) or {}
    cfg = p.get("follow")
    return cfg if isinstance(cfg, dict) else ({} if cfg is True else None)


def watch_check(ro, project, tl, cfg: dict, mine: dict, *, snap: dict | None = None,
                transitions: bool = True) -> tuple[dict, list, dict | None]:
    """The checks on the timeline now; the findings not posted before; a dry-run plan for their fixes."""
    if snap is None:
        snap, _ = timeline_snapshot.take(ro)
    report = timeline_watch.run(snap, cfg, previous_chapters=mine.get("chapters"), transitions=transitions)
    mine["chapters"], mine["digest"] = report["chapters"], edit_digest(snap)
    seen = set(mine.get("posted") or [])
    new = [f for f in report["findings"] if f["key"] not in seen]
    fixes = [f["fix"] for f in new if f.get("fix")]
    plan = make_plan(ro, project, tl, {"ops": fixes, "note": f"watch: {len(fixes)} fixes"}) if fixes else None
    return report, new, plan


def watch_post(snap_title: str, project, new: list, plan: dict | None, fps: float, work: Path) -> dict | None:
    routes = markers.load_routes()
    routing = markers.route(routes, project.GetName(), None, marker_fallback().get())
    if not routing["thread"]:
        ext.log(f"watch: {len(new)} findings in {project.GetName()!r}, but nowhere to post them")
        return None
    counts: dict = {}
    for f in new:
        counts[f["check"]] = counts.get(f["check"], 0) + 1
    pid = plan["plan"] if plan and plan.get("ok") else None
    text = (f"@{routing['mention']} watching {snap_title} while James paused: "
            + ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
            + (f". The mechanical fixes are plan `{pid}` (dry-run; `resolve_preview`, then `resolve_apply`)." if pid else ".")
            + (f" The plan has problems: {'; '.join(plan['problems'][:2])}" if plan and plan.get("problems") else ""))
    work.mkdir(parents=True, exist_ok=True)
    doc = work / "watch.md"
    doc.write_text(timeline_watch.markdown({"findings": new}, snap_title, fps, pid), encoding="utf-8")
    build = media.Build("document", [media.OutFile("document", doc, doc.name, "text/markdown")])
    return publish(build, project=project_for(project.GetName()), asset_path=f"resolve/{project.GetName()}/{snap_title}/watch",
                   title=f"{snap_title} watch", caption=text, post={"thread_id": routing["thread"], "text": text})


def watch_round() -> None:
    """Every 10 s: once James has paused 15 s and the cut has changed since the last check, the checks run, and
    what's new goes to the project's agent with a plan for the mechanical fixes. Only projects with a `follow`
    table in routes.toml are watched. Reads only; the plan is a dry run."""
    if not ext.settings.get("watch", True) or not publishes():
        return
    try:
        raw = bridge.connect()
    except ResolveUnavailable:
        return
    ro = ReadOnly(raw)
    project = ro.GetProjectManager().GetCurrentProject()
    tl = project.GetCurrentTimeline() if project else None
    if not project or not tl:
        return
    cfg = watch_config(project.GetName())
    if cfg is None or plan_versions.is_version(tl.GetUniqueId()):   # a copy to A/B (PFE-12): not the person's cut
        return
    end = tl.GetEndFrame()
    if hid_idle_seconds() < WATCH_IDLE or watching["end"] != end:
        watching["end"] = end                             # a pause starts when the timeline stops changing
        return
    state = watch_load()
    mine = state.setdefault(tl.GetUniqueId(), {"posted": [], "chapters": None, "digest": None})
    snap, _ = timeline_snapshot.take(ro)
    if edit_digest(snap) == mine.get("digest"):
        return                                            # checked already, nothing changed since
    report, new, plan = watch_check(ro, project, tl, cfg, mine, snap=snap)
    if new:
        try:
            watch_post(tl.GetName(), project, new, plan, snap["timeline"].get("fps") or 25,
                       Path(ext.node.get("work_dir") or tempfile.gettempdir()) / "watch")
            mine["posted"] = (list(mine.get("posted") or []) + [f["key"] for f in new])[-3000:]
        except Exception as e:  # noqa: BLE001 - the next pause tries again
            ext.log(f"watch: couldn't post {len(new)} findings: {e}")
    watch_save(state)


@ext.command("resolve.watch")
def watch_cmd(job):
    """The watcher's checks, now, on the open timeline (read-only): every finding, not just new ones, and a
    dry-run plan for the mechanical fixes."""
    _, ro, project, tl = open_timeline()
    cfg = watch_config(project.GetName()) or {}
    mine = {"posted": [], "chapters": (watch_load().get(tl.GetUniqueId()) or {}).get("chapters")}
    report, new, plan = watch_check(ro, project, tl, cfg, mine, transitions=job.params.get("transitions", True))
    return {"findings": report["findings"][:200], "roles": report["roles"], "plan": plan and plan.get("plan"),
            "plan_problems": plan and plan.get("problems"), "configured": bool(watch_config(project.GetName()) is not None)}


def marker_fallback() -> markers.Fallback:
    if marks["fallback"] is None:
        marks["fallback"] = markers.Fallback(state=marker_state(), routes=markers.load_routes())
    return marks["fallback"]


def post_marker(call: markers.Call, snap: dict, routing: dict, work: Path,
                renamed: tuple[str, str] | None = None) -> int | None:
    """The marker as a post: the text, with a still of its frame drawn from the source files (or the
    text as a file, if the still can't be drawn: core only posts with a file). The post's id."""
    text = markers.compose(call, snap, routing["mention"], ext.node.get("machine"), ticks=ticks_count(),
                           unknown=routing.get("unknown"), fallback=not routing["routed"], renamed=renamed)
    work.mkdir(parents=True, exist_ok=True)
    shot = work / f"marker-{call.frame}.png"
    drawn = timeline_render.still(snap, call.frame, shot)
    if drawn.get("ok"):
        build = media.build_image(shot, Path(tempfile.mkdtemp(dir=work)), lambda stage, f: None)
    else:
        ext.log(f"marker at {call.frame}: no still ({drawn.get('error')}); posting the text as a file")
        shot = shot.with_suffix(".md")
        shot.write_text(text, encoding="utf-8")
        build = media.Build("document", [media.OutFile("document", shot, shot.name, "text/markdown")])
    project, timeline = snap["project"]["name"], snap["timeline"]["name"]
    post = {"thread_id": routing["thread"], "text": text}
    answer = publish(build, project=project_for(project), asset_path=f"resolve/{project}/{timeline}/markers/{call.key}",
                     title=f"Marker at {markers.clock(call.frame, snap['timeline'].get('fps') or 25)}", caption=text,
                     post=post)
    return answer.get("message_id")


def reactions_of(post: int) -> list | None:
    """A post's reactions, where the node lets extensions read posts; otherwise None, and it isn't asked
    again until the extension restarts."""
    if marks["read_posts"] is False:
        return None
    try:
        answer = core("GET", f"/v1/node/messages/{post}") or {}
        marks["read_posts"] = True
        return answer.get("reactions") or []
    except RuntimeError as e:
        if "may not call" in str(e):
            marks["read_posts"] = False
            ext.log("the node doesn't let extensions read posts: markers turn green with resolve.marker_done")
        else:
            marks["read_posts"] = True      # allowed; this post just isn't there (yet)
        return None


def ticks_count() -> bool:
    """Whether a ✅ on the post will turn the marker green. The node may only let us read posts we made,
    so it's asked about one of ours; with none yet, the post says resolve.marker_done (the first poll of
    a real post settles it for the next ones)."""
    if marks["read_posts"] is None:
        ours = [v["post"] for v in marker_state().data.values() if v.get("post")]
        if ours:
            reactions_of(max(ours))
    return bool(marks["read_posts"])


def watch_markers() -> None:
    """Every 10 s, between the lane's commands: new markers that call an agent are posted, and posted
    ones turn green once they're done. Reads go through ReadOnly; the only write is MarkerWriter's."""
    if not ext.settings.get("marker_calls", True) or not publishes():
        return
    try:
        raw = bridge.connect()
    except ResolveUnavailable:
        return
    resolve = ReadOnly(raw)
    project = resolve.GetProjectManager().GetCurrentProject()
    tl = project.GetCurrentTimeline() if project else None
    if not project or not tl:
        return
    state, tid = marker_state(), tl.GetUniqueId()
    if plan_versions.is_version(tid):                   # a copy we made (PFE-12): its markers are the original's
        return
    name, pid = project.GetName(), project.GetUniqueId()
    known = marker_projects()
    known.see(name, pid, tid)
    writer = markers.MarkerWriter(raw.GetProjectManager().GetCurrentProject().GetCurrentTimeline())
    claims = {e.get("claim"): (k, e) for k, e in state.data.items() if e.get("claim")}
    for frame, m in markers.claimed(tl.GetMarkers()):
        key, entry = claims.get(m.get("customData"), (None, None))
        if entry and entry.get("status") == "posted":   # posted, the post's tag not on yet: put it on
            entry["marked"] = writer.mark(frame, m, markers.POSTED, entry["tag"])
            state.data[key] = entry
            state.save()
        elif not entry or entry.get("status") == "claimed":
            writer.release(m.get("customData"))          # never posted (a crash mid-post): next round posts it
            if key:
                state.data.pop(key, None)
                state.save()
    todo = []
    for call in markers.calls(tl.GetMarkers()):
        done = state.get(tid, call.key)
        if done is None:
            todo.append(call)
        elif done.get("status") == "posted" and done.get("marked") != "ok":
            # posted, but Resolve didn't take the tag last time: try again, never post again
            done["marked"] = writer.mark(call.frame, call.marker, markers.POSTED, done["tag"])
            state.put(tid, call.key, done)
    if todo:
        snap, _ = timeline_snapshot.take(resolve)        # one snapshot for this round, so stills match
        routes = markers.load_routes()
        routed = ((routes.get("projects") or {}).get(name) or {}).get("thread")
        renamed = None if routed else known.renamed_from(routes, name, pid, tid)
        fallback = marker_fallback()
        work = Path(ext.node.get("work_dir") or tempfile.gettempdir()) / "markers"
        for call in todo[:3]:                             # a burst of new markers goes out over a few rounds
            routing = markers.route(routes, renamed[0] if renamed else name, call.handle, fallback.get())
            if not routing["thread"]:
                if name not in marks["unrouted"]:
                    marks["unrouted"].add(name)
                    ext.log(f"markers in {name!r} call agents, but it has no route and there's no fallback yet "
                            "(a marker from a routed project, or resolve.set_fallback, sets one)")
                continue
            # claim it first: a post can take a while (a Wi-Fi drop), and if James's edits move the marker
            # meanwhile, an untagged marker at a new frame would look new and be posted again (marker 59)
            claim = f"{markers.CLAIM}{call.key}"
            got = writer.claim(call.frame, call.marker, claim)
            if got != "ok":
                if call.key not in marks.setdefault("unclaimed", set()):
                    marks["unclaimed"].add(call.key)
                    ext.log(f"marker at {call.frame}: not claimed ({got}); it's read again each round")
                continue
            state.put(tid, call.key, {"status": "claimed", "claim": claim, "frame": call.frame, "at": time.time(),
                                      "color": call.marker.get("color")})
            try:
                post = post_marker(call, snap, routing, work, renamed)
            except Exception as e:  # noqa: BLE001 - one marker failing mustn't stop the rest
                ext.log(f"marker at {call.frame}: couldn't post: {e}")
                writer.release(claim)                    # untagged again, so the next round tries it again
                state.data.pop(f"{tid}/{call.key}", None)
                state.save()
                continue
            if not post:
                ext.log(f"marker at {call.frame}: core stored the still but made no post (thread {routing['thread']})")
                writer.release(claim)
                state.put(tid, call.key, {"status": "unposted", "frame": call.frame, "at": time.time()})
                continue
            if routing["routed"]:                         # where the next unrouted project's markers go
                fallback.set(routing["thread"], routing["agent"], by="marker", project=renamed[0] if renamed else name)
            tag = f"pm:{post}"
            entry = {"status": "posted", "post": post, "tag": tag, "claim": claim, "mention": routing["mention"],
                     "frame": call.frame, "project": name, "timeline": tl.GetName(), "timeline_id": tid,
                     "at": time.time(), "color": call.marker.get("color"), "marked": "gone"}
            state.put(tid, call.key, entry)              # posted: whatever happens next, it's never posted again
            found = writer.find(claim)                   # wherever James's edits have moved it by now
            if found and (found[1].get("name"), found[1].get("note")) != (call.marker.get("name"), call.marker.get("note")):
                writer.release(claim)                    # edited while its post was out: post the new text too
                entry["marked"] = "edited"
                state.put(tid, call.key, entry)
                found = None
            if found:
                entry["marked"] = writer.mark(found[0], found[1], markers.POSTED, tag)
                entry["frame"] = found[0]
                state.put(tid, call.key, entry)
    # every round (10 s): James idle for HEAL_IDLE and the timeline unchanged since the last round, so a heal
    # never lands mid-undo, and a reverted colour is back within about 20 s (James: 10 s, not 30)
    end = tl.GetEndFrame()
    if marks.get("heal_end") == end:
        heal_colours(writer, state, tid)                # checks idle only if a colour needs healing
    marks["heal_end"] = end
    if time.monotonic() - marks["polled"] > MARK_POLL:
        marks["polled"] = time.monotonic()
        for key, entry in state.open():
            if entry.get("status") != "posted" or entry.get("timeline_id") != tid:
                continue
            by = markers.done_by(reactions_of(entry["post"]) or [], entry.get("mention") or "")
            if by:
                finish(writer, state, key, entry, by)


def heal_colours(writer: markers.MarkerWriter, state: markers.State, tid: str) -> None:
    """Our markers show where they stand (cyan posted, green done). One that's back at the colour it had
    before we touched it (an undo in Resolve takes back our delete + add) gets ours again. One James has
    given some other colour is his choice, and is left alone."""
    have = {m.get("customData"): (int(float(f)), m) for f, m in (writer.GetMarkers() or {}).items()
            if str(m.get("customData") or "").startswith("pm:")}
    idle = None
    for key, entry in list(state.data.items()):
        if entry.get("timeline_id") != tid or entry.get("status") not in ("posted", "done") or not entry.get("tag"):
            continue
        found = have.get(entry["tag"])
        want = markers.DONE if entry["status"] == "done" else markers.POSTED
        if not found or found[1].get("color") == want or found[1].get("color") != (entry.get("color") or "Blue"):
            continue
        if idle is None:
            idle = hid_idle_seconds()                   # one ioreg, and only when there's something to heal
        if idle < HEAL_IDLE:
            return
        result = writer.mark(found[0], found[1], want, entry["tag"])
        ext.log(f"marker {entry['tag']} at {found[0]} was back to {found[1].get('color')}: recoloured {want} ({result})")


def finish(writer: markers.MarkerWriter, state: markers.State, key: str, entry: dict, by: str) -> dict:
    found = writer.find(entry["tag"])
    if found is None:
        return {"marker": "not on the open timeline"}
    frame, now = found
    result = writer.mark(frame, now, markers.DONE, entry["tag"])
    if result == "ok":
        state.data[key] = {**entry, "status": "done", "done_by": by, "done_at": time.time()}
        state.save()
    return {"marker": result, "frame": frame}


@ext.command("resolve.marker_done")
def marker_done(job):
    """The marker behind a post turns green: the agent it called is done with it."""
    post = int(job.params["post"])
    try:
        raw = bridge.connect()
    except ResolveUnavailable as e:
        raise unavailable(e) from None
    tl = raw.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    if tl is None:
        raise Retry("no timeline open in Resolve")
    state = marker_state()
    key, entry = next(((k, v) for k, v in state.data.items() if v.get("post") == post),
                      (None, {"tag": f"pm:{post}", "post": post}))
    result = finish(markers.MarkerWriter(tl), state, key or f"{tl.GetUniqueId()}/post-{post}", entry,
                    job.params.get("by") or "agent")
    return {"post": post, **result}


@ext.command("resolve.set_fallback")
def set_fallback(job):
    """Where markers from a project with no route go: a thread, and who @pm means there. With neither
    given, where it is now."""
    fb = marker_fallback()
    thread, agent = job.params.get("thread"), job.params.get("agent")
    if thread is None and agent is None:
        return {"fallback": fb.get()}
    now = fb.get() or {}
    if thread is None and not now.get("thread"):
        raise ValueError("there's no fallback thread yet: give `thread` too")
    try:
        thread = int(thread if thread is not None else now["thread"])
    except (TypeError, ValueError):
        raise ValueError(f"`thread` is a board thread's id, not {thread!r}") from None
    agent = str(agent or now.get("agent") or markers.load_routes().get("person") or "james").lstrip("@").lower()
    hit = markers.TRIGGER.match("@" + agent)
    if thread <= 0 or not hit or hit[1] != agent:
        raise ValueError("`thread` is a board thread's id and `agent` a handle")
    was = fb.set(thread, agent, by=str(job.params.get("by") or "agent"))
    marks["unrouted"].clear()                          # say so again if it's ever missing
    return {"fallback": fb.get(), "was": was}


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
ext.every(MARK_EVERY, watch_markers, lane="resolve")
ext.every(WATCH_EVERY, watch_round, lane="resolve")

if __name__ == "__main__":
    ext.run()
