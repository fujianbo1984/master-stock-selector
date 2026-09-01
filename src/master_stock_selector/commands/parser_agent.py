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

    observations = actions.add_parser("observations", help="管理当前账号的“我的观察”")
    observation_actions = observations.add_subparsers(
        dest="observation_action", required=True
    )
    observation_add = observation_actions.add_parser(
        "add", help="将大师观察池股票加入“我的观察”"
    )
    observation_add.add_argument("symbol")
    observation_add.add_argument("--tag", action="append", default=[])
    observation_add.add_argument("--note", default="")
    observation_add.add_argument("--commit", action="store_true", help="确认加入我的观察")

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

    trade_time = trade_actions.add_parser("set-time", help="补全既有交易的成交时间")
    trade_time.add_argument("execution_id")
    trade_time.add_argument("traded_at", help="成交时间，格式 HH:MM:SS")
    trade_time.add_argument("--expected-revision", type=int, required=True)
    trade_time.add_argument("--commit", action="store_true", help="确认补全成交时间")

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

    reading = actions.add_parser("reading", help="编写、提交和共享阅读感想")
    reading_actions = reading.add_subparsers(dest="reading_action", required=True)
    reading_actions.add_parser("list", help="列出自己的私有阅读感想")
    reading_show = reading_actions.add_parser("show", help="查看私有草稿和已提交版本")
    reading_show.add_argument("note_id")
    reading_validate = reading_actions.add_parser("validate", help="在本地检查 Markdown 文件")
    reading_validate.add_argument("path")
    reading_save = reading_actions.add_parser("save", help="保存私有草稿")
    reading_save.add_argument("path")
    reading_save.add_argument("--note-id", default="")
    reading_save.add_argument("--expected-revision", type=int)
    reading_save.add_argument("--commit", action="store_true")
    reading_submit = reading_actions.add_parser("submit", help="提交不可变的私有版本")
    reading_submit.add_argument("note_id")
    reading_submit.add_argument("--expected-revision", type=int, required=True)
    reading_submit.add_argument("--commit", action="store_true")
    reading_share = reading_actions.add_parser("share", help="站长共享已提交版本")
    reading_share.add_argument("note_id")
    reading_share.add_argument("--version-id", required=True)
    reading_share.add_argument("--statement", default="")
    reading_share.add_argument("--commit", action="store_true")
    reading_unshare = reading_actions.add_parser("unshare", help="站长停止共享")
    reading_unshare.add_argument("note_id")
    reading_unshare.add_argument("--commit", action="store_true")


REGISTRARS: dict[str, ParserRegistrar] = {"agent": register_agent}
