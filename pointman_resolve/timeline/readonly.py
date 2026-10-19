"""Connect to DaVinci Resolve, read-only.

Everything this package does to Resolve goes through `ReadOnly`, which lets through only calls whose
name starts with Get, Is or Export (and upper-case constants such as EXPORT_OTIO). A Set, Delete,
Append, Load, Save or anything else raises before it reaches Resolve: James edits live, and a
snapshot must never change his project, his timeline or his screen.

Resolve must be open, with Preferences > System > General > External scripting using: Local.
"""

import os
import sys

API = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
LIB = "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
ALLOWED = ("Get", "Is", "Export")
PLAIN = (str, int, float, bool, bytes, dict)


class ReadOnlyError(PermissionError):
    pass


class ReadOnly:
    """A Resolve API object that only reads. Objects returned by its calls are wrapped too."""

    __slots__ = ("_obj",)

    def __init__(self, obj):
        object.__setattr__(self, "_obj", obj)

    def __getattr__(self, name):
        if name.startswith("_"):  # Python's own probes (copy, pickle...): not Resolve calls
            raise AttributeError(name)
        target = object.__getattribute__(self, "_obj")
        if name.isupper():  # a constant, e.g. resolve.EXPORT_OTIO
            return getattr(target, name)
        if not name.startswith(ALLOWED):
            raise ReadOnlyError(f"pointman-resolve is read-only: {name}() is not allowed")
        fn = getattr(target, name)
        if fn is None:  # Resolve's proxies return None for a method they don't have
            raise AttributeError(f"Resolve has no {name}()")

        def call(*args):
            return wrap(fn(*args))
        return call

    def __setattr__(self, name, value):
        raise ReadOnlyError("pointman-resolve is read-only")

    def __bool__(self):
        return bool(object.__getattribute__(self, "_obj"))


def wrap(value):
    if value is None or isinstance(value, PLAIN):
        return value
    if isinstance(value, (list, tuple)):
        return [wrap(v) for v in value]
    return ReadOnly(value)


def connect() -> ReadOnly:
    os.environ.setdefault("RESOLVE_SCRIPT_API", API)
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", LIB)
    modules = os.path.join(os.environ["RESOLVE_SCRIPT_API"], "Modules")
    if modules not in sys.path:
        sys.path.append(modules)
    import DaVinciResolveScript as dvr  # noqa: E402  (Resolve's own module, found via the path above)

    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        raise ConnectionError("Can't reach Resolve. Is it open, with Preferences > System > General > "
                              "External scripting using: Local?")
    return ReadOnly(resolve)
