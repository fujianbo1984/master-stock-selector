"""Private, versioned trading plans and deterministic Grill discussions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from .users import UserRepository

GRILL_VERSION = "trading-plan-grill/1"
SCHEMA_VERSION = "trading-plan/1"
ANSWER_STATES = {"CONFIRMED", "PROPOSED", "DEFERRED", "NOT_APPLICABLE"}


def _question(
    section: str,
    identifier: str,
    title: str,
    prompt: str,
    choices: tuple[str, ...] = (),
) -> dict[str, Any]:
    question: dict[str, Any] = {
        "id": identifier,
        "section": section,
        "title": title,
        "prompt": prompt,
        "why": "这是可执行、可复盘交易合同的一部分。",
        "answer_schema": {"type": "string", "minLength": 1},
    }
    if choices:
        question["choices"] = choices
        question["answer_schema"]["enum"] = list(choices)
    return question


QUESTIONS = tuple(
    _question(*item)
    for item in (
        ("operations", "scope.market", "交易边界", "交易市场、方向和周期是什么？"),
        ("operations", "scope.universe", "股票池", "排除哪些股票或市场？"),
        ("operations", "trend.background", "趋势背景", "所有形态需满足什么趋势背景？"),
        ("operations", "workflow.daily", "日常流程", "收盘后扫描、盘中和周末如何执行？"),
        ("operations", "review.recording", "记录与复盘", "每笔交易和每周复盘必须记录什么？"),
        ("operations", "event.filter", "事件过滤", "财报、解禁、除权复牌等事件是否过滤？"),
        ("risk", "risk.risk_percent", "1R 风险比例", "1R占账户权益百分之几？"),
        (
            "risk",
            "risk.position_sizing",
            "单笔仓位合同",
            "每笔必须使用完整1R，还是允许资金不足时缩小仓位？",
            ("FULL_R", "ALLOW_REDUCED"),
        ),
        ("risk", "risk.equity_base", "权益基数", "账户权益如何定义？"),
        ("risk", "risk.reset", "1R 重设", "何时重设1R权益基数？"),
        ("risk", "risk.rounding", "股数取整", "A股整手/零股如何处理？"),
        ("risk", "risk.slippage", "高开与滑点", "实际成交价偏离计划时是否缩减仓位？"),
        (
            "risk",
            "portfolio.cash_shortage",
            "资金不足",
            "不能建立完整1R时如何处理？",
            ("SKIP", "REDUCE", "MARGIN"),
        ),
        ("risk", "portfolio.total_r", "组合总R", "是否限制组合总风险R数？"),
        ("risk", "portfolio.industry_scheme", "行业口径", "使用哪一版行业分类？"),
        (
            "risk",
            "portfolio.industry_count_unit",
            "行业计数",
            "同一股票双仓按股票、仓位还是R计数？",
            ("DISTINCT_SYMBOL", "POSITION", "RISK_UNIT"),
        ),
        ("risk", "portfolio.related_groups", "相关风险组", "黄金白银等相关资产是否合并限制？"),
        ("execution", "execution.entry", "公共买入", "触发价、条件单有效期和涨停未成交如何处理？"),
        ("execution", "execution.exit", "公共退出", "止损、部分止盈、移动止损和T+1如何执行？"),
        ("setups", "A1.rules", "A1 突破前入场", "A1识别、初始止损和第一目标是什么？"),
        ("setups", "A3.rules", "A3 首次回调", "A3识别、初始止损和第一目标是什么？"),
        ("setups", "B1.rules", "B1 简单回调", "B1识别、初始止损和第一目标是什么？"),
        ("setups", "B2.rules", "B2 复杂回调", "B2识别、初始止损和第一目标是什么？"),
        ("setups", "setup.reentry", "重入与叠加", "止损后重入、A1+A3和B1转B2如何处理？"),
        ("setups", "setup.enabled", "启用形态", "启用哪些交易形态？"),
    )
)

DEMO_ANSWERS = {
    "scope.market": "沪深主板、科创板、创业板；只做多，日线分析与触发。",
    "scope.universe": "排除北交所、ST、*ST及前一交易日总市值低于50亿元的股票；不设流动性过滤。",
    "trend.background": "必须符合Minervini趋势模板或Weinstein第二阶段；黄金白银板块可主观参考COMEX多头背景。",
    "workflow.daily": "收盘后扫描、人工判断形态、计算触发止损与仓位，设置仅下一交易日有效的条件单；盘中不增加计划外交易。",
    "review.recording": "每笔保存入场前截图、形态及版本、计划与实际成交、止损、股数、名义1R、实际风险、R倍数、遵守度和主观依据；每周按形态复盘。",
    "event.filter": "均不设事件过滤，形态合格即交易；公司行动与条件单调整按东方财富规则。",
    "risk.risk_percent": "1R=账户净权益×2%，计算结果按千元四舍五入。",
    "risk.position_sizing": "FULL_R",
    "risk.equity_base": "账户净权益=现金+证券市值-融资负债及应计利息。",
    "risk.reset": "只有权益达到上次基准的200%或50%时才重设1R。",
    "risk.rounding": "计划股数向下取整至100股；部分止盈由交易软件按可成交规则取最接近一半。",
    "risk.slippage": "高开和滑点后不缩减计划股数；统计R时使用实际成交后风险。",
    "portfolio.industry_count_unit": "DISTINCT_SYMBOL",
    "portfolio.cash_shortage": "MARGIN",
    "portfolio.total_r": "不设最大持股数、组合总R上限、个人融资上限或回撤暂停线。",
    "portfolio.industry_scheme": "申万2021版三级行业，每个行业最多同时持有2只不同股票。",
    "portfolio.related_groups": "黄金和白银合并为同一相关风险组，合计最多2只股票。",
    "execution.entry": "触发价=前一交易日最高价+0.01元；按即时卖一买入；高开不设追价上限；条件单当日有效；涨停未成交次日不自动追入。",
    "execution.exit": "成交后建立初始止损；按即时买一卖出；T+1、跳空和跌停按实际可成交规则处理；第一目标卖出最接近一半，剩余仓位移动止损只能提高。",
    "A1.rules": "Adam式压力积累、VCP或VCP Cheat；止损=整理结构最低点-0.01元；第一目标=实际成交价+实际初始每股风险。",
    "A3.rules": "成功突破重要阻力后的第一次受控回调；收盘跌回突破位下方则成交前失效；止损=回调低点-入场前一日ATR(14)；第一目标为实际1R。",
    "B1.rules": "既有上升趋势中的受控简单回调；止损=回调低点-入场前一日ATR(14)；第一目标为实际1R。",
    "B2.rules": "两段清晰逆势运动，高2、楔形或显性/隐藏ABCD；到前高空间不少于计划1R；止损=第二腿低点-0.01元；第一目标为前一摆动高点。",
    "setup.reentry": "同一形态止损后重新合格可以完整1R重入；A1持仓后可另建A3；B1未止损但演化成B2时不叠加。",
    "setup.enabled": "A1,A3,B1,B2",
}


class PlanConflict(ValueError):
    pass


class TradingPlanService:
    def __init__(self, users: "UserRepository") -> None:
        self.users = users

    def current(self, user_id: str) -> dict[str, Any] | None:
        with self.users.connect(read_only=True) as c:
            row = c.execute(
                """SELECT v.* FROM user_trading_plan p JOIN user_trading_plan_version v
                ON v.version_id=p.current_version_id WHERE p.user_id=?""",
                (user_id,),
            ).fetchone()
        return self._version(row) if row else None

    def versions(self, user_id: str) -> list[dict[str, Any]]:
        with self.users.connect(read_only=True) as c:
            rows = c.execute(
                """SELECT v.* FROM user_trading_plan p JOIN user_trading_plan_version v
              ON v.plan_id=p.plan_id WHERE p.user_id=? ORDER BY v.created_at DESC""",
                (user_id,),
            ).fetchall()
        return [self._version(row) for row in rows]

    def show(self, user_id: str, version_id: str) -> dict[str, Any] | None:
        with self.users.connect(read_only=True) as c:
            row = c.execute(
                """SELECT v.* FROM user_trading_plan_version v JOIN user_trading_plan p ON p.plan_id=v.plan_id
              WHERE p.user_id=? AND v.version_id=?""",
                (user_id, version_id),
            ).fetchone()
        return self._version(row) if row else None

    def start(
        self,
        user_id: str,
        *,
        template: str = "blank",
        from_version: str = "",
        change_goal: str = "",
    ) -> dict[str, Any]:
        if template not in {"blank", "demo", "personal-a-share-v1"}:
            raise ValueError("交易计划模板不存在")
        if from_version and template != "blank":
            raise ValueError("不能同时指定模板和来源版本")
        answers: dict[str, Any] = {}
        if template in {"demo", "personal-a-share-v1"}:
            answers = {
                key: {"state": "PROPOSED", "value": value} for key, value in DEMO_ANSWERS.items()
            }
        with self.users.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            plan = c.execute(
                "SELECT plan_id,current_version_id FROM user_trading_plan WHERE user_id=?",
                (user_id,),
            ).fetchone()
            plan_id = str(plan["plan_id"]) if plan else uuid4().hex
            head_version_id = str(plan["current_version_id"] or "") if plan else ""
            base_version_id = ""
            if from_version:
                requested_version_id = (
                    head_version_id if from_version == "current" else from_version
                )
                if not requested_version_id:
                    raise ValueError("来源版本不存在")
                if requested_version_id != head_version_id:
                    raise PlanConflict("只能从当前生效版本建立新版本")
                base_row = c.execute(
                    """SELECT content_json FROM user_trading_plan_version
                    WHERE version_id=? AND plan_id=?""",
                    (requested_version_id, plan_id),
                ).fetchone()
                if base_row is None:
                    raise ValueError("来源版本不存在")
                base_content = json.loads(str(base_row["content_json"]))
                answers = {
                    key: {**value, "state": "PROPOSED"}
                    for key, value in base_content.get("answers", {}).items()
                }
                base_version_id = requested_version_id
            if not plan:
                c.execute(
                    "INSERT INTO user_trading_plan(plan_id,user_id,title) VALUES(?,?,?)",
                    (plan_id, user_id, "我的交易计划"),
                )
            session_id = uuid4().hex
            c.execute(
                """INSERT INTO user_plan_grill_session(
                    session_id,user_id,plan_id,base_version_id,head_version_id_at_start,
                    grill_version,change_goal,status,answers_json,revision
                ) VALUES(?,?,?,?,?,?,?, 'ACTIVE', ?, 1)""",
                (
                    session_id,
                    user_id,
                    plan_id,
                    base_version_id,
                    head_version_id,
                    GRILL_VERSION,
                    change_goal[:500],
                    json.dumps(answers, ensure_ascii=False),
                ),
            )
        return self.status(user_id, session_id) or {}

    def status(self, user_id: str, session_id: str) -> dict[str, Any] | None:
        with self.users.connect(read_only=True) as c:
            row = c.execute(
                "SELECT * FROM user_plan_grill_session WHERE session_id=? AND user_id=?",
                (session_id, user_id),
            ).fetchone()
        if not row:
            return None
        answers = json.loads(str(row["answers_json"]))
        return {
            "session_id": session_id,
            "status": row["status"],
            "revision": row["revision"],
            "grill_version": row["grill_version"],
            "base_version_id": row["base_version_id"],
            "head_version_id_at_start": row["head_version_id_at_start"],
            "change_goal": row["change_goal"],
            "published_version_id": row["published_version_id"],
            "progress": {
                "confirmed": sum(1 for v in answers.values() if v.get("state") == "CONFIRMED"),
                "total": len(QUESTIONS),
                "unresolved": sum(
                    1
                    for q in QUESTIONS
                    if q["id"] not in answers or answers[q["id"]].get("state") != "CONFIRMED"
                ),
            },
            "answers": answers,
        }

    def next(self, user_id: str, session_id: str) -> dict[str, Any]:
        status = self.status(user_id, session_id)
        if not status:
            raise ValueError("Grill 会话不存在")
        for q in QUESTIONS:
            if status["answers"].get(q["id"], {}).get("state") != "CONFIRMED":
                return {
                    "status": "QUESTION",
                    "session_id": session_id,
                    "revision": status["revision"],
                    "grill_version": GRILL_VERSION,
                    "progress": status["progress"],
                    "question": {
                        "question_id": q["id"],
                        "section": q["section"],
                        "title": q["title"],
                        "prompt": q["prompt"],
                        "why": q["why"],
                        "answer_schema": q["answer_schema"],
                        "choices": [{"value": x, "label": x} for x in q.get("choices", [])],
                    },
                }
        return {
            "status": "COMPLETE",
            "session_id": session_id,
            "revision": status["revision"],
            "progress": status["progress"],
        }

    def answer(
        self,
        user_id: str,
        session_id: str,
        payload: Mapping[str, Any],
        *,
        actor_type: str = "AGENT_CLI",
    ) -> dict[str, Any]:
        qid = str(payload.get("question_id") or "")
        state = str((payload.get("decision") or {}).get("state") or "").upper()
        if qid not in {q["id"] for q in QUESTIONS} or state not in ANSWER_STATES:
            raise ValueError("问题或回答状态不正确")
        expected = payload.get("expected_revision")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise ValueError("expected_revision 必须为正整数")
        decision = payload.get("decision")
        if not isinstance(decision, Mapping):
            raise ValueError("decision 必须为对象")
        value = decision.get("value")
        question = next(q for q in QUESTIONS if q["id"] == qid)
        allowed_values = set(question.get("choices", ()))
        if state == "CONFIRMED" and (value is None or str(value).strip() == ""):
            raise ValueError("确认回答不能为空")
        if state == "CONFIRMED" and allowed_values and value not in allowed_values:
            raise ValueError("回答不在允许的选项中")
        with self.users.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM user_plan_grill_session WHERE session_id=? AND user_id=?",
                (session_id, user_id),
            ).fetchone()
            if not row:
                raise ValueError("Grill 会话不存在")
            if str(row["status"]) != "ACTIVE":
                raise PlanConflict("已发布或关闭的 Grill 会话不可修改")
            if expected != int(row["revision"]):
                raise PlanConflict("草稿已被其他操作更新")
            answers = json.loads(str(row["answers_json"]))
            answers[qid] = {
                "state": state,
                "value": value,
                "user_summary": str(decision.get("user_summary") or "")[:1000],
            }
            revision = int(row["revision"]) + 1
            cursor = c.execute(
                """UPDATE user_plan_grill_session
                SET answers_json=?,revision=?,updated_at=CURRENT_TIMESTAMP
                WHERE session_id=? AND user_id=? AND status='ACTIVE' AND revision=?""",
                (
                    json.dumps(answers, ensure_ascii=False),
                    revision,
                    session_id,
                    user_id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                raise PlanConflict("草稿已被其他操作更新")
            c.execute(
                "INSERT INTO user_plan_grill_event(event_id,session_id,question_id,event_type,value_json,revision,actor_type) VALUES(?,?,?,?,?,?,?)",
                (
                    uuid4().hex,
                    session_id,
                    qid,
                    "ANSWER",
                    json.dumps(answers[qid], ensure_ascii=False),
                    revision,
                    actor_type,
                ),
            )
        return self.next(user_id, session_id)

    def validate(self, user_id: str, session_id: str) -> dict[str, Any]:
        status = self.status(user_id, session_id)
        if not status:
            raise ValueError("Grill 会话不存在")
        answers = status["answers"]
        missing = [
            q["id"] for q in QUESTIONS if answers.get(q["id"], {}).get("state") != "CONFIRMED"
        ]
        conflicts = []
        warnings = self._warnings(answers)
        if (
            answers.get("risk.position_sizing", {}).get("value") == "FULL_R"
            and answers.get("portfolio.cash_shortage", {}).get("value") == "REDUCE"
        ):
            conflicts.append(
                {
                    "conflict_id": "cash.full_r_conflict",
                    "severity": "BLOCKING",
                    "rules": ["risk.position_sizing", "portfolio.cash_shortage"],
                    "message": "每笔必须完整1R与资金不足时缩小仓位不能同时执行。",
                }
            )
        change_class = self._change_class(user_id, status)
        return {
            "status": "READY" if not missing and not conflicts else "NEEDS_RESOLUTION",
            "draft_revision": status["revision"],
            "blocking_issues": [
                {"question_id": x, "message": "尚未得到会员明确确认"} for x in missing
            ]
            + conflicts,
            "warnings": warnings,
            "change_class": change_class,
            "evaluation_effect": (
                "CONTINUE_CURRENT_CYCLE" if change_class == "MINOR" else "START_NEW_CYCLE"
            ),
            "content": self._content(answers),
        }

    def publish(self, user_id: str, session_id: str, expected_revision: int) -> dict[str, Any]:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision 必须为正整数")
        report = self.validate(user_id, session_id)
        if report["blocking_issues"]:
            raise ValueError("草稿尚未完成，不能发布")
        with self.users.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            s = c.execute(
                "SELECT * FROM user_plan_grill_session WHERE session_id=? AND user_id=?",
                (session_id, user_id),
            ).fetchone()
            if not s:
                raise ValueError("Grill 会话不存在")
            if str(s["status"]) != "ACTIVE":
                raise PlanConflict("Grill 会话已经发布，不能重复发布")
            if int(s["revision"]) != expected_revision:
                raise PlanConflict("草稿已被其他操作更新")
            plan_id = str(s["plan_id"])
            prior = c.execute(
                "SELECT current_version_id FROM user_trading_plan WHERE plan_id=?", (plan_id,)
            ).fetchone()
            prior_id = str(prior["current_version_id"] or "")
            head_at_start = str(s["head_version_id_at_start"] or "")
            if head_at_start != prior_id:
                raise PlanConflict("当前计划已有新版本，请从最新版本重新开始 Grill")
            prior_row = (
                c.execute(
                    "SELECT version_number, evaluation_cycle_id "
                    "FROM user_trading_plan_version WHERE version_id=?",
                    (prior_id,),
                ).fetchone()
                if prior_id
                else None
            )
            minor = report["change_class"] == "MINOR"
            if prior_row is None:
                version = "v0.1"
            else:
                major, patch = str(prior_row["version_number"]).removeprefix("v").split(".")
                version = f"v{major}.{int(patch) + 1}" if minor else f"v{int(major) + 1}.0"
            version_id = uuid4().hex
            cycle = (
                str(prior_row["evaluation_cycle_id"])
                if minor and prior_row is not None
                else uuid4().hex
            )
            content = report["content"]
            raw = json.dumps(content, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            if prior_id:
                c.execute(
                    "UPDATE user_trading_plan_version SET status='ARCHIVED',archived_at=CURRENT_TIMESTAMP WHERE version_id=?",
                    (prior_id,),
                )
            c.execute(
                """INSERT INTO user_trading_plan_version(version_id,plan_id,version_number,status,schema_version,content_json,change_summary,change_class,evaluation_cycle_id,supersedes_version_id,content_hash,activated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    version_id,
                    plan_id,
                    version,
                    "ACTIVE",
                    SCHEMA_VERSION,
                    raw,
                    "Grill 发布",
                    report["change_class"],
                    cycle,
                    prior_id,
                    digest,
                ),
            )
            plan_cursor = c.execute(
                """UPDATE user_trading_plan
                SET current_version_id=?,updated_at=CURRENT_TIMESTAMP
                WHERE plan_id=? AND COALESCE(current_version_id,'')=?""",
                (version_id, plan_id, prior_id),
            )
            if plan_cursor.rowcount != 1:
                raise PlanConflict("当前计划已有新版本，请从最新版本重新开始 Grill")
            session_cursor = c.execute(
                """UPDATE user_plan_grill_session
                SET status='PUBLISHED',published_version_id=?,updated_at=CURRENT_TIMESTAMP
                WHERE session_id=? AND user_id=? AND status='ACTIVE' AND revision=?""",
                (version_id, session_id, user_id, expected_revision),
            )
            if session_cursor.rowcount != 1:
                raise PlanConflict("草稿已被其他操作更新")
        return {
            "status": "PUBLISHED",
            "version": version,
            "version_id": version_id,
            "evaluation_cycle": cycle,
            "content_hash": "sha256:" + digest,
        }

    def diff(self, user_id: str, old: str, new: str) -> dict[str, Any]:
        a = self.show(user_id, old)
        b = self.show(user_id, new)
        if not a or not b:
            raise ValueError("版本不存在")
        aa = a["content"].get("answers", {})
        bb = b["content"].get("answers", {})
        keys = sorted(set(aa) | set(bb))
        return {
            "old_version_id": old,
            "new_version_id": new,
            "changes": [
                {"question_id": k, "old": aa.get(k), "new": bb.get(k)}
                for k in keys
                if aa.get(k) != bb.get(k)
            ],
        }

    def _content(self, answers: dict[str, Any]) -> dict[str, Any]:
        layers: dict[str, dict[str, Any]] = {"operations": {}, "risk": {}, "setups": {}}
        for question in QUESTIONS:
            layer = "operations" if question["section"] == "execution" else question["section"]
            layers[layer][question["id"].split(".", 1)[-1]] = answers.get(question["id"], {})
        return {
            "schema_version": SCHEMA_VERSION,
            "answers": answers,
            "layers": layers,
            "risk_warnings": self._warnings(answers),
        }

    @staticmethod
    def _warnings(answers: Mapping[str, Any]) -> list[dict[str, str]]:
        warnings: list[dict[str, str]] = []
        if answers.get("portfolio.cash_shortage", {}).get("value") == "MARGIN":
            warnings.append(
                {"code": "MARGIN_ENABLED", "message": "计划允许融资，实际风险可能放大。"}
            )
        if "2%" in str(answers.get("risk.risk_percent", {}).get("value") or ""):
            warnings.append(
                {"code": "RISK_2_PERCENT", "message": "单笔名义风险为账户净权益的 2%。"}
            )
        if "不设" in str(answers.get("portfolio.total_r", {}).get("value") or ""):
            warnings.append(
                {
                    "code": "UNBOUNDED_PORTFOLIO_RISK",
                    "message": "计划不设组合总R或账户回撤上限。",
                }
            )
        if "不设事件过滤" in str(answers.get("event.filter", {}).get("value") or ""):
            warnings.append(
                {
                    "code": "NO_EVENT_FILTER",
                    "message": "计划不回避财报、解禁、复牌等已知事件风险。",
                }
            )
        return warnings

    def _change_class(self, user_id: str, status: Mapping[str, Any]) -> str:
        base_version_id = str(status.get("head_version_id_at_start") or "")
        if not base_version_id:
            return "SUBSTANTIVE"
        base = self.show(user_id, base_version_id)
        if base is None:
            return "SUBSTANTIVE"
        base_answers = base["content"].get("answers", {})
        current_answers = status["answers"]
        for question in QUESTIONS:
            identifier = question["id"]
            if base_answers.get(identifier, {}).get("value") != current_answers.get(
                identifier, {}
            ).get("value"):
                return "SUBSTANTIVE"
        return "MINOR"

    def _version(self, row: Any) -> dict[str, Any]:
        value = dict(row)
        value["content"] = json.loads(str(value.pop("content_json")))
        return value
