# -*- coding: utf-8 -*-
"""HK143 cloud downloader / price-only screen.

Primary data: Sina Finance via AKShare stock_hk_daily(adjust=""), unadjusted OHLC.
Repair data: Tencent Finance HK K-line API first, with Eastmoney as last-resort fallback; both unadjusted OHLC.
The runner scans the HKEX trading calendar for internal historical gaps before
accepting a ticker as structurally complete. It retries the primary source first,
then uses the independent secondary source only for unresolved gaps and source
cross-checking. Never synthesize OHLC or replace a missing close with an old close.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import signal
import sys
import threading
import time
from contextlib import contextmanager

import requests
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
SECONDARY_SOURCE = "Tencent Finance HK K-line API | unadjusted HKD OHLC"
TERTIARY_SOURCE = "Eastmoney HK K-line API | unadjusted HKD OHLC"
TENCENT_KLINE_URLS = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
)
TENCENT_KLINE_COUNT = 640
TENCENT_REQUEST_TIMEOUT = 8
EASTMONEY_KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_UT = "7eea3edcaed734bea9cbfc24409ed989"
SOURCE_CONFLICT_TOLERANCE = 0.003
SOURCE_CONFLICT_ABS_MIN = 0.01


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


def expected_sessions_between(calendar, start: date, end: date) -> set[str]:
    """Return all HKEX trading sessions in [start, end] as ISO date strings."""
    if start > end:
        return set()
    sessions = calendar.sessions_in_range(pd.Timestamp(start), pd.Timestamp(end))
    return {d.date().isoformat() for d in sessions}


def find_history_gaps(history: pd.DataFrame, calendar, asof: date) -> list[str]:
    """Scan for missing HKEX sessions inside the available history."""
    if history.empty or "date" not in history.columns:
        return []
    first = date.fromisoformat(str(history["date"].min()))
    if first > asof:
        return []
    expected = expected_sessions_between(
        calendar, max(first, date(START_YEAR, 1, 1)), asof
    )
    actual = set(history["date"].astype(str))
    return sorted(expected - actual)


def count_new_dates(base: pd.DataFrame, candidate: pd.DataFrame) -> int:
    if candidate.empty:
        return 0
    base_dates = set(base["date"].astype(str)) if not base.empty else set()
    return len(set(candidate["date"].astype(str)) - base_dates)


def source_conflicts(primary: pd.DataFrame, secondary: pd.DataFrame,
                     limit: int = 12) -> list[str]:
    """Compare overlapping unadjusted OHLC bars without overwriting primary data."""
    if primary.empty or secondary.empty:
        return []
    left = primary[["date", "open", "high", "low", "close"]]
    right = secondary[["date", "open", "high", "low", "close"]]
    merged = left.merge(
        right, on="date", how="inner", suffixes=("_primary", "_secondary")
    )
    conflicts = []
    for row in merged.itertuples(index=False):
        for field in ("open", "high", "low", "close"):
            a = float(getattr(row, f"{field}_primary"))
            b = float(getattr(row, f"{field}_secondary"))
            tolerance = max(SOURCE_CONFLICT_ABS_MIN, abs(a) * SOURCE_CONFLICT_TOLERANCE)
            if abs(a - b) > tolerance:
                conflicts.append(f"{row.date} {field}: primary={a:g}, secondary={b:g}")
                break
        if len(conflicts) >= limit:
            break
    return conflicts


def fill_missing_history(base: pd.DataFrame, supplement: pd.DataFrame,
                         calendar, asof: date) -> tuple[pd.DataFrame, int]:
    """Add only missing dates; the backup can never overwrite a primary bar."""
    if supplement.empty:
        return base, 0
    if base.empty:
        repaired = normalize(supplement, asof)
        return repaired, len(repaired)
    gaps = set(find_history_gaps(base, calendar, asof))
    if not gaps:
        return base, 0
    missing_rows = supplement[supplement["date"].astype(str).isin(gaps)].copy()
    if missing_rows.empty:
        return base, 0
    repaired = normalize(pd.concat([base, missing_rows], ignore_index=True), asof)
    return repaired, len(missing_rows)


def fetch_tencent_hk_daily(symbol: str, end: date) -> pd.DataFrame:
    """Tencent HK daily K-line backup; accept only unadjusted `day` rows."""
    symbol = symbol.zfill(5)
    api_symbol = f"hk{symbol}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/155 Safari/537.36",
        "Referer": "https://gu.qq.com/",
        "Accept": "application/json,text/plain,*/*",
    }
    last_error = None
    for url in TENCENT_KLINE_URLS:
        try:
            response = requests.get(
                url,
                params={"param": f"{api_symbol},day,,,{TENCENT_KLINE_COUNT},"},
                headers=headers,
                timeout=TENCENT_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            data = (payload.get("data") or {}).get(api_symbol) or {}
            rows = data.get("day") or []
            if not rows:
                raise ValueError(f"Tencent returned no unadjusted day rows for {symbol}")
            parsed = []
            for row in rows:
                if len(row) < 6:
                    raise ValueError(f"Tencent returned malformed K-line for {symbol}")
                parsed.append({
                    "date": row[0],
                    "open": row[1],
                    "close": row[2],
                    "high": row[3],
                    "low": row[4],
                    "volume": row[5],
                })
            result = normalize(pd.DataFrame(parsed), end)
            if end.isoformat() not in set(result["date"].astype(str)):
                raise ValueError(
                    f"Tencent data is stale for {symbol}: target={end.isoformat()} "
                    f"latest={result['date'].max()}"
                )
            result.attrs["source"] = "tencent"
            return result
        except Exception as exc:
            last_error = exc
            print(
                f"        [Tencent HK] {url} FAIL "
                f"{type(exc).__name__}: {str(exc)[:130]}",
                flush=True,
            )
    raise last_error or RuntimeError(f"Tencent failed for {symbol}")


def fetch_backup_hk_daily(symbol: str, end: date) -> pd.DataFrame:
    """Try Tencent first; use Eastmoney only if Tencent cannot provide target-day data."""
    try:
        print(f"        [Tencent HK] START {symbol}", flush=True)
        result = fetch_tencent_hk_daily(symbol, end)
        print(
            f"        [Tencent HK] OK {symbol} rows={len(result)} "
            f"latest={result['date'].max()}",
            flush=True,
        )
        return result
    except Exception as tencent_exc:
        print(
            f"        [Tencent HK] exhausted -> Eastmoney fallback "
            f"reason={type(tencent_exc).__name__}: {str(tencent_exc)[:130]}",
            flush=True,
        )
        result = fetch_eastmoney_hk_daily(symbol, end)
        if end.isoformat() not in set(result["date"].astype(str)):
            raise ValueError(
                f"Eastmoney data is stale for {symbol}: target={end.isoformat()} "
                f"latest={result['date'].max() if not result.empty else 'empty'}"
            )
        result.attrs["source"] = "eastmoney"
        return result


def fetch_eastmoney_hk_daily(symbol: str, end: date) -> pd.DataFrame:
    """Independent backup: Eastmoney HK daily K-line, unadjusted."""
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": EASTMONEY_UT,
        "klt": "101",
        "fqt": "0",
        "secid": f"116.{symbol}",
        "beg": f"{START_YEAR}0101",
        "end": end.strftime("%Y%m%d"),
        "lmt": "1000",
        "_": str(int(time.time() * 1000)),
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 Chrome/155 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/",
    }
    response = requests.get(
        EASTMONEY_KLINE_URL, params=params, headers=headers, timeout=20
    )
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data")
    if data is None:
        raise ValueError(f"Eastmoney returned no data for {symbol}")
    klines = data.get("klines") or []
    if not klines:
        return pd.DataFrame(columns=FIELDS)
    rows = [item.split(",") for item in klines]
    if any(len(row) < 7 for row in rows):
        raise ValueError(f"Eastmoney returned malformed K-lines for {symbol}")
    raw = pd.DataFrame(rows, columns=[
        "date", "open", "close", "high", "low",
        "volume", "amount", "amplitude", "pct", "change", "turnover",
    ])
    return normalize(raw, end)


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


def persist_history(root: Path, code: str, history: pd.DataFrame, end: date):
    """Persist an already-merged history without changing source precedence."""
    combined = normalize(history, end)
    for year in range(START_YEAR, end.year + 1):
        rows = combined[combined["date"].str.startswith(str(year))]
        if not rows.empty:
            atomic_csv(rows, root / "data" / str(year) / f"{code}_{year}.csv")
    return combined


def save_history(root: Path, code: str, prior: pd.DataFrame, fetched: pd.DataFrame, end: date):
    frames = [frame for frame in (prior, fetched) if not frame.empty]
    return persist_history(
        root, code, pd.concat(frames, ignore_index=True), end
    )


class FetchTimeout(TimeoutError):
    """One data-source attempt exceeded its time budget."""


class StockTimeout(TimeoutError):
    """One stock exceeded its total data-processing budget."""


@contextmanager
def time_limit(seconds: float | None, label: str):
    """Interrupt a blocking source call on Unix runners.

    GitHub's hosted Ubuntu runner is Unix, so SIGALRM gives us a real per-call
    timeout even when an upstream library does not expose a requests timeout.
    On platforms without SIGALRM, the source function's own timeout behavior
    remains in effect.
    """
    if seconds is None or seconds <= 0 or not hasattr(signal, "SIGALRM") or (
        threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def handler(signum, frame):
        raise FetchTimeout(f"{label} timed out after {seconds:.1f}s")

    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(
                signal.ITIMER_REAL, previous_timer[0], previous_timer[1]
            )


def remaining_budget(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise StockTimeout("stock total timeout exceeded")
    return remaining


def sleep_with_budget(seconds: float, deadline: float | None):
    if seconds <= 0:
        return
    remaining = remaining_budget(deadline)
    if remaining is not None and seconds >= remaining:
        time.sleep(max(0, remaining))
        raise StockTimeout("stock total timeout exceeded during retry backoff")
    time.sleep(seconds)


def save_progress(root: Path, *, index: int, total: int, stock: dict,
                  stage: str, started_monotonic: float, status: str = "running",
                  error: str = ""):
    """Write a lightweight local progress marker for post-run diagnostics."""
    payload = {
        "updated_at_hkt": datetime.now(HKT).strftime("%Y-%m-%d %H:%M:%S %z"),
        "index": index,
        "total": total,
        "code": stock["code"],
        "name": stock["name"],
        "stage": stage,
        "status": status,
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 2),
        "error": error,
    }
    atomic_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        root / "data" / "run_progress.json",
    )


def download(code: str, end: date, attempts: int, delay: float, source_func,
             *, source_name: str, timeout: float, deadline: float | None = None):
    for idx in range(attempts):
        budget = remaining_budget(deadline)
        attempt_timeout = timeout if budget is None else min(timeout, budget)
        started = time.monotonic()
        print(
            f"        [{source_name}] attempt {idx + 1}/{attempts} START "
            f"timeout={attempt_timeout:.1f}s",
            flush=True,
        )
        try:
            with time_limit(attempt_timeout, f"{source_name}:{code}"):
                raw = source_func(symbol=code, adjust="")
            normalized = normalize(raw, end)
            if getattr(raw, "attrs", None):
                normalized.attrs.update(raw.attrs)
            print(
                f"        [{source_name}] attempt {idx + 1}/{attempts} OK "
                f"rows={len(normalized)} elapsed={time.monotonic() - started:.1f}s",
                flush=True,
            )
            return normalized
        except StockTimeout:
            raise
        except FetchTimeout as exc:
            print(
                f"        [{source_name}] attempt {idx + 1}/{attempts} TIMEOUT "
                f"elapsed={time.monotonic() - started:.1f}s",
                flush=True,
            )
            last_exc = exc
        except Exception as exc:
            print(
                f"        [{source_name}] attempt {idx + 1}/{attempts} FAIL "
                f"{type(exc).__name__}: {str(exc)[:130]} "
                f"elapsed={time.monotonic() - started:.1f}s",
                flush=True,
            )
            last_exc = exc

        if idx + 1 == attempts:
            raise last_exc

        wait = delay * (idx + 1) + random.uniform(0, .8)
        sleep_with_budget(wait, deadline)

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
    """全量候选排名：双重低位 -> 仅当年低位 -> 仅上年低位。

    三组互斥，杜绝重复计数。连续3日以上收阴只影响组内名次，
    不作为入选门槛；组内其次按照相应年度价格区间位置升序。
    """
    both = [r for r in records if r["previous_year_low"] and r["current_year_low"]]
    current_only = [r for r in records if r["current_year_low"] and not r["previous_year_low"]]
    previous_only = [r for r in records if r["previous_year_low"] and not r["current_year_low"]]

    both.sort(key=lambda r: (
        r["bearish_streak"] < 3,
        r["current_year_range"]["position_pct"],
        r["previous_year_range"]["position_pct"], r["code"]))
    current_only.sort(key=lambda r: (
        r["bearish_streak"] < 3, r["current_year_range"]["position_pct"], r["code"]))
    previous_only.sort(key=lambda r: (
        r["bearish_streak"] < 3, r["previous_year_range"]["position_pct"], r["code"]))

    ranked = both + current_only + previous_only
    for number, r in enumerate(ranked, start=1):
        previous_year = r.get("prior_year", 2025)
        current_year = previous_year + 1
        r["rank"] = number
        r["bucket"] = (
            f"{previous_year}与{current_year}双重低位" if r["previous_year_low"] and r["current_year_low"]
            else f"仅{current_year}年低位" if r["current_year_low"]
            else f"仅{previous_year}年低位")
    return both, current_only, previous_only, ranked

def public_stock(r):
    return {k: v for k, v in r.items() if k != "error"}



def format_rank(r):
    py = r["previous_year_range"] or {}
    cy = r["current_year_range"] or {}

    def position_str(rec):
        return (str(rec.get("position_pct")) + "%") if rec.get("position_pct") is not None else "—"

    return (f"| {r['rank']} | {r['code']} {r['name']} | {r['close']:.3f} | "
            f"{py.get('low', '—')} | {position_str(py)} | "
            f"{cy.get('low', '—')} | {position_str(cy)} | "
            f"{r['return_120_sessions_pct']}% | {r['bearish_streak']} | {r['falling_close_streak']} |")


def make_markdown(report):
    meta = report["meta"]
    records = report["stocks"]
    both = report["double_low_candidates"]
    current_only = report["current_year_only_candidates"]
    previous_only = report["previous_year_only_candidates"]
    ranked = report["ranked_candidates"]
    year = int(meta["target_session_hkt"][:4])
    previous_year = year - 1

    lines = [
        "# 港股143只历史低位筛选：全部合格股票排序",
        "",
        f"- **交易日：{meta['target_session_hkt']}（香港）**；生成时间：{meta['generated_at_hkt']}",
        f"- **数据源**：{SOURCE}",
        f"- **覆盖**：{meta['fresh_count']}/143只包含目标日有效日线；{meta['stale_count']}只陈旧，{meta['missing_count']}只缺失；{meta['download_failures']}只本轮请求失败",
        f"- **历史缺口扫描**：发现{meta['initial_gap_stocks']}只存在内部交易日缺口；主源重抓{meta['primary_refetch_stocks']}只；备用源核验/补缺{meta['secondary_refetch_stocks']}只；最终仍有缺口{meta['unresolved_gap_stocks']}只（{meta['unresolved_gap_sessions']}个交易日）。",
        f"- **缺口修复**：主源新增/修复{meta['repaired_primary_sessions']}个交易日；东方财富备用源补入{meta['repaired_secondary_sessions']}个交易日；主备价格冲突涉及{meta['source_conflict_stocks']}只。",
        f"- **故障处理**：{meta['skipped_stocks']}只因处理异常跳过；{meta['timeout_stocks']}只触发单股超时；故障股票不会阻塞后续股票。",
        f"- **运行状态**：{meta['status']}；覆盖不完整时不冒充143只全量排名。",
        f"- **合格总数：{len(ranked)}只**，其中双重低位{len(both)}只、仅{year}年低位{len(current_only)}只、仅{previous_year}年低位{len(previous_only)}只。",
        f"- **入选规则**：只使用不复权OHLC价格；120交易日跌幅≤-20%；区间位置≤20%且距对应年度最低点≤15%；要求年度行情完整。",
        f"- **排名规则**：{previous_year}和{year}双重低位 → 仅{year}年低位 → 仅{previous_year}年低位；组内先看连续收阴≥3日，再按该组主要年度价格位置从低到高排序；**不限制名额**。",
        "- **排名性质**：低位价格形态匹配榜（未经样本外概率验证），不显示虚构反弹概率。",
        "- **风险提示**：破年低或持续收阴不表示已经形成支撑，也不是买入信号。",
        "",
    ]

    header = "| 总排名 | 股票 | 最新收盘价 | 上年最低价 | 上年区间位置 | 当年最低价 | 当年区间位置 | 120日涨跌 | 连阴天数 | 连续收盘下跌 |"
    separator = "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    for heading, group in [
        (f"{previous_year}与{year}年同时处于低位", both),
        (f"仅{year}年处于低位", current_only),
        (f"仅{previous_year}年处于低位", previous_only),
    ]:
        lines += [f"## {heading}（{len(group)}只）", ""]
        if group:
            lines += [header, separator]
            lines += [format_rank(r) for r in group]
        else:
            lines.append("该组没有符合条件的股票。")
        lines.append("")

    lines += ["## 数据完整性", "",
              "以下股票缺少目标日收盘K线，不能参与当日排名；不能用旧收盘价代替：", ""]
    excluded = [r for r in records if r["data_status"] != "fresh"]
    if excluded:
        lines += [f"- {r['code']} {r['name']}：最后K线 {r['latest_date'] or '无'}；{r['data_status']}" for r in excluded]
    else:
        lines.append("- 所有股票的目标交易日均有日线（不代表每个年度的日线都完整）。")
    gap_records = [r for r in records if r.get("initial_gap_count", 0) or r.get("history_gap_count", 0)]
    if gap_records:
        lines += ["", "### 历史缺口修复明细", ""]
        for r in gap_records:
            initial = ", ".join(r.get("initial_gap_dates", [])) or "无"
            remaining = ", ".join(r.get("history_gap_dates", [])) or "无"
            lines.append(
                f"- {r['code']} {r['name']}：初始缺口{r.get('initial_gap_count', 0)}个；"
                f"主源重抓={'是' if r.get('primary_refetched') else '否'}；"
                f"备用源={'是' if r.get('secondary_refetched') else '否'}；"
                f"修复后剩余{r.get('history_gap_count', 0)}个；"
                f"初始日期：{initial}；剩余日期：{remaining}"
            )
    lines += ["", "## 说明", "",
              "年度完整性不满足的股票不会被认证为该年度低位；不复权历史价格可能受供股、拆合股等公司行为影响。",
              "出现超过45%隔夜价格跳变者会暂时排除以待核验。此排名不等于未来20日的反弹概率，也不是交易指令。",
              ""]
    return "\n".join(lines)

def run(root: Path, stocks, asof: date, calendar, *, attempts: int,
        delay: float, fetch_func, secondary_fetch_func=None, sleep=True,
        source_timeout: float = 20.0, stock_timeout: float = 75.0,
        secondary_attempts: int = 1):
    """Download 143 stocks with bounded per-source/per-stock work.

    Repair order:
      1) inspect local history for missing HKEX sessions;
      2) if target day or any internal session is missing, refetch Sina/AKShare;
      3) if gaps remain, query independent Eastmoney data;
      4) fill only missing dates from Eastmoney; existing primary bars win;
      5) unresolved gaps remain explicitly marked and are never synthesized.

    A bad/slow ticker is recorded and skipped; it can never stop later tickers.
    """
    if source_timeout <= 0 or stock_timeout <= 0:
        raise ValueError("source_timeout and stock_timeout must be > 0")
    if attempts < 1 or secondary_attempts < 1:
        raise ValueError("attempts and secondary_attempts must be >= 1")

    started_at = datetime.now(HKT)
    errors = []
    results = []
    timeout_stocks = 0
    skipped_stocks = 0
    total = len(stocks)

    print(
        f"RUN START target={asof} stocks={total} "
        f"source_timeout={source_timeout:.0f}s stock_timeout={stock_timeout:.0f}s "
        f"primary_attempts={attempts} secondary_attempts={secondary_attempts}",
        flush=True,
    )

    for i, stock in enumerate(stocks, start=1):
        code = stock["code"]
        stock_started = time.monotonic()
        deadline = stock_started + stock_timeout
        print(
            f"[{i}/{total}] START {code} {stock['name']} "
            f"budget={stock_timeout:.0f}s",
            flush=True,
        )
        save_progress(
            root, index=i, total=total, stock=stock, stage="start",
            started_monotonic=stock_started,
        )

        prior = pd.DataFrame(columns=FIELDS)
        initial_gaps = []
        history = prior
        primary_refetched = False
        secondary_refetched = False
        primary_added = 0
        secondary_added = 0
        secondary_source_used = None
        conflicts = []
        error_messages = []
        processing_status = "processed"
        skip_reason = ""
        timeout_hit = False

        try:
            prior = read_history(root, code, asof)
            history = prior
            initial_gaps = find_history_gaps(prior, calendar, asof)
            last = date.fromisoformat(prior["date"].iloc[-1]) if not prior.empty else None
            needs_primary_refresh = prior.empty or last != asof or bool(initial_gaps)
            save_progress(
                root, index=i, total=total, stock=stock, stage="local_check",
                started_monotonic=stock_started,
            )

            if needs_primary_refresh:
                remaining_budget(deadline)
                print(
                    f"    PRIMARY needed reason="
                    f"{'no_cache' if prior.empty else 'target_missing' if last != asof else 'history_gap'}",
                    flush=True,
                )
                save_progress(
                    root, index=i, total=total, stock=stock, stage="primary_fetch",
                    started_monotonic=stock_started,
                )
                try:
                    fetched = download(
                        code, asof, attempts, delay, fetch_func,
                        source_name="Sina/AKShare", timeout=source_timeout,
                        deadline=deadline,
                    )
                    primary_added = count_new_dates(history, fetched)
                    history = save_history(root, code, history, fetched, asof)
                    primary_refetched = True
                    save_progress(
                        root, index=i, total=total, stock=stock, stage="primary_saved",
                        started_monotonic=stock_started,
                    )
                    print(
                        f"    PRIMARY saved new_sessions={primary_added}",
                        flush=True,
                    )
                except StockTimeout:
                    raise
                except Exception as exc:
                    msg = f"primary {type(exc).__name__}: {str(exc)[:150]}"
                    error_messages.append(msg)
                    errors.append({"code": code, "name": stock["name"], "error": msg})
                    save_progress(
                        root, index=i, total=total, stock=stock, stage="primary_failed",
                        started_monotonic=stock_started, error=msg,
                    )
                    print(f"    PRIMARY exhausted -> {msg}", flush=True)
                if sleep and delay > 0:
                    sleep_with_budget(
                        delay + random.uniform(0, 0.6), deadline
                    )

            remaining_gaps = find_history_gaps(history, calendar, asof)
            history_latest = (
                date.fromisoformat(history["date"].iloc[-1])
                if not history.empty else None
            )
            needs_secondary = bool(
                secondary_fetch_func is not None
                and (remaining_gaps or history_latest != asof)
            )

            if needs_secondary:
                remaining_budget(deadline)
                print(
                    f"    SECONDARY needed target_or_gaps_missing="
                    f"{history_latest != asof or bool(remaining_gaps)} "
                    f"unresolved_gaps={len(remaining_gaps)}",
                    flush=True,
                )
                save_progress(
                    root, index=i, total=total, stock=stock, stage="secondary_fetch",
                    started_monotonic=stock_started,
                )
                try:
                    secondary = download(
                        code, asof, secondary_attempts, delay,
                        secondary_fetch_func, source_name="Eastmoney",
                        timeout=source_timeout, deadline=deadline,
                    )
                    conflicts = source_conflicts(history, secondary)
                    before = set(history["date"].astype(str)) if not history.empty else set()
                    history, _ = fill_missing_history(
                        history, secondary, calendar, asof
                    )
                    after = set(history["date"].astype(str)) if not history.empty else set()
                    secondary_added = len(after - before)
                    history = persist_history(root, code, history, asof)
                    secondary_refetched = True
                    secondary_source_used = getattr(secondary, "attrs", {}).get("source")
                    save_progress(
                        root, index=i, total=total, stock=stock, stage="secondary_saved",
                        started_monotonic=stock_started,
                    )
                    print(
                        f"    SECONDARY saved new_sessions={secondary_added} "
                        f"conflicts={len(conflicts)}",
                        flush=True,
                    )
                except StockTimeout:
                    raise
                except Exception as exc:
                    msg = f"secondary {type(exc).__name__}: {str(exc)[:150]}"
                    error_messages.append(msg)
                    errors.append({"code": code, "name": stock["name"], "error": msg})
                    save_progress(
                        root, index=i, total=total, stock=stock, stage="secondary_failed",
                        started_monotonic=stock_started, error=msg,
                    )
                    print(f"    SECONDARY exhausted -> {msg}", flush=True)
                if sleep and delay > 0:
                    sleep_with_budget(
                        delay + random.uniform(0, 0.6), deadline
                    )

            remaining_gaps = find_history_gaps(history, calendar, asof)
            remaining_budget(deadline)
            save_progress(
                root, index=i, total=total, stock=stock, stage="screen",
                started_monotonic=stock_started,
            )
            error = "; ".join(error_messages)
            result = calc_stock(stock, history, asof, calendar, error)
        except StockTimeout as exc:
            timeout_hit = True
            processing_status = "skipped"
            skip_reason = str(exc)
            timeout_stocks += 1
            msg = f"stock timeout: {skip_reason}"
            error_messages.append(msg)
            errors.append({"code": code, "name": stock["name"], "error": msg})
            print(f"    SKIP TIMEOUT -> {msg}", flush=True)
            error = "; ".join(error_messages)
            result = calc_stock(stock, history, asof, calendar, error)
            remaining_gaps = find_history_gaps(history, calendar, asof)
        except Exception as exc:
            processing_status = "skipped"
            skip_reason = f"{type(exc).__name__}: {str(exc)[:150]}"
            skipped_stocks += 1
            msg = f"stock processing error: {skip_reason}"
            error_messages.append(msg)
            errors.append({"code": code, "name": stock["name"], "error": msg})
            print(f"    SKIP ERROR -> {msg}", flush=True)
            error = "; ".join(error_messages)
            result = calc_stock(stock, history, asof, calendar, error)
            remaining_gaps = find_history_gaps(history, calendar, asof)

        if (
            processing_status == "processed"
            and (
                history.empty
                or date.fromisoformat(history["date"].iloc[-1]) != asof
            )
        ):
            processing_status = "skipped"
            skip_reason = "目标交易日数据在主源与备用源处理后仍不可用"
            skipped_stocks += 1
            msg = f"data unavailable: {skip_reason}"
            if msg not in errors:
                errors.append({"code": code, "name": stock["name"], "error": msg})
            result["reasons"].append(skip_reason)

        result.update({
            "initial_gap_count": len(initial_gaps),
            "initial_gap_dates": initial_gaps[:20],
            "history_gap_count": len(remaining_gaps),
            "history_gap_dates": remaining_gaps[:20],
            "primary_refetched": primary_refetched,
            "primary_added_sessions": primary_added,
            "secondary_refetched": secondary_refetched,
            "secondary_added_sessions": secondary_added,
            "source_conflict_count": len(conflicts),
            "source_conflicts": conflicts,
            "secondary_source": secondary_source_used,
            "history_source": (
                "sina_akshare"
                if primary_refetched and not secondary_refetched
                else f"{secondary_source_used}_backup"
                if secondary_refetched and not primary_refetched
                else f"sina_akshare+{secondary_source_used}_backup"
                if primary_refetched and secondary_refetched
                else "local_cache"
            ),
            "processing_status": processing_status,
            "skip_reason": skip_reason,
            "timeout_hit": timeout_hit,
            "elapsed_seconds": round(time.monotonic() - stock_started, 2),
        })
        if remaining_gaps:
            result["reasons"].append(
                f"历史交易日仍缺失{len(remaining_gaps)}个，未使用旧价或人工补造"
            )
        if conflicts:
            result["reasons"].append(
                f"主备数据存在{len(conflicts)}个价格差异，保留主源数据并记录冲突"
            )
        results.append(result)

        status = result["data_status"]
        print(
            f"[{i}/{total}] DONE {code} {stock['name']} | "
            f"{status} | last={result['latest_date']} | "
            f"gaps={len(initial_gaps)}->{len(remaining_gaps)} | "
            f"primary={primary_refetched} secondary={secondary_refetched} | "
            f"status={processing_status} elapsed={result['elapsed_seconds']:.1f}s"
            + (f" | {error}" if error else ""),
            flush=True,
        )
        save_progress(
            root, index=i, total=total, stock=stock, stage="done",
            started_monotonic=stock_started, status=processing_status,
            error=error,
        )

    both, current_only, previous_only, ranked = prioritize(results)
    fresh = sum(r["data_status"] == "fresh" for r in results)
    stale = sum(r["data_status"] == "stale" for r in results)
    missing = sum(r["data_status"] == "missing" for r in results)
    initial_gap_stocks = sum(r["initial_gap_count"] > 0 for r in results)
    primary_refetch_stocks = sum(r["primary_refetched"] for r in results)
    secondary_refetch_stocks = sum(r["secondary_refetched"] for r in results)
    repaired_primary_sessions = sum(r["primary_added_sessions"] for r in results)
    repaired_secondary_sessions = sum(r["secondary_added_sessions"] for r in results)
    unresolved_gap_stocks = sum(r["history_gap_count"] > 0 for r in results)
    unresolved_gap_sessions = sum(r["history_gap_count"] for r in results)
    source_conflict_stocks = sum(r["source_conflict_count"] > 0 for r in results)

    meta = {
        "generated_at_hkt": started_at.strftime("%Y-%m-%d %H:%M:%S %z"),
        "target_session_hkt": asof.isoformat(),
        "price_type": "unadjusted", "currency": "HKD",
        "source": SOURCE, "secondary_source": SECONDARY_SOURCE,
        "total": len(stocks), "fresh_count": fresh, "stale_count": stale,
        "missing_count": missing, "download_failures": len(errors),
        "status": "complete_date_coverage" if fresh == len(stocks) else ("partial" if fresh else "unavailable"),
        "candidate_count": len(ranked),
        "double_low_count": len(both),
        "current_year_only_count": len(current_only),
        "previous_year_only_count": len(previous_only),
        "initial_gap_stocks": initial_gap_stocks,
        "primary_refetch_stocks": primary_refetch_stocks,
        "secondary_refetch_stocks": secondary_refetch_stocks,
        "repaired_primary_sessions": repaired_primary_sessions,
        "repaired_secondary_sessions": repaired_secondary_sessions,
        "unresolved_gap_stocks": unresolved_gap_stocks,
        "unresolved_gap_sessions": unresolved_gap_sessions,
        "source_conflict_stocks": source_conflict_stocks,
        "skipped_stocks": skipped_stocks,
        "timeout_stocks": timeout_stocks,
        "history_gap_policy": "scan HKEX sessions from first available bar through target session",
        "repair_policy": "Sina/AKShare retry first; direct Eastmoney backup fills only unresolved dates",
        "ranking_method": "all_candidates_both_current_previous_not_validated_probability",
        "warning": "Never interpret rankings as calibrated rebound probabilities; provider may lag/suspend/correct prices."
    }
    report = {
        "meta": meta,
        "double_low_candidates": [public_stock(r) for r in both],
        "current_year_only_candidates": [public_stock(r) for r in current_only],
        "previous_year_only_candidates": [public_stock(r) for r in previous_only],
        "ranked_candidates": [public_stock(r) for r in ranked],
        "previous_year_candidates": [public_stock(r) for r in both + previous_only],
        "current_year_candidates": [public_stock(r) for r in both + current_only],
        "stocks": [public_stock(r) for r in results], "errors": errors}
    data_dir = root / "data"
    atomic_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", data_dir / "latest.json")
    atomic_text(make_markdown(report), data_dir / "latest.md")
    fields = (
        "code", "name", "latest_date", "close", "data_status",
        "return_120_sessions_pct", "bearish_streak", "falling_close_streak",
        "previous_year_low", "current_year_low", "prior_year_full",
        "current_year_full_from_start", "initial_gap_count", "history_gap_count",
        "primary_refetched", "secondary_refetched", "secondary_added_sessions",
        "secondary_source", "source_conflict_count", "history_source", "processing_status",
        "skip_reason", "timeout_hit", "elapsed_seconds"
    )
    atomic_csv(
        pd.DataFrame([{field: r.get(field) for field in fields} for r in results]),
        data_dir / "覆盖与评分.csv"
    )
    ranked_fields = ("rank", "bucket", "code", "name", "latest_date", "close",
                     "previous_year_low", "current_year_low",
                     "prior_year_low_price", "prior_year_low_date", "prior_year_position_pct",
                     "current_year_low_price", "current_year_low_date", "current_year_position_pct",
                     "return_120_sessions_pct", "bearish_streak", "falling_close_streak")
    ranked_rows = []
    for r in ranked:
        py = r["previous_year_range"] or {}
        cy = r["current_year_range"] or {}
        ranked_rows.append({
            "rank": r["rank"], "bucket": r["bucket"],
            "code": r["code"], "name": r["name"], "latest_date": r["latest_date"],
            "close": r["close"], "previous_year_low": r["previous_year_low"],
            "current_year_low": r["current_year_low"],
            "prior_year_low_price": py.get("low"), "prior_year_low_date": py.get("low_date"),
            "prior_year_position_pct": py.get("position_pct"),
            "current_year_low_price": cy.get("low"), "current_year_low_date": cy.get("low_date"),
            "current_year_position_pct": cy.get("position_pct"),
            "return_120_sessions_pct": r["return_120_sessions_pct"],
            "bearish_streak": r["bearish_streak"],
            "falling_close_streak": r["falling_close_streak"],
        })
    atomic_csv(pd.DataFrame(ranked_rows, columns=ranked_fields), data_dir / "全部低位候选.csv")
    obsolete = data_dir / "低位前20.csv"
    if obsolete.exists():
        obsolete.unlink()
    dated = data_dir / "reports" / f"{asof.isoformat()}.md"
    atomic_text(make_markdown(report), dated)
    print(
        f"REPORT target={asof} fresh={fresh}/{len(stocks)} stale={stale} "
        f"missing={missing} errors={len(errors)} ranked={len(ranked)} "
        f"gaps={initial_gap_stocks} primary_refetch={primary_refetch_stocks} "
        f"secondary_refetch={secondary_refetch_stocks} unresolved={unresolved_gap_stocks}"
    )
    return report


def main():
    p = argparse.ArgumentParser(description="HK143 cloud daily screening; pure unadjusted price")
    p.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    p.add_argument("--asof", help="Override expected HK market session YYYY-MM-DD (for testing/recovery)")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--attempts", type=int, default=2)
    p.add_argument("--secondary-attempts", type=int, default=1)
    p.add_argument("--source-timeout", type=float, default=20.0)
    p.add_argument("--stock-timeout", type=float, default=75.0)
    args = p.parse_args()
    if args.delay < 0 or args.attempts < 1 or args.secondary_attempts < 1:
        p.error("--delay must be >= 0; attempts and secondary-attempts must be >= 1")
    if args.source_timeout <= 0 or args.stock_timeout <= 0:
        p.error("--source-timeout and --stock-timeout must be > 0")
    cal = hk_calendar()
    now = datetime.now(HKT)
    cutoff = date.fromisoformat(args.asof) if args.asof else expected_session(now, cal)
    if not cal.is_session(pd.Timestamp(cutoff)):
        p.error(f"not an XHKG trading session: {cutoff}")
    root = args.output.resolve()
    stocks = load_stocks(root / "config" / "stocks.csv")
    import akshare as ak

    def primary_fetch(symbol, adjust):
        return ak.stock_hk_daily(symbol=symbol, adjust=adjust)

    def secondary_fetch(symbol, adjust):
        if adjust != "":
            raise ValueError("backup sources support only unadjusted data")
        return fetch_backup_hk_daily(symbol, cutoff)

    run(
        root, stocks, cutoff, cal,
        attempts=args.attempts,
        delay=args.delay,
        fetch_func=primary_fetch,
        secondary_fetch_func=secondary_fetch,
        source_timeout=args.source_timeout,
        stock_timeout=args.stock_timeout,
        secondary_attempts=args.secondary_attempts,
    )


if __name__ == "__main__":
    main()
