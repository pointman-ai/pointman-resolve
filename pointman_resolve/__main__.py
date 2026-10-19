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
import os
import shutil
import tempfile
import time
from pathlib import Path

try:
    from pointman_extension import Cancelled, Extension, Retry
except ImportError:                 # a node from before the rename (thread 2255 [3561])
    from mb_extension import Cancelled, Extension, Retry  # type: ignore[no-redef]

from . import markers, media
from .bridge import ResolveBridge, ResolveUnavailable, hid_idle_seconds, snapshot_digest
from .timeline import check as timeline_check
from .timeline import framecheck
from .timeline import diff as timeline_diff
from .timeline import render as timeline_render
from .timeline import snapshot as timeline_snapshot
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


SHADOW_MAX_S = 600   # a shadow render is ffmpeg on this Mac: ten minutes at most per request


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
    # grades are never drawn yet (PFE-42); the rest is what this range has that the renderer skipped
    result["not_drawn"] = ["grades"] + sorted({s["why"] for s in r["skipped"]})
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


@ext.method("health.check")
def health(params):
    """One part, DaVinci Resolve, from the latest resolve.status. Methods run off the resolve lane, so
    this never calls Resolve itself."""
    st = dict(last_status)
    if not st:
        state, why = "unknown", "no status from Resolve yet"
    elif not st.get("connected"):
        reason = st.get("reason") or "not connected"
        state, why = ("failing" if "not accepting scripts" in reason else "stopped"), reason
    elif st.get("blocked"):
        state, why = "failing", st["blocked"]
    else:
        state, why = "ok", None
    return [{"id": "resolve", "kind": "app", "name": "DaVinci Resolve", "version": st.get("version"),
             "state": state, "needs": [], "why": why}]


# ------------------------------------------------------------ markers that call an agent (PFE-30)


def marker_state() -> markers.State:
    if marks["state"] is None:
        marks["state"] = markers.State()
    return marks["state"]


def marker_projects() -> markers.Projects:
    if marks["projects"] is None:
        marks["projects"] = markers.Projects(state=marker_state())
    return marks["projects"]


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
    name, pid = project.GetName(), project.GetUniqueId()
    known = marker_projects()
    known.see(name, pid, tid)
    writer = markers.MarkerWriter(raw.GetProjectManager().GetCurrentProject().GetCurrentTimeline())
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
            try:
                post = post_marker(call, snap, routing, work, renamed)
            except Exception as e:  # noqa: BLE001 - one marker failing mustn't stop the rest
                ext.log(f"marker at {call.frame}: couldn't post: {e}")
                continue
            if not post:
                ext.log(f"marker at {call.frame}: core stored the still but made no post (thread {routing['thread']})")
                state.put(tid, call.key, {"status": "unposted", "frame": call.frame, "at": time.time()})
                continue
            if routing["routed"]:                         # where the next unrouted project's markers go
                fallback.set(routing["thread"], routing["agent"], by="marker", project=renamed[0] if renamed else name)
            tag = f"pm:{post}"
            state.put(tid, call.key, {"status": "posted", "post": post, "tag": tag, "mention": routing["mention"],
                                      "frame": call.frame, "project": name, "timeline": tl.GetName(),
                                      "timeline_id": tid, "at": time.time(),
                                      "marked": writer.mark(call.frame, call.marker, markers.POSTED, tag)})
    if time.monotonic() - marks["polled"] > MARK_POLL:
        marks["polled"] = time.monotonic()
        for key, entry in state.open():
            if entry.get("status") != "posted" or entry.get("timeline_id") != tid:
                continue
            by = markers.done_by(reactions_of(entry["post"]) or [], entry.get("mention") or "")
            if by:
                finish(writer, state, key, entry, by)


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

if __name__ == "__main__":
    ext.run()
