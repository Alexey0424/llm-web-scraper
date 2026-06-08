"""Offline tests for the web app: batch dispatch, combined export, deletions.

The real crawl pipeline (Ollama) is never invoked — runner.run_crawl is stubbed.
"""

import time

import openpyxl
import pytest

from src.web import app as app_mod
from src.crawler.store import FreeFormStore, now_iso


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "UI_FF_DB", tmp_path / "ui.sqlite")
    monkeypatch.setattr(app_mod, "EXPORTS_DIR", tmp_path / "exports")
    (tmp_path / "exports").mkdir()
    flask_app = app_mod.create_app()
    flask_app.config.update(TESTING=True)
    return flask_app.test_client()


def _seed_run(tmp_path, batch_id=None, prompt="agents: name", records=(("Ann",),)):
    s = FreeFormStore(tmp_path / "ui.sqlite")
    cols = [("Name", "name"), ("Source URL", "source_url")]
    rid = s.start_run("https://a.com", prompt, cols, now_iso(), batch_id=batch_id)
    for i, (name,) in enumerate(records):
        s.add_record(rid, f"k{i}", "https://a.com/p", {"name": name}, now_iso())
    s.close()
    return rid


# --- combined export builder ------------------------------------------------

def test_build_batch_export_stack(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "EXPORTS_DIR", tmp_path / "exports")
    s = FreeFormStore(tmp_path / "ff.sqlite")
    cols = [("Name", "name"), ("Source URL", "source_url")]
    for seed in ("https://a.com", "https://b.com"):
        rid = s.start_run(seed, "p", cols, now_iso(), batch_id="b1")
        s.add_record(rid, f"{seed}", "u", {"name": "X" + seed}, now_iso())
    path, removed = app_mod._build_batch_export(s, "b1", "stack")
    s.close()
    ws = openpyxl.load_workbook(path).active
    assert [c.value for c in ws[1]][0] == "Site"
    assert ws.max_row == 3 and removed == 0  # header + 2 distinct rows


def test_build_batch_export_stack_dedups_cross_site(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "EXPORTS_DIR", tmp_path / "exports")
    s = FreeFormStore(tmp_path / "ff.sqlite")
    cols = [("Name", "name"), ("Email", "email"), ("Source URL", "source_url")]
    r1 = s.start_run("https://a.com", "p", cols, now_iso(), batch_id="bd")
    s.add_record(r1, "ann", "u", {"name": "Ann", "email": "ann@x.com"}, now_iso())
    r2 = s.start_run("https://b.com", "p", cols, now_iso(), batch_id="bd")
    s.add_record(r2, "ann2", "u", {"name": "Ann A", "email": "ANN@x.com"}, now_iso())  # same email
    path, removed = app_mod._build_batch_export(s, "bd", "stack")
    s.close()
    ws = openpyxl.load_workbook(path).active
    assert removed == 1 and ws.max_row == 2  # header + 1 surviving row


def test_build_batch_export_sheets(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "EXPORTS_DIR", tmp_path / "exports")
    s = FreeFormStore(tmp_path / "ff.sqlite")
    r1 = s.start_run("https://a.com", "p1", [("Name", "name")], now_iso(), batch_id="b2")
    s.add_record(r1, "k", "u", {"name": "A"}, now_iso())
    r2 = s.start_run("https://b.com", "p2", [("Price", "price")], now_iso(), batch_id="b2")
    s.add_record(r2, "k", "u", {"price": "9"}, now_iso())
    path, removed = app_mod._build_batch_export(s, "b2", "sheets")
    s.close()
    wb = openpyxl.load_workbook(path)
    assert len(wb.sheetnames) == 2 and removed == 0


# --- batch dispatch ---------------------------------------------------------

def test_crawl_batch_rejects_empty(client):
    r = client.post("/crawl-batch", data={"mode": "bulk", "urls": "", "prompt": ""})
    assert r.status_code == 400
    assert not r.get_json()["ok"]


def test_crawl_batch_runs_serially(client, tmp_path, monkeypatch):
    calls = []

    def fake_run_crawl(store, url, prompt, states, *a, batch_id=None, **k):
        calls.append((url, batch_id))
        rid = store.start_run(url, prompt, [("Name", "name")], now_iso(), batch_id=batch_id)
        store.add_record(rid, url, url, {"name": "x"}, now_iso())
        return {"records": 1, "run_id": rid}

    monkeypatch.setattr(app_mod.runner, "run_crawl", fake_run_crawl)
    r = client.post("/crawl-batch", data={
        "mode": "bulk", "urls": "https://a.com\nhttps://b.com", "prompt": "p"})
    assert r.status_code == 200
    assert r.get_json()["jobs"] == 2

    for _ in range(100):
        st = client.get("/crawl-status").get_json()
        if st["state"] in ("done", "error"):
            break
        time.sleep(0.02)
    assert st["state"] == "done"
    assert len(calls) == 2
    assert st["batch"]["total"] == 2
    assert st["batch"]["export"]  # a combined file was produced


# --- deletions --------------------------------------------------------------

def test_delete_run(client, tmp_path):
    rid = _seed_run(tmp_path)
    r = client.post(f"/delete-run/{rid}")
    assert r.status_code == 200 and r.get_json()["ok"]
    assert client.post(f"/delete-run/{rid}").status_code == 404  # gone now


def test_crawl_stop_when_idle_returns_409(client):
    r = client.post("/crawl-stop")
    assert r.status_code == 409 and not r.get_json()["ok"]


def test_run_trace_returns_info_and_steps(client, tmp_path):
    s = FreeFormStore(tmp_path / "ui.sqlite")
    cols = [("Name", "name"), ("Source URL", "source_url")]
    rid = s.start_run("https://a.com", "agents: name", cols, now_iso())
    s.finish_run(rid, now_iso(), {
        "pages_fetched": 3, "records": 1, "ejected": True, "llm_calls": 5,
        "visited": [{"url": "https://a.com", "status": "ok", "note": "data page"},
                    {"url": "https://a.com/x", "status": "blocked", "note": "robots"}],
    })
    s.add_record(rid, "k", "https://a.com", {"name": "Ann"}, now_iso())
    s.close()
    j = client.get(f"/run/{rid}/trace").get_json()
    assert j["ok"]
    assert j["info"]["pages_fetched"] == 3 and j["info"]["ejected"] is True
    assert j["rows"] == 1
    assert len(j["steps"]) == 2 and j["steps"][1]["status"] == "blocked"
    assert client.get("/run/999999/trace").status_code == 404


def test_delete_export_rejects_traversal(client):
    for bad in ["../secret.txt", "a/b.xlsx", "..\\x"]:
        r = client.post(f"/delete-export/{bad}")
        assert r.status_code in (400, 404)


def test_delete_and_clear_exports(client, tmp_path):
    exp = tmp_path / "exports"
    (exp / "one.xlsx").write_text("x")
    (exp / "two.xlsx").write_text("y")
    assert client.post("/delete-export/one.xlsx").get_json()["ok"]
    assert not (exp / "one.xlsx").exists()
    assert client.post("/clear-exports").get_json()["ok"]
    assert not (exp / "two.xlsx").exists()
