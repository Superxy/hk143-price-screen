# -*- coding: utf-8 -*-
"""Deterministic OHLC exit simulations, no network."""
import exit_backtest as m

def trade():
    return {"code":"01810","name":"TEST","kind":"2B","signal_day":"2026-09-01",
            "entry_day":"2026-09-02","entry":10.,"stop":9.5,"resistance":11.,
            "future":[f"2026-09-{i:02d}" for i in range(2,22)]}
def bars():
    x={d:{"open":10.,"high":10.4,"low":9.8,"close":10.1} for d in trade()["future"]}
    x["2026-09-02"]={"open":9.9,"high":10.5,"low":9.8,"close":10.1}
    return x

def test_2r_3r_resistance_targets():
    t,b=trade(),bars()
    assert m.simulate(t,b,"2R",.0015)["target"]==11.
    assert m.simulate(t,b,"3R",.0015)["target"]==11.5
    assert m.simulate(t,b,"PREVIOUS_RESISTANCE",.0015)["target"]==11.

def test_2r_wins_before_3r():
    t,b=trade(),bars();b["2026-09-04"]["high"]=11.2
    assert m.simulate(t,b,"2R",.0015)["reason"]=="target"
    assert m.simulate(t,b,"3R",.0015)["reason"]=="time_exit_20d"

def test_ambiguous_ohlc_stop_first():
    t,b=trade(),bars()
    b["2026-09-03"]={"open":10.,"high":12.,"low":9.,"close":10.}
    r=m.simulate(t,b,"2R",.0015)
    assert r["reason"]=="ambiguous_stop_first"
    assert r["net_pct"]<0

def test_entry_day_ambiguous_stop_first():
    t,b=trade(),bars()
    b["2026-09-02"]={"open":9.9,"high":12.,"low":9.,"close":10.}
    assert m.simulate(t,b,"2R",.0015)["reason"]=="ambiguous_stop_first"

def test_stop_gap_slippage():
    t,b=trade(),bars()
    b["2026-09-03"]={"open":8.,"high":8.5,"low":7.5,"close":8.}
    r=m.simulate(t,b,"2R",.0015)
    assert r["exit"]==8. and r["net_pct"] < -20

def test_no_valid_resistance_does_not_invent_target():
    t,b=trade(),bars()
    t["resistance"]=None
    assert m.simulate(t,b,"PREVIOUS_RESISTANCE",.0015) is None
    assert m.simulate(t,b,"2R",.0015) is not None

def test_fees_reduce_return():
    t,b=trade(),bars()
    assert m.simulate(t,b,"2R",.0015)["net_pct"] < m.simulate(t,b,"2R",0)["net_pct"]

def test_empty_samples_are_missing_not_zero_win():
    d=m.calc([])
    assert d["n"]==0 and d["win_rate_pct"] is None

def test_target_hit_and_win_summary():
    t,b=trade(),bars();b["2026-09-04"]["high"]=11.25
    r=m.calc([m.simulate(t,b,"2R",.0015)])
    assert r["n"]==1 and r["target_hit_pct"]==100.
    assert r["mean_net_pct"]>0 and r["stocks"]==1
