"""Seed the UI database with disposable demo data for visual verification.

Creates two finished runs (one with records + an older export so the
"exported" flag renders, one empty with a diagnosis) so every UI state is
visible. Run from the project root:  python scripts/seed_ui_demo.py
"""

import os
import time

from src import config
from src.crawler.store import FreeFormStore
from src.exporters import dynamic_exporter

DB = config.DATA_DIR / "processed" / "ui_freeform.sqlite"
EXPORTS = config.DATA_DIR / "exports"

COLS = [["Full Name", "full_name"], ["Email", "email"], ["Phone", "phone"],
        ["Agency", "agency"], ["City", "city"], ["Source URL", "source_url"]]

ROWS = [
    ("Maria DePasquale", "maria.dep@compass.com", "609-555-0143", "Compass", "Princeton"),
    ("James Okafor", "j.okafor@kw.com", "973-555-0188", "Keller Williams", "Newark"),
    ("Priya Natarajan", "priya.n@weichert.com", None, "Weichert", "Edison"),
    ("Daniel Reyes", None, "201-555-0117", "RE/MAX Select", "Jersey City"),
    ("Hannah Liu", "hliu@compass.com", "609-555-0102", "Compass", "Princeton"),
    ("Robert Ash", "rash@c21.com", "732-555-0156", "Century 21", "Toms River"),
    ("Sofia Marino", "sofia@marinorealty.com", "856-555-0124", "Marino Realty", "Cherry Hill"),
    ("Tom Becker", None, None, "Coldwell Banker", "Montclair"),
]


def iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def main() -> None:
    config.ensure_dirs()
    now = time.time()
    s = FreeFormStore(DB)

    # Run 1 (older, empty, with a diagnosis) — shows the zero-rows treatment.
    r1 = s.start_run("https://blocked-site.example", "agents in PA: name, email", COLS, iso(now - 7200))
    s.finish_run(r1, iso(now - 7100), {
        "records": 0, "data_pages": 0, "pages_fetched": 6, "blocked": 4, "errors": 2,
        "llm_calls": 3, "diagnosis": "4 page(s) answered with an anti-bot/CAPTCHA block "
        "page, which this tool does not bypass; 2 request(s) timed out.",
        "visited": [
            {"url": "https://blocked-site.example/", "status": "blocked",
             "note": "blocked page detected: 'access denied'"},
            {"url": "https://blocked-site.example/agents", "status": "error",
             "note": "timeout after 30s"},
        ]})

    # An export written BEFORE run 2 starts -> rows in it get the "exported" flag.
    export_path = EXPORTS / "real_estate_agent_demo_earlier.xlsx"
    dynamic_exporter.write_xlsx(
        [tuple(c) for c in COLS],
        [{"full_name": ROWS[0][0], "email": ROWS[0][1], "phone": ROWS[0][2],
          "agency": ROWS[0][3], "city": ROWS[0][4], "source_url": "https://njrealty.example/agents/maria"},
         {"full_name": ROWS[5][0], "email": ROWS[5][1], "phone": ROWS[5][2],
          "agency": ROWS[5][3], "city": ROWS[5][4], "source_url": "https://njrealty.example/agents/robert"}],
        export_path)
    old = now - 3600
    os.utime(export_path, (old, old))

    # Run 2 (latest, with records) — the main table.
    r2 = s.start_run("https://njrealty.example",
                     "Real-estate agents in New Jersey: full name, email, phone, agency, city",
                     COLS, iso(now - 900))
    for i, (name, email, phone, agency, city) in enumerate(ROWS):
        slug = name.lower().split()[0]
        s.add_record(r2, (email or name).lower(), f"https://njrealty.example/agents/{slug}",
                     {"full_name": name, "email": email, "phone": phone,
                      "agency": agency, "city": city,
                      "source_url": f"https://njrealty.example/agents/{slug}"},
                     iso(now - 800))
    s.finish_run(r2, iso(now - 600), {
        "records": len(ROWS), "data_pages": 9, "extracted_pages": 9,
        "pages_fetched": 22, "blocked": 0, "errors": 1, "llm_calls": 31,
        "target_records": 50, "target_met": False, "model": "gemma4:e4b",
        "engine": "scrapling", "diagnosis": "Found 9 data page(s).",
        "field_coverage": {"found": ["full name", "email", "phone", "agency", "city"], "missing": []},
        "visited": [
            {"url": "https://njrealty.example/", "status": "ok", "note": "HUB -> /agents"},
            {"url": "https://njrealty.example/agents/maria", "status": "data_page",
             "note": "DATA PAGE (~1): individual agent profile"},
            {"url": "https://njrealty.example/agents/james", "status": "data_page",
             "note": "DATA PAGE (~1): individual agent profile"},
            {"url": "https://njrealty.example/careers", "status": "ok", "note": "not a listing"},
        ]})
    s.close()
    print(f"Seeded {DB} (runs #{r1}, #{r2}) and {export_path.name}")


if __name__ == "__main__":
    main()
