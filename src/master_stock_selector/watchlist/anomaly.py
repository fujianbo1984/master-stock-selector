from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import fmean, median
from typing import Any

ANOMALY_EVIDENCE_POLICY_VERSION = "daily-anomaly-review-v1"


def evaluate_daily_anomaly_evidence(
    symbol: str,
    series_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Describe daily trading anomalies without changing method eligibility."""

    normalized_symbol = symbol.upper()
    series = {
        key.upper(): sorted((dict(row) for row in values), key=lambda row: str(row["trade_date"]))
        for key, values in series_by_symbol.items()
        if values
    }
    target = series.get(normalized_symbol, [])
    evidence = [
        _float_cap_evidence(normalized_symbol, series),
        _car20_evidence(normalized_symbol, series),
        _turnover_shock_evidence(target),
        _volume_ratio_evidence(target),
        _churn_evidence(normalized_symbol, series),
    ]
    structural_trigger = evidence[0]["state"] == "TRIGGERED"
    trading_trigger_count = sum(
        item["state"] == "TRIGGERED" for item in evidence[1:]
    )
    available_trading_count = sum(
        item["state"] != "INSUFFICIENT" for item in evidence[1:]
    )
    review = (structural_trigger and trading_trigger_count >= 2) or trading_trigger_count >= 3
    if available_trading_count < 2:
        status = "DATA_INSUFFICIENT"
        label = "数据不足"
        conclusion = "可用交易证据不足，不能形成异常交易复核结论。"
    elif review:
        status = "REVIEW"
        label = "建议复核"
        conclusion = "多项独立异常证据联合触发；不改变大师方法结论，由用户人工复核。"
    else:
        status = "CLEAR"
        label = "未形成联合触发"
        conclusion = (
            f"当前有 {trading_trigger_count} 项交易异常单项触发，"
            "尚未达到联合复核条件；这不代表股票值得买入。"
        )
    return {
        "policy_version": ANOMALY_EVIDENCE_POLICY_VERSION,
        "status": status,
        "label": label,
        "conclusion": conclusion,
        "evidence": evidence,
        "trading_trigger_count": trading_trigger_count,
        "available_trading_count": available_trading_count,
        "changes_method_eligibility": False,
    }


def empty_daily_anomaly_evidence() -> dict[str, Any]:
    return evaluate_daily_anomaly_evidence("", {})


def _float_cap_evidence(
    symbol: str, series: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, Any]:
    caps: dict[str, float] = {}
    for key, values in series.items():
        value = _number(values[-1].get("circ_mv")) if values else None
        if value is not None and value > 0:
            caps[key] = value
    value = caps.get(symbol)
    if value is None or len(caps) < 5:
        return _evidence("float_cap", "流通市值行业位置", None, "INSUFFICIENT", "同行业有效样本不足5只")
    percentile = _percentile(list(caps.values()), value)
    return _evidence(
        "float_cap",
        "流通市值行业位置",
        round(percentile, 1),
        "TRIGGERED" if percentile <= 20.0 else "NORMAL",
        f"{value / 10000.0:.1f}亿元 · 同行业P{percentile:.1f}；P20及以下记为结构易感",
        unit="%",
    )


def _car20_evidence(
    symbol: str, series: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, Any]:
    returns = {
        key: value
        for key, values in series.items()
        if (value := _return_over(values, 20)) is not None
    }
    target_return = returns.get(symbol)
    if target_return is None or len(returns) < 5:
        return _evidence("car20", "20日行业超额收益", None, "INSUFFICIENT", "需要21个交易日且同行业有效样本不少于5只")
    industry_return = median(returns.values())
    car20 = (target_return - industry_return) * 100.0
    return _evidence(
        "car20",
        "20日行业超额收益",
        round(car20, 2),
        "TRIGGERED" if car20 >= 30.0 else "NORMAL",
        f"个股{target_return * 100:.2f}% · 同行业中位{industry_return * 100:.2f}%；达到30%记为异常",
        unit="%",
    )


def _turnover_shock_evidence(series: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(series) < 8:
        return _evidence("turnover_shock", "换手突变", None, "INSUFFICIENT", "需要连续8个交易日换手率")
    values = [_number(row.get("turnover_rate")) for row in series[-8:]]
    if any(value is None for value in values):
        return _evidence("turnover_shock", "换手突变", None, "INSUFFICIENT", "最近8个交易日换手率不完整")
    recent = fmean(value for value in values[-3:] if value is not None)
    prior = fmean(value for value in values[:5] if value is not None)
    if prior <= 0:
        return _evidence("turnover_shock", "换手突变", None, "INSUFFICIENT", "此前5日平均换手率为零")
    ratio = recent / prior
    return _evidence(
        "turnover_shock",
        "换手突变",
        round(ratio, 2),
        "TRIGGERED" if ratio >= 5.0 else "NORMAL",
        "近3日/此前5日；达到5倍记为异常",
        unit="倍",
    )


def _volume_ratio_evidence(series: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(series) < 21:
        return _evidence("volume_ratio", "量能异常", None, "INSUFFICIENT", "需要21个交易日成交量")
    latest = _number(series[-1].get("volume"))
    history = [_number(row.get("volume")) for row in series[-21:-1]]
    if latest is None or latest <= 0 or any(value is None or value <= 0 for value in history):
        return _evidence("volume_ratio", "量能异常", None, "INSUFFICIENT", "最近21个交易日成交量不完整")
    baseline = median(value for value in history if value is not None)
    ratio = latest / baseline
    return _evidence(
        "volume_ratio",
        "量能异常",
        round(ratio, 2),
        "TRIGGERED" if ratio >= 3.0 else "NORMAL",
        "当日成交量/此前20日中位数；达到3倍记为异常",
        unit="倍",
    )


def _churn_evidence(
    symbol: str, series: Mapping[str, Sequence[Mapping[str, Any]]]
) -> dict[str, Any]:
    returns = {
        key: value
        for key, values in series.items()
        if (value := _return_over(values, 5)) is not None
    }
    if symbol not in returns or len(returns) < 5:
        return _evidence("churn5", "高换手低位移", None, "INSUFFICIENT", "需要6个交易日且同行业有效样本不少于5只")
    industry_return = median(returns.values())
    churns: dict[str, tuple[float, float]] = {}
    for key, stock_return in returns.items():
        values = series[key]
        turnovers = [_number(row.get("turnover_rate")) for row in values[-5:]]
        if len(turnovers) < 5 or any(value is None or value < 0 for value in turnovers):
            continue
        cumulative = sum(value for value in turnovers if value is not None) / 100.0
        churns[key] = (cumulative / (abs(stock_return - industry_return) + 0.03), cumulative)
    target = churns.get(symbol)
    if target is None or len(churns) < 5:
        return _evidence("churn5", "高换手低位移", None, "INSUFFICIENT", "同行业Churn有效样本不足5只")
    percentile = _percentile([value[0] for value in churns.values()], target[0])
    triggered = percentile >= 98.0 and target[1] >= 0.8
    return _evidence(
        "churn5",
        "高换手低位移",
        round(target[0], 2),
        "TRIGGERED" if triggered else "NORMAL",
        f"5日累计换手{target[1] * 100:.1f}% · 同行业P{percentile:.1f}；同时达到80%和P98记为异常",
    )


def _return_over(series: Sequence[Mapping[str, Any]], sessions: int) -> float | None:
    if len(series) < sessions + 1:
        return None
    start = _number(series[-sessions - 1].get("close"))
    end = _number(series[-1].get("close"))
    if start is None or end is None or start <= 0 or end <= 0:
        return None
    return end / start - 1.0


def _percentile(values: Sequence[float], target: float) -> float:
    return 100.0 * sum(value <= target for value in values) / len(values)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _evidence(
    key: str,
    label: str,
    value: float | None,
    state: str,
    detail: str,
    *,
    unit: str = "",
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "value": value,
        "unit": unit,
        "state": state,
        "state_label": {
            "TRIGGERED": "触发",
            "NORMAL": "未触发",
            "INSUFFICIENT": "数据不足",
        }[state],
        "detail": detail,
    }
