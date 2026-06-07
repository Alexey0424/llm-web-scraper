"""SQLite store for free-form crawl results.

Kept separate from the fixed-schema AgentRecord store: free-form records have
arbitrary, per-run columns, so each row's values are stored as a JSON blob and
each run records its own column set. The UI reads the latest run to render a
dynamic table and offer the dynamic export.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class FreeFormStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ff_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                seed TEXT, prompt TEXT, columns_json TEXT,
                started TEXT, finished TEXT, stats_json TEXT
            );
            CREATE TABLE IF NOT EXISTS ff_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER, dedup TEXT, source_url TEXT,
                data_json TEXT, extracted_at TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_ff_records_run ON ff_records(run_id);
            """
        )
        # Migration: link runs that belong to the same bulk/queue batch. Older
        # databases predate this column, so add it if missing.
        cols = {c[1] for c in self.conn.execute("PRAGMA table_info(ff_runs)").fetchall()}
        if "batch_id" not in cols:
            self.conn.execute("ALTER TABLE ff_runs ADD COLUMN batch_id TEXT")
        self.conn.commit()

    # -- runs ---------------------------------------------------------------
    def start_run(self, seed: str, prompt: str, columns: list[tuple[str, str]],
                  started: str, batch_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO ff_runs (seed, prompt, columns_json, started, batch_id) "
            "VALUES (?,?,?,?,?)",
            (seed, prompt, json.dumps(columns), started, batch_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(self, run_id: int, finished: str, stats: dict) -> None:
        self.conn.execute(
            "UPDATE ff_runs SET finished=?, stats_json=? WHERE id=?",
            (finished, json.dumps(stats), run_id),
        )
        self.conn.commit()

    def add_record(self, run_id: int, dedup: str, source_url: str, data: dict,
                   extracted_at: str) -> bool:
        """Insert a record unless its dedup key already exists for the run."""
        if dedup:
            row = self.conn.execute(
                "SELECT 1 FROM ff_records WHERE run_id=? AND dedup=? LIMIT 1",
                (run_id, dedup),
            ).fetchone()
            if row:
                return False
        self.conn.execute(
            "INSERT INTO ff_records (run_id, dedup, source_url, data_json, extracted_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, dedup, source_url, json.dumps(data), extracted_at),
        )
        self.conn.commit()
        return True

    # -- reads --------------------------------------------------------------
    def latest_run(self) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM ff_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_run(self, run_id: int) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM ff_runs WHERE id=?", (run_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_runs(self, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM ff_runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def records_for_run(self, run_id: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT data_json, source_url FROM ff_records WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = json.loads(r["data_json"])
            d.setdefault("source_url", r["source_url"])
            out.append(d)
        return out

    def count_for_run(self, run_id: int) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) c FROM ff_records WHERE run_id=?", (run_id,)
        ).fetchone()["c"])

    def runs_for_batch(self, batch_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM ff_runs WHERE batch_id=? ORDER BY id", (batch_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- deletes ------------------------------------------------------------
    def delete_run(self, run_id: int) -> None:
        """Remove a single run and all of its records."""
        self.conn.execute("DELETE FROM ff_records WHERE run_id=?", (run_id,))
        self.conn.execute("DELETE FROM ff_runs WHERE id=?", (run_id,))
        self.conn.commit()

    def reset(self) -> None:
        self.conn.executescript("DELETE FROM ff_records; DELETE FROM ff_runs;")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
