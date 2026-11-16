"""Keep the ten most recent rating-update results."""

from __future__ import annotations

MAX_RUNS = 10


def summary_text(checked: int, updated: int, unchanged: int, skipped: int, failed: int, cancelled: bool = False, stopped: str = "") -> str:
    text = (
        f"Checked {checked}. Updated {updated}. "
        f"Unchanged {unchanged}. Skipped {skipped}. Failed {failed}."
    )
    if stopped:
        return f"Stopped: {stopped}. {text}"
    if cancelled:
        return f"Cancelled. {text}"
    return text


def remember(runs: list, record: dict, limit: int = MAX_RUNS) -> list:
    return [record, *[item for item in runs if isinstance(item, dict)]][:limit]
