# -*- coding: utf-8 -*-
"""Offline deterministic tests, no future bars and no vendor requests."""
import csv
import json

import exchange_calendars as xcals
import pandas as pd
import pytest

import risk_manager as risk

CAL = xcals.get_calendar("XHKG")
ASOF = "2026-10-13"
DAYS = [x.date().isoformat() for x in
        CAL.sessions_in_range(pd.Timestamp("2026-08-01"), pd.Timestamp(ASOF))][-21:]
assert len(DAYS) == 21


def fixture(kind="two_b", high=9.45, resistance=10.4):
    data = {d: {"open": 10., "high": max(10.4,resistance), "low": 9.6, "close": 10.}
            for d in DAYS[:-1]}
    data[DAYS[7]] = {"open": 9.9, "high": 10.4, "low": 9., "close": 9.9}
    data[DAYS[-2]] = {"open": 9.4, "high": 10.4, "low": 9.2, "close": 9.25}
    if kind == "two_b":
        data[ASOF] = {"open": 9.1, "high": high, "low": 8.9, "close": 9.3}
    else:
        data[ASOF] = {"open": 9.15, "high": high, "low": 9.1, "close": 9.3}
    signal = {
        "rank": 1, "bucket": "2025与2026双重低位", "code": "01810", "name": "测试股票",
        "close": 9.3, "analysis_status": "analyzed", "reference_low": 9.,
        "reference_low_date": DAYS[7], "target_ohlc": data[ASOF],
        "signals": {"two_b": kind=="two_b", "retest": kind=="retest",
                    "breakdown": False, "strength": False},
    }
    return signal, data


def test_two_b_loss_stop_and_resistance():
    signal, data = fixture()
    r = risk.assess(signal,data,ASOF,CAL)
    assert r["signal_type"] == "2B假跌破"
    assert r["entry_reference"] == round(9.45*1.002, 5)
    assert r["structure_invalidation"] == round(8.9*.995, 5)
    assert r["risk_within_8pct"]
    assert r["historical_resistance"] == 10.4
    assert r["reward_risk_to_resistance"] > 0
    assert r["hypothetical_2r_target"] > r["entry_reference"]


def test_retest_stop_below_previous_low():
    signal, data = fixture("retest")
    r = risk.assess(signal,data,ASOF,CAL)
    assert r["signal_type"] == "二次回踩"
    assert r["structure_invalidation"] == round(9*.995, 5)
    assert r["risk_within_8pct"]


def test_over_8pct_is_not_acceptable():
    signal, data = fixture("two_b",high=12)
    r = risk.assess(signal,data,ASOF,CAL)
    assert not r["risk_within_8pct"]
    assert r["risk_status"] == "over_8pct"
    assert r["loss_pct_to_stop"] > 8


def test_high_historical_reward_meets_2r_research_threshold():
    signal, data = fixture("two_b",resistance=12)
    r = risk.assess(signal,data,ASOF,CAL)
    assert r["risk_status"] == "scenario_pass"
    assert r["reward_risk_to_resistance"] >= 2


def test_unavailable_overhead_resistance_does_not_invent_reward():
    signal, data = fixture("two_b", high=11)
    r = risk.assess(signal,data,ASOF,CAL)
    assert r["historical_resistance"] == 10.4
    assert r["reward_pct_to_resistance"] is None
    assert r["reward_risk_to_resistance"] is None
    assert r["risk_status"] in ("no_upside_resistance","over_8pct")


def test_missing_bars_or_mismatched_ohlc_blocks():
    signal,data = fixture()
    del data[DAYS[-4]]
    r = risk.assess(signal,data,ASOF,CAL)
    assert r["risk_status"] == "data_not_verified"
    assert r["entry_reference"] is None
    signal, data = fixture()
    signal["target_ohlc"] = {**signal["target_ohlc"], "low": 8.1}
    assert risk.assess(signal,data,ASOF,CAL)["risk_status"] == "data_not_verified"


def test_broken_reference_low_blocks():
    signal, data = fixture()
    signal["reference_low"] = 8.5
    assert risk.assess(signal,data,ASOF,CAL)["risk_status"] == "data_not_verified"


def test_stale_signal_cannot_generate_trade_plan():
    signal,data = fixture()
    signal["analysis_status"] = "insufficient_history"
    assert risk.assess(signal,data,ASOF,CAL)["entry_reference"] is None


def test_later_bars_have_no_impact():
    signal,data = fixture()
    first = risk.assess(signal,data,ASOF,CAL)
    data["2026-10-14"] = {"open":1,"high":1,"low":.5,"close":.7}
    second = risk.assess(signal,data,ASOF,CAL)
    for k in ("entry_reference","structure_invalidation","loss_pct_to_stop",
              "historical_resistance","reward_risk_to_resistance"):
        assert first[k] == second[k]


def test_run_exports_only_two_signals_and_checks_join(tmp_path):
    s1,b1 = fixture("two_b")
    s2,b2 = fixture("retest")
    s2.update(rank=2,code="03896")
    s3 = {**s2,"rank":3,"code":"00780","signals":{"two_b":False,"retest":False}}
    latest = {"meta":{"target_session_hkt":ASOF,"fresh_count":143,"total":143},
              "ranked_candidates":[
                  {"code":"01810","rank":1},{"code":"03896","rank":2},{"code":"00780","rank":3}]}
    signals = {"meta":{"target_session":ASOF,"candidate_count":3},"candidates":[s1,s2,s3]}
    by_code = {"01810":b1,"03896":b2}
    rr = risk.run(tmp_path,latest=latest,signal_report=signals,calendar=CAL,
                  read_bars_fn=lambda root,code,asof:by_code[code])
    assert rr["meta"]["signal_candidates"] == 2
    assert len(rr["assessments"]) == 2
    assert [x["code"] for x in rr["assessments"]] == ["01810","03896"]
    for p in ("risk_manager.json","risk_manager.md","风险收益评估.csv"):
        assert (tmp_path/"data"/p).exists()
    assert (tmp_path/"data"/"risk"/f"{ASOF}.md").exists()
    with (tmp_path/"data"/"风险收益评估.csv").open(encoding="utf-8-sig") as f:
        assert len(list(csv.DictReader(f))) == 2
    signals["meta"]["target_session"] = "2026-10-12"
    with pytest.raises(ValueError,match="目标交易日不一致"):
        risk.run(tmp_path,latest=latest,signal_report=signals,calendar=CAL,
                 read_bars_fn=lambda root,code,asof:by_code[code])


def test_invalid_threshold_parameters_and_rank_mismatch(tmp_path):
    s1,b1 = fixture()
    latest = {"meta":{"target_session_hkt":ASOF,"fresh_count":143},
              "ranked_candidates":[{"code":"01810","rank":1}]}
    sr = {"meta":{"target_session":ASOF,"candidate_count":1},"candidates":[s1]}
    with pytest.raises(ValueError,match="不合法"):
        risk.run(tmp_path,latest=latest,signal_report=sr,calendar=CAL,
                 read_bars_fn=lambda *_:b1,max_risk=2)
    sr["candidates"][0]["rank"] = 2
    with pytest.raises(ValueError,match="排名不一致"):
        risk.run(tmp_path,latest=latest,signal_report=sr,calendar=CAL,
                 read_bars_fn=lambda *_:b1)
