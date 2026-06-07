"""Offline tests for FreeFormStore batch + deletion support."""

import sqlite3

from src.crawler.store import FreeFormStore, now_iso


def _store(tmp_path):
    return FreeFormStore(tmp_path / "ff.sqlite")


def test_batch_id_persisted_and_grouped(tmp_path):
    s = _store(tmp_path)
    cols = [("Name", "name"), ("Source URL", "source_url")]
    r1 = s.start_run("https://a.com", "p", cols, now_iso(), batch_id="batch-x")
    r2 = s.start_run("https://b.com", "p", cols, now_iso(), batch_id="batch-x")
    r3 = s.start_run("https://c.com", "p", cols, now_iso())  # not in a batch
    runs = s.runs_for_batch("batch-x")
    ids = sorted(r["id"] for r in runs)
    assert ids == sorted([r1, r2])
    assert r3 not in ids
    s.close()


def test_delete_run_removes_run_and_records(tmp_path):
    s = _store(tmp_path)
    cols = [("Name", "name"), ("Source URL", "source_url")]
    rid = s.start_run("https://a.com", "p", cols, now_iso())
    s.add_record(rid, "k1", "https://a.com/1", {"name": "Ann"}, now_iso())
    s.add_record(rid, "k2", "https://a.com/2", {"name": "Bob"}, now_iso())
    assert s.count_for_run(rid) == 2
    s.delete_run(rid)
    assert s.get_run(rid) is None
    assert s.count_for_run(rid) == 0
    s.close()


def test_migration_adds_batch_id_to_old_schema(tmp_path):
    # Simulate a pre-existing DB created without the batch_id column.
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE ff_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seed TEXT, prompt TEXT, columns_json TEXT,
            started TEXT, finished TEXT, stats_json TEXT
        );
        CREATE TABLE ff_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER, dedup TEXT, source_url TEXT,
            data_json TEXT, extracted_at TEXT
        );
        """
    )
    conn.execute("INSERT INTO ff_runs (seed, prompt) VALUES ('u','p')")
    conn.commit()
    conn.close()

    s = FreeFormStore(path)  # opening should migrate in the batch_id column
    cols = [c[1] for c in s.conn.execute("PRAGMA table_info(ff_runs)").fetchall()]
    assert "batch_id" in cols
    # old row still readable, new batched run works
    rid = s.start_run("u2", "p2", [("Name", "name")], now_iso(), batch_id="b1")
    assert [r["id"] for r in s.runs_for_batch("b1")] == [rid]
    s.close()
