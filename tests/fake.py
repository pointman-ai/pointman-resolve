"""A small fake of Resolve's scripting objects: just enough timeline for snapshot, check and the
read-only guard. Every call is logged on the fake Resolve, so tests can see what reached it.

Writes raise unless the timeline is `writable` (edit plans' tests): then AppendToTimeline, DeleteClips
(with ripple), settings, CopyGrades and AddTransition behave as Resolve 21.1 does where PFE-9 and the
Spacesheep edit showed how: an append onto a taken spot returns a clip and places nothing, a busy
Resolve (`busy` > 0) returns nothing, and a 60 fps file is placed in its own frames."""

import json
import math
from pathlib import Path

CALLS: list[str] = []


class Obj:
    def __getattribute__(self, name):
        if name[0].isupper() and not name.isupper():
            CALLS.append(name)
        return object.__getattribute__(self, name)


class Graph(Obj):
    def __init__(self, n, tools=None):
        self.n, self.tools = n, tools or {}

    def GetNumNodes(self):
        return self.n

    def GetToolsInNode(self, i):
        return self.tools.get(i)            # None on an untouched node, as Resolve 21.1 gives it

    def GetLUT(self, i):
        return ""


class Media(Obj):
    def __init__(self, path, fps=25.0, tc_start=0, frames=100000):
        self.path, self.fps, self.tc_start, self.frames = path, fps, tc_start, frames

    def GetClipProperty(self, name):
        return {"File Path": self.path, "FPS": str(self.fps),
                "Frames": str(self.frames) if self.fps else ""}.get(name, "")

    def GetUniqueId(self):
        return "mp-" + Path(self.path).stem

    def ReplaceClip(self, path):  # a write: must never be reached through the guard
        raise AssertionError("ReplaceClip reached Resolve")


class Item(Obj):
    def __init__(self, uid, start, frames, left, media=None, *, type="video", props=None, fades=None,
                 speed=None, markers=None, nodes=1, name=None):
        self.uid, self.start, self.frames, self.left, self.media = uid, start, frames, left, media
        self.type, self.props = type, props or {}
        self.fades = fades if fades is not None else {"FadeIn": 0.0, "FadeOut": 0.0}
        self.speed = speed if speed is not None else {"Percentage": 100.0}
        self.markers, self.nodes, self.name = markers or {}, nodes, name or (Path(media.path).name if media else type)
        self.linked, self.tools, self.fusion = [], {}, 0
        self.src, self.color, self.enabled, self.tl = None, "", True, None

    def GetUniqueId(self): return self.uid
    def GetStart(self): return self.start
    def GetDuration(self): return self.frames
    def GetLeftOffset(self): return self.left
    def GetSourceStartTime(self): return (self.media.tc_start + self.left) / 25 if self.media else 0.0
    def GetSourceStartFrame(self): return self.left if self.src is None else self.src
    def GetMediaPoolItem(self): return self.media
    def GetName(self): return self.name
    def GetType(self): return self.type
    def GetProperties(self): return dict(self.props)
    def GetFades(self): return None if self.type == "transition" else dict(self.fades)
    def GetSpeed(self): return False if self.type == "transition" else dict(self.speed)
    def GetClipEnabled(self): return self.enabled
    def GetClipColor(self): return self.color
    def GetNodeGraph(self): return Graph(self.nodes, self.tools)
    def GetColorGroup(self): return None
    def GetFusionCompCount(self): return self.fusion
    def GetLinkedItems(self): return self.linked
    def GetMarkers(self): return dict(self.markers)

    def SetProperty(self, *a):
        raise AssertionError("SetProperty reached Resolve")

    # writes, for edit plans (the timeline must be writable)
    def _w(self, name):
        if not (self.tl and self.tl.writable):
            raise AssertionError(f"{name} reached Resolve")

    def SetProperties(self, props):
        self._w("SetProperties")
        self.props.update(props)
        return True

    def SetFades(self, fades):
        self._w("SetFades")
        self.fades.update(fades)
        return True

    def SetClipColor(self, color):
        self._w("SetClipColor")
        self.color = color
        return True

    def SetClipEnabled(self, on):
        self._w("SetClipEnabled")
        self.enabled = on
        return True

    def AddMarker(self, frame, color, name, note, duration, custom=""):
        self._w("AddMarker")
        if int(frame) in self.markers or not 0 <= int(frame) < self.frames:
            return False
        self.markers[int(frame)] = {"color": color, "name": name, "note": note, "duration": duration,
                                    "customData": custom}
        return True

    def CopyGrades(self, targets):
        self._w("CopyGrades")
        for x in targets:
            x.tools, x.nodes = dict(self.tools), self.nodes
        return True

    def AddTransition(self, opts):
        self._w("AddTransition")
        d = int(opts.get("duration") or 8)
        end = self.start + self.frames
        tr = Item(f"tr{self.tl.next()}", end - d // 2, d, 0, type="transition", name=opts.get("type"))
        tr.tl = self.tl
        key = next(k for k, v in self.tl.tracks.items() if self in v)
        self.tl.tracks[key].append(tr)
        return tr


class Timeline(Obj):
    def __init__(self, tracks, markers=None, start=90000, names=None):
        self.tracks, self.markers, self.start = tracks, markers or {}, start  # {("video", 1): [Item]}
        self.names, self.writable, self.busy, self.serial, self.playhead = names or {}, False, 0, 0, "01:00:00:00"
        for items in tracks.values():
            for it in items:
                it.tl = self

    def next(self):
        self.serial += 1
        return self.serial

    def GetCurrentTimecode(self): return self.playhead

    def GetName(self): return "Stage 1 · silences out"
    def GetUniqueId(self): return "tl-1"
    def GetSettings(self): return {"timelineFrameRate": "25", "timelineResolutionWidth": "1920",
                                   "timelineResolutionHeight": "1080"}
    def GetStartFrame(self): return self.start
    def GetEndFrame(self): return self.start + max((i.start - self.start + i.frames for v in self.tracks.values() for i in v
                                                     if i.type != "transition"), default=0)
    def GetStartTimecode(self): return "01:00:00:00"
    def GetTrackCount(self, kind): return max((i for k, i in self.tracks if k == kind), default=0)
    def GetTrackName(self, kind, i): return self.names.get((kind, i), f"{kind.capitalize()} {i}")
    def GetIsTrackEnabled(self, kind, i): return True
    def GetIsTrackLocked(self, kind, i): return False
    def GetTrackSubType(self, kind, i): return "stereo" if kind == "audio" else ""
    def GetItemListInTrack(self, kind, i): return list(self.tracks.get((kind, i), []))
    def GetMarkers(self): return {float(f): dict(m) for f, m in self.markers.items()}  # Resolve keys are floats

    # marker writes, for markers.MarkerWriter (fail_add makes AddMarker refuse, to test the restore)
    fail_add = 0

    def UpdateMarkerCustomData(self, frame, custom):
        if int(frame) not in self.markers:
            return False
        self.markers[int(frame)]["customData"] = custom
        return True

    def DeleteMarkerAtFrame(self, frame):
        return self.markers.pop(int(frame), None) is not None

    def AddMarker(self, frame, color, name, note, duration, custom=""):
        if self.fail_add > 0:
            self.fail_add -= 1
            return False
        if int(frame) in self.markers:
            return False
        self.markers[int(frame)] = {"color": color, "name": name, "note": note, "duration": duration, "customData": custom}
        return True

    def DeleteClips(self, items, ripple=False):
        if not self.writable:
            raise AssertionError("DeleteClips reached Resolve")
        gone = {id(i) for i in items}
        cut = {}
        for key, v in self.tracks.items():
            spans = sorted((i.start, i.start + i.frames) for i in v if id(i) in gone)
            edges = {x for s in spans for x in s}
            keep = [i for i in v if id(i) not in gone and not (i.type == "transition" and any(
                i.start <= x <= i.start + i.frames for x in edges))]
            if ripple and spans:
                for i in keep:
                    i.start -= sum(e - s for s, e in spans if e <= i.start)
                cut[key] = spans
            self.tracks[key] = keep
        if ripple and cut:                       # Resolve moves the timeline's markers with the ripple
            spans = max(cut.values(), key=len)
            self.markers = {f - sum(e - s for s, e in spans if e <= self.start + f): m for f, m in self.markers.items()
                            if not any(s <= self.start + f < e for s, e in spans)}
        return True

    def Export(self, path, kind, sub):
        """Writes OTIO the way Resolve 21.1 does for what this fake holds (see PFE-9)."""
        children = []
        for kind_name, otio_kind in (("video", "Video"), ("audio", "Audio")):
            for i in range(1, self.GetTrackCount(kind_name) + 1):
                at, kids = 0, []
                items = sorted(self.tracks.get((kind_name, i), []), key=lambda x: x.start)
                for it in items:
                    rec = it.start - self.start
                    if it.type == "transition":
                        kids.append({"OTIO_SCHEMA": "Transition.1", "name": it.name, "in_offset": {"value": it.frames / 2},
                                     "out_offset": {"value": it.frames / 2}})
                        continue
                    if rec > at:
                        kids.append({"OTIO_SCHEMA": "Gap.1", "source_range": {
                            "duration": {"value": rec - at, "rate": 25.0}, "start_time": {"value": 0, "rate": 25.0}}})
                    params = []
                    for prop, pid, eff, scale in (("ZoomX", "transformationZoomX", "Transform", 1),
                                                  ("ZoomY", "transformationZoomY", "Transform", 1),
                                                  ("Pan", "transformationPan", "Transform", 960),
                                                  ("Tilt", "transformationTilt", "Transform", 540),
                                                  ("CropLeft", "cropLeft", "Cropping", 960),
                                                  ("CropRight", "cropRight", "Cropping", 960),
                                                  ("CropTop", "cropTop", "Cropping", 540),
                                                  ("CropBottom", "cropBottom", "Cropping", 540)):
                        v = it.props.get(prop)
                        if v not in (None, 0, 0.0, 1.0):
                            params.append((eff, {"Parameter ID": pid, "Parameter Value": v / scale}))
                    effects = [{"metadata": {"Resolve_OTIO": {"Effect Name": eff, "Parameters": [p]}}} for eff, p in params]
                    tc = it.media.tc_start if it.media else 0
                    kids.append({"OTIO_SCHEMA": "Clip.2", "name": it.name, "effects": effects,
                                 "source_range": {"duration": {"value": it.frames, "rate": 25.0},
                                                  "start_time": {"value": tc + it.left, "rate": 25.0}},
                                 "active_media_reference_key": "DEFAULT_MEDIA",
                                 "media_references": {"DEFAULT_MEDIA": {"available_range": {"start_time": {"value": tc}}}}})
                    at = rec + it.frames
                children.append({"kind": otio_kind, "children": kids})
        markers = [{"name": m["name"]} for m in self.markers.values()]
        # raw UTF-8, as Resolve writes it ("·" in names), not JSON's ASCII escapes
        Path(path).write_text(json.dumps({"tracks": {"children": children, "markers": markers}}, ensure_ascii=False),
                              encoding="utf-8")
        return True


class Folder(Obj):
    def __init__(self, clips):
        self.clips = clips

    def GetClipList(self): return list(self.clips)
    def GetSubFolderList(self): return []


class MediaPool(Obj):
    def __init__(self, tl):
        self.tl = tl
        self.extra = []

    def GetRootFolder(self):
        seen = {}
        for v in self.tl.tracks.values():
            for i in v:
                if i.media:
                    seen.setdefault(i.media.path, i.media)
        for m in self.extra:
            seen.setdefault(m.path, m)
        return Folder(list(seen.values()))

    def AppendToTimeline(self, infos):
        tl = self.tl
        if not tl.writable:
            raise AssertionError("AppendToTimeline reached Resolve")
        if tl.busy > 0:
            tl.busy -= 1
            return []
        out = []
        for info in infos:
            media = info["mediaPoolItem"]
            key = ("video" if info.get("mediaType", 1) == 1 else "audio", info["trackIndex"])
            rate = media.fps or 25.0
            span = info["endFrame"] - info["startFrame"]
            fast = key[0] == "video" and abs(rate - 25) > 0.01
            frames = math.floor(span * 25 / rate + 1e-9) if fast else span
            it = Item(f"n{tl.next()}", info["recordFrame"], frames,
                      round(info["startFrame"] * 25 / rate) if fast else info["startFrame"], media,
                      type=key[0], nodes=1 if key[0] == "video" else None)
            it.src = info["startFrame"] if fast else None
            it.tl = tl
            taken = any(i.start < it.start + it.frames and it.start < i.start + i.frames and i.type != "transition"
                        for i in tl.tracks.get(key, []))
            if not taken:                         # onto a taken spot: Resolve returns the clip and places nothing
                tl.tracks.setdefault(key, []).append(it)
                tl.tracks[key].sort(key=lambda x: x.start)
            out.append(it)
        return out


class Project(Obj):
    def __init__(self, tl):
        self.tl = tl
        self.pool = MediaPool(tl)

    def GetMediaPool(self): return self.pool

    def GetName(self): return "feat-flow-with-spacesheep"
    def GetUniqueId(self): return "pr-1"
    def GetCurrentTimeline(self): return self.tl

    def SaveProject(self):
        raise AssertionError("SaveProject reached Resolve")


class PM(Obj):
    def __init__(self, project):
        self.project = project

    def GetCurrentProject(self): return self.project

    def LoadProject(self, name):
        raise AssertionError("LoadProject reached Resolve")


class Resolve(Obj):
    EXPORT_OTIO, EXPORT_NONE = 31, 0

    def __init__(self, tl):
        self.pm = PM(Project(tl))

    def GetProjectManager(self): return self.pm
    def GetVersionString(self): return "21.1.0.17"


def sample_timeline() -> Timeline:
    """Camera on V2 under a punched-in loft on V3, a cutout on V5 with crops, a card with a dissolve on
    V6, voice on A1, and markers."""
    cam = Media("/raw/C0030.MP4", tc_start=121092)
    loft = Media("/raw/composites/C0030-f009844.mov")
    cut = Media("/raw/composites/C0030-f009844-alpha.mov")
    card = Media("/graphics/o02-transformed.mov")
    voice = Media("/audio/10-48-11-declick.wav", fps=0)
    t0 = 90000
    tracks = {
        ("video", 1): [],
        ("video", 2): [Item("v2a", t0, 100, 500, cam), Item("v2b", t0 + 100, 50, 640, cam)],
        ("video", 3): [Item("v3a", t0, 100, 10, loft, props={"ZoomX": 1.15, "ZoomY": 1.15}),
                       Item("v3b", t0 + 100, 50, 150, loft)],
        ("video", 5): [Item("v5a", t0 + 20, 60, 30, cut, props={"ZoomX": 0.55, "ZoomY": 0.55, "Pan": 584.0,
                                                                 "Tilt": -458.0, "CropLeft": 520.0})],
        ("video", 6): [Item("v6a", t0 + 10, 40, 0, card), Item("v6t", t0 + 46, 8, 0, type="transition",
                                                               name="Cross Dissolve"),
                       Item("v6b", t0 + 50, 40, 0, card)],
        ("audio", 1): [Item("a1a", t0, 150, 500, voice, type="audio",
                            props={"AudioVolume": 0.0, "AudioPan": 0.0}, speed={"Percentage": 100.0,
                                                                                "PitchCorrection": True}, nodes=None)],
    }
    tracks[("video", 2)][0].markers = {12: {"color": "Red", "name": "check lip sync", "note": "", "duration": 1,
                                            "customData": ""}}
    markers = {0: {"color": "Blue", "name": "10-48-11 · C0030", "note": "", "duration": 1, "customData": ""},
               136: {"color": "Red", "name": "marker 136", "note": "sync?", "duration": 1, "customData": ""}}
    return Timeline(tracks, markers, start=t0)
