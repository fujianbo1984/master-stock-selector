from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


class TushareError(RuntimeError):
    """Tushare transport or response error without exposing credentials."""


class TushareMarketProvider:
    source_name = "TusharePro"
    source_version = "tushare-pro-api-v1"
    endpoint = "https://api.tushare.pro"

    def __init__(
        self,
        token: str | None = None,
        *,
        timeout: float = 20.0,
        retries: int = 2,
        calls_per_minute: float | None = None,
    ) -> None:
        self._token = token if token is not None else os.environ.get("TUSHARE_TOKEN", "")
        self._timeout = max(1.0, float(timeout))
        self._retries = max(0, int(retries))
        configured_rate = calls_per_minute
        if configured_rate is None:
            try:
                configured_rate = float(
                    os.environ.get("MASTERSTOCK_TUSHARE_CALLS_PER_MINUTE", "48")
                )
            except ValueError:
                configured_rate = 48.0
        self._minimum_interval = (
            60.0 / float(configured_rate) if configured_rate and configured_rate > 0 else 0.0
        )
        self._last_request_at = 0.0
        self.request_count = 0
        # The desktop shell can inject an HTTPS proxy that does not support
        # api.tushare.pro correctly. Tushare is a fixed trusted endpoint, so
        # use the same direct route that the local browser uses.
        self._opener = build_opener(ProxyHandler({}))

    def assert_ready(self) -> None:
        if not self._token:
            raise TushareError("缺少 TUSHARE_TOKEN；采集未开始")

    def trade_calendar(self, start_date: str, end_date: str) -> list[str]:
        rows = self._request_rows(
            "trade_cal",
            {
                "exchange": "SSE",
                "start_date": _compact_date(start_date),
                "end_date": _compact_date(end_date),
                "is_open": "1",
            },
            "exchange,cal_date,is_open,pretrade_date",
        )
        return sorted(
            {
                _normalize_date(str(row.get("cal_date") or ""))
                for row in rows
                if row.get("cal_date")
            }
        )

    def stock_basic(self) -> list[dict[str, Any]]:
        return self._request_rows(
            "stock_basic",
            {"exchange": "", "list_status": "L"},
            "ts_code,symbol,name,industry,market,list_date",
        )

    def market_daily_bars(self, trade_date: str) -> dict[str, dict[str, Any]]:
        rows = self._paged_rows(
            "daily",
            {"trade_date": _compact_date(trade_date)},
            "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount",
        )
        return _rows_by_symbol(rows, trade_date)

    def market_adjustment_factors(self, trade_date: str) -> dict[str, float]:
        rows = self._paged_rows(
            "adj_factor",
            {"trade_date": _compact_date(trade_date)},
            "ts_code,trade_date,adj_factor",
        )
        result: dict[str, float] = {}
        for symbol, row in _rows_by_symbol(rows, trade_date).items():
            factor = _positive_float(row.get("adj_factor"))
            if factor is not None:
                result[symbol] = factor
        return result

    def market_daily_metrics(self, trade_date: str) -> dict[str, dict[str, Any]]:
        rows = self._paged_rows(
            "daily_basic",
            {"trade_date": _compact_date(trade_date)},
            "ts_code,trade_date,turnover_rate,total_mv,circ_mv",
        )
        return _rows_by_symbol(rows, trade_date)

    def index_daily_bars(
        self,
        trade_date: str,
        index_symbols: tuple[str, ...],
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for symbol in index_symbols:
            rows = self._request_rows(
                "index_daily",
                {"ts_code": symbol, "trade_date": _compact_date(trade_date)},
                "ts_code,trade_date,open,high,low,close,pre_close,pct_chg,vol,amount",
            )
            parsed = _rows_by_symbol(rows, trade_date)
            if symbol in parsed:
                result[symbol] = parsed[symbol]
        return result

    def _paged_rows(
        self,
        api_name: str,
        params: Mapping[str, Any],
        fields: str,
        *,
        page_size: int = 6000,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._request_rows(
                api_name,
                {**params, "limit": page_size, "offset": offset},
                fields,
            )
            result.extend(page)
            if len(page) < page_size:
                return result
            offset += len(page)

    def _request_rows(
        self,
        api_name: str,
        params: Mapping[str, Any],
        fields: str,
    ) -> list[dict[str, Any]]:
        self.assert_ready()
        body = json.dumps(
            {
                "api_name": api_name,
                "token": self._token,
                "params": dict(params),
                "fields": fields,
            }
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "MasterStockSelector/0.1",
            },
        )
        last_error = ""
        for attempt in range(self._retries + 1):
            self._wait_for_rate_limit()
            self.request_count += 1
            try:
                with self._opener.open(request, timeout=self._timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                code = int(payload.get("code", 0) or 0)
                if code != 0:
                    message = str(payload.get("msg") or "provider rejected request")
                    raise TushareError(f"Tushare {api_name} 返回错误 {code}: {message}")
                data = payload.get("data") or {}
                names = [str(value) for value in data.get("fields") or []]
                return [dict(zip(names, item)) for item in data.get("items") or []]
            except TushareError:
                raise
            except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = type(exc).__name__
                if attempt < self._retries:
                    time.sleep(0.4 * (attempt + 1))
        raise TushareError(f"Tushare {api_name} 请求失败: {last_error or 'unknown error'}")

    def _wait_for_rate_limit(self) -> None:
        if self._minimum_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if self._last_request_at and elapsed < self._minimum_interval:
            time.sleep(self._minimum_interval - elapsed)
        self._last_request_at = time.monotonic()


def _rows_by_symbol(
    rows: list[dict[str, Any]],
    expected_date: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        trade_date = _normalize_date(str(row.get("trade_date") or ""))
        if trade_date != expected_date:
            raise TushareError(
                f"Tushare 返回日期 {trade_date or 'empty'}，预期 {expected_date}"
            )
        symbol = str(row.get("ts_code") or "").strip().upper()
        if symbol:
            result[symbol] = {**row, "trade_date": trade_date}
    return result


def _normalize_date(value: str) -> str:
    compact = value.strip().replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        return ""
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}"


def _compact_date(value: str) -> str:
    normalized = _normalize_date(value)
    return normalized.replace("-", "") if normalized else ""


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def default_collection_date() -> str:
    return date.today().isoformat()


def date_window(end_date: str, sessions: int = 8) -> tuple[str, str]:
    """Small helper retained for future provider diagnostics, not backfill."""

    parsed = date.fromisoformat(end_date)
    return (parsed - timedelta(days=max(1, sessions) * 2)).isoformat(), end_date
