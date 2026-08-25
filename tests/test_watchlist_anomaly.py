from __future__ import annotations

from master_stock_selector.watchlist.anomaly import evaluate_daily_anomaly_evidence


def _series(
    *,
    closes: list[float],
    turnovers: list[float],
    volumes: list[float] | None = None,
    circ_mv: float,
) -> list[dict[str, float | str]]:
    volume_values = volumes or [100.0] * len(closes)
    return [
        {
            "trade_date": f"2026-07-{index + 1:02d}",
            "close": close,
            "volume": volume,
            "turnover_rate": turnover,
            "circ_mv": circ_mv,
        }
        for index, (close, volume, turnover) in enumerate(
            zip(closes, volume_values, turnovers, strict=True)
        )
    ]


def test_joint_daily_anomalies_request_review_without_changing_method_result():
    peers = {
        "000001.SZ": _series(
            closes=[100.0] * 20 + [150.0],
            turnovers=[1.0] * 18 + [6.0, 6.0, 6.0],
            circ_mv=100000.0,
        )
    }
    for index in range(2, 6):
        peers[f"00000{index}.SZ"] = _series(
            closes=[100.0] * 21,
            turnovers=[1.0] * 21,
            circ_mv=100000.0 * index,
        )

    result = evaluate_daily_anomaly_evidence("000001.SZ", peers)
    states = {item["key"]: item["state"] for item in result["evidence"]}

    assert result["status"] == "REVIEW"
    assert result["changes_method_eligibility"] is False
    assert states["float_cap"] == "TRIGGERED"
    assert states["car20"] == "TRIGGERED"
    assert states["turnover_shock"] == "TRIGGERED"
    assert states["volume_ratio"] == "NORMAL"


def test_isolated_abnormal_return_does_not_create_joint_review():
    peers = {
        "000001.SZ": _series(
            closes=[100.0] * 20 + [150.0],
            turnovers=[1.0] * 21,
            circ_mv=600000.0,
        )
    }
    for index in range(2, 6):
        peers[f"00000{index}.SZ"] = _series(
            closes=[100.0] * 21,
            turnovers=[1.0] * 21,
            circ_mv=100000.0 * index,
        )

    result = evaluate_daily_anomaly_evidence("000001.SZ", peers)

    assert result["status"] == "CLEAR"
    assert result["trading_trigger_count"] == 1
    assert "1 项交易异常单项触发" in result["conclusion"]


def test_missing_daily_history_is_reported_as_insufficient():
    result = evaluate_daily_anomaly_evidence("000001.SZ", {})

    assert result["status"] == "DATA_INSUFFICIENT"
    assert all(item["state"] == "INSUFFICIENT" for item in result["evidence"])
