from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import requests

from cloud_run import (
    KNOWN_SUSPENSION_DATES,
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
                    f"Tencent returned no rows for {code} {start}~{end}"
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
                    f"Tencent returned no rows in requested range "
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


def repair_one(code: str, name: str, calendar) -> tuple[int, int, int]:
    history = read_history(ROOT, code, ASOF)
    gaps = find_history_gaps(history, ASOF, calendar, code)

    if not gaps:
        print(f"[VERIFY] {code} {name}: no gaps", flush=True)
        return 0, 0, 0

    suspension = KNOWN_SUSPENSION_DATES.get(code, set())
    verified_suspension_gaps = sorted(set(gaps) & suspension)
    real_gaps = sorted(set(gaps) - suspension)

    print(
        f"[CHECK] {code} {name}: detected={len(gaps)} "
        f"verified_suspension={len(verified_suspension_gaps)} "
        f"real_data_gaps={len(real_gaps)}",
        flush=True,
    )

    if verified_suspension_gaps:
        print(
            f"[SUSPENSION] {code}: {verified_suspension_gaps}",
            flush=True,
        )

    total_added = 0
    for start, end in contiguous_ranges(real_gaps, calendar):
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

    remaining = find_history_gaps(history, ASOF, calendar, code)
    unresolved_real_gaps = sorted(set(remaining) - suspension)

    print(
        f"[RESULT] {code} {name}: "
        f"real_gaps_before={len(real_gaps)} added={total_added} "
        f"remaining_real_gaps={len(unresolved_real_gaps)} "
        f"verified_suspensions={len(set(remaining) & suspension)}",
        flush=True,
    )

    if unresolved_real_gaps:
        raise RuntimeError(
            f"{code} still has real data gaps: {unresolved_real_gaps}"
        )

    return len(real_gaps), total_added, len(verified_suspension_gaps)


def main() -> None:
    calendar = hk_calendar()
    total_real = 0
    total_added = 0
    total_suspensions = 0

    for code, name in TARGETS.items():
        real_gaps, added, suspensions = repair_one(code, name, calendar)
        total_real += real_gaps
        total_added += added
        total_suspensions += suspensions

    print(
        f"[DONE] Tencent repair/verification complete: "
        f"real_gaps_before={total_real} added={total_added} "
        f"remaining_real_gaps=0 verified_suspension_days={total_suspensions}",
        flush=True,
    )


if __name__ == "__main__":
    main()
