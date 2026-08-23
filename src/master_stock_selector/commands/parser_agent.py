from __future__ import annotations

import os

from .parser_types import ParserRegistrar, SubparserRegistry


def register_agent(subparsers: SubparserRegistry) -> None:
    parser = subparsers.add_parser("agent", help="通过 API Token 调用个人交易 Agent 接口")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MASTERSTOCK_AGENT_URL", "https://longchangwudao.cn"),
        help=(
            "大师选股网站地址（默认：https://longchangwudao.cn）；也可使用 MASTERSTOCK_AGENT_URL"
        ),
    )
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="从标准输入第一行读取 Token；默认读取 MASTERSTOCK_AGENT_TOKEN",
    )
    actions = parser.add_subparsers(dest="agent_action", required=True)
    actions.add_parser("me", help="验证 Token 和当前账户")

    trades = actions.add_parser("trades", help="预检、批量录入和查询交易")
    trade_actions = trades.add_subparsers(dest="trade_action", required=True)

    validate = trade_actions.add_parser("validate", help="预检 JSON 交易批次，不写入")
    validate.add_argument("file", help="JSON 文件；使用 - 从标准输入读取")

    import_parser = trade_actions.add_parser("import", help="预检或提交 JSON 交易批次")
    import_parser.add_argument("file", help="JSON 文件；使用 - 从标准输入读取")
    import_parser.add_argument("--commit", action="store_true", help="确认提交；省略时仅预检")
    import_parser.add_argument("--idempotency-key", default="")

    list_parser = trade_actions.add_parser("list", help="查询已记录交易")
    list_parser.add_argument("--symbol", default="")
    list_parser.add_argument("--date-from", default="")
    list_parser.add_argument("--date-to", default="")
    list_parser.add_argument("--limit", type=int, default=200)

    get_parser = trade_actions.add_parser("get", help="按 execution_id 查询交易")
    get_parser.add_argument("execution_id")

    stop = trade_actions.add_parser("set-stop", help="只更新既有 BUY 的止损价")
    stop.add_argument("execution_id")
    stop.add_argument("stop_price", type=float)
    stop.add_argument("--expected-revision", type=int, required=True)
    stop.add_argument("--commit", action="store_true", help="确认修改止损")

    plans = actions.add_parser("plans", help="读取或讨论个人交易计划")
    plan_actions = plans.add_subparsers(dest="plan_action", required=True)
    plan_actions.add_parser("current")
    plan_actions.add_parser("versions")
    show = plan_actions.add_parser("show")
    show.add_argument("version_id")
    diff = plan_actions.add_parser("diff")
    diff.add_argument("old_version_id")
    diff.add_argument("new_version_id")
    grill = plan_actions.add_parser("grill", help="确定性 Grill 讨论")
    grill_actions = grill.add_subparsers(dest="grill_action", required=True)
    start = grill_actions.add_parser("start")
    start.add_argument("--template", default="blank")
    start.add_argument("--from-version", default="")
    start.add_argument("--change-goal", default="")
    status = grill_actions.add_parser("status")
    status.add_argument("session_id")
    next_parser = grill_actions.add_parser("next")
    next_parser.add_argument("session_id")
    answer = grill_actions.add_parser("answer")
    answer.add_argument("session_id")
    answer.add_argument("--file", required=True)
    validate_plan = grill_actions.add_parser("validate")
    validate_plan.add_argument("session_id")
    publish = grill_actions.add_parser("publish")
    publish.add_argument("session_id")
    publish.add_argument("--expected-revision", type=int, required=True)
    publish.add_argument("--commit", action="store_true")


REGISTRARS: dict[str, ParserRegistrar] = {"agent": register_agent}
