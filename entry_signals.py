# -*- coding: utf-8 -*-
"""Pure-price entry structure WATCHLIST for cloud_run.py's low-price candidates.

Uses only data available through the target session's close; no lookahead,
predicted rebound probabilities, automatic orders, or changes to the base rank.
Four independent labels can overlap (not four exclusive ranking buckets).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

HKT = ZoneInfo("Asia/Hong_Kong")
OHLC = ("open", "high", "low", "close")
SIGNALS = {
    "two_b": "2B假跌破收回",
    "retest": "二次回踩不破前低",
    "breakdown": "创新低且收盘未收回（破底）",
    "strength": "底部附近局部价格转强",
}
CSV_FIELDS = [
    "rank", "bucket", "code", "name", "target_session", "close",
    "analysis_status", "two_b", "retest", "breakdown", "strength",
    "reference_low", "reference_low_date", "low_age_sessions",
    "today_low_vs_reference_pct", "today_close_vs_reference_pct",
    "prior_3_high", "signal_labels", "notes",
]


def read_bars(root: Path, code: str, asof: str) -> dict[str, dict]:
    """Read only raw prices available by asof; do not use any later rows."""
    end_year = int(asof[:4])
    frames = []
    # Previous year is enough for a 20-session window across New Year.
    for year in range(max(2025, end_year - 1), end_year + 1):
        path = root / "data" / str(year) / f"{code}_{year}.csv"
        if path.exists():
            try:
                frame = pd.read_csv(path, dtype={"date": str}, encoding="utf-8-sig")
            except (OSError, ValueError, pd.errors.EmptyDataError):
                continue
            if {"date", *OHLC}.issubset(frame.columns):
                frames.append(frame[["date", *OHLC]])
    if not frames:
        return {}
    df = pd.concat(frames, ignore_index=True).drop_duplicates("date", keep="last")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for c in OHLC:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", *OHLC])
    df = df[df["date"].dt.strftime("%Y-%m-%d") <= asof]
    df = df[(df[list(OHLC)] > 0).all(axis=1)]
    df = df[(df["high"] >= df[["open", "low", "close"]].max(axis=1)) &
            (df["low"] <= df[["open", "high", "close"]].min(axis=1))]
    return {
        r.date.strftime("%Y-%m-%d"): {
            "open": float(r.open), "high": float(r.high),
            "low": float(r.low), "close": float(r.close),
        }
        for r in df.itertuples(index=False)
    }


def analyze_candidate(candidate: dict, bars: dict, asof: str, calendar) -> dict:
    """Analyze only twenty *complete* preceding HK exchange sessions."""
    rank = int(candidate["rank"])
    item = {
        "rank": rank,
        "bucket": candidate["bucket"],
        "code": candidate["code"],
        "name": candidate["name"],
        "target_session": asof,
        "close": candidate.get("close"),
        "analysis_status": "insufficient_history",
        "signals": {k: False for k in SIGNALS},
        "signal_labels": [],
        "notes": [],
        "reference_low": None,
        "reference_low_date": None,
        "low_age_sessions": None,
        "today_low_vs_reference_pct": None,
        "today_close_vs_reference_pct": None,
        "prior_3_high": None,
        "target_ohlc": None,
    }

    if candidate.get("latest_date") != asof or candidate.get("data_status") != "fresh":
        item["analysis_status"] = "not_fresh"
        item["notes"].append("候选股目标交易日K线不新鲜；不计算形态")
        return item

    ts = pd.Timestamp(asof)
    if not calendar.is_session(ts):
        item["analysis_status"] = "not_a_session"
        item["notes"].append("目标日不是港股交易日")
        return item

    try:
        first = calendar.session_offset(ts, -20)
        window = [t.date().isoformat() for t in calendar.sessions_in_range(first, ts)]
    except (ValueError, OverflowError, KeyError):
        item["notes"].append("交易日历不足20个完整交易日")
        return item
    if len(window) != 21 or any(d not in bars for d in window):
        item["notes"].append("最近20个交易日或目标日存在缺失K线，暂不判断结构")
        return item

    past = [(d, bars[d]) for d in window[:-1]]
    today = bars[asof]
    yesterday = past[-1][1]
    reference_low = min(p["low"] for _, p in past)
    low_index = next(i for i, (d, p) in enumerate(past) if p["low"] == reference_low)
    ref_date = past[low_index][0]
    age = len(past) - low_index
    close = today["close"]
    op, high, low = today["open"], today["high"], today["low"]
    prior_3_high = max(p["high"] for _, p in past[-3:])
    recent_5_low = min([p["low"] for _, p in past[-4:]] + [low])
    reference_established = 5 <= age <= 20

    two_b = (reference_established and reference_low * .97 <= low < reference_low
             and close > reference_low)
    retest = (reference_established and reference_low <= low <= reference_low * 1.02
              and close > op and close > yesterday["close"])
    breakdown = low < reference_low and close <= reference_low
    strength = (close > op and close > prior_3_high and
                recent_5_low <= reference_low * 1.03)

    detected = {
        "two_b": bool(two_b), "retest": bool(retest),
        "breakdown": bool(breakdown), "strength": bool(strength),
    }
    item.update({
        "analysis_status": "analyzed",
        "close": close,
        "signals": detected,
        "signal_labels": [SIGNALS[k] for k, v in detected.items() if v],
        "reference_low": round(reference_low, 5),
        "reference_low_date": ref_date,
        "low_age_sessions": age,
        "today_low_vs_reference_pct": round(100 * (low / reference_low - 1), 2),
        "today_close_vs_reference_pct": round(100 * (close / reference_low - 1), 2),
        "prior_3_high": round(prior_3_high, 5),
        "target_ohlc": {k: round(today[k], 5) for k in OHLC},
    })
    if two_b:
        item["notes"].append("盘中跌破前20日低价不超过3%，收盘重新站上该价；仍需后续确认")
    if retest:
        item["notes"].append("第二次回踩最低价不低于前低且距前低≤2%，收阳且收盘高于前日")
    if breakdown:
        item["notes"].append("创此前20交易日新低且收盘未收复；不是已确认支撑")
    if strength:
        item["notes"].append("底部附近，收阳且收盘突破前三个交易日最高价")
    if not item["signal_labels"]:
        item["notes"].append("今天未触发四类严格结构；仅保持低位观察")
    return item


def atomic_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def make_markdown(report: dict) -> str:
    meta, items = report["meta"], report["candidates"]
    year = int(meta["target_session"][:4])
    counts = meta["signal_counts"]
    lines = [
        "# 港股低位候选：每日纯价格形态观察",
        "",
        f"- **交易日**：{meta['target_session']}（香港）；生成时间：{meta['generated_at_hkt']}",
        f"- **候选数**：{meta['candidate_count']}；可分析：{meta['analyzed_count']}；数据不足：{meta['insufficient_count']}",
        f"- **形态计数（可能重叠）**：2B {counts['two_b']}只，二踩 {counts['retest']}只，破底 {counts['breakdown']}只，价格转强 {counts['strength']}只。",
        f"- **数据来源**：cloud_run.py生成的全量低位排名（{meta['source_report_generated_at_hkt']}）；不更改原排名或选股条件。",
        "- **性质**：纯价格观察信号，不是买卖指令，不代表支撑确认或反弹成功概率。",
        "",
        "## 检测定义",
        "",
        "- 参考低点：目标日之前20个连续港股交易日的最低价；2B与二踩要求该低点距今5—20个交易日。",
        "- **2B**：当日最低价小于参考低点、跌破幅度不超过3%，收盘价重新站上参考低点。",
        "- **二踩**：当日最低价不低于参考低点、最多高出2%；收阳且收盘高于上一交易日收盘价。",
        "- **破底**：当日创前20交易日新低，收盘价仍未站回参考低点。",
        "- **局部价格转强**：当日收阳、收盘突破此前3个交易日最高价，且最近5日（含今日）曾触及参考低点上方3%以内。",
        "- 四种状态可以交叉出现；没有信号不代表数据缺失。只用截至目标交易日的K线，不看未来走势。",
        "",
    ]
    groups = [
        ("双重低位", [x for x in items if x.get("previous_year_low") and x.get("current_year_low")]),
        (f"仅{year}年低位", [x for x in items if x.get("current_year_low") and not x.get("previous_year_low")]),
        (f"仅{year-1}年低位", [x for x in items if x.get("previous_year_low") and not x.get("current_year_low")]),
    ]
    for label, group in groups:
        lines.extend([f"## {label}（{len(group)}只）", ""])
        if not group:
            lines.append("没有符合条件的股票。")
            lines.append("")
            continue
        lines.extend([
            "| 原排名 | 股票 | 收盘价 | 参考低点（日期） | 2B | 二踩 | 破底 | 转强 | 结果 |",
            "|---:|---|---:|---|:---:|:---:|:---:|:---:|---|",
        ])
        for x in group:
            label_text = "、".join(x["signal_labels"]) if x["signal_labels"] else (
                "数据不足" if x["analysis_status"] != "analyzed" else "未触发")
            ref = ("—" if x["reference_low"] is None else
                   f"{x['reference_low']:.3f}（{x['reference_low_date']}）")
            yes = lambda k: "✓" if x["signals"][k] else "—"
            close = "—" if x["close"] is None else f"{x['close']:.3f}"
            lines.append(
                f"| {x['rank']} | {x['code']} {x['name']} | {close} | {ref} | "
                f"{yes('two_b')} | {yes('retest')} | {yes('breakdown')} | {yes('strength')} | {label_text} |")
        lines.append("")
    lines.extend([
        "## 使用注意", "",
        "当前属于结构观察：既不改变低位总排名，也不计算买入价、仓位、止损、止盈或未来胜率。",
        "连续破底的股票应特别警惕进一步下跌；2B与二踩也可能失败。",
        "停牌、日K线缺口、价格调整和公司行为可能影响判断，分析不足会标记而非补造数据。",
        "",
    ])
    return "\n".join(lines)


def run(root: Path, report: dict | None = None, calendar=None) -> dict:
    if report is None:
        with (root / "data" / "latest.json").open("r", encoding="utf-8") as fh:
            report = json.load(fh)
    cal = calendar or xcals.get_calendar("XHKG")
    meta = report["meta"]
    asof = meta["target_session_hkt"]
    stocks = report["ranked_candidates"]
    seen = set()
    for n, stock in enumerate(stocks, start=1):
        if stock["code"] in seen:
            raise ValueError("cloud_run 候选名单重复股票代码")
        seen.add(stock["code"])
        if int(stock["rank"]) != n:
            raise ValueError("cloud_run 候选名单总排名不连续")
    out = []
    for stock in stocks:
        bars = read_bars(root, stock["code"], asof)
        one = analyze_candidate(stock, bars, asof, cal)
        one["previous_year_low"] = stock["previous_year_low"]
        one["current_year_low"] = stock["current_year_low"]
        out.append(one)

    signal_counts = {k: sum(bool(v["signals"][k]) for v in out) for k in SIGNALS}
    analyzed_count = sum(v["analysis_status"] == "analyzed" for v in out)
    result = {
        "meta": {
            "target_session": asof,
            "generated_at_hkt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S %z"),
            "source_report_generated_at_hkt": meta.get("generated_at_hkt"),
            "source_fresh_count": meta["fresh_count"],
            "source_status": meta.get("status"),
            "candidate_count": len(out),
            "analyzed_count": analyzed_count,
            "insufficient_count": len(out) - analyzed_count,
            "signal_counts": signal_counts,
            "rule_version": "entry_structures_v1_20session_price_only",
            "warning": "Signals are observations, not validated probabilities or order instructions",
        },
        "definitions": SIGNALS,
        "candidates": out,
    }
    data = root / "data"
    atomic_text(data / "entry_signals.json", json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    md = make_markdown(result)
    atomic_text(data / "entry_signals.md", md)
    atomic_text(data / "signals" / (asof + ".md"), md)
    csvpath = data / "入场结构观察.csv"
    csvpath.parent.mkdir(parents=True, exist_ok=True)
    with csvpath.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for x in out:
            writer.writerow({
                **{k: x.get(k) for k in CSV_FIELDS
                   if k not in SIGNALS and k not in ("signal_labels", "notes")},
                **{k: int(x["signals"][k]) for k in SIGNALS},
                "signal_labels": "；".join(x["signal_labels"]),
                "notes": "；".join(x["notes"]),
            })
    print(f"ENTRY_SIGNALS target={asof} candidates={len(out)} analyzed={analyzed_count} " +
          " ".join(f"{k}={v}" for k, v in signal_counts.items()), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description="Identify price-only daily structures from cloud_run candidates")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    run(args.output.resolve())


if __name__ == "__main__":
    main()
