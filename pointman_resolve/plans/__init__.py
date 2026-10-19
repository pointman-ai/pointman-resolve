"""Edit plans (Pointman for Editors, PFE-11): an agent's change to the timeline as data, checked before
it touches Resolve, applied by one placer, verified, and undone in one call.

    plan     the ops, on item ids from a snapshot ({"ops": [{"op": "slip", "items": [...], "by": -3}]})
    compile  ops -> steps on a model of the timeline, with the problems that stop it and a frame-map diff
             (model.py, compile.py: offline, no Resolve calls)
    apply    the steps through PlanWriter, which lets only the placer's few writes through, each
             placement checked against Resolve afterwards (apply.py)
    undo     the applied record, run backwards by the same placer

Resolve 21.1 can't move, slip, split or ripple-insert a clip, so every op that changes where a clip is
or what it shows is a delete plus an AppendToTimeline, with the clip's settings, fades, colour, grade
(CopyGrades) and transitions put back.
"""
