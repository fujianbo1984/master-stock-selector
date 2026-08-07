from __future__ import annotations

from master_stock_selector.watchlist.industry import build_industry_observations


def _membership(symbol: str, code: str = "850001.SI") -> dict[str, str]:
    return {
        "symbol": symbol,
        "industry_code": code,
        "industry_name": "测试行业",
    }


def _fact(symbol: str, method: str, result: str, state: str = "") -> dict[str, str]:
    return {
        "symbol": symbol,
        "method": method,
        "result": result,
        "state": state,
        "source_digest": f"{symbol}-{method}",
    }


def test_industry_observation_counts_methods_and_transitions_independently() -> None:
    symbols = [f"00000{i}.SZ" for i in range(1, 6)]
    observations = build_industry_observations(
        as_of_date="2026-07-31",
        eligible_symbols=symbols,
        memberships=[_membership(symbol) for symbol in symbols],
        method_facts=[
            _fact(symbols[0], "weinstein", "PASS", "ENTERED"),
            _fact(symbols[0], "minervini", "PASS", "CONTINUING"),
            _fact(symbols[1], "weinstein", "PASS", "CONTINUING"),
            _fact(symbols[1], "minervini", "FAIL"),
            _fact(symbols[2], "weinstein", "TRANSITION"),
            _fact(symbols[2], "minervini", "PASS", "REENTERED"),
            _fact(symbols[3], "weinstein", "FAIL", "EXITED"),
            _fact(symbols[3], "minervini", "UNKNOWN", "DATA_GAP"),
            _fact(symbols[4], "weinstein", "UNKNOWN"),
            _fact(symbols[4], "minervini", "FAIL"),
        ],
        membership_snapshot_date="2026-07-31",
        membership_source_digest="mapping",
        minervini_policy_version="m-v1",
        weinstein_policy_version="w-v1",
        origin="RECONSTRUCTED",
    )

    assert len(observations) == 1
    row = observations[0]
    assert row["quality_state"] == "COMPLETE"
    assert row["membership_coverage_pct"] == 100.0
    assert row["weinstein_evaluable_count"] == 4
    assert row["weinstein_pass_count"] == 2
    assert row["minervini_evaluable_count"] == 4
    assert row["minervini_pass_count"] == 2
    assert row["both_pass_count"] == 1
    assert row["union_pass_count"] == 3
    assert row["w_entered_count"] == 1
    assert row["w_exited_count"] == 1
    assert row["m_reentered_count"] == 1
    assert row["m_data_gap_count"] == 1


def test_industry_observation_marks_small_samples_and_bad_mapping_unknown() -> None:
    small = build_industry_observations(
        as_of_date="2026-07-31",
        eligible_symbols=["000001.SZ"],
        memberships=[_membership("000001.SZ")],
        method_facts=[_fact("000001.SZ", "weinstein", "PASS")],
        membership_snapshot_date="2026-07-31",
        membership_source_digest="mapping",
        minervini_policy_version="m-v1",
        weinstein_policy_version="w-v1",
        origin="RECONSTRUCTED",
    )
    unknown = build_industry_observations(
        as_of_date="2026-07-31",
        eligible_symbols=["000001.SZ", "000002.SZ"],
        memberships=[_membership("000001.SZ")],
        method_facts=[_fact("000001.SZ", "weinstein", "PASS")],
        membership_snapshot_date="2026-07-31",
        membership_source_digest="mapping",
        minervini_policy_version="m-v1",
        weinstein_policy_version="w-v1",
        origin="RECONSTRUCTED",
    )

    assert small[0]["quality_state"] == "SMALL_SAMPLE"
    assert unknown[0]["quality_state"] == "UNKNOWN"
    assert unknown[0]["membership_coverage_pct"] == 50.0
