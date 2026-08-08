from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from master_stock_selector.watchlist.reference_data import (
    ReferenceBackfillConfig,
    collect_reference_data,
)
from master_stock_selector.watchlist.reference_materialization import _identities, _memberships


class _Provider:
    source_name = "FakeTushare"
    source_version = "fake-v1"
    request_count = 0

    def assert_ready(self) -> None:
        return None

    def stock_name_changes(self):
        self.request_count += 1
        return [{"ts_code": "000001.SZ", "name": "旧名", "start_date": "20240101", "end_date": "20250101", "ann_date": "20231201", "change_reason": "x"}, {"ts_code": "000001.SZ", "name": "新名", "start_date": "20250102", "end_date": "", "ann_date": "20250101", "change_reason": "y"}]

    def sw_l3_classifications(self):
        self.request_count += 1
        return [{"index_code": "850001.SI"}]

    def sw_l3_members(self, _: str):
        self.request_count += 1
        return [{"ts_code": "000001.SZ", "l3_code": "850001.SI", "l3_name": "测试行业", "l2_code": "801001.SI", "in_date": "20240101", "out_date": ""}]

    def stock_st(self, trade_date: str):
        self.request_count += 1
        return [{"ts_code": "000001.SZ", "name": "旧名", "trade_date": trade_date.replace("-", ""), "type": "ST", "type_name": "风险警示"}] if trade_date == "2025-01-02" else []


class ReferenceDataTests(unittest.TestCase):
    def test_collects_temporal_reference_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "market.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE daily_bars (market TEXT, adj_type TEXT, trade_date TEXT)")
                connection.executemany("INSERT INTO daily_bars VALUES ('ashare','qfq',?)", [("2025-01-02",), ("2025-01-03",)])
            payload = collect_reference_data(ReferenceBackfillConfig(database, "2025-01-02", "2025-01-03"), provider=_Provider())
            self.assertEqual(payload["trading_days"], 2)
            self.assertEqual(payload["name_change_count"], 2)
            self.assertEqual(payload["st_fact_count"], 1)
            self.assertEqual(payload["membership_count"], 1)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM reference_data_run_receipt").fetchone()[0], 1)

    def test_resolves_only_effective_name_and_industry_intervals(self) -> None:
        members = {"000001.SZ": {"list_date": "2020-01-01"}}
        identities = _identities("2025-01-02", members, {"000001.SZ": [{"name": "旧名", "valid_from": "2024-01-01", "valid_to": "2025-01-01"}, {"name": "新名", "valid_from": "2025-01-02", "valid_to": ""}]}, {"2025-01-02": {"000001.SZ"}})
        self.assertEqual(identities[0]["name"], "新名")
        self.assertTrue(identities[0]["is_st"])
        dimensions, memberships = _memberships("2025-01-02", {"000001.SZ": [{"symbol": "000001.SZ", "industry_code": "850001.SI", "industry_name": "测试行业", "parent_industry_code": "801001.SI", "valid_from": "2024-01-01", "valid_to": "", "source_digest": "x"}]})
        self.assertEqual(dimensions[0]["industry_code"], "850001.SI")
        self.assertEqual(memberships[0]["assignment_state"], "VERIFIED")


if __name__ == "__main__":
    unittest.main()
