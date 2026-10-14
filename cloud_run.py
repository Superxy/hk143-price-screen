# -*- coding: utf-8 -*-
"""HK143 cloud downloader / price-only screen.

Data: Sina HK daily OHLC (AKShare stock_hk_daily, adjust='').
Never treat an old close as a new close. No synthetic OHLC or return probabilities.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

OHLC = ["date", "open", "high", "low", "close"]
FIELDS = OHLC + ["volume", "amount"]
HKT = ZoneInfo("Asia/Hong_Kong")
UTC = timezone.utc
START_YEAR = 2025
SOURCE = "Sina Finance via AKShare stock_hk_daily(adjust='') | unadjusted HKD OHLC"


def hk_calendar():
    return xcals.get_calendar("XHKG")


def session_on_or_before(day: date, calendar) -> date:
    return calendar.date_to_session(pd.Timestamp(day), direction="previous").date()


def expected_session(now: datetime, calendar) -> date:
    # Use today's closing bar only after 18:00 HKT; 06:17 run means preceding session.
    local = now.astimezone(HKT)
    candidate = local.date() if local.hour >= 18 else local.date() - timedelta(days=1)
    return session_on_or_before(candidate, calendar)


def session_first_last(calendar, year: int, end: date | None = None):
    start = date(year, 1, 1)
    upper = end if end else date(year, 12, 31)
    sessions = calendar.sessions_in_range(pd.Timestamp(start), pd.Timestamp(upper))
    if len(sessions) == 0:
        return None, None
    return sessions[0].date(), sessions[-1].date()


def load_stocks(csvfile: Path):
    with csvfile.open("r", encoding="utf-8-sig", newline="") as handle:
        stocks = [{"rank": int(r["rank"]), "code": r["code"].strip().zfill(5),
                   "name": r["name"].strip()} for r in csv.DictReader(handle)]
    if len(stocks) != 143 or len({s["code"] for s in stocks}) != 143:
        raise ValueError("股票列表不是143只唯一港股")
    return stocks


def normalize(raw, cutoff: date) -> pd.DataFrame:
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        raise ValueError("行情接口返回空表")
    missing = [c for c in OHLC if c not in raw.columns]
    if missing:
        raise ValueError(f"OHLC字段缺失: {missing}")
    df = raw.copy()
    for field in ("volume", "amount"):
        if field not in df.columns:
            df[field] = float("nan")
    df = df[FIELDS].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for field in FIELDS[1:]:
        df[field] = pd.to_numeric(df[field], errors="coerce")
    df = df.dropna(subset=OHLC)
    df = df[(df["date"].dt.date >= date(START_YEAR, 1, 1)) &
            (df["date"].dt.date <= cutoff)]
    df = df[(df[OHLC[1:]] > 0).all(axis=1)]
    df = df[(df["high"] + 1e-9 >= df[["open", "low", "close"]].max(axis=1)) &
            (df["low"] - 1e-9 <= df[["open", "high", "close"]].min(axis=1))]
    df = df.sort_values("date").drop_duplicates(subset="date", keep="last")
    if df.empty:
        raise ValueError("2025年至今没有可用的不复权日线")
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return df.reset_index(drop=True)


def read_history(root: Path, code: str, end: date) -> pd.DataFrame:
    frames = []
    for year in range(START_YEAR, end.year + 1):
        f = root / "data" / str(year) / f"{code}_{year}.csv"
        if not f.exists():
            continue
        try:
            with f.open("r", encoding="utf-8-sig", newline="") as h:
                old = pd.read_csv(h, dtype={"date": str})
            if all(c in old for c in OHLC):
                frames.append(old)
        except (OSError, ValueError, pd.errors.EmptyDataError):
            pass
    if not frames:
        return pd.DataFrame(columns=FIELDS)
    return normalize(pd.concat(frames, ignore_index=True), end)


def atomic_csv(df: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        df.to_csv(temp, index=False, encoding="utf-8-sig")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def atomic_text(content: str, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def save_history(root: Path, code: str, prior: pd.DataFrame, fetched: pd.DataFrame, end: date):
    frames = [frame for frame in (prior, fetched) if not frame.empty]
    combined = normalize(pd.concat(frames, ignore_index=True), end)
    for year in range(START_YEAR, end.year + 1):
        rows = combined[combined["date"].str.startswith(str(year))]
        if not rows.empty:
            atomic_csv(rows, root / "data" / str(year) / f"{code}_{year}.csv")
    return combined


def download(code: str, end: date, attempts: int, delay: float, source_func):
    for idx in range(attempts):
        try:
            return normalize(source_func(symbol=code, adjust=""), end)
        except Exception as exc:
            print(f"{code} attempt {idx+1}/{attempts}: {type(exc).__name__}: {str(exc)[:130]}", flush=True)
            if idx + 1 == attempts:
                raise
            time.sleep(delay * (idx + 1) + random.uniform(0, .8))
    raise RuntimeError("unreachable")


def consecutive_bearish(df, calendar):
    df = df.sort_values("date").reset_index(drop=True)
    dates = [date.fromisoformat(s) for s in df["date"]]
    count = 0
    next_expected = dates[-1]
    for i in range(len(df)-1, -1, -1):
        if dates[i] != next_expected:
            break
        if float(df.iloc[i]["close"]) >= float(df.iloc[i]["open"]):
            break
        count += 1
        if i:
            next_expected = calendar.previous_session(pd.Timestamp(next_expected)).date()
    return count


def consecutive_down(df, calendar):
    df = df.sort_values("date").reset_index(drop=True)
    dates = [date.fromisoformat(s) for s in df["date"]]
    count = 0
    for i in range(len(df)-1, 0, -1):
        if calendar.previous_session(pd.Timestamp(dates[i])).date() != dates[i-1]:
            break
        if float(df.iloc[i]["close"]) >= float(df.iloc[i-1]["close"]):
            break
        count += 1
    return count


def price_position(df, price):
    if df.empty:
        return None
    low = float(df["low"].min())
    high = float(df["high"].max())
    first_low = df.loc[df["low"].idxmin(), "date"]
    return {
        "low": round(low, 5), "high": round(high, 5), "low_date": first_low,
        "position_pct": round(100 * (price-low)/(high-low), 2) if high > low else None,
        "distance_from_low_pct": round(100 * (price/low-1), 2) if low > 0 else None,
    }


def low_matches(position):
    return position is not None and position["position_pct"] is not None and (
        position["position_pct"] <= 20 and position["distance_from_low_pct"] <= 15)


def suspected_jump(df):
    # Flag possible corporate-action gaps; do not label them as confirmed split/rights issue.
    if len(df) < 2:
        return False
    prev_close = df["close"].shift(1)
    overnight_gap = (df["open"] / prev_close - 1).abs()
    return bool((overnight_gap > 0.45).any())


def calc_stock(stock, history: pd.DataFrame, asof: date, calendar, error: str = ""):
    code, name = stock["code"], stock["name"]
    row = {"code": code, "name": name, "source_rank": stock["rank"],
           "latest_date": None, "close": None, "data_status": "missing",
           "error": error, "prior_year": asof.year-1,
           "prior_year_full": False, "current_year_full_from_start": False,
           "previous_year_range": None, "current_year_range": None,
           "previous_year_low": False, "current_year_low": False,
           "below_previous_year_low": False, "return_120_sessions_pct": None,
           "bearish_streak": 0, "falling_close_streak": 0,
           "suspicious_price_jump": False, "screen_eligible": False,
           "reasons": []}
    if history.empty:
        row["reasons"].append("无2025年起历史日K线")
        return row
    last_day = date.fromisoformat(history["date"].iloc[-1])
    row["latest_date"] = last_day.isoformat()
    row["close"] = round(float(history["close"].iloc[-1]), 5)
    row["data_status"] = "fresh" if last_day == asof else "stale"
    if last_day != asof:
        row["reasons"].append("缺少本次指定交易日收盘K线，不能参与今日排名（可能停牌或接口延迟）")
        return row
    cy, py = asof.year, asof.year-1
    current = history[history["date"].str.startswith(str(cy))]
    previous = history[history["date"].str.startswith(str(py))]
    cy_first, cy_last = session_first_last(calendar, cy, asof)
    py_first, py_last = session_first_last(calendar, py)
    # Price feeds can skip a trading session in the middle while still showing
    # first+last dates. Do not call such an incomplete year verified/full.
    prior_days = set(previous["date"])
    current_days = set(current["date"])
    py_expected = {d.date().isoformat() for d in calendar.sessions_in_range(pd.Timestamp(py_first), pd.Timestamp(py_last))} if py_first else set()
    cy_expected = {d.date().isoformat() for d in calendar.sessions_in_range(pd.Timestamp(cy_first), pd.Timestamp(cy_last))} if cy_first else set()
    row["prior_year_full"] = bool(py_expected and py_expected.issubset(prior_days))
    row["current_year_full_from_start"] = bool(cy_expected and cy_expected.issubset(current_days))
    row["previous_year_range"] = price_position(previous, row["close"])
    row["current_year_range"] = price_position(current, row["close"])
    if not row["prior_year_full"]:
        row["reasons"].append("上一年度首末交易日覆盖不足，不能认证全年低位")
    if not row["current_year_full_from_start"]:
        row["reasons"].append("本年度起点覆盖不足，不能认证年内低位")
    row["suspicious_price_jump"] = suspected_jump(history)
    if row["suspicious_price_jump"]:
        row["reasons"].append("存在超过45%的隔夜价格跳变，需核验拆合股、供股或突发事件")
    try:
        ref = calendar.session_offset(pd.Timestamp(asof), -120).date().isoformat()
        reference = history.loc[history["date"] == ref, "close"]
        if len(reference):
            row["return_120_sessions_pct"] = round(100 * (row["close"] / float(reference.iloc[0]) - 1), 2)
        else:
            row["reasons"].append("缺少恰好120个交易日前的收盘价")
    except (ValueError, OverflowError, KeyError):
        row["reasons"].append("交易日历历史长度不足120个交易日")
    row["bearish_streak"] = consecutive_bearish(history, calendar)
    row["falling_close_streak"] = consecutive_down(history, calendar)
    long_fall = row["return_120_sessions_pct"] is not None and row["return_120_sessions_pct"] <= -20
    row["screen_eligible"] = bool(long_fall and not row["suspicious_price_jump"])
    row["previous_year_low"] = bool(row["screen_eligible"] and row["prior_year_full"] and
                                    low_matches(row["previous_year_range"]))
    row["current_year_low"] = bool(row["screen_eligible"] and row["current_year_full_from_start"] and
                                   low_matches(row["current_year_range"]))
    row["below_previous_year_low"] = bool(row["prior_year_full"] and
                                          row["previous_year_range"] is not None and
                                          row["close"] < row["previous_year_range"]["low"])
    if not long_fall:
        row["reasons"].append("120个交易日跌幅未达到20%或缺失")
    return row


def prioritize(records):
    previous = [r for r in records if r["previous_year_low"]]
    current_only = [r for r in records if r["current_year_low"] and not r["previous_year_low"]]
    def prev_key(r):
        return (not r["current_year_low"], r["bearish_streak"] < 3,
                r["previous_year_range"]["position_pct"], r["code"])
    def current_key(r):
        return (r["bearish_streak"] < 3, r["current_year_range"]["position_pct"], r["code"])
    previous.sort(key=prev_key)
    current_only.sort(key=current_key)
    top = previous[:20] + current_only[:max(0, 20-len(previous))]
    for r in top:
        r["bucket"] = "上一年度低位优先" if r["previous_year_low"] else "年内低位补充"
    return previous, current_only, top


def public_stock(r):
    return {k: v for k, v in r.items() if k != "error"}


def format_rank(r):
    py = r["previous_year_range"] or {}
    cy = r["current_year_range"] or {}
    def position_str(rec):
        return (str(rec.get("position_pct")) + "%") if rec.get("position_pct") is not None else "—"
    return (f"| {r['code']} {r['name']} | {r['close']:.3f} | "
            f"{py.get('low', '—')} | {position_str(py)} | "
            f"{cy.get('low', '—')} | {position_str(cy)} | "
            f"{r['return_120_sessions_pct']}% | {r['bearish_streak']} | {r['falling_close_streak']} |")


def make_markdown(report):
    meta, records, prev, ytd, top = (report[k] for k in ("meta", "stocks", "previous_year_candidates", "current_year_candidates", "top20"))
    lines = ["# 港股143只历史低位抄底筛选：最新报告", "",
             f"- **交易日：{meta['target_session_hkt']}（香港）**；生成时间：{meta['generated_at_hkt']}",
             f"- **数据源**：{SOURCE}",
             f"- **覆盖**：{meta['fresh_count']}/143只包含该日有效日线；{meta['stale_count']}只陈旧，{meta['missing_count']}只缺失；{meta['download_failures']}只本轮请求失败",
             f"- **运行状态**：{meta['status']}。非完整覆盖不可冒充143只全量排名。",
             "- **规则**：只看不复权价格；120交易日跌幅≤-20%；区间位置≤20%且距低点≤15%；2025年低位优先于2026年低位（年份随时间滚动）。",
             "- **排名性质**：低位形态匹配榜（**未经样本外概率验证**），不显示虚构反弹成功率。",
             "- **注意**：低位不代表已经见底；连续收阴不保证反弹。", "",
             f"## 上一年度低位（符合要求共{len(prev)}只）", "",
             "| 股票 | 最新收盘价 | 上年低价 | 上年区间位置 | 当年低价 | 当年区间位置 | 120日涨跌 | 连阴天数 | 连续收盘下跌 |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|",]
    lines += [format_rank(r) for r in top if r["previous_year_low"]] or ["| 无合格样本 | — | — | — | — | — | — | — | — |"]
    lines += ["", f"## 年内低位补充（符合要求共{len(ytd)}只，不含上组）", "",
              "| 股票 | 最新收盘价 | 上年低价 | 上年区间位置 | 当年低价 | 当年区间位置 | 120日涨跌 | 连阴天数 | 连续收盘下跌 |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|",]
    lines += [format_rank(r) for r in top if r["current_year_low"] and not r["previous_year_low"]] or ["| 无合格样本 | — | — | — | — | — | — | — | — |"]
    lines += ["", "## 数据完整性", "", "以下股票不参加今日排名；不把旧收盘价冒充目标交易日收盘价：", ""]
    excluded = [r for r in records if r["data_status"] != "fresh"]
    for r in excluded:
        lines.append(f"- {r['code']} {r['name']}：最后K线 {r['latest_date'] or '无'}；{r['data_status']}")
    if not excluded:
        lines.append("- 无数据日期缺失（不代表公司行为或历史区间均已核验）。")
    lines += ["", "## 说明", "", "本报告不是买卖指令。复权/供股/拆合股可能使不复权历史价格失去可比性；出现超过45%隔夜跳变者暂时排除，需人工核验。", ""]
    return "\n".join(lines)


def run(root: Path, stocks, asof: date, calendar, *, attempts: int, delay: float, fetch_func, sleep=True):
    started_at = datetime.now(HKT)
    errors = []
    results = []
    for i, stock in enumerate(stocks, start=1):
        code = stock["code"]
        print(f"[{i}/{len(stocks)}] {code} {stock['name']}", flush=True)
        prior = read_history(root, code, asof)
        last = date.fromisoformat(prior["date"].iloc[-1]) if not prior.empty else None
        error = ""
        history = prior
        if last != asof:
            try:
                fetched = download(code, asof, attempts, delay, fetch_func)
                history = save_history(root, code, prior, fetched, asof)
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:150]}"
                errors.append({"code": code, "name": stock["name"], "error": error})
            if sleep and delay > 0:
                time.sleep(delay + random.uniform(0, 0.6))
        results.append(calc_stock(stock, history, asof, calendar, error))
        print(f"    {results[-1]['data_status']} | last={results[-1]['latest_date']}" + (f" | {error}" if error else ""), flush=True)
    prev, ytd, top = prioritize(results)
    fresh = sum(r["data_status"] == "fresh" for r in results)
    stale = sum(r["data_status"] == "stale" for r in results)
    missing = sum(r["data_status"] == "missing" for r in results)
    meta = {
        "generated_at_hkt": started_at.strftime("%Y-%m-%d %H:%M:%S %z"),
        "target_session_hkt": asof.isoformat(),
        "price_type": "unadjusted", "currency": "HKD", "source": SOURCE,
        "total": len(stocks), "fresh_count": fresh, "stale_count": stale,
        "missing_count": missing, "download_failures": len(errors),
        "status": "complete_date_coverage" if fresh == len(stocks) else ("partial" if fresh else "unavailable"),
        "ranking_method": "pure_price_pattern_match_not_validated_probability",
        "warning": "Never interpret rankings as calibrated rebound probabilities; provider may lag/suspend/correct prices."
    }
    report = {"meta": meta, "previous_year_candidates": [public_stock(r) for r in prev],
              "current_year_candidates": [public_stock(r) for r in ytd],
              "top20": [public_stock(r) for r in top],
              "stocks": [public_stock(r) for r in results], "errors": errors}
    data_dir = root / "data"
    atomic_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", data_dir / "latest.json")
    atomic_text(make_markdown(report), data_dir / "latest.md")
    fields = ("code", "name", "latest_date", "close", "data_status", "return_120_sessions_pct", "bearish_streak",
              "falling_close_streak", "previous_year_low", "current_year_low", "prior_year_full", "current_year_full_from_start")
    atomic_csv(pd.DataFrame([{field: r[field] for field in fields} for r in results]), data_dir / "覆盖与评分.csv")
    atomic_csv(pd.DataFrame([{"group": r["bucket"], "code": r["code"], "name": r["name"], "close": r["close"],
                              "prior_year_position_pct": (r["previous_year_range"] or {}).get("position_pct"),
                              "current_year_position_pct": (r["current_year_range"] or {}).get("position_pct"),
                              "return_120_sessions_pct": r["return_120_sessions_pct"],
                              "bearish_streak": r["bearish_streak"]} for r in top],
                             columns=["group", "code", "name", "close", "prior_year_position_pct", "current_year_position_pct", "return_120_sessions_pct", "bearish_streak"]), data_dir / "低位前20.csv")
    dated = data_dir / "reports" / f"{asof.isoformat()}.md"
    atomic_text(make_markdown(report), dated)
    print(f"REPORT target={asof} fresh={fresh}/{len(stocks)} stale={stale} missing={missing} errors={len(errors)} top={len(top)}")
    return report


def main():
    p = argparse.ArgumentParser(description="HK143 cloud daily screening; pure unadjusted price")
    p.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    p.add_argument("--asof", help="Override expected HK market session YYYY-MM-DD (for testing/recovery)")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--attempts", type=int, default=3)
    args = p.parse_args()
    if args.delay < 0 or args.attempts < 1:
        p.error("--delay must be >= 0 and --attempts >= 1")
    cal = hk_calendar()
    now = datetime.now(HKT)
    cutoff = date.fromisoformat(args.asof) if args.asof else expected_session(now, cal)
    if not cal.is_session(pd.Timestamp(cutoff)):
        p.error(f"not an XHKG trading session: {cutoff}")
    root = args.output.resolve()
    stocks = load_stocks(root / "config" / "stocks.csv")
    import akshare as ak
    run(root, stocks, cutoff, cal, attempts=args.attempts, delay=args.delay, fetch_func=ak.stock_hk_daily)


if __name__ == "__main__":
    main()
