"""Tests for the extraction-benchmark scorer (scripts/bench_extraction.py).

The scorer compares extracted rows against hand-checked golden records and
reports per-field true/false positives and false negatives. It must be
normalization-aware (phone formats, email case) and penalize spurious rows.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "bench_extraction",
    Path(__file__).resolve().parent.parent / "scripts" / "bench_extraction.py",
)
bench = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bench)


FIELDS = ["full_name", "email", "phone", "agency", "city"]

GOLDEN = [
    {"full_name": "Maria Gonzalez", "email": "maria.gonzalez@greenfieldrealty.com",
     "phone": "973-555-0101", "agency": "Greenfield Realty", "city": "Newark"},
    {"full_name": "David Chen", "email": "david.chen@greenfieldrealty.com",
     "phone": "973-555-0102", "agency": "Greenfield Realty", "city": "Jersey City"},
]


def test_perfect_extraction_scores_full_marks():
    extracted = [dict(r) for r in GOLDEN]
    s = bench.score_case(GOLDEN, extracted, FIELDS)
    assert s["record_recall"] == 1.0
    assert s["spurious"] == 0
    for f in FIELDS:
        pf = s["per_field"][f]
        assert pf["fp"] == 0 and pf["fn"] == 0 and pf["tp"] == 2


def test_normalization_aware_matching():
    # Different phone formatting and email casing must still count as correct.
    extracted = [
        {"full_name": "Maria Gonzalez", "email": "Maria.Gonzalez@GreenfieldRealty.com",
         "phone": "(973) 555-0101", "agency": "Greenfield Realty", "city": "Newark"},
    ]
    s = bench.score_case(GOLDEN[:1], extracted, FIELDS)
    assert s["per_field"]["phone"]["tp"] == 1
    assert s["per_field"]["email"]["tp"] == 1


def test_missing_and_wrong_values():
    extracted = [
        # email missing -> fn; phone wrong -> fp+fn; rest correct.
        {"full_name": "Maria Gonzalez", "email": None,
         "phone": "999-999-9999", "agency": "Greenfield Realty", "city": "Newark"},
    ]
    s = bench.score_case(GOLDEN[:1], extracted, FIELDS)
    assert s["per_field"]["email"] == {"tp": 0, "fp": 0, "fn": 1}
    assert s["per_field"]["phone"] == {"tp": 0, "fp": 1, "fn": 1}
    assert s["per_field"]["full_name"]["tp"] == 1


def test_unmatched_golden_counts_as_missed_record():
    extracted = [dict(GOLDEN[0])]
    s = bench.score_case(GOLDEN, extracted, FIELDS)
    assert s["record_recall"] == 0.5
    # David Chen's non-empty fields are all false negatives.
    assert s["per_field"]["email"]["fn"] == 1


def test_spurious_extracted_row_penalizes_precision():
    extracted = [dict(GOLDEN[0]),
                 {"full_name": "Invented Person", "email": "fake@nowhere.com",
                  "phone": None, "agency": None, "city": None}]
    s = bench.score_case(GOLDEN[:1], extracted, FIELDS)
    assert s["spurious"] == 1
    assert s["per_field"]["email"]["fp"] == 1  # the invented email
    assert s["per_field"]["full_name"]["fp"] == 1


def test_rows_matched_by_name_not_order():
    extracted = [dict(GOLDEN[1]), dict(GOLDEN[0])]  # reversed order
    s = bench.score_case(GOLDEN, extracted, FIELDS)
    assert s["record_recall"] == 1.0
    assert s["per_field"]["city"]["tp"] == 2


def test_precision_recall_summary():
    extracted = [dict(GOLDEN[0])]
    s = bench.score_case(GOLDEN, extracted, FIELDS)
    summary = bench.summarize([s])
    assert 0 < summary["precision"] <= 1.0
    assert 0 < summary["recall"] < 1.0
