"""DaVinci Resolve Studio, through its scripting API: the bridge the Mac's node had built in
(src/messageboard/agent/resolve.py), unchanged apart from where it finds Resolve.

Resolve must be open with Preferences > System > General > External scripting
using: Local. Every call here happens on the extension's "resolve" lane, one thread,
because the API isn't meant to be driven from several threads at once.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

API = os.environ.get("RESOLVE_SCRIPT_API",
                     "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting")
LIB = os.environ.get("RESOLVE_SCRIPT_LIB",
                     "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so")
APP = os.environ.get("RESOLVE_APP", "/Applications/DaVinci Resolve/DaVinci Resolve.app")
FAKE = bool(os.environ.get("MB_RESOLVE_FAKE"))   # tests: a stand-in DaVinciResolveScript under API
FAKE_CLOSED = os.environ.get("FAKE_RESOLVE_CLOSED")   # tests: while this file exists, Resolve isn't open


class ResolveUnavailable(RuntimeError):
    pass


def snapshot_digest(snap: dict) -> str:
    """Changes whenever the edit does: clips, trims, tracks or markers."""
    keep = {k: snap.get(k) for k in ("name", "project", "duration_frames", "tracks")}
    # markers dailies added for phone notes aren't edits
    keep["markers"] = [m for m in snap.get("markers", []) if not str(m.get("custom") or "").startswith("dailies:")]
    return hashlib.sha1(json.dumps(keep, sort_keys=True).encode()).hexdigest()


def hid_idle_seconds() -> float:
    """Seconds since the keyboard, mouse or trackpad was last used."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    m = re.search(r'"HIDIdleTime" = (\d+)', out)
    return int(m.group(1)) / 1e9 if m else 0.0


def _fps(value) -> float:
    try:
        return float(str(value).split()[0])
    except (TypeError, ValueError, IndexError):
        return 25.0


class ResolveBridge:
    def __init__(self):
        self._resolve = None
        self._module = None

    # ------------------------------------------------------------ connection

    @staticmethod
    def installed() -> bool:
        return Path(APP).exists() and Path(LIB).exists()

    def _import(self):
        if self._module is None:
            os.environ["RESOLVE_SCRIPT_API"] = API
            os.environ["RESOLVE_SCRIPT_LIB"] = LIB
            if f"{API}/Modules" not in sys.path:
                sys.path.append(f"{API}/Modules")
            import DaVinciResolveScript  # type: ignore

            self._module = DaVinciResolveScript
        return self._module

    @staticmethod
    def running() -> bool:
        if FAKE:
            return not (FAKE_CLOSED and Path(FAKE_CLOSED).exists())
        r = subprocess.run(["pgrep", "-x", "Resolve"], capture_output=True)
        return r.returncode == 0

    def connect(self):
        """The Resolve object, or ResolveUnavailable. Cheap when already connected."""
        if not self.installed():
            raise ResolveUnavailable("Resolve isn't installed")
        if not self.running():
            self._resolve = None
            raise ResolveUnavailable("Resolve isn't open")
        if self._resolve is not None:
            try:
                if self._resolve.GetProjectManager() is not None:
                    return self._resolve
            except Exception:  # noqa: BLE001 - stale handle after Resolve restarted
                pass
        self._resolve = self._import().scriptapp("Resolve")
        if self._resolve is None:
            raise ResolveUnavailable("Resolve is open but not accepting scripts "
                                     "(Preferences > System > General > External scripting using: Local)")
        return self._resolve

    def _project(self):
        project = self.connect().GetProjectManager().GetCurrentProject()
        if project is None:
            raise ResolveUnavailable("no project open in Resolve")
        return project

    @staticmethod
    def _timelines(project) -> list:
        return [project.GetTimelineByIndex(i) for i in range(1, (project.GetTimelineCount() or 0) + 1)]

    def _timeline(self, project, name: str | None):
        if not name:
            tl = project.GetCurrentTimeline()
            if tl is None:
                raise ResolveUnavailable("no timeline open in Resolve")
            return tl
        for tl in self._timelines(project):
            if tl and tl.GetName() == name:
                return tl
        raise ResolveUnavailable(f"no timeline called {name!r} in {project.GetName()!r}")

    # ------------------------------------------------------------ reading

    def status(self) -> dict:
        try:
            resolve = self.connect()
        except ResolveUnavailable as e:
            return {"connected": False, "reason": str(e)}
        project = resolve.GetProjectManager().GetCurrentProject()
        out = {"connected": True, "version": resolve.GetVersionString(), "page": resolve.GetCurrentPage(),
               "project": project.GetName() if project else None}
        if project is None:
            return out
        out["rendering"] = bool(project.IsRenderingInProgress())
        out["timelines"] = [t.GetName() for t in self._timelines(project)[:100] if t]
        tl = project.GetCurrentTimeline()
        if tl is not None:
            fps = _fps(tl.GetSetting("timelineFrameRate") or project.GetSetting("timelineFrameRate"))
            start, end = tl.GetStartFrame(), tl.GetEndFrame()
            out["timeline"] = {
                "name": tl.GetName(), "fps": fps, "duration": (end - start) / fps,
                "timecode": tl.GetCurrentTimecode(), "start_timecode": tl.GetStartTimecode(),
                "video_tracks": tl.GetTrackCount("video"), "audio_tracks": tl.GetTrackCount("audio"),
                "markers": len(tl.GetMarkers() or {}),
            }
        return out

    def snapshot(self, project, tl) -> dict:
        """The timeline's layout, stored with each preview render so the phone can draw it."""
        fps = _fps(tl.GetSetting("timelineFrameRate") or project.GetSetting("timelineFrameRate"))
        start, end = tl.GetStartFrame(), tl.GetEndFrame()
        tracks = []
        for kind in ("video", "audio"):
            for i in range(1, (tl.GetTrackCount(kind) or 0) + 1):
                clips = []
                for item in (tl.GetItemListInTrack(kind, i) or [])[:3000]:
                    clips.append({"name": item.GetName(), "start": item.GetStart() - start,
                                  "end": item.GetEnd() - start})
                tracks.append({"type": kind, "index": i, "name": tl.GetTrackName(kind, i), "clips": clips})
        markers = []
        for frame, m in sorted((tl.GetMarkers() or {}).items()):
            markers.append({"frame": int(frame), "color": m.get("color"), "name": m.get("name"),
                            "note": m.get("note"), "duration": m.get("duration"),
                            "custom": m.get("customData") or None})
        return {"name": tl.GetName(), "project": project.GetName(), "fps": fps, "start_frame": start,
                "duration_frames": end - start, "start_timecode": tl.GetStartTimecode(),
                "width": int(tl.GetSetting("timelineResolutionWidth") or 1920),
                "height": int(tl.GetSetting("timelineResolutionHeight") or 1080),
                "tracks": tracks, "markers": markers}

    def signature(self) -> tuple[str, str, str] | None:
        """(project, timeline, digest) of the timeline open in Resolve, or None."""
        project = self._project()
        tl = project.GetCurrentTimeline()
        if tl is None:
            return None
        return project.GetName(), tl.GetName(), snapshot_digest(self.snapshot(project, tl))

    # ------------------------------------------------------------ rendering

    def render(self, work: Path, *, timeline: str | None = None, height: int = 720,
               progress: Callable[[float], None] = lambda p: None,
               cancelled: Callable[[], bool] = lambda: False) -> tuple[Path, dict]:
        """Render the whole timeline to a small H.265 file. The project's render
        settings are saved to a temporary preset first and restored afterwards."""
        resolve = self.connect()
        project = self._project()
        if project.IsRenderingInProgress():
            raise RuntimeError("Resolve is already rendering")
        page = resolve.GetCurrentPage()   # rendering switches to Deliver; put the user back afterwards
        tl = self._timeline(project, timeline)
        snap = self.snapshot(project, tl)
        previous = project.GetCurrentTimeline()
        switched = previous is None or previous.GetName() != tl.GetName()
        if switched:
            project.SetCurrentTimeline(tl)
        preset = f"dailies-restore-{int(time.time())}"
        saved = bool(project.SaveAsNewRenderPreset(preset))
        job = None
        try:
            project.SetCurrentRenderMode(1)
            codecs = project.GetRenderCodecs("mp4") or {}
            codec = "H265" if "H265" in codecs.values() else "H264"
            if not project.SetCurrentRenderFormatAndCodec("mp4", codec):
                raise RuntimeError(f"Resolve rejected mp4/{codec}")
            h = min(height, snap["height"])
            w = int(round(snap["width"] * h / snap["height"] / 2) * 2)
            name = f"dailies-{int(time.time())}"
            ok = project.SetRenderSettings({
                "SelectAllFrames": True, "TargetDir": str(work), "CustomName": name,
                "FormatWidth": w, "FormatHeight": h, "ExportVideo": True, "ExportAudio": True,
            })
            if not ok:
                raise RuntimeError("Resolve rejected the render settings")
            job = project.AddRenderJob()
            if not job or not project.StartRendering(job):
                raise RuntimeError("Resolve wouldn't start the render")
            time.sleep(1)
            while project.IsRenderingInProgress():
                if cancelled():
                    project.StopRendering()
                    raise RuntimeError("cancelled")
                status = project.GetRenderJobStatus(job) or {}
                progress(float(status.get("CompletionPercentage") or 0) / 100)
                time.sleep(1)
            status = project.GetRenderJobStatus(job) or {}
            if status.get("JobStatus") not in ("Complete", None):
                raise RuntimeError(f"render {status.get('JobStatus')}: {status.get('Error', '')}".strip())
        finally:
            if job:
                project.DeleteRenderJob(job)
            if saved:
                project.LoadRenderPreset(preset)
                project.DeleteRenderPreset(preset)
            if switched and previous is not None:
                project.SetCurrentTimeline(previous)
            if page and resolve.GetCurrentPage() != page:
                resolve.OpenPage(page)
        outs = sorted(work.glob(f"{name}*.mp4"), key=lambda p: p.stat().st_mtime)
        if not outs:
            raise RuntimeError("Resolve said it finished but no file appeared")
        return outs[-1], snap

    def still(self, work: Path) -> tuple[Path, dict]:
        """The frame under the playhead, as a PNG."""
        resolve = self.connect()
        project = self._project()
        tl = self._timeline(project, None)
        dest = work / "frame.png"
        info = {"project": project.GetName(), "timeline": tl.GetName(), "timecode": tl.GetCurrentTimecode()}
        if not project.ExportCurrentFrameAsStill(str(dest)):
            # some versions only export from the Color page; visit it and come back
            page = resolve.GetCurrentPage()
            resolve.OpenPage("color")
            try:
                ok = project.ExportCurrentFrameAsStill(str(dest))
            finally:
                if page:
                    resolve.OpenPage(page)
            if not ok:
                raise RuntimeError("Resolve couldn't export the current frame")
        return dest, info

    # ------------------------------------------------------------ notes -> markers

    def apply_note(self, note: dict, project_name: str, timeline_name: str) -> dict | None:
        """Mirror a phone note as a timeline marker (purple while open, green once
        resolved, removed if deleted). None means "try again later": the note's
        project isn't the one open in Resolve right now."""
        project = self._project()
        if project.GetName() != project_name:
            return None
        tl = self._timeline(project, timeline_name)
        custom = f"dailies:{note['id']}"
        tl.DeleteMarkerByCustomData(custom)
        if note.get("deleted"):
            return {"marker": "removed"}
        fps = _fps(tl.GetSetting("timelineFrameRate") or project.GetSetting("timelineFrameRate"))
        frame = int(round((note.get("timecode") or 0) * fps))
        color = "Green" if note.get("resolved") else "Purple"
        title = ("Resolved: " if note.get("resolved") else "Review: ") + note["body"].splitlines()[0][:60]
        body = f"{note['body']}\n\n({note.get('author') or 'phone'} via Message Board)"
        for f in range(frame, frame + 50):
            if tl.AddMarker(f, color, title, body, 1, custom):
                return {"marker": "added", "frame": f, "timeline": timeline_name, "project": project_name}
        raise RuntimeError("couldn't place a marker near that frame")
