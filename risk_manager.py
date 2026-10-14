# -*- coding: utf-8 -*-
"""Conditional price-only trading risk scenarios for 2B and higher-low retest.

Reads cloud_run.py's ranked low candidates and entry_signals.py's daily
observations. Does NOT forecast the next open, generate orders, or guarantee
an 8% realized loss cap. All future trigger/target prices are hypotheses.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

HKT = ZoneInfo("Asia/Hong_Kong")
CSV_FIELDS = [
    "rank", "bucket", "code", "name", "target_session", "signal_type",
    "close", "entry_reference", "structure_invalidation", "loss_pct_to_stop",
    "risk_within_8pct", "historical_resistance", "reward_pct_to_resistance",
    "reward_risk_to_resistance", "hypothetical_2r_target", "hypothetical_3r_target",
    "risk_status", "reference_low", "reference_low_date", "notes",
]


def _price(value):
    return round(float(value), 5)


def _recent_window(asof: str, bars: dict, calendar):
    """Previous twenty full HK sessions and the target day; exclude future."""
    ts = pd.Timestamp(asof)
    if not calendar.is_session(ts):
        return None
    try:
        start = calendar.session_offset(ts, -20)
        dates = [x.date().isoformat() for x in calendar.sessions_in_range(start, ts)]
    except (ValueError, OverflowError, KeyError):
        return None
    if len(dates) != 21 or any(day not in bars for day in dates):
        return None
    return dates


def assess(signal: dict, bars: dict, asof: str, calendar, *,
           max_risk: float = 0.08, entry_buffer: float = 0.002,
           stop_buffer: float = 0.005, min_rr: float = 2.0) -> dict:
    """Assess a pre-identified 2B/retest; never reads future price bars.

    Entry hypothesis: signal-day high * (1 + 0.2%), NOT a predicted fill.
    2B invalidation: signal-day low * (1 - 0.5%).
    Retest invalidation: old reference low * (1 - 0.5%).
    Resistance: maximum traded high AFTER prior low and BEFORE signal day;
    if below projected entry, no positive historical upside is available.
    """
    signals = signal.get("signals") or {}
    chosen = [k for k in ("two_b", "retest") if signals.get(k) is True]
    result = {
        "rank": int(signal["rank"]), "bucket": signal["bucket"],
        "code": signal["code"], "name": signal["name"],
        "target_session": asof,
        "signal_type": "2B假跌破" if chosen == ["two_b"] else (
            "二次回踩" if chosen == ["retest"] else "异常信号"),
        "close": signal.get("close"),
        "entry_reference": None, "structure_invalidation": None,
        "loss_pct_to_stop": None, "risk_within_8pct": False,
        "historical_resistance": None,
        "historical_resistance_date_window": None,
        "reward_pct_to_resistance": None,
        "reward_risk_to_resistance": None,
        "hypothetical_2r_target": None, "hypothetical_3r_target": None,
        "risk_status": "data_not_verified",
        "reference_low": signal.get("reference_low"),
        "reference_low_date": signal.get("reference_low_date"),
        "notes": [],
        "trigger_rule": "未来交易日向上突破参考触发价，且实际成交价接近参考价才重新评估；非订单",
    }
    if len(chosen) != 1 or signal.get("analysis_status") != "analyzed":
        result["notes"].append("结构信号状态不一致或未完成分析")
        return result

    window = _recent_window(asof, bars, calendar)
    if not window:
        result["notes"].append("最近20个港股交易日数据不完整，停止风险计算")
        return result
    today = bars[asof]
    target = signal.get("target_ohlc") or {}
    ohlc = ("open", "high", "low", "close")
    if not all(isinstance(today.get(k), (int, float)) and
               isinstance(target.get(k), (int, float)) and
               math.isfinite(today[k]) and today[k] > 0 and
               math.isclose(today[k], target[k], rel_tol=0, abs_tol=1e-5)
               for k in ohlc):
        result["notes"].append("结构观察文件的当日OHLC与原始日线不一致")
        return result
    if not math.isclose(float(signal.get("close") or 0), today["close"], rel_tol=0, abs_tol=1e-5):
        result["notes"].append("结构观察文件收盘价不一致")
        return result

    previous = window[:-1]
    lows = [bars[d]["low"] for d in previous]
    reference_low = min(lows)
    lowest_pos = lows.index(reference_low)
    ref_date = previous[lowest_pos]
    age = len(previous) - lowest_pos
    reported_low = signal.get("reference_low")
    if (not isinstance(reported_low, (int, float)) or
        not math.isclose(reference_low, reported_low, abs_tol=1e-5, rel_tol=0) or
        signal.get("reference_low_date") != ref_date or not (5 <= age <= 20)):
        result["notes"].append("前低或低点日期不一致，不计算风险方案")
        return result

    low, high, close, op = (today[k] for k in ("low", "high", "close", "open"))
    yesterday_close = bars[previous[-1]]["close"]
    correct_two_b = reference_low * .97 <= low < reference_low and close > reference_low
    correct_retest = reference_low <= low <= reference_low * 1.02 and close > op and close > yesterday_close
    if (chosen == ["two_b"] and not correct_two_b) or (
        chosen == ["retest"] and not correct_retest):
        result["notes"].append("价格未能复现当日的2B/二踩条件")
        return result

    entry = high * (1 + entry_buffer)
    invalidation_base = low if chosen == ["two_b"] else reference_low
    invalidation = invalidation_base * (1 - stop_buffer)
    if entry <= invalidation or invalidation <= 0:
        result["notes"].append("结构失效价与触发价关系异常")
        return result
    one_r = entry - invalidation
    loss_pct = 100 * one_r / entry
    # Historical price-based upside reference only, not a forecast.
    after_low_dates = previous[lowest_pos + 1:]
    historical_resistance = max((bars[d]["high"] for d in after_low_dates), default=None)
    result.update({
        "entry_reference": _price(entry),
        "structure_invalidation": _price(invalidation),
        "loss_pct_to_stop": round(loss_pct, 2),
        "risk_within_8pct": bool(one_r / entry <= max_risk + 1e-12),
        "hypothetical_2r_target": _price(entry + 2 * one_r),
        "hypothetical_3r_target": _price(entry + 3 * one_r),
        "reference_low": _price(reference_low),
        "reference_low_date": ref_date,
        "risk_status": "evaluated",
    })
    if historical_resistance is not None:
        result["historical_resistance"] = _price(historical_resistance)
        result["historical_resistance_date_window"] = [after_low_dates[0], after_low_dates[-1]]
        if historical_resistance > entry:
            result["reward_pct_to_resistance"] = round(100 * (historical_resistance / entry - 1), 2)
            result["reward_risk_to_resistance"] = round((historical_resistance - entry) / one_r, 2)

    if not result["risk_within_8pct"]:
        result["risk_status"] = "over_8pct"
        result["notes"].append("参考触发价到结构失效价的价格风险超过8%；不符合风险上限")
    elif result["reward_risk_to_resistance"] is None:
        result["risk_status"] = "no_upside_resistance"
        result["notes"].append("历史区间高点未高于参考触发价，无法证实上方存在正收益空间")
    elif result["reward_risk_to_resistance"] < min_rr:
        result["risk_status"] = "rr_below_threshold"
        result["notes"].append(f"已满足{max_risk:.0%}价格风险上限，但至历史反弹高点空间不足{min_rr:g}R")
    else:
        result["risk_status"] = "scenario_pass"
        result["notes"].append(f"已满足{max_risk:.0%}价格风险上限，历史阻力空间≥{min_rr:g}R；仅供研究")
    result["notes"].append("参考价非明日开盘价，真实成交需确认港股最小价位、跳空、价差和交易费用")
    result["notes"].append("若触发前先跌破结构失效价，条件方案作废；失效价不保证止损按该价成交")
    return result


def make_markdown(report: dict) -> str:
    meta = report["meta"]
    rows = report["assessments"]
    lines = [
        "# 港股低位2B/二踩：结构失效与8%价格风险评估", "",
        f"- **价格截止日**：{meta['target_session']}（香港）；生成时间：{meta['generated_at_hkt']}",
        f"- **筛选结果**：2B或二踩共{meta['signal_candidates']}只；已计算{meta['evaluated_count']}只；未完成验证{meta['invalid_count']}只。",
        f"- **参数**：触发价=信号日最高价上方{100*meta['entry_buffer']:.1f}%；2B失效价=当日最低价下方{100*meta['stop_buffer']:.1f}%；二踩失效价=前低下方{100*meta['stop_buffer']:.1f}%。",
        f"- **风险**：每笔股票价格风险上限{100*meta['max_risk']:.0f}%，历史阻力至少{meta['min_rr']:g}R为研究参考门槛（参数未经过独立回测验证）。",
        "- **性质**：这些都是收盘后预先计算的条件式参考价，不是对下一交易日的开盘或可成交价预测，也不是下单指令。",
        "",
        "## 全部2B与二踩候选（沿用原低位排名）", "",
    ]
    if rows:
        lines += [
            "| 原排名 | 股票 | 结构 | 收盘 | 假设触发价 | 失效参考价 | 参考风险 | 8%限制 | 历史阻力 | 阻力收益/风险 | 2R演算价 | 评估结果 |",
            "|---:|---|---|---:|---:|---:|---:|:---:|---:|---:|---:|---|",
        ]
    for r in rows:
        def val(v, width=3):
            return "—" if v is None else f"{v:.{width}f}"
        status = {
            "scenario_pass": "研究阈值满足（非买点）",
            "rr_below_threshold": "历史阻力不足2R",
            "no_upside_resistance": "无可用上方历史阻力",
            "over_8pct": "超过8%上限",
            "data_not_verified": "资料未核验，暂停",
        }.get(r["risk_status"], r["risk_status"])
        cap = ("—" if r["loss_pct_to_stop"] is None else
               "符合" if r["risk_within_8pct"] else "不符合")
        loss = "—" if r["loss_pct_to_stop"] is None else f"{r['loss_pct_to_stop']:.2f}%"
        lines.append(f"| {r['rank']} | {r['code']} {r['name']} | {r['signal_type']} | "
                     f"{val(r['close'])} | {val(r['entry_reference'])} | "
                     f"{val(r['structure_invalidation'])} | {loss} | {cap} | "
                     f"{val(r['historical_resistance'])} | "
                     f"{val(r['reward_risk_to_resistance'], 2)} | "
                     f"{val(r['hypothetical_2r_target'])} | {status} |")
    if not rows:
        lines.append("今天没有符合2B或二踩条件的低位候选。")
    lines += [
        "", "## 原理与限制", "",
        "- **触发方式**：只有未来交易日价格突破假设触发价，且实际成交价接近该价时，才可以重新计算风险；如果跳空高开，不能直接套用表中的8%结论。",
        "- **结构失效**：2B关注假跌破当日最低点；二踩关注此前参考低点。额外0.5%只是研究中的缓冲，不是港交所最小报价单位。",
        "- **历史阻力**：参考前低出现以后、信号日以前交易日的最高价；不是未来必达目标，也不能拿历史高点当反弹概率。",
        "- **2R/3R**：由假设价差推导的数学情景，不是价格预测；盈亏比没有扣佣金、印花税、费用及滑点。",
        "- **实际风险**：止损跳空或流动性不足可能导致亏损大于8%，这里8%仅是理论触发价与失效价的价格距离上限，不是保证止损。",
        "- **研究阶段**：不自动下单、不开立止损订单、不改变cloud_run.py的三组排名。", "",
    ]
    return "\n".join(lines)


def _atomic_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        temp.replace(path)
    finally:
        if temp.exists():
            temp.unlink()


def run(root: Path, *, latest=None, signal_report=None, calendar=None,
        read_bars_fn=None, max_risk=.08, entry_buffer=.002,
        stop_buffer=.005, min_rr=2.0):
    """Match two dated upstream reports before generating conditional price plans."""
    if not (0 < max_risk < 1 and 0 <= entry_buffer < .1 and
            0 <= stop_buffer < .1 and min_rr > 0):
        raise ValueError("不合法的风险或缓冲参数")
    if latest is None:
        with (root / "data" / "latest.json").open(encoding="utf-8") as handle:
            latest = json.load(handle)
    if signal_report is None:
        with (root / "data" / "entry_signals.json").open(encoding="utf-8") as handle:
            signal_report = json.load(handle)
    if calendar is None:
        calendar = xcals.get_calendar("XHKG")
    if read_bars_fn is None:
        from entry_signals import read_bars as read_bars_fn
    cutoff = latest["meta"]["target_session_hkt"]
    if signal_report["meta"]["target_session"] != cutoff:
        raise ValueError("低位与信号报告的目标交易日不一致，拒绝混用")
    stocks = latest["ranked_candidates"]
    signals = signal_report["candidates"]
    if len(stocks) != len(signals):
        raise ValueError("低位候选与信号候选数量不一致，拒绝混用")
    if signal_report["meta"]["candidate_count"] != len(signals):
        raise ValueError("信号候选计数不一致")
    if len({x["code"] for x in stocks}) != len(stocks):
        raise ValueError("低位股票代码重复")
    if any((x["code"], int(x["rank"])) != (y["code"], int(y["rank"]))
           for x, y in zip(stocks, signals)):
        raise ValueError("低位候选与形态信号对应代码/排名不一致")
    evaluated = []
    for stock, signal in zip(stocks, signals):
        flags = signal.get("signals") or {}
        if flags.get("two_b") is not True and flags.get("retest") is not True:
            continue
        bars = read_bars_fn(root, stock["code"], cutoff)
        evaluated.append(assess(signal, bars, cutoff, calendar, max_risk=max_risk,
                                entry_buffer=entry_buffer, stop_buffer=stop_buffer,
                                min_rr=min_rr))
    counts = {}
    for status in ("scenario_pass", "rr_below_threshold", "no_upside_resistance",
                   "over_8pct", "data_not_verified"):
        counts[status] = sum(r["risk_status"] == status for r in evaluated)
    report = {
        "meta": {
            "target_session": cutoff,
            "generated_at_hkt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S %z"),
            "source_fresh_count": latest["meta"].get("fresh_count"),
            "source_total": latest["meta"].get("total"),
            "source_candidate_count": len(stocks),
            "signal_candidates": len(evaluated),
            "evaluated_count": len(evaluated) - counts["data_not_verified"],
            "invalid_count": counts["data_not_verified"],
            "status_counts": counts,
            "max_risk": max_risk,
            "entry_buffer": entry_buffer,
            "stop_buffer": stop_buffer,
            "min_rr": min_rr,
            "rule_version": "conditional_risk_2b_retest_v1",
            "notice": "No future prices, no order, no guaranteed loss cap; fees/slippage not included",
        },
        "assessments": evaluated,
    }
    d = root / "data"
    _atomic_text(d / "risk_manager.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    md = make_markdown(report)
    _atomic_text(d / "risk_manager.md", md)
    _atomic_text(d / "risk" / f"{cutoff}.md", md)
    csvpath = d / "风险收益评估.csv"
    csvpath.parent.mkdir(parents=True, exist_ok=True)
    with csvpath.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for item in evaluated:
            writer.writerow({**{k: item.get(k) for k in CSV_FIELDS if k != "notes"},
                             "notes": "；".join(item["notes"])})
    print("RISK_MANAGER " + " ".join([
        f"target={cutoff}", f"signal_candidates={len(evaluated)}",
        f"evaluated={report['meta']['evaluated_count']}",
        *(f"{k}={v}" for k, v in counts.items()),
    ]), flush=True)
    return report


def main():
    p = argparse.ArgumentParser(description="Conditional pure-price risk scenarios, not orders")
    p.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    p.add_argument("--risk-cap", type=float, default=.08)
    p.add_argument("--entry-buffer", type=float, default=.002)
    p.add_argument("--stop-buffer", type=float, default=.005)
    p.add_argument("--min-rr", type=float, default=2.0)
    args = p.parse_args()
    run(args.output.resolve(), max_risk=args.risk_cap,
        entry_buffer=args.entry_buffer, stop_buffer=args.stop_buffer,
        min_rr=args.min_rr)


if __name__ == "__main__":
    main()
