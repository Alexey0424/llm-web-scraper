"""Extraction-accuracy benchmark: golden records vs FreeFormExtractor output.

Runs the real free-form extractor (live Ollama model) over saved fixture pages
with hand-checked golden records, and reports per-field precision/recall plus
record-level recall. Use it before and after any pipeline change so accuracy
claims carry a number.

    python scripts/bench_extraction.py            # all cases
    python scripts/bench_extraction.py --case zillow_profile

Golden cases live in scripts/bench_golden.json. Scoring is deterministic and
normalization-aware (phone formats, email case), so formatting differences
don't count as errors but wrong/invented/missing values do.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GOLDEN_FILE = Path(__file__).resolve().parent / "bench_golden.json"


# ---------------------------------------------------------------------------
# Normalization + matching
# ---------------------------------------------------------------------------
def _norm_val(field: str, v) -> str | None:
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    if not s:
        return None
    f = field.lower()
    if any(t in f for t in ("phone", "tel", "cell", "mobile")):
        d = re.sub(r"\D", "", s)
        if len(d) == 11 and d.startswith("1"):
            d = d[1:]
        return d or s.lower()
    if "email" in f:
        return s.lower()
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip() or None


def _tokens(v) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", str(v or "").lower()))


def _row_sim(a: dict, b: dict, fields: list[str]) -> float:
    """Name-weighted token similarity used to pair extracted rows to golden."""
    name_fields = [f for f in fields if "name" in f.lower()] or fields
    ta = set().union(*(_tokens(a.get(f)) for f in name_fields))
    tb = set().union(*(_tokens(b.get(f)) for f in name_fields))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def score_case(golden: list[dict], extracted: list[dict], fields: list[str]) -> dict:
    """Compare extracted rows to golden records.

    Pairs rows greedily by name similarity, then scores each field within the
    pairs; unmatched golden rows count their non-empty fields as misses (fn),
    unmatched extracted rows are spurious and count non-empty fields as fp.
    """
    pairs: list[tuple[int, int]] = []
    used_g: set[int] = set()
    used_e: set[int] = set()
    cands = sorted(
        ((gi, ei, _row_sim(g, e, fields))
         for gi, g in enumerate(golden) for ei, e in enumerate(extracted)),
        key=lambda t: -t[2],
    )
    for gi, ei, sim in cands:
        if sim < 0.4 or gi in used_g or ei in used_e:
            continue
        used_g.add(gi)
        used_e.add(ei)
        pairs.append((gi, ei))

    per_field = {f: {"tp": 0, "fp": 0, "fn": 0} for f in fields}
    for gi, ei in pairs:
        g, e = golden[gi], extracted[ei]
        for f in fields:
            gv, ev = _norm_val(f, g.get(f)), _norm_val(f, e.get(f))
            if gv is None and ev is None:
                continue
            if gv is None:
                per_field[f]["fp"] += 1
            elif ev is None:
                per_field[f]["fn"] += 1
            elif gv == ev:
                per_field[f]["tp"] += 1
            else:
                per_field[f]["fp"] += 1
                per_field[f]["fn"] += 1
    for gi, g in enumerate(golden):
        if gi in used_g:
            continue
        for f in fields:
            if _norm_val(f, g.get(f)) is not None:
                per_field[f]["fn"] += 1
    spurious = 0
    for ei, e in enumerate(extracted):
        if ei in used_e:
            continue
        spurious += 1
        for f in fields:
            if _norm_val(f, e.get(f)) is not None:
                per_field[f]["fp"] += 1

    return {
        "per_field": per_field,
        "record_recall": (len(pairs) / len(golden)) if golden else 1.0,
        "matched": len(pairs),
        "spurious": spurious,
    }


def summarize(scores: list[dict]) -> dict:
    tp = fp = fn = 0
    for s in scores:
        for pf in s["per_field"].values():
            tp += pf["tp"]
            fp += pf["fp"]
            fn += pf["fn"]
    return {
        "precision": tp / (tp + fp) if (tp + fp) else 1.0,
        "recall": tp / (tp + fn) if (tp + fn) else 1.0,
        "tp": tp, "fp": fp, "fn": fn,
        "record_recall": (sum(s["record_recall"] for s in scores) / len(scores))
        if scores else 1.0,
        "spurious": sum(s["spurious"] for s in scores),
    }


# ---------------------------------------------------------------------------
# Runner (live model)
# ---------------------------------------------------------------------------
def run_benchmark(only_case: str | None = None) -> int:
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.crawler.freeform import FreeFormExtractor
    from src.crawler.llm import OllamaClient

    cases = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    if only_case:
        cases = [c for c in cases if c["name"] == only_case]
        if not cases:
            print(f"No case named {only_case!r}.")
            return 2

    client = OllamaClient()
    if not client.available():
        print(f"Ollama not reachable at {client.host} — benchmark needs the live model.")
        return 2
    print(f"Model: {client.model} @ {client.host}\n")

    scores = []
    for case in cases:
        html = (PROJECT_ROOT / case["fixture"]).read_text(encoding="utf-8", errors="replace")
        columns = [(h, k) for h, k in case["columns"]]
        ex = FreeFormExtractor(columns, entity=case["entity"], client=client)
        t0 = time.monotonic()
        rows = ex.extract(html, base_url=case.get("base_url", ""))
        secs = time.monotonic() - t0
        fields = [k for _, k in columns if k != "source_url"]
        s = score_case(case["golden"], rows, fields)
        scores.append(s)
        summ = summarize([s])
        print(f"[{case['name']}] {len(rows)} rows / {len(case['golden'])} golden "
              f"in {secs:.0f}s  P={summ['precision']:.2f} R={summ['recall']:.2f} "
              f"recrec={s['record_recall']:.2f} spurious={s['spurious']}")
        for f in fields:
            pf = s["per_field"][f]
            if pf["fp"] or pf["fn"]:
                print(f"    {f:<14} tp={pf['tp']} fp={pf['fp']} fn={pf['fn']}")

    total = summarize(scores)
    print(f"\nTOTAL  precision={total['precision']:.3f}  recall={total['recall']:.3f}  "
          f"record_recall={total['record_recall']:.3f}  spurious={total['spurious']}  "
          f"(tp={total['tp']} fp={total['fp']} fn={total['fn']})  "
          f"llm_calls={client.calls}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default=None, help="run only this named case")
    args = ap.parse_args()
    raise SystemExit(run_benchmark(args.case))
