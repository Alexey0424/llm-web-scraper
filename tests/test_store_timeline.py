"""Timeline persistence on ff_runs: round-trip + additive migration."""

import json
import sqlite3

from src.crawler.store import FreeFormStore, now_iso


def _cols():
    return [("Name", "name"), ("Source URL", "source_url")]


def test_finish_run_persists_timeline(tmp_path):
    s = FreeFormStore(tmp_path / "ff.sqlite")
    rid = s.start_run("https://a.com", "p", _cols(), now_iso())
    payload = {"events": [{"kind": "visit", "seconds": 1.2, "url": "https://a.com",
                           "status": "ok", "note": "seed"}],
               "truncated": False, "total": 1}
    s.finish_run(rid, now_iso(), {"records": 0}, timeline=payload)
    run = s.get_run(rid)
    s.close()
    assert json.loads(run["timeline_json"]) == payload


def test_finish_run_without_timeline_is_null(tmp_path):
    s = FreeFormStore(tmp_path / "ff.sqlite")
    rid = s.start_run("https://a.com", "p", _cols(), now_iso())
    s.finish_run(rid, now_iso(), {"records": 0})  # no timeline arg
    run = s.get_run(rid)
    s.close()
    assert run["timeline_json"] is None


def test_count_runs_and_list_runs_limit(tmp_path):
    s = FreeFormStore(tmp_path / "ff.sqlite")
    for i in range(25):
        s.start_run(f"https://a.com/{i}", "p", _cols(), now_iso())
    assert s.count_runs() == 25            # total, regardless of list limit
    assert len(s.list_runs(20)) == 20      # the "page" the UI shows by default
    assert len(s.list_runs(40)) == 25      # "Load more" beyond the total caps at total
    s.close()


def test_migration_adds_timeline_column_to_old_db(tmp_path):
    db = tmp_path / "old.sqlite"
    # Pre-feature schema: ff_runs WITHOUT timeline_json (or batch_id).
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE ff_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, seed TEXT, "
        "prompt TEXT, columns_json TEXT, started TEXT, finished TEXT, stats_json TEXT);"
        "INSERT INTO ff_runs (seed, prompt) VALUES ('https://a.com', 'p');"
    )
    conn.commit()
    conn.close()
    s = FreeFormStore(db)  # opening runs the migration
    run = s.get_run(1)
    s.close()
    assert "timeline_json" in run and run["timeline_json"] is None
