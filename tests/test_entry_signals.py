# -*- coding: utf-8 -*-
"""Offline deterministic tests; no vendor network, no future prices."""
import csv
import json
from datetime import date

import exchange_calendars as xcals
import pandas as pd

import entry_signals as s

CAL = xcals.get_calendar("XHKG")
ASOF = "2026-10-13"
DAYS = [x.date().isoformat() for x in
        CAL.sessions_in_range(pd.Timestamp("2026-08-01"), pd.Timestamp(ASOF))][-21:]
assert len(DAYS) == 21 and DAYS[-1] == ASOF


def candidate(code="01810", rank=1, prev=True, cur=True):
    return {
        "code": code, "name": "测试股票", "rank": rank,
        "bucket": "2025与2026双重低位" if prev and cur else (
            "仅2026年低位" if cur else "仅2025年低位"),
        "latest_date": ASOF, "data_status": "fresh", "close": 9.3,
        "previous_year_low": prev, "current_year_low": cur,
    }


def bars(kind):
    result = {d: {"open": 10.0, "high": 10.4, "low": 9.6, "close": 10.0}
              for d in DAYS[:-1]}
    old = DAYS[7]  # minimum is 13 HK sessions earlier
    result[old] = {"open": 9.9, "high": 10.4, "low": 9.0, "close": 9.9}
    result[DAYS[-2]] = {"open": 9.4, "high": 10.4, "low": 9.2, "close": 9.25}
    samples = {
        "two_b": {"open": 9.1, "high": 9.45, "low": 8.9, "close": 9.3},
        "retest": {"open": 9.15, "high": 9.4, "low": 9.1, "close": 9.3},
        "breakdown": {"open": 9.05, "high": 9.2, "low": 8.8, "close": 8.95},
        "strength": {"open": 10.1, "high": 10.9, "low": 9.2, "close": 10.8},
        "none": {"open": 9.8, "high": 10.0, "low": 9.6, "close": 9.9},
    }
    result[ASOF] = samples[kind]
    return result


def test_two_b_recovers_prior_low():
    x = s.analyze_candidate(candidate(), bars("two_b"), ASOF, CAL)
    assert x["analysis_status"] == "analyzed"
    assert x["signals"] == dict(two_b=True, retest=False, breakdown=False, strength=False)
    assert x["reference_low"] == 9.0
    assert x["reference_low_date"] == DAYS[7]


def test_higher_retest_holds_prior_low():
    x = s.analyze_candidate(candidate(), bars("retest"), ASOF, CAL)
    assert x["signals"]["retest"]
    assert not x["signals"]["two_b"]
    assert not x["signals"]["breakdown"]


def test_unrecovered_new_low_flagged_breakdown():
    x = s.analyze_candidate(candidate(), bars("breakdown"), ASOF, CAL)
    assert x["signals"]["breakdown"]
    assert not x["signals"]["two_b"]


def test_local_price_strength_near_low():
    x = s.analyze_candidate(candidate(), bars("strength"), ASOF, CAL)
    assert x["signals"]["strength"]
    assert not x["signals"]["retest"]


def test_no_structure_is_still_analyzed():
    x = s.analyze_candidate(candidate(), bars("none"), ASOF, CAL)
    assert x["analysis_status"] == "analyzed"
    assert not any(x["signals"].values())


def test_broken_past_sessions_never_infer_structure():
    rows = bars("two_b")
    del rows[DAYS[-4]]
    x = s.analyze_candidate(candidate(), rows, ASOF, CAL)
    assert x["analysis_status"] == "insufficient_history"
    assert not any(x["signals"].values())


def test_reference_needs_five_sessions_age():
    rows = bars("two_b")
    rows[DAYS[7]]["low"] = 9.6
    rows[DAYS[-2]]["low"] = 9.0  # only one session old
    x = s.analyze_candidate(candidate(), rows, ASOF, CAL)
    assert x["analysis_status"] == "analyzed"
    assert not x["signals"]["two_b"]


def test_stale_candidates_have_no_signal():
    cand = candidate()
    cand["latest_date"] = DAYS[-2]
    cand["data_status"] = "stale"
    x = s.analyze_candidate(cand, bars("two_b"), ASOF, CAL)
    assert x["analysis_status"] == "not_fresh"
    assert not any(x["signals"].values())


def write_bars(root, code, rows, *, future=False):
    folder = root / "data" / "2026"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / f"{code}_2026.csv").open("w", newline="", encoding="utf-8-sig") as h:
        w = csv.DictWriter(h, fieldnames=["date", "open", "high", "low", "close"])
        w.writeheader()
        for d, data in rows.items():
            w.writerow({"date": d, **data})
        if future:
            w.writerow({"date": "2026-10-14", **{"open": 1, "high": 1, "low": 1, "close": 1}})


def test_no_future_prices_read_or_used(tmp_path):
    write_bars(tmp_path, "01810", bars("two_b"), future=True)
    rows = s.read_bars(tmp_path, "01810", ASOF)
    assert max(rows) == ASOF
    x = s.analyze_candidate(candidate(), rows, ASOF, CAL)
    assert x["signals"]["two_b"]


def test_pipeline_reads_only_ranked_candidates_and_writes_every_row(tmp_path):
    cands = [candidate("01810", rank=1, prev=True, cur=True),
             candidate("03896", rank=2, prev=False, cur=True),
             candidate("00780", rank=3, prev=True, cur=False)]
    write_bars(tmp_path, "01810", bars("two_b"))
    write_bars(tmp_path, "03896", bars("breakdown"))
    write_bars(tmp_path, "00780", bars("retest"))
    report = {
        "meta": {"target_session_hkt": ASOF, "fresh_count": 143, "status": "complete_date_coverage",
                 "generated_at_hkt": "2026-10-14 06:17:00 +0800"},
        "ranked_candidates": cands,
    }
    out = s.run(tmp_path, report=report, calendar=CAL)
    assert out["meta"]["candidate_count"] == 3
    assert out["meta"]["analyzed_count"] == 3
    assert out["meta"]["signal_counts"]["two_b"] == 1
    assert out["meta"]["signal_counts"]["retest"] == 1
    assert out["meta"]["signal_counts"]["breakdown"] == 1
    assert [x["rank"] for x in out["candidates"]] == [1, 2, 3]
    assert [x["bucket"] for x in out["candidates"]] == [x["bucket"] for x in cands]
    assert (tmp_path/"data"/"entry_signals.md").exists()
    assert (tmp_path/"data"/"entry_signals.json").exists()
    assert (tmp_path/"data"/"入场结构观察.csv").exists()
    assert (tmp_path/"data"/"signals"/f"{ASOF}.md").exists()
    assert "01810" in (tmp_path/"data"/"entry_signals.md").read_text(encoding="utf-8")
    actual = json.loads((tmp_path/"data"/"entry_signals.json").read_text(encoding="utf-8"))
    assert len(actual["candidates"]) == 3


def test_pipeline_empty_candidates_produces_valid_report(tmp_path):
    report = {
        "meta": {"target_session_hkt": ASOF, "fresh_count": 143, "status": "complete_date_coverage",
                 "generated_at_hkt": "2026-10-14 06:17:00 +0800"},
        "ranked_candidates": [],
    }
    out = s.run(tmp_path, report=report, calendar=CAL)
    assert out["meta"]["candidate_count"] == 0
    assert out["meta"]["signal_counts"] == {k: 0 for k in s.SIGNALS}


def test_duplicate_codes_raise_instead_of_double_counting(tmp_path):
    report = {
        "meta": {"target_session_hkt": ASOF, "fresh_count": 143, "status": "complete_date_coverage",
                 "generated_at_hkt": "2026-10-14 06:17:00 +0800"},
        "ranked_candidates": [candidate("01810", 1), candidate("01810", 2)],
    }
    import pytest
    with pytest.raises(ValueError, match="重复"):
        s.run(tmp_path, report=report, calendar=CAL)
