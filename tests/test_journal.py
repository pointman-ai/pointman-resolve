import json

from pointman_resolve.timeline import journal


def test_record_and_window(tmp_path, monkeypatch):
    monkeypatch.setenv("POINTMAN_RESOLVE_HOME", str(tmp_path))
    a = journal.record("one", added=["x"], timeline="tl-1")
    b = journal.record("two", removed=["y"], timeline="tl-1")
    journal.record("elsewhere", timeline="tl-2")
    assert [e["tool"] for e in journal.entries(timeline="tl-1")] == ["one", "two"]
    assert [e["tool"] for e in journal.entries(after=a["time"], timeline="tl-1")] == ["two"]
    assert [e["tool"] for e in journal.entries(until=a["time"])] == ["one"]
    assert b["removed"] == ["y"]


def test_a_cut_short_line_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("POINTMAN_RESOLVE_HOME", str(tmp_path))
    journal.record("ok")
    with journal.path().open("a") as f:
        f.write('{"time": "2026-10-19T01:00')  # a crash mid-write
    assert [e["tool"] for e in journal.entries()] == ["ok"]
    assert json.loads(journal.path().read_text().splitlines()[0])["tool"] == "ok"
