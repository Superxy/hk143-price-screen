from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from cloud_run import (
    find_history_gaps,
    hk_calendar,
    normalize,
    read_history,
    save_history,
)

ROOT = Path(__file__).resolve().parent
ASOF = date(2026, 10, 16)

TARGETS = {
    "03908": "中金公司",
    "01357": "美图公司",
    "01428": "耀才证券金融",
}

TENCENT_URLS = (
    "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
)


def fetch_tencent_range(code: str, start: date, end: date) -> pd.DataFrame:
    symbol = f"hk{code}"
    params = {
        "param": f"{symbol},day,{start.isoformat()},{end.isoformat()},640,"
    }
    last_error = None

    for url in TENCENT_URLS:
        try:
            response = requests.get(
                url,
                params=params,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://finance.qq.com/",
                },
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            block = (payload.get("data") or {}).get(symbol) or {}
            rows = block.get("day") or block.get("qfqday") or []
            if not rows:
                raise ValueError(
                    f"Tencent returned no rows for {code} "
                    f"{start}~{end}"
                )

            frame = pd.DataFrame(
                [row[:6] for row in rows if len(row) >= 6],
                columns=["date", "open", "close", "high", "low", "volume"],
            )
            frame["amount"] = float("nan")
            normalized = normalize(frame, ASOF)
            normalized = normalized[
                (normalized["date"] >= start.isoformat())
                & (normalized["date"] <= end.isoformat())
            ].copy()

            if normalized.empty:
                raise ValueError(
                    f"Tencent returned rows but none in requested range "
                    f"{start}~{end}"
                )

            print(
                f"[TENCENT] {code} {start}~{end}: "
                f"returned={len(normalized)}",
                flush=True,
            )
            return normalized

        except Exception as exc:
            last_error = exc
            print(
                f"[TENCENT] {code} {start}~{end} FAIL "
                f"{type(exc).__name__}: {str(exc)[:180]}",
                flush=True,
            )

    raise last_error or RuntimeError(
        f"Tencent failed for {code} {start}~{end}"
    )


def contiguous_ranges(gaps: list[str], calendar) -> list[tuple[date, date]]:
    dates = sorted(date.fromisoformat(x) for x in gaps)
    if not dates:
        return []

    ranges = []
    start = previous = dates[0]
    for current in dates[1:]:
        expected_next = calendar.next_session(
            pd.Timestamp(previous)
        ).date()
        if current != expected_next:
            ranges.append((start, previous))
            start = current
        previous = current
    ranges.append((start, previous))
    return ranges


def repair_one(code: str, name: str, calendar) -> tuple[int, int]:
    history = read_history(ROOT, code, ASOF)
    gaps = find_history_gaps(history, ASOF, calendar)

    if not gaps:
        print(f"[VERIFY] {code} {name}: no gaps", flush=True)
        return 0, 0

    print(
        f"[REPAIR] {code} {name}: gaps={len(gaps)} dates={gaps}",
        flush=True,
    )

    total_added = 0
    for start, end in contiguous_ranges(gaps, calendar):
        fetched = fetch_tencent_range(code, start, end)
        required = set(
            d.date().isoformat()
            for d in calendar.sessions_in_range(
                pd.Timestamp(start), pd.Timestamp(end)
            )
        )
        additions = fetched[fetched["date"].isin(required)].copy()
        missing = sorted(required - set(additions["date"]))
        if missing:
            raise RuntimeError(
                f"{code} Tencent missing required dates: {missing}"
            )

        history = save_history(ROOT, code, history, additions, ASOF)
        total_added += len(additions)

    remaining = find_history_gaps(history, ASOF, calendar)
    print(
        f"[RESULT] {code} {name}: "
        f"requested={len(gaps)} added={total_added} "
        f"remaining={len(remaining)}",
        flush=True,
    )

    if remaining:
        raise RuntimeError(
            f"{code} still has {len(remaining)} gaps: {remaining}"
        )

    return len(gaps), total_added


def main() -> None:
    calendar = hk_calendar()
    total_before = 0
    total_added = 0

    for code, name in TARGETS.items():
        before, added = repair_one(code, name, calendar)
        total_before += before
        total_added += added

    print(
        f"[DONE] Tencent repair complete: "
        f"gaps_before={total_before} added={total_added} remaining=0",
        flush=True,
    )


if __name__ == "__main__":
    main()
