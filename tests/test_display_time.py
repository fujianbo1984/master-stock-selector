from master_stock_selector.web.display_time import sqlite_utc_to_shanghai


def test_sqlite_utc_timestamp_crossing_midnight_displays_in_shanghai() -> None:
    assert sqlite_utc_to_shanghai("2026-08-25 23:31:26") == "2026-08-26 07:31:26"


def test_content_date_and_unknown_legacy_value_are_preserved() -> None:
    assert sqlite_utc_to_shanghai("2026-08-25") == "2026-08-25"
    assert sqlite_utc_to_shanghai("not-a-timestamp") == "not-a-timestamp"
