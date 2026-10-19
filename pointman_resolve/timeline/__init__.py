"""The timeline as data: read-only snapshots of the open timeline, diffs between them, and a
round-trip check against Resolve's own OTIO export (Pointman for Editors, PFE-10 and PFE-14).

`readonly.ReadOnly` wraps every Resolve object these modules touch, so they can only read."""

SCHEMA = 1
