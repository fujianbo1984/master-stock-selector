from master_stock_selector.watchlist.industry_heat import build_industry_heat_rows


def _observation(code: str, name: str) -> dict:
    return {
        "industry_code": code,
        "industry_name": name,
        "eligible_member_count": 5,
        "quality_state": "COMPLETE",
        "w_entered_count": 0,
        "w_reentered_count": 0,
        "m_entered_count": 0,
        "m_reentered_count": 0,
    }


def test_hot_candidate_uses_price_and_breadth_while_methods_stay_annotations() -> None:
    observations = [_observation("A", "强行业"), _observation("B", "弱行业")]
    observations[1].update(
        {
            "w_entered_count": 4,
            "m_reentered_count": 3,
        }
    )
    memberships = [
        {"symbol": f"A{i}", "industry_code": "A"} for i in range(5)
    ] + [{"symbol": f"B{i}", "industry_code": "B"} for i in range(5)]
    stock_returns = {
        **{
            f"A{i}": {
                "return_1d_pct": 1.0,
                "return_5d_pct": value,
                "return_20d_pct": 12.0,
            }
            for i, value in enumerate((10.0, 9.0, 8.0, 7.0, -1.0))
        },
        **{
            f"B{i}": {
                "return_1d_pct": 0.2,
                "return_5d_pct": 1.0,
                "return_20d_pct": 2.0,
            }
            for i in range(5)
        },
    }
    brooks = [
        {"symbol": "A0", "brooks_phase": "h1"},
        {"symbol": "A1", "brooks_phase": "new"},
    ]

    rows = build_industry_heat_rows(
        observations=observations,
        memberships=memberships,
        stock_returns=stock_returns,
        brooks_rows=brooks,
    )
    by_code = {row["industry_code"]: row for row in rows}

    assert by_code["A"]["is_hot"] is True
    assert by_code["A"]["brooks_current_count"] == 2
    assert by_code["A"]["brooks_h1_count"] == 1
    assert by_code["A"]["brooks_new_count"] == 1
    assert by_code["A"]["brooks_resonance"] is True
    assert "Leg1共振" in by_code["A"]["heat_reasons"]
    assert by_code["B"]["is_hot"] is False
    assert "W扩散" in by_code["B"]["heat_reasons"]
    assert "M扩散" in by_code["B"]["heat_reasons"]
    assert not any("score" in key for row in rows for key in row)


def test_incomplete_price_coverage_cannot_become_hot() -> None:
    observation = _observation("A", "覆盖不足")
    memberships = [{"symbol": f"A{i}", "industry_code": "A"} for i in range(5)]
    stock_returns = {
        f"A{i}": {
            "return_1d_pct": 5.0,
            "return_5d_pct": 10.0 if i < 4 else None,
            "return_20d_pct": 20.0,
        }
        for i in range(5)
    }

    row = build_industry_heat_rows(
        observations=[observation],
        memberships=memberships,
        stock_returns=stock_returns,
        brooks_rows=[],
    )[0]

    assert row["price_coverage_5d_pct"] == 80.0
    assert row["heat_quality_state"] == "PRICE_INSUFFICIENT"
    assert row["is_hot"] is False
