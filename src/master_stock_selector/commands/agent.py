from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

_READING_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_READING_HTML_IMAGE = re.compile(
    r'<img\b[^>]*?\bsrc=["\'](?!/|[a-z][a-z0-9+.-]*:|//)([^"\']+)["\']',
    re.IGNORECASE,
)


class AgentApiError(RuntimeError):
    def __init__(self, status: int, payload: Any):
        super().__init__(f"Agent API 请求失败：HTTP {status}")
        self.status = status
        self.payload = payload


def handle(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.agent_action == "reading" and args.reading_action == "validate":
        try:
            payload = _load_reading_payload(str(args.path), parser)
        except ValueError as exc:
            parser.error(str(exc))
        print(
            json.dumps(
                {
                    "status": "READY",
                    "slug": payload["slug"],
                    "title": payload["title"],
                    "tag_count": len(payload["tags"]),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
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
    if args.agent_action == "plans":
        action = str(args.plan_action)
        if action == "current":
            return _request_json(base_url, token, "GET", "/api/v1/plans/current")
        if action == "versions":
            return _request_json(base_url, token, "GET", "/api/v1/plans/versions")
        if action == "show":
            return _request_json(
                base_url,
                token,
                "GET",
                "/api/v1/plans/versions/" + urllib.parse.quote(args.version_id, safe=""),
            )
        if action == "diff":
            return _request_json(
                base_url,
                token,
                "GET",
                "/api/v1/plans/diff?"
                + urllib.parse.urlencode({"old": args.old_version_id, "new": args.new_version_id}),
            )
        grill = str(args.grill_action)
        if grill == "start":
            return _request_json(
                base_url,
                token,
                "POST",
                "/api/v1/plans/grill/sessions",
                {
                    "template": args.template,
                    "from_version": args.from_version,
                    "change_goal": args.change_goal,
                },
            )
        path = "/api/v1/plans/grill/sessions/" + urllib.parse.quote(args.session_id, safe="")
        if grill == "status":
            return _request_json(base_url, token, "GET", path)
        if grill == "next":
            return _request_json(base_url, token, "GET", path + "/next")
        if grill == "validate":
            return _request_json(base_url, token, "POST", path + "/validate", {})
        if grill == "answer":
            if args.token_stdin and args.file == "-":
                parser.error("--token-stdin 不能与从标准输入读取回答 JSON 同时使用")
            return _request_json(
                base_url, token, "POST", path + "/answers", _load_json_object(args.file, parser)
            )
        if grill == "publish":
            if not args.commit:
                parser.error("publish 会发布冻结版本；必须显式传入 --commit")
            return _request_json(
                base_url,
                token,
                "POST",
                path + "/publish",
                {"expected_revision": args.expected_revision},
            )
        parser.error("unknown grill action")
    if args.agent_action == "reading":
        action = str(args.reading_action)
        if action == "list":
            return _request_json(base_url, token, "GET", "/api/v1/reading")
        if action == "show":
            note_id = urllib.parse.quote(str(args.note_id), safe="")
            return _request_json(base_url, token, "GET", f"/api/v1/reading/{note_id}")
        if action == "save":
            if not args.commit:
                parser.error("save 会保存私有草稿；必须显式传入 --commit")
            if args.token_stdin and args.path == "-":
                parser.error("--token-stdin 不能与从标准输入读取 Markdown 同时使用")
            payload = _load_reading_payload(str(args.path), parser)
            if args.note_id:
                payload["note_id"] = args.note_id
            if args.expected_revision is not None:
                payload["expected_revision"] = args.expected_revision
            return _request_json(base_url, token, "POST", "/api/v1/reading", payload)
        note_id = urllib.parse.quote(str(args.note_id), safe="")
        if action == "submit":
            if not args.commit:
                parser.error("submit 会生成私有冻结版本；必须显式传入 --commit")
            return _request_json(
                base_url,
                token,
                "POST",
                f"/api/v1/reading/{note_id}/submit",
                {"expected_revision": args.expected_revision},
            )
        if action == "share":
            if not args.commit:
                parser.error("share 会向登录用户公开冻结版本；必须显式传入 --commit")
            return _request_json(
                base_url,
                token,
                "POST",
                f"/api/v1/reading/{note_id}/share",
                {"version_id": args.version_id, "statement": args.statement},
            )
        if action == "unshare":
            if not args.commit:
                parser.error("unshare 会立即隐藏共享入口；必须显式传入 --commit")
            return _request_json(
                base_url, token, "POST", f"/api/v1/reading/{note_id}/share/stop", {}
            )
        parser.error("unknown reading action")
    if args.agent_action == "observations":
        if args.observation_action == "add":
            if not args.commit:
                parser.error("observations add 会写入我的观察；必须显式传入 --commit")
            return _request_json(
                base_url,
                token,
                "POST",
                "/api/v1/observations",
                {
                    "symbol": str(args.symbol).upper(),
                    "tags": list(args.tag),
                    "note": args.note,
                },
            )
        parser.error("unknown observations action")
    if args.agent_action != "trades":
        parser.error("unknown agent action")
    action = str(args.trade_action)
    if action in {"validate", "import"}:
        if args.token_stdin and args.file == "-":
            parser.error("--token-stdin 不能与从标准输入读取交易 JSON 同时使用")
        payload = _load_trade_payload(str(args.file), parser)
        if action == "validate" or not bool(getattr(args, "commit", False)):
            return _request_json(base_url, token, "POST", "/api/v1/trades/validate", payload)
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
    if action == "set-time":
        if not args.commit:
            parser.error("set-time 会补全既有交易的成交时间；必须显式传入 --commit")
        execution_id = urllib.parse.quote(str(args.execution_id), safe="")
        return _request_json(
            base_url,
            token,
            "PATCH",
            f"/api/v1/trades/{execution_id}/time",
            {
                "traded_at": args.traded_at,
                "expected_revision": args.expected_revision,
            },
        )
    parser.error("unknown trades action")
    return {}


def _token(args: argparse.Namespace, parser: argparse.ArgumentParser) -> str:
    if args.token_stdin:
        token = sys.stdin.readline().strip()
    else:
        token = os.environ.get("MASTERSTOCK_AGENT_TOKEN", "").strip()
    if not token:
        parser.error("缺少 Agent Token；请设置 MASTERSTOCK_AGENT_TOKEN 或使用 --token-stdin")
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


def _load_json_object(path: str, parser: argparse.ArgumentParser) -> dict[str, Any]:
    try:
        value = json.loads(
            sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"无法读取 JSON：{exc}")
    if not isinstance(value, dict):
        parser.error("JSON 必须是对象")
    return value


def _load_reading_payload(path: str, parser: argparse.ArgumentParser) -> dict[str, Any]:
    source_path = Path(path)
    if source_path.is_dir():
        source_path = source_path / "index.md"
    try:
        source = sys.stdin.read() if path == "-" else source_path.read_text(encoding="utf-8")
    except OSError as exc:
        parser.error(f"无法读取阅读感想：{exc}")
    if not source.startswith("+++\n"):
        parser.error("Markdown 必须以 TOML front matter 开头")
    closing = source.find("\n+++\n", 4)
    if closing < 0:
        parser.error("TOML front matter 没有结束标记")
    try:
        metadata = tomllib.loads(source[4:closing])
    except tomllib.TOMLDecodeError as exc:
        parser.error(f"TOML front matter 无效：{exc}")
    body = source[closing + 5 :].strip()
    slug = str(metadata.get("slug") or (source_path.parent.name if path != "-" else ""))
    tags = metadata.get("tags")
    assets: dict[str, dict[str, str]] = {}
    image_sources = [*_READING_IMAGE.findall(body), *_READING_HTML_IMAGE.findall(body)]
    for image_source in image_sources:
        if image_source.startswith(("https://", "http://", "/")):
            continue
        if path == "-":
            parser.error("从标准输入读取时不能使用相对路径图片")
        asset_path = (source_path.parent / image_source).resolve()
        article_dir = source_path.parent.resolve()
        if not asset_path.is_relative_to(article_dir) or not asset_path.is_file():
            parser.error(f"图片缺失或路径不安全：{image_source}")
        media_type = mimetypes.guess_type(asset_path.name)[0] or ""
        try:
            data = asset_path.read_bytes()
        except OSError as exc:
            parser.error(f"无法读取图片 {image_source}：{exc}")
        assets[image_source] = {
            "media_type": media_type,
            "data": base64.b64encode(data).decode("ascii"),
        }
    payload = {
        "title": metadata.get("title"),
        "slug": slug,
        "summary": metadata.get("summary"),
        "tags": tags,
        "body_markdown": body,
        "metadata": {
            key: value
            for key, value in metadata.items()
            if key not in {"title", "slug", "summary", "tags", "status", "published_on"}
        },
        "assets": assets,
    }
    # Keep local validation identical to the server contract without importing Web state.
    from ..web.reading_notes import ReadingNoteService

    return ReadingNoteService.validate_payload(payload)


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
