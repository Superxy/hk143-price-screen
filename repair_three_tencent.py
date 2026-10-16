from __future__ import annotations

from datetime import date
from pathlib import Path

from cloud_run import (
    fetch_tencent_history,
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


def repair_one(code: str, name: str, calendar) -> tuple[int, int]:
    history = read_history(ROOT, code, ASOF)
    gaps = find_history_gaps(history, ASOF, calendar)

    if not gaps:
        print(f"[VERIFY] {code} {name}: no gaps", flush=True)
        return 0, 0

    print(
        f"[REPAIR] {code} {name}: gaps={len(gaps)} "
        f"dates={gaps}",
        flush=True,
    )

    # Tencent is intentionally the only repair source in this standalone job.
    fetched = normalize(fetch_tencent_history(code, timeout=20.0), ASOF)
    required = set(gaps)
    additions = fetched[fetched["date"].isin(required)].copy()

    missing_from_tencent = sorted(required - set(additions["date"]))
    if missing_from_tencent:
        raise RuntimeError(
            f"{code} Tencent did not return required dates: "
            f"{missing_from_tencent}"
        )

    before = len(history)
    history = save_history(ROOT, code, history, additions, ASOF)
    remaining = find_history_gaps(history, ASOF, calendar)

    print(
        f"[RESULT] {code} {name}: fetched={len(additions)} "
        f"remaining={len(remaining)}",
        flush=True,
    )

    if remaining:
        raise RuntimeError(
            f"{code} still has {len(remaining)} gaps after Tencent repair: "
            f"{remaining}"
        )

    return len(gaps), len(additions)


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
