from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

SHANGHAI_TIMEZONE = ZoneInfo("Asia/Shanghai")


def sqlite_utc_to_shanghai(value: object) -> str:
    """Format a SQLite UTC timestamp for display in Asia/Shanghai.

    SQLite ``CURRENT_TIMESTAMP`` values are naive UTC strings. Date-only values
    are content dates rather than instants, so they are intentionally unchanged.
    Unknown legacy formats are also preserved instead of guessing.
    """
    text = str(value or "").strip()
    if not text or len(text) <= 10:
        return text

    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    localized = parsed.astimezone(SHANGHAI_TIMEZONE)
    return localized.strftime("%Y-%m-%d %H:%M:%S")
