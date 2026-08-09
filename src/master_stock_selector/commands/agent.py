from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class AgentApiError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        super().__init__(f"Agent API 请求失败：HTTP {status}")
        self.status = status
        self.payload = payload


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    token = _token(args, parser)
    base_url = _base_url(str(args.base_url), parser)
    try:
        result = _dispatch_agent(args, parser, base_url, token)
    except AgentApiError as exc:
        print(
            json.dumps(
                {"status": "ERROR", "http_status": exc.status, "response": exc.payload},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _dispatch_agent(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    base_url: str,
    token: str,
) -> Any:
    if args.agent_action == "me":
        return _request_json(base_url, token, "GET", "/api/v1/me")
    if args.agent_action != "trades":
        parser.error("unknown agent action")
    action = str(args.trade_action)
    if action in {"validate", "import"}:
        if args.token_stdin and args.file == "-":
            parser.error("--token-stdin 不能与从标准输入读取交易 JSON 同时使用")
        payload = _load_trade_payload(str(args.file), parser)
        if action == "validate" or not bool(getattr(args, "commit", False)):
            return _request_json(
                base_url, token, "POST", "/api/v1/trades/validate", payload
            )
        key = str(args.idempotency_key or "").strip() or _default_idempotency_key(payload)
        return _request_json(
            base_url,
            token,
            "POST",
            "/api/v1/trades/batch",
            payload,
            headers={"Idempotency-Key": key},
        )
    if action == "list":
        query = urllib.parse.urlencode(
            {
                key: value
                for key, value in {
                    "symbol": args.symbol,
                    "date_from": args.date_from,
                    "date_to": args.date_to,
                    "limit": args.limit,
                }.items()
                if value not in (None, "")
            }
        )
        return _request_json(base_url, token, "GET", f"/api/v1/trades?{query}")
    if action == "get":
        execution_id = urllib.parse.quote(str(args.execution_id), safe="")
        return _request_json(base_url, token, "GET", f"/api/v1/trades/{execution_id}")
    if action == "set-stop":
        if not args.commit:
            parser.error("set-stop 会修改既有 BUY；必须显式传入 --commit")
        execution_id = urllib.parse.quote(str(args.execution_id), safe="")
        stop_payload: dict[str, Any] = {
            "stop_price": args.stop_price,
            "expected_revision": args.expected_revision,
        }
        return _request_json(
            base_url,
            token,
            "PATCH",
            f"/api/v1/trades/{execution_id}/stop",
            stop_payload,
        )
    parser.error("unknown trades action")
    return {}


def _token(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    if args.token_stdin:
        token = sys.stdin.readline().strip()
    else:
        token = os.environ.get("MASTERSTOCK_AGENT_TOKEN", "").strip()
    if not token:
        parser.error(
            "缺少 Agent Token；请设置 MASTERSTOCK_AGENT_TOKEN 或使用 --token-stdin"
        )
    return token


def _base_url(value: str, parser: argparse.ArgumentParser) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        parser.error("--base-url 必须是 http 或 https 地址")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("--base-url 不能包含凭据、查询参数或片段")
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme == "http" and (parsed.hostname or "").lower() not in local_hosts:
        parser.error("公网 Agent API 必须使用 HTTPS；HTTP 仅允许本机地址")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


def _load_trade_payload(path: str, parser: argparse.ArgumentParser) -> dict[str, Any]:
    try:
        text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        value = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"无法读取交易 JSON：{exc}")
    if isinstance(value, list):
        return {"trades": value}
    if isinstance(value, dict) and isinstance(value.get("trades"), list):
        return value
    parser.error("交易 JSON 必须是数组或包含 trades 数组的对象")
    return {}


def _default_idempotency_key(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "cli-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]


def _request_json(
    base_url: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> Any:
    body = (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if payload is not None
        else None
    )
    request_headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "masterstock-agent-cli/0.1",
    }
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    request = urllib.request.Request(
        base_url + path, data=body, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            response_payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            response_payload = {"detail": raw[:1000]}
        raise AgentApiError(exc.code, response_payload) from exc
    except urllib.error.URLError as exc:
        raise AgentApiError(0, {"detail": str(exc.reason)}) from exc
