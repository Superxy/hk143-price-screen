# -*- coding: utf-8 -*-
"""HK143 cloud downloader / price-only screen.

Data: daily unadjusted HK OHLC. Normal run uses batch realtime quotes (Sina -> Eastmoney -> Tencent), then appends the target session to local CSVs. Historical sources are contacted only for detected local gaps.
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
import threading
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
SOURCE = "Batch realtime: Sina -> Eastmoney -> Tencent | unadjusted HKD OHLC"
SINA_HK_SPOT_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/" "Market_Center.getHKStockData")
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
TENCENT_KLINE_URLS = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get")
TENCENT_BATCH_SIZE = 60
TENCENT_KLINE_COUNT = 640


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


def normalize_hk_code(value) -> str:
    s = str(value).strip().upper().replace(".HK", "")
    if s.startswith("HK"):
        s = s[2:]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s.zfill(5) if s.isdigit() else s


def call_with_timeout(func, timeout: float, label: str):
    box = {}

    def worker():
        try:
            box["result"] = func()
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=worker, name=f"provider-{label}", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"{label} timeout after {timeout:.1f}s")
    if "error" in box:
        raise box["error"]
    if "result" not in box:
        raise RuntimeError(f"{label} returned no result")
    return box["result"]


def _numeric(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def _valid_bar(row) -> bool:
    values = [_numeric(row.get(k)) for k in ("open", "high", "low", "close")]
    if not all(pd.notna(values)):
        return False
    op, hi, lo, cl = values
    return min(op, hi, lo, cl) > 0 and hi + 1e-9 >= max(op, lo, cl) and lo - 1e-9 <= min(op, hi, cl)


def _standardize_quote_rows(rows, asof: date, source: str) -> pd.DataFrame:
    if not rows:
        raise ValueError(f"{source} returned no valid HK quotes")
    frame = pd.DataFrame(rows)
    required = ["code", "name", "open", "high", "low", "close", "volume", "amount", "date"]
    missing = [c for c in required if c not in frame.columns]
    if missing:
        raise ValueError(f"{source} standardized fields missing: {missing}")
    frame["code"] = frame["code"].map(normalize_hk_code)
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    for field in required[2:8]:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    frame = frame[frame["date"] == asof].copy()
    frame = frame[frame.apply(_valid_bar, axis=1)]
    frame = frame.drop_duplicates("code", keep="last")
    if frame.empty:
        raise ValueError(f"{source} has no usable quotes for {asof}")
    frame.attrs["source"] = source
    return frame.reset_index(drop=True)


def fetch_sina_realtime(codes, asof: date, timeout: float) -> pd.DataFrame:
    params = {"page": "1", "num": "3000", "sort": "symbol", "asc": "1", "node": "qbgg_hk", "_s_r_a": "page"}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://vip.stock.finance.sina.com.cn/"}
    response = requests.get(SINA_HK_SPOT_URL, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    wanted = set(codes)
    rows = []
    for item in payload or []:
        code = normalize_hk_code(item.get("symbol") or item.get("code"))
        if code not in wanted:
            continue
        rows.append({
            "code": code, "name": item.get("name") or item.get("中文名称") or "",
            "open": item.get("open"), "high": item.get("high"), "low": item.get("low"),
            "close": item.get("lasttrade"),
            "volume": _numeric(item.get("volume")) * 10000,
            "amount": _numeric(item.get("amount")) * 10000,
            "date": str(item.get("ticktime") or item.get("日期时间") or "")[:10],
        })
    return _standardize_quote_rows(rows, asof, "sina")


def fetch_eastmoney_realtime(ak, codes, asof: date) -> pd.DataFrame:
    raw = ak.stock_hk_spot_em()
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        raise ValueError("Eastmoney HK realtime returned empty table")
    wanted = set(codes)
    rows = []
    for _, item in raw.iterrows():
        code = normalize_hk_code(item.get("代码"))
        if code not in wanted:
            continue
        rows.append({
            "code": code, "name": item.get("名称", ""),
            "open": item.get("今开"), "high": item.get("最高"), "low": item.get("最低"),
            "close": item.get("最新价"), "volume": item.get("成交量"), "amount": item.get("成交额"),
            "date": asof.isoformat(),
        })
    return _standardize_quote_rows(rows, asof, "eastmoney")


def fetch_tencent_realtime(codes, asof: date, timeout: float) -> pd.DataFrame:
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"}
    wanted = set(codes)
    rows = []
    for start in range(0, len(codes), TENCENT_BATCH_SIZE):
        batch = codes[start:start + TENCENT_BATCH_SIZE]
        url = TENCENT_QUOTE_URL + ",".join(f"hk{normalize_hk_code(code)}" for code in batch)
        response = requests.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
        response.encoding = "gbk"
        for line in response.text.split(";"):
            if "=" not in line or '"' not in line:
                continue
            try:
                fields = line.split('"', 2)[1].split("~")
                if len(fields) < 35:
                    continue
                code = normalize_hk_code(fields[2])
                if code not in wanted:
                    continue
                quote_date = datetime.strptime(fields[30], "%Y/%m/%d %H:%M:%S").date()
                if quote_date != asof:
                    continue
                rows.append({
                    "code": code, "name": fields[1], "open": fields[5], "high": fields[33],
                    "low": fields[34], "close": fields[3], "volume": fields[6],
                    "amount": fields[37] if len(fields) > 37 else float("nan"),
                    "date": quote_date.isoformat(),
                })
            except (ValueError, IndexError):
                continue
    return _standardize_quote_rows(rows, asof, "tencent")


def batch_today_quotes(stocks, asof: date, ak, attempts: int, secondary_attempts: int,
                       source_timeout: float, delay: float):
    remaining = [s["code"] for s in stocks]
    quotes, source_used, source_errors = {}, {}, []
    providers = [
        ("sina", lambda codes: fetch_sina_realtime(codes, asof, source_timeout), attempts),
        ("eastmoney", lambda codes: fetch_eastmoney_realtime(ak, codes, asof), secondary_attempts),
        ("tencent", lambda codes: fetch_tencent_realtime(codes, asof, source_timeout), secondary_attempts),
    ]
    for source, fetcher, source_attempts in providers:
        if not remaining:
            break
        for attempt in range(1, max(1, source_attempts) + 1):
            try:
                print(f"[TODAY] {source.upper()} batch START missing={len(remaining)} attempt={attempt}/{source_attempts}", flush=True)
                raw = call_with_timeout(lambda: fetcher(list(remaining)), source_timeout, source)
                for _, row in raw.iterrows():
                    code = normalize_hk_code(row["code"])
                    if code in remaining:
                        quotes[code] = row.to_dict()
                        source_used[code] = source
                remaining = [code for code in remaining if code not in quotes]
                print(f"[TODAY] {source.upper()} batch OK recovered={len(source_used)} remaining={len(remaining)}", flush=True)
                break
            except Exception as exc:
                source_errors.append({"source": source, "attempt": attempt,
                                      "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
                print(f"[TODAY] {source.upper()} FAIL {type(exc).__name__}: {str(exc)[:180]}", flush=True)
                if attempt < max(1, source_attempts) and delay > 0:
                    time.sleep(delay)
        if remaining:
            print(f"[TODAY] {source.upper()} -> next source; remaining={len(remaining)}", flush=True)
    return quotes, source_used, remaining, source_errors


def find_history_gaps(history: pd.DataFrame, asof: date, calendar):
    if history.empty:
        return ["NO_HISTORY"]
    existing = set(history["date"].astype(str))
    first = max(min(date.fromisoformat(s) for s in existing), date(START_YEAR, 1, 1))
    expected = calendar.sessions_in_range(pd.Timestamp(first), pd.Timestamp(asof))
    return [d.date().isoformat() for d in expected if d.date().isoformat() not in existing]


def fetch_sina_history(ak, code):
    return ak.stock_hk_daily(symbol=code, adjust="")


def fetch_eastmoney_history(ak, code, start_date, end_date):
    return ak.stock_hk_hist(symbol=code, period="daily",
                            start_date=start_date.strftime("%Y%m%d"),
                            end_date=end_date.strftime("%Y%m%d"), adjust="")


def fetch_tencent_history(code, timeout: float):
    code = normalize_hk_code(code)
    last_error = None
    for url in TENCENT_KLINE_URLS:
        try:
            response = requests.get(url, params={"param": f"hk{code},day,,,{TENCENT_KLINE_COUNT},"},
                                     headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            data = (payload.get("data") or {}).get(f"hk{code}") or {}
            rows = data.get("day") or []
            if not rows:
                raise ValueError(f"Tencent returned no day rows for {code}")
            frame = pd.DataFrame([row[:6] for row in rows if len(row) >= 6],
                                 columns=["date", "open", "close", "high", "low", "volume"])
            frame["amount"] = float("nan")
            return frame
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError(f"Tencent history failed for {code}")


def repair_history_for_stock(stock, history, asof, calendar, ak, attempts,
                             secondary_attempts, source_timeout, stock_timeout, root, delay):
    gaps = find_history_gaps(history, asof, calendar)
    if not gaps:
        return history, [], None, []
    if gaps == ["NO_HISTORY"]:
        start_date, required = date(START_YEAR, 1, 1), None
    else:
        start_date, required = min(date.fromisoformat(g) for g in gaps), set(gaps)
    sources = [
        ("sina", lambda: fetch_sina_history(ak, stock["code"]), attempts),
        ("eastmoney", lambda: fetch_eastmoney_history(ak, stock["code"], start_date, asof), secondary_attempts),
        ("tencent", lambda: fetch_tencent_history(stock["code"], source_timeout), secondary_attempts),
    ]
    errors = []
    for source, fetcher, source_attempts in sources:
        for attempt in range(1, max(1, source_attempts) + 1):
            try:
                print(f"        [HISTORY] {stock['code']} {source.upper()} START gaps={len(gaps)} attempt={attempt}/{source_attempts}", flush=True)
                fetched = call_with_timeout(fetcher, min(source_timeout, stock_timeout), f"{source}-{stock['code']}")
                normalized = normalize(fetched, asof)
                if required is None:
                    additions = normalized
                else:
                    additions = normalized[normalized["date"].isin(required)].copy()
                if required is not None and not required.intersection(set(normalized["date"])):
                    raise ValueError(f"{source} returned none of the required gap dates")
                if asof.isoformat() in set(history["date"]):
                    additions = additions[additions["date"] != asof.isoformat()]
                if not additions.empty:
                    history = save_history(root, stock["code"], history, additions, asof)
                remaining = find_history_gaps(history, asof, calendar)
                if not remaining:
                    return history, [], source, errors
                gaps = remaining
                break
            except Exception as exc:
                errors.append({"code": stock["code"], "source": source, "attempt": attempt,
                               "error": f"{type(exc).__name__}: {str(exc)[:180]}"})
                print(f"        [HISTORY] {stock['code']} {source.upper()} FAIL {type(exc).__name__}: {str(exc)[:180]}", flush=True)
                if attempt < max(1, source_attempts) and delay > 0:
                    time.sleep(delay)
    return history, gaps, None, errors


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


def calc_stock(stock, history: pd.DataFrame, asof: date, calendar, error: str = "", today_source: str = ""):
    code, name = stock["code"], stock["name"]
    row = {"code": code, "name": name, "source_rank": stock["rank"],
           "latest_date": None, "close": None, "data_status": "missing",
           "today_source": today_source, "error": error, "prior_year": asof.year-1,
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
        f"- **当天行情来源**：Sina {meta.get('today_source_counts', {}).get('sina', 0)}只；东方财富 {meta.get('today_source_counts', {}).get('eastmoney', 0)}只；腾讯 {meta.get('today_source_counts', {}).get('tencent', 0)}只；未解决 {len(meta.get('today_unresolved_codes', []))}只。",
        f"- **历史缺口**：本地扫描发现 {meta.get('history_gap_stocks', 0)}只股票存在缺口，共 {meta.get('history_gap_count', 0)}个缺口；只有这些股票才访问历史数据源。",
        f"- **覆盖**：{meta['fresh_count']}/143只包含目标日有效日线；{meta['stale_count']}只陈旧，{meta['missing_count']}只缺失；{meta['download_failures']}条本轮请求/数据错误记录",
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
    lines += ["", "## 说明", "",
              "年度完整性不满足的股票不会被认证为该年度低位；不复权历史价格可能受供股、拆合股等公司行为影响。",
              "出现超过45%隔夜价格跳变者会暂时排除以待核验。此排名不等于未来20日的反弹概率，也不是交易指令。",
              ""]
    return "\n".join(lines)

def run(root: Path, stocks, asof: date, calendar, *, attempts: int, delay: float,
        fetch_func=None, sleep=True, secondary_attempts: int = 1,
        source_timeout: float = 20.0, stock_timeout: float = 75.0, ak=None):
    started_at = datetime.now(HKT)
    errors = []
    results = []
    today_source_used = {}
    history_repairs = []
    history_gap_stocks = 0
    history_gap_count = 0

    if fetch_func is not None:
        # Backward-compatible offline-test path.
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
            print(f"    {results[-1]['data_status']} | last={results[-1]['latest_date']}" +
                  (f" | {error}" if error else ""), flush=True)
    else:
        if ak is None:
            raise ValueError("production run requires akshare module")
        print(f"[TODAY] 1) Batch realtime download for {len(stocks)} stocks target={asof}", flush=True)
        quotes, today_source_used, unresolved_today, today_errors = batch_today_quotes(
            stocks, asof, ak, attempts, secondary_attempts, source_timeout, delay
        )
        stock_by_code = {s["code"]: s for s in stocks}
        for code in unresolved_today:
            errors.append({"code": code, "name": stock_by_code[code]["name"],
                           "error": "today quote unresolved after Sina -> Eastmoney -> Tencent"})
        errors.extend({"stage": "today_source", **item} for item in today_errors)

        histories = {}
        for i, stock in enumerate(stocks, start=1):
            code = stock["code"]
            history = read_history(root, code, asof)
            quote = quotes.get(code)
            if quote:
                today_bar = pd.DataFrame([{
                    "date": asof.isoformat(), "open": quote["open"], "high": quote["high"],
                    "low": quote["low"], "close": quote["close"],
                    "volume": quote["volume"], "amount": quote["amount"],
                }])
                history = save_history(root, code, history, today_bar, asof)
            histories[code] = history
            print(f"[TODAY {i}/{len(stocks)}] {code} {stock['name']} source={today_source_used.get(code, 'UNRESOLVED')} last={history['date'].iloc[-1] if not history.empty else 'NONE'}", flush=True)

        print("[HISTORY] 2) Local-only scan of all CSV files for missing XHKG sessions", flush=True)
        for i, stock in enumerate(stocks, start=1):
            code = stock["code"]
            history = histories[code]
            gaps = find_history_gaps(history, asof, calendar)
            if not gaps:
                results.append(calc_stock(stock, history, asof, calendar,
                                          today_source=today_source_used.get(code, "")))
                print(f"[HISTORY {i}/{len(stocks)}] {code} no gap -> no history request", flush=True)
                continue

            history_gap_stocks += 1
            history_gap_count += len(gaps) if gaps != ["NO_HISTORY"] else 1
            repaired, remaining, repair_source, repair_errors = repair_history_for_stock(
                stock, history, asof, calendar, ak, attempts, secondary_attempts,
                source_timeout, stock_timeout, root, delay
            )
            histories[code] = repaired
            history_repairs.append({
                "code": code, "name": stock["name"],
                "initial_gap_count": len(gaps) if gaps != ["NO_HISTORY"] else 1,
                "remaining_gap_count": len(remaining),
                "source": repair_source,
            })
            errors.extend({"stage": "history_repair", **item} for item in repair_errors)
            error = f"history gaps remain: {len(remaining)}" if remaining else ""
            if error:
                errors.append({"code": code, "name": stock["name"], "error": error})
            results.append(calc_stock(stock, repaired, asof, calendar, error,
                                      today_source=today_source_used.get(code, "")))
            print(f"[HISTORY {i}/{len(stocks)}] {code} repaired_by={repair_source or 'NONE'} remaining={len(remaining)}", flush=True)

    both, current_only, previous_only, ranked = prioritize(results)
    fresh = sum(r["data_status"] == "fresh" for r in results)
    stale = sum(r["data_status"] == "stale" for r in results)
    missing = sum(r["data_status"] == "missing" for r in results)
    meta = {
        "generated_at_hkt": started_at.strftime("%Y-%m-%d %H:%M:%S %z"),
        "target_session_hkt": asof.isoformat(),
        "price_type": "unadjusted", "currency": "HKD", "source": SOURCE,
        "total": len(stocks), "fresh_count": fresh, "stale_count": stale,
        "missing_count": missing, "download_failures": len(errors),
        "today_source_counts": {source: list(today_source_used.values()).count(source)
                                for source in ("sina", "eastmoney", "tencent")},
        "today_unresolved_codes": unresolved_today if fetch_func is None else [],
        "history_gap_stocks": history_gap_stocks,
        "history_gap_count": history_gap_count,
        "history_repairs": history_repairs,
        "status": "complete_date_coverage" if fresh == len(stocks) else ("partial" if fresh else "unavailable"),
        "candidate_count": len(ranked),
        "double_low_count": len(both),
        "current_year_only_count": len(current_only),
        "previous_year_only_count": len(previous_only),
        "ranking_method": "all_candidates_both_current_previous_not_validated_probability",
        "warning": "Never interpret rankings as calibrated rebound probabilities; provider may lag/suspend/correct prices."
    }
    report = {
        "meta": meta,
        "double_low_candidates": [public_stock(r) for r in both],
        "current_year_only_candidates": [public_stock(r) for r in current_only],
        "previous_year_only_candidates": [public_stock(r) for r in previous_only],
        "ranked_candidates": [public_stock(r) for r in ranked],
        # 保留之前的年度候选字段，供其他研究和外部读取使用；两组会包含交集。
        "previous_year_candidates": [public_stock(r) for r in both + previous_only],
        "current_year_candidates": [public_stock(r) for r in both + current_only],
        "stocks": [public_stock(r) for r in results], "errors": errors}
    data_dir = root / "data"
    atomic_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", data_dir / "latest.json")
    atomic_text(make_markdown(report), data_dir / "latest.md")
    fields = ("code", "name", "latest_date", "close", "data_status", "today_source", "return_120_sessions_pct", "bearish_streak",
              "falling_close_streak", "previous_year_low", "current_year_low", "prior_year_full", "current_year_full_from_start")
    atomic_csv(pd.DataFrame([{field: r[field] for field in fields} for r in results]), data_dir / "覆盖与评分.csv")
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
    # 删除以前带20只上限的旧文件，防止读者误读过期榜单。
    obsolete = data_dir / "低位前20.csv"
    if obsolete.exists():
        obsolete.unlink()
    dated = data_dir / "reports" / f"{asof.isoformat()}.md"
    atomic_text(make_markdown(report), dated)
    print(f"REPORT target={asof} fresh={fresh}/{len(stocks)} stale={stale} missing={missing} errors={len(errors)} ranked={len(ranked)} both={len(both)} current_only={len(current_only)} previous_only={len(previous_only)}")
    return report


def main():
    p = argparse.ArgumentParser(description="HK143 daily incremental downloader and low-position screen")
    p.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    p.add_argument("--asof", help="Override expected HK market session YYYY-MM-DD (for testing/recovery)")
    p.add_argument("--delay", type=float, default=2.0)
    p.add_argument("--attempts", type=int, default=2)
    p.add_argument("--secondary-attempts", type=int, default=1)
    p.add_argument("--source-timeout", type=float, default=20.0)
    p.add_argument("--stock-timeout", type=float, default=75.0)
    args = p.parse_args()
    if args.delay < 0 or args.attempts < 1 or args.secondary_attempts < 1:
        p.error("attempt counts must be >= 1 and --delay must be >= 0")
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
    run(root, stocks, cutoff, cal, attempts=args.attempts, delay=args.delay,
        fetch_func=None, secondary_attempts=args.secondary_attempts,
        source_timeout=args.source_timeout, stock_timeout=args.stock_timeout, ak=ak)


if __name__ == "__main__":
    main()
