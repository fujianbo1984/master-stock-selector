"""Private, versioned trading plans and deterministic Grill discussions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .display_time import sqlite_utc_to_shanghai
from .plan_setups import PLAN_SETUP_DEFINITIONS, plan_setup_choices_text

if TYPE_CHECKING:
    from .users import UserRepository

GRILL_VERSION = "trading-plan-grill/3"
SCHEMA_VERSION = "trading-plan/4"
ANSWER_STATES = {"CONFIRMED", "PROPOSED", "DEFERRED", "NOT_APPLICABLE"}

SETUP_TEXT_FIELDS = (
    "eligibility",
    "structure",
    "trigger",
    "invalidation",
    "initial_stop",
    "first_target",
    "management",
    "reentry",
    "evidence",
)
SETUP_FIELD_LABELS = {
    "eligibility": "适用范围",
    "structure": "结构定义",
    "trigger": "触发条件",
    "invalidation": "候选失效",
    "initial_stop": "初始止损",
    "first_target": "第一目标",
    "management": "后续管理",
    "reentry": "重入与重叠",
    "evidence": "入场前证据",
}
SETUP_CYCLE_FIELDS = (
    "eligibility", "structure", "trigger", "invalidation", "initial_stop",
    "first_target", "management", "reentry", "rules",
)
COMMON_CYCLE_QUESTION_IDS = frozenset(
    {
        "scope.market", "scope.universe", "trend.background", "risk.risk_percent",
        "risk.position_sizing", "risk.equity_base", "risk.reset", "risk.rounding",
        "risk.slippage", "execution.entry", "execution.exit", "setup.reentry",
    }
)

ANSWER_VALUE_LABELS = {
    "FULL_R": "每笔使用完整 1R",
    "ALLOW_REDUCED": "允许资金不足时缩小仓位",
    "SKIP": "资金不足时放弃交易",
    "REDUCE": "资金不足时缩小仓位",
    "MARGIN": "现金不足时使用融资，额度仍不足则放弃",
    "DISTINCT_SYMBOL": "按不同股票计数（同一股票双仓仍计 1 只）",
    "POSITION": "按独立仓位计数",
    "RISK_UNIT": "按风险单位计数",
}

PLAN_SECTIONS = (
    ("operations", "第一层", "交易运营计划"),
    ("risk", "第二层", "类别、风险与目标"),
    ("setups", "第三层", "入场形态表"),
)

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
        (
            "setups",
            "C1.rules",
            "C1 区间失败测试",
            "C1区间、失败测试、强下跌后二次入场、初始止损和第一目标是什么？",
        ),
        (
            "setups",
            "C2.rules",
            "C2 底部区间突破后的首次回调",
            "C2区间、突破第一腿、首次回调、初始止损和第一目标是什么？",
        ),
        ("setups", "setup.reentry", "重入与叠加", "止损后重入、A1+A3和B1转B2如何处理？"),
        ("setups", "setup.enabled", "启用形态", "启用哪些交易形态？"),
    )
)

DEMO_ANSWERS = {
    "scope.market": "沪深主板、科创板、创业板；只做多；日线分析、日线触发；平均持仓约20个交易日，但不设时间退出。",
    "scope.universe": "排除北交所、ST、*ST及以前一交易日收盘总市值计算低于50亿元的股票；不设置流动性过滤。",
    "trend.background": "全部形态必须先进入软件以Minervini趋势模板或Weinstein第二阶段形成的观察池。A1、A3、B1、B2使用趋势延续逻辑；C1只在该观察池内人工识别长期横向区间，不作为第三种选股方法。黄金、白银板块个股仍须符合A股基础股票池规则，但不要求满足上述趋势模板，可由交易者主观参考COMEX黄金或白银日线多头背景。",
    "workflow.daily": "每个交易日收盘后扫描全部A股；软件完成趋势模板筛选；人工判断A1、A3、B1、B2，并在现有观察池内判断C1；计算触发价、止损、仓位和第一目标；设置仅下一交易日有效的条件单；周五工作可延迟至周日晚；盘中不临时增加候选或计划外交易；收盘后重新评估未触发候选并更新全部持仓止损。C1人工记录区间上下沿、ATR(14)、区间起始日、证据日期及首次/二次入场类型。",
    "review.recording": "每笔记录入场前截图、形态类别及版本、计划触发价和实际成交价、初始止损和股数、名义1R和成交后实际风险、盈亏R倍数、是否遵守计划及主观判断依据；每周按A1、A3、B1、B2、C1分别复盘。C1总体使用独立30笔口径，首次与二次入场保留子类型并分别展示描述统计。每类至少30笔已平仓交易后，以毛平均R大于0作初步评价；不扣佣金、融资利息和滑点。实质修改入场、止损、仓位或退出规则后建立新版本并重新累计30笔，不因灾难性风险提前暂停。",
    "event.filter": "不设置财报、业绩预告、解禁、除权、复牌等事件过滤；公司行动和条件单价格调整按东方财富规则处理。",
    "risk.risk_percent": "重设时1R=账户净权益×2%，计算结果按千元四舍五入；接受实际风险比例约1%—4%漂移。",
    "risk.position_sizing": "FULL_R",
    "risk.equity_base": "账户净权益=现金＋证券市值－融资负债及应计利息。",
    "risk.reset": "只有权益达到上次基准的200%或50%时才重设1R。",
    "risk.rounding": "计划股数=1R÷（计划触发价－初始止损价），买入股数向下取整至100股；第一目标按可成交数量卖出最接近一半。",
    "risk.slippage": "股数根据计划触发价计算；实际成交价因高开或滑点上升时不缩减仓位，仍使用计划股数；接受实际初始风险超过名义1R；实际R倍数=交易毛盈亏÷［（实际成交价－初始止损价）×实际股数］。",
    "portfolio.industry_count_unit": "DISTINCT_SYMBOL",
    "portfolio.cash_shortage": "MARGIN",
    "portfolio.total_r": "不设最大持股数量、组合总风险R上限、个人融资上限、单日亏损停止线、连续亏损暂停线、账户回撤降风险、市场转弱停止开仓、强制清仓或交易笔数和利润目标；不因灾难性风险提前暂停；信号按触发先后顺序执行；没有合格信号就不交易。",
    "portfolio.industry_scheme": "申万2021版三级行业；同一行业最多持有2只股票。",
    "portfolio.related_groups": "黄金和白银合并为同一相关风险组，合计最多2只股票。",
    "execution.entry": "所有形态触发价=前一交易日最高价+0.01元；触发后按即时卖一价买入；高开不设追价上限；使用计划股数，不因实际成交价上升而减仓；涨停触发但未成交不在次日自动追入；条件单当日有效，未触发则收盘后重新评估。",
    "execution.exit": "成交后立即建立初始止损条件单；因A股T+1，当日买入后即使跌破止损也无法卖出；止损触发按即时买一价卖出；次日跳空低于止损开盘立即卖出；跌停未成交则后续继续卖至成交。第一目标按即时买一价卖出最接近一半；第一目标前初始止损不变，之后剩余仓位按结构低点－0.01元移动止损；尚无结构低点或怀疑高潮时用前一交易日低点－0.01元；移动止损只能提高。",
    "A1.rules": "Adam式阻力下方压力积累、VCP或VCP Cheat；允许主观裁量，但必须在入场前截图并记录依据。初始止损=整理结构最低点－0.01元；第一目标=实际成交价＋实际初始每股风险，达到1R后卖出一半，剩余仓位按公共移动止损规则处理。",
    "A3.rules": "已经成功突破重要阻力，突破后有良好参与意愿和上行动量，首次上冲结束后出现第一次受控回调，按简单回调处理。成交前日线收盘重新跌回原突破位下方则取消；成交后不再以突破位得失退出。初始止损=回调最低点－入场前一交易日TradingView ATR(14)；第一目标为实际1R并卖出一半。",
    "B1.rules": "既有上升趋势中的一段受控逆势回调，没有明显强劲下行动量，以高1等简单回调转强；不要求前高至少提供1R空间。初始止损=回调最低点－入场前一交易日TradingView ATR(14)；第一目标为实际1R并卖出一半。",
    "B2.rules": "两段清晰逆势运动的高2、楔形或显性/隐藏ABCD复杂回调；计划触发价至前高空间至少为计划1R；初始止损=第二腿最低点－0.01元；第一目标=前一摆动高点并卖出一半。高开后即使到前高不足1R仍交易并以前高为目标；若成交价已超前高，T+1次日仍在前高上方立即卖一半，否则等待重回前高。",
    "C1.rules": "C1只分析当日Minervini或Weinstein观察池内股票。以信号日可见数据人工确认至少60个交易日的横向区间；本次测试前至少有两个相隔不少于10个交易日、落在同一支撑带的历史摆动低点，并有可辨认上沿。盘中跌破支撑带下沿、收盘重新进入带内后，下一交易日突破失败测试K线最高价0.01元买入。初始止损=本次失败测试最低价－信号日TradingView ATR(14)；实际1R卖出一半，剩余仓位按公共移动止损规则处理。若测试前连续至少3根阴线且累计下跌不少于2 ATR，不做首次承接；等待至少反弹1 ATR后再次下探，第二次测试低点不低于首次低点0.25 ATR以上且收盘重新进入支撑带，下一交易日突破第二次测试K线最高价0.01元买入；止损=两次测试最低价－第二次信号日ATR(14)。首次与二次均归入C1，但分别记录子类型。区间有效向下破位、向上突破或公司行动造成价格尺度不连续时取消观察。",
    "C2.rules": "符合基础A股股票池，不强制事先满足Minervini或Weinstein Stage 2。突破日前已存在至少60个交易日的横向区间，上下沿均有至少两次可辨认互动，上沿必须使用突破前可见数据确定。日线收盘有效站上上沿并形成可辨认向上第一腿后，只等待突破后第一次受控回调，不追第一腿末端。允许盘中测试上沿；入场前若日线收盘重新回到区间内部，候选失效。回调结束后突破恢复信号K线最高价+0.01元买入；初始止损=首次回调最低点-0.01元。实际1R卖出一半，余仓按公共结构低点移动止损。区间上沿只负责入场前取消，成交后由初始止损和公共退出规则管理。C2独立统计，不并入A3，不进入“两法同时符合”。",
    "setup.reentry": "同一形态止损后，重新评估合格可完整1R重入并独立统计；持有A1后形成A3可再加完整1R并分别管理统计；B1止损后形成B2可重新完整1R入场；B1未止损但演化B2则不增加B2仓位。C1强下跌后的二次测试不是新方案，按C1二次入场子类型记录；C1止损后只有重新形成完整合格结构才可重入。C2止损后必须重新形成合格突破和首次回调，不得将同一回调反复计为新C2。同一股票多方案持仓时，行业集中度仍按1只股票计算。",
    "setup.enabled": "A1,A3,B1,B2,C1,C2",
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

    def current_share(self, user_id: str) -> dict[str, Any] | None:
        with self.users.connect(read_only=True) as c:
            row = c.execute(
                """SELECT s.share_id, s.statement, s.status AS share_status,
                          s.published_at, s.revoked_at AS share_revoked_at, v.*
                   FROM user_trading_plan_share s
                   JOIN user_trading_plan p ON p.plan_id=s.plan_id
                   JOIN user_trading_plan_version v ON v.version_id=s.version_id
                   WHERE p.user_id=? AND s.status='ACTIVE'
                   ORDER BY s.published_at DESC, s.share_id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
        return self._shared_version(row) if row else None

    def latest_share(self, user_id: str) -> dict[str, Any] | None:
        with self.users.connect(read_only=True) as c:
            row = c.execute(
                """SELECT s.share_id, s.statement, s.status AS share_status,
                          s.published_at, s.revoked_at AS share_revoked_at, v.*
                   FROM user_trading_plan_share s
                   JOIN user_trading_plan p ON p.plan_id=s.plan_id
                   JOIN user_trading_plan_version v ON v.version_id=s.version_id
                   WHERE p.user_id=?
                   ORDER BY s.published_at DESC, s.share_id DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
        return self._shared_version(row) if row else None

    def shared_versions(self, user_id: str) -> list[dict[str, Any]]:
        with self.users.connect(read_only=True) as c:
            rows = c.execute(
                """SELECT s.share_id, s.statement, s.status AS share_status,
                          s.published_at, s.revoked_at AS share_revoked_at, v.*
                   FROM user_trading_plan_share s
                   JOIN user_trading_plan p ON p.plan_id=s.plan_id
                   JOIN user_trading_plan_version v ON v.version_id=s.version_id
                   WHERE p.user_id=? AND s.status IN ('ACTIVE','SUPERSEDED')
                   ORDER BY CASE s.status WHEN 'ACTIVE' THEN 0 ELSE 1 END,
                            s.published_at DESC, s.share_id DESC""",
                (user_id,),
            ).fetchall()
        return [self._shared_version(row) for row in rows]

    def shared_by_id(self, user_id: str, share_id: str) -> dict[str, Any] | None:
        with self.users.connect(read_only=True) as c:
            row = c.execute(
                """SELECT s.share_id, s.statement, s.status AS share_status,
                          s.published_at, s.revoked_at AS share_revoked_at, v.*
                   FROM user_trading_plan_share s
                   JOIN user_trading_plan p ON p.plan_id=s.plan_id
                   JOIN user_trading_plan_version v ON v.version_id=s.version_id
                   WHERE p.user_id=? AND s.share_id=?
                         AND s.status IN ('ACTIVE','SUPERSEDED')""",
                (user_id, share_id),
            ).fetchone()
        return self._shared_version(row) if row else None

    def share_current(self, user_id: str, statement: str = "") -> dict[str, Any]:
        normalized_statement = statement.strip()
        if len(normalized_statement) > 300:
            raise ValueError("共享说明不能超过300字")
        with self.users.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            version = c.execute(
                """SELECT v.* FROM user_trading_plan p
                   JOIN user_trading_plan_version v ON v.version_id=p.current_version_id
                   WHERE p.user_id=?""",
                (user_id,),
            ).fetchone()
            if version is None:
                raise ValueError("尚未发布可共享的交易计划")
            plan_id = str(version["plan_id"])
            version_id = str(version["version_id"])
            active = c.execute(
                """SELECT share_id, version_id FROM user_trading_plan_share
                   WHERE plan_id=? AND status='ACTIVE'
                   ORDER BY published_at DESC, share_id DESC LIMIT 1""",
                (plan_id,),
            ).fetchone()
            if active is not None and str(active["version_id"]) == version_id:
                share_id = str(active["share_id"])
            else:
                c.execute(
                    """UPDATE user_trading_plan_share SET status='SUPERSEDED'
                       WHERE plan_id=? AND status='ACTIVE'""",
                    (plan_id,),
                )
                share_id = uuid4().hex
                c.execute(
                    """INSERT INTO user_trading_plan_share(
                           share_id, plan_id, version_id, statement, status
                       ) VALUES(?,?,?,?, 'ACTIVE')""",
                    (share_id, plan_id, version_id, normalized_statement),
                )
            row = c.execute(
                """SELECT s.share_id, s.statement, s.status AS share_status,
                          s.published_at, s.revoked_at AS share_revoked_at, v.*
                   FROM user_trading_plan_share s
                   JOIN user_trading_plan_version v ON v.version_id=s.version_id
                   WHERE s.share_id=?""",
                (share_id,),
            ).fetchone()
        return self._shared_version(row)

    def revoke_shares(self, user_id: str) -> int:
        with self.users.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            cursor = c.execute(
                """UPDATE user_trading_plan_share
                   SET status='REVOKED', revoked_at=CURRENT_TIMESTAMP
                   WHERE plan_id=(SELECT plan_id FROM user_trading_plan WHERE user_id=?)
                         AND status IN ('ACTIVE','SUPERSEDED')""",
                (user_id,),
            )
        return int(cursor.rowcount)

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
                    key: {**value, "state": "CONFIRMED"}
                    for key, value in base_content.get("answers", {}).items()
                    if key != "__setups__"
                }
                answers["__setups__"] = base_content.get(
                    "setup_catalog", base_content.get("setups", [])
                )
                try:
                    self._setup_catalog(answers)
                except ValueError:
                    legacy = answers.get("setup.enabled", {})
                    raw_enabled = str(legacy.get("value") or "").upper()
                    code_pattern = "|".join(map(re.escape, PLAN_SETUP_DEFINITIONS))
                    legacy_codes = set(
                        re.findall(
                            rf"(?<![A-Z0-9])({code_pattern})(?![A-Z0-9])",
                            raw_enabled,
                        )
                    )
                    if legacy_codes:
                        answers["setup.enabled"] = {
                            **legacy,
                            "state": "PROPOSED",
                            "value": ",".join(
                                code for code in PLAN_SETUP_DEFINITIONS if code in legacy_codes
                            ),
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
        setups = self._setup_catalog(answers)
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
                "confirmed": sum(
                    1
                    for v in answers.values()
                    if isinstance(v, Mapping) and v.get("state") == "CONFIRMED"
                ),
                "total": len(QUESTIONS),
                "unresolved": sum(
                    1
                    for q in QUESTIONS
                    if q["id"] not in answers or answers[q["id"]].get("state") != "CONFIRMED"
                ),
            },
            "answers": answers,
            "setups": setups,
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

    def save_setup(
        self,
        user_id: str,
        session_id: str,
        payload: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Add or update one setup card inside an active version draft."""

        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision 必须为正整数")
        setup_id = str(payload.get("setup_id") or "").strip() or (
            "S" + uuid4().hex.upper()
        )
        with self.users.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM user_plan_grill_session WHERE session_id=? AND user_id=?",
                (session_id, user_id),
            ).fetchone()
            if row is None:
                raise ValueError("Grill 会话不存在")
            if str(row["status"]) != "ACTIVE":
                raise PlanConflict("已发布或关闭的 Grill 会话不可修改")
            if expected_revision != int(row["revision"]):
                raise PlanConflict("草稿已被其他操作更新")
            answers = json.loads(str(row["answers_json"]))
            catalog = self._setup_catalog(answers)
            existing = next(
                (item for item in catalog if str(item["setup_id"]) == setup_id), None
            )
            card: dict[str, Any] = {
                **(existing or {}),
                "setup_id": setup_id,
                "code": str(payload.get("code") or "").strip().upper(),
                "name": str(payload.get("name") or "").strip(),
                "enabled": str(payload.get("enabled") or "").lower()
                in {"1", "true", "on", "yes"},
                "status": "ACTIVE",
                "order": payload.get("order") or (existing or {}).get("order") or len(catalog) + 1,
                "rules": str(payload.get("rules") or "").strip(),
            }
            for field in SETUP_TEXT_FIELDS:
                card[field] = str(payload.get(field) or "").strip()
            if not card["rules"] and not all(
                card[field] for field in ("structure", "trigger", "initial_stop", "first_target")
            ):
                raise ValueError("请填写完整规则，或至少填写结构、触发、初始止损和第一目标")
            if existing is None:
                catalog.append(card)
            else:
                catalog[catalog.index(existing)] = card
            answers["__setups__"] = self._validated_setup_catalog(catalog)
            revision = int(row["revision"]) + 1
            self._write_setup_draft_event(
                c,
                session_id=session_id,
                user_id=user_id,
                answers=answers,
                expected_revision=expected_revision,
                revision=revision,
                setup_id=setup_id,
                event_type="SETUP_SAVE",
                value=card,
            )
        return self.status(user_id, session_id) or {}

    def archive_setup(
        self,
        user_id: str,
        session_id: str,
        setup_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision 必须为正整数")
        with self.users.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM user_plan_grill_session WHERE session_id=? AND user_id=?",
                (session_id, user_id),
            ).fetchone()
            if row is None:
                raise ValueError("Grill 会话不存在")
            if str(row["status"]) != "ACTIVE":
                raise PlanConflict("已发布或关闭的 Grill 会话不可修改")
            if expected_revision != int(row["revision"]):
                raise PlanConflict("草稿已被其他操作更新")
            answers = json.loads(str(row["answers_json"]))
            catalog = self._setup_catalog(answers)
            match = next(
                (item for item in catalog if str(item["setup_id"]) == setup_id), None
            )
            if match is None:
                raise ValueError("交易形态不存在")
            match["status"] = "ARCHIVED"
            match["enabled"] = False
            answers["__setups__"] = self._validated_setup_catalog(catalog)
            revision = int(row["revision"]) + 1
            self._write_setup_draft_event(
                c,
                session_id=session_id,
                user_id=user_id,
                answers=answers,
                expected_revision=expected_revision,
                revision=revision,
                setup_id=setup_id,
                event_type="SETUP_ARCHIVE",
                value=match,
            )
        return self.status(user_id, session_id) or {}

    @staticmethod
    def _write_setup_draft_event(
        connection: Any,
        *,
        session_id: str,
        user_id: str,
        answers: Mapping[str, Any],
        expected_revision: int,
        revision: int,
        setup_id: str,
        event_type: str,
        value: Mapping[str, Any],
    ) -> None:
        cursor = connection.execute(
            """UPDATE user_plan_grill_session
            SET answers_json=?,revision=?,updated_at=CURRENT_TIMESTAMP
            WHERE session_id=? AND user_id=? AND status='ACTIVE' AND revision=?""",
            (
                json.dumps(answers, ensure_ascii=False),
                revision,
                session_id,
                user_id,
                expected_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise PlanConflict("草稿已被其他操作更新")
        connection.execute(
            """INSERT INTO user_plan_grill_event(
                event_id,session_id,question_id,event_type,value_json,revision,actor_type
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                uuid4().hex,
                session_id,
                f"setup:{setup_id}",
                event_type,
                json.dumps(value, ensure_ascii=False),
                revision,
                "USER_WEB",
            ),
        )

    def validate(self, user_id: str, session_id: str) -> dict[str, Any]:
        status = self.status(user_id, session_id)
        if not status:
            raise ValueError("Grill 会话不存在")
        answers = status["answers"]
        missing = [
            q["id"] for q in QUESTIONS if answers.get(q["id"], {}).get("state") != "CONFIRMED"
        ]
        conflicts = []
        try:
            catalog = self._setup_catalog(answers)
            if not any(item.get("enabled") for item in catalog):
                raise ValueError("至少需要启用一种交易形态")
        except ValueError as exc:
            conflicts.append(
                {
                    "conflict_id": "setup.enabled.invalid",
                    "severity": "BLOCKING",
                    "rules": ["setup.enabled"],
                    "message": str(exc),
                }
            )
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
                    "SELECT version_number, evaluation_cycle_id, content_json "
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
            prior_content = (
                json.loads(str(prior_row["content_json"])) if prior_row is not None else {}
            )
            content["setup_catalog"] = self._setup_cycles_for_publish(
                content.get("setup_catalog", []),
                prior_content,
                fallback_cycle=(
                    str(prior_row["evaluation_cycle_id"]) if prior_row is not None else ""
                ),
                initial_cycle=cycle,
                common_rules_changed=self._common_cycle_rules_changed(
                    prior_content.get("answers", {}), content.get("answers", {})
                ),
            )
            content["setups"] = content["setup_catalog"]
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
            # Keep the complete identifier.  A1.rules/A3.rules/B1.rules/B2.rules
            # previously collapsed to the same ``rules`` key and silently hid
            # three confirmed setup decisions in the rendered layer.
            layers[layer][question["id"]] = answers.get(question["id"], {})
        return {
            "schema_version": SCHEMA_VERSION,
            "answers": answers,
            "layers": layers,
            "setup_catalog": self._setup_catalog(answers),
            "risk_warnings": self._warnings(answers),
        }

    @staticmethod
    def _setup_catalog(answers: Mapping[str, Any]) -> list[dict[str, Any]]:
        explicit = answers.get("__setups__")
        if isinstance(explicit, list):
            return TradingPlanService._validated_setup_catalog(explicit)
        decision = answers.get("setup.enabled", {})
        raw = str(decision.get("value") or "") if isinstance(decision, Mapping) else ""
        tokens = [token for token in re.split(r"[\s,，、;；]+", raw.upper()) if token]
        unknown = [token for token in tokens if token not in PLAN_SETUP_DEFINITIONS]
        if unknown:
            raise ValueError(
                f"启用形态只能填写 {plan_setup_choices_text()}，以逗号分隔；无法识别："
                + "、".join(unknown)
            )
        enabled = set(tokens)
        catalog: list[dict[str, Any]] = []
        for code, definition in PLAN_SETUP_DEFINITIONS.items():
            rule_id = str(definition["rule_id"])
            if rule_id not in answers and code not in enabled:
                # Do not inject setup definitions added by newer software into an
                # older frozen version that never contained them.
                continue
            rule = answers.get(rule_id, {})
            catalog.append(
                {
                    "setup_id": code,
                    "code": code,
                    "name": str(definition["name"]),
                    "enabled": code in enabled,
                    "status": "ACTIVE",
                    "order": len(catalog) + 1,
                    "rules": str(rule.get("value") or "") if isinstance(rule, Mapping) else "",
                    **{field: "" for field in SETUP_TEXT_FIELDS},
                }
            )
        return catalog

    @staticmethod
    def _validated_setup_catalog(raw_catalog: list[Any]) -> list[dict[str, Any]]:
        catalog: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        seen_codes: set[str] = set()
        for index, raw in enumerate(raw_catalog):
            if not isinstance(raw, Mapping):
                raise ValueError("交易形态格式不正确")
            setup_id = str(raw.get("setup_id") or "").strip()
            code = str(raw.get("code") or "").strip().upper()
            name = str(raw.get("name") or "").strip()
            status = str(raw.get("status") or "ACTIVE").strip().upper()
            if not setup_id or len(setup_id) > 64:
                raise ValueError("交易形态缺少稳定内部ID")
            if not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,11}", code):
                raise ValueError("形态代码需以大写字母开头，最长12位")
            if not name or len(name) > 60:
                raise ValueError("形态名称必填且不能超过60字")
            if setup_id in seen_ids:
                raise ValueError("形态内部ID不能重复")
            if code in seen_codes:
                raise ValueError(f"形态代码不能重复：{code}")
            if status not in {"ACTIVE", "ARCHIVED"}:
                raise ValueError("形态状态不正确")
            seen_ids.add(setup_id)
            seen_codes.add(code)
            item: dict[str, Any] = {
                "setup_id": setup_id,
                "code": code,
                "name": name,
                "enabled": bool(raw.get("enabled", True)) and status == "ACTIVE",
                "status": status,
                "order": int(raw.get("order") or index + 1),
                "rules": str(raw.get("rules") or "").strip()[:8000],
            }
            for field in SETUP_TEXT_FIELDS:
                item[field] = str(raw.get(field) or "").strip()[:2000]
            cycle_id = str(raw.get("evaluation_cycle_id") or "").strip()
            if cycle_id:
                item["evaluation_cycle_id"] = cycle_id
            catalog.append(item)
        catalog.sort(key=lambda item: (int(item["order"]), str(item["code"])))
        return catalog

    @staticmethod
    def _common_cycle_rules_changed(
        old_answers: Mapping[str, Any], new_answers: Mapping[str, Any]
    ) -> bool:
        return any(
            (old_answers.get(identifier) or {}).get("value")
            != (new_answers.get(identifier) or {}).get("value")
            for identifier in COMMON_CYCLE_QUESTION_IDS
        )

    @staticmethod
    def _setup_cycle_signature(setup: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(str(setup.get(field) or "").strip() for field in SETUP_CYCLE_FIELDS)

    @classmethod
    def _setup_cycles_for_publish(
        cls,
        catalog: list[Mapping[str, Any]],
        prior_content: Mapping[str, Any],
        *,
        fallback_cycle: str,
        initial_cycle: str,
        common_rules_changed: bool,
    ) -> list[dict[str, Any]]:
        prior_catalog = prior_content.get("setup_catalog", prior_content.get("setups", []))
        prior_by_id = {
            str(item.get("setup_id")): item
            for item in prior_catalog
            if isinstance(item, Mapping) and item.get("setup_id")
        }
        result: list[dict[str, Any]] = []
        for raw in catalog:
            item = dict(raw)
            previous = prior_by_id.get(str(item["setup_id"]))
            previous_cycle = (
                str(previous.get("evaluation_cycle_id") or fallback_cycle)
                if previous is not None
                else ""
            )
            changed = (
                previous is None
                or common_rules_changed
                or cls._setup_cycle_signature(previous) != cls._setup_cycle_signature(item)
            )
            item["evaluation_cycle_id"] = (
                initial_cycle
                if previous is None and not prior_by_id
                else (uuid4().hex if changed else previous_cycle)
            )
            result.append(item)
        return result

    @staticmethod
    def _warnings(answers: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Derive accepted risk boundaries from confirmed rules, without mutating a version."""

        def confirmed(identifier: str) -> str:
            decision = answers.get(identifier, {})
            if not isinstance(decision, Mapping) or decision.get("state") != "CONFIRMED":
                return ""
            return str(decision.get("value") or "").strip()

        def negative(value: str) -> bool:
            compact = value.replace(" ", "")
            return any(
                marker in compact
                for marker in ("不设", "不设置", "不限制", "不回避", "均不", "无上限", "无限制")
            )

        warnings: list[dict[str, Any]] = []

        def add(code: str, message: str, *sources: str) -> None:
            warnings.append(
                {"code": code, "message": message, "source_question_ids": list(sources)}
            )

        cash_shortage = confirmed("portfolio.cash_shortage")
        risk_percent = confirmed("risk.risk_percent")
        total_r = confirmed("portfolio.total_r")
        event_filter = confirmed("event.filter")
        universe = confirmed("scope.universe")
        exit_rule = confirmed("execution.exit")
        slippage = confirmed("risk.slippage")
        recording = confirmed("review.recording")

        if cash_shortage == "MARGIN" or "融资" in cash_shortage:
            add("MARGIN_ENABLED", "计划允许融资，实际风险可能放大。", "portfolio.cash_shortage")
        if re.search(r"(?:^|\D)2(?:\.0+)?\s*%|百分之二", risk_percent):
            add("RISK_2_PERCENT", "单笔名义风险为账户净权益的 2%。", "risk.risk_percent")
        if negative(total_r):
            add(
                "UNBOUNDED_PORTFOLIO_RISK",
                "计划不设组合总R或最大持股数量上限。",
                "portfolio.total_r",
            )
        if "回撤" in total_r and negative(total_r):
            add("NO_DRAWDOWN_GUARD", "计划不设置账户回撤降风险或暂停线。", "portfolio.total_r")
        if any(word in total_r for word in ("暂停", "停止线", "连续亏损", "灾难性")) and negative(
            total_r
        ):
            add("NO_PAUSE_GUARD", "计划不设置连续亏损或灾难性风险暂停机制。", "portfolio.total_r")
        if event_filter and negative(event_filter):
            add(
                "NO_EVENT_FILTER",
                "计划不回避财报、解禁、除权、复牌等已知事件风险。",
                "event.filter",
            )
        if "流动性" in universe and negative(universe):
            add("NO_LIQUIDITY_FILTER", "股票池不设置流动性过滤。", "scope.universe")
        normalized_exit = exit_rule.upper().replace("＋", "+").replace(" ", "")
        if "T+1" in normalized_exit or "当日无法卖" in exit_rule:
            add("T1_STOP_GAP", "A股T+1使买入当日止损无法执行。", "execution.exit")
        if any(word in slippage for word in ("不缩减", "不减仓", "不减少")):
            add(
                "FULL_R_SLIPPAGE",
                "高开或滑点时不减仓，实际单笔风险可能超过名义1R。",
                "risk.slippage",
            )
        compact_recording = recording.replace(" ", "")
        if "毛" in recording and any(
            word in compact_recording for word in ("不扣", "未扣", "不计交易成本")
        ):
            add(
                "GROSS_EXPECTANCY_ONLY",
                "期望评价使用未扣佣金、融资利息和滑点的毛R。",
                "review.recording",
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
        base_setups = {
            str(item.get("setup_id")): item for item in base.get("setup_catalog", [])
        }
        current_setups = {
            str(item.get("setup_id")): item for item in self._setup_catalog(current_answers)
        }
        if set(base_setups) != set(current_setups):
            return "SUBSTANTIVE"
        for setup_id, current_setup in current_setups.items():
            base_setup = base_setups[setup_id]
            if self._setup_cycle_signature(base_setup) != self._setup_cycle_signature(
                current_setup
            ):
                return "SUBSTANTIVE"
            if bool(base_setup.get("enabled")) != bool(current_setup.get("enabled")):
                return "SUBSTANTIVE"
            if str(base_setup.get("status")) != str(current_setup.get("status")):
                return "SUBSTANTIVE"
        return "MINOR"

    def _version(self, row: Any) -> dict[str, Any]:
        value = dict(row)
        for field in ("created_at", "activated_at", "archived_at"):
            if field in value:
                value[field] = sqlite_utc_to_shanghai(value[field])
        value["content"] = json.loads(str(value.pop("content_json")))
        answers = value["content"].get("answers", {})
        catalog = value["content"].get("setup_catalog")
        if isinstance(catalog, list):
            catalog = [dict(item) for item in catalog if isinstance(item, Mapping)]
        else:
            try:
                catalog = self._setup_catalog(answers)
            except ValueError:
                # Read-only compatibility for older frozen versions.  Never infer
                # anything except the four explicit, recognizable setup codes.
                raw = str(answers.get("setup.enabled", {}).get("value") or "").upper()
                code_pattern = "|".join(map(re.escape, PLAN_SETUP_DEFINITIONS))
                codes = set(
                    re.findall(
                        rf"(?<![A-Z0-9])({code_pattern})(?![A-Z0-9])",
                        raw,
                    )
                )
                compatible_answers = dict(answers)
                compatible_answers["setup.enabled"] = {
                    "state": "CONFIRMED",
                    "value": ",".join(code for code in PLAN_SETUP_DEFINITIONS if code in codes),
                }
                catalog = self._setup_catalog(compatible_answers)
        for setup in catalog:
            setup["display_fields"] = [
                {"key": field, "label": SETUP_FIELD_LABELS[field], "value": setup.get(field)}
                for field in SETUP_TEXT_FIELDS
                if setup.get(field)
            ]
        value["setup_catalog"] = catalog
        value["setup_cards"] = [
            setup for setup in catalog if str(setup.get("status") or "ACTIVE") == "ACTIVE"
        ]
        value["risk_profile"] = self._warnings(answers)
        value["display_sections"] = self._display_sections(answers)
        value["confirmed_rule_count"] = sum(
            1
            for decision in answers.values()
            if isinstance(decision, Mapping) and decision.get("state") == "CONFIRMED"
        )
        return value

    def _shared_version(self, row: Any) -> dict[str, Any]:
        value = self._version(row)
        for field in ("published_at", "share_revoked_at"):
            if field in value:
                value[field] = sqlite_utc_to_shanghai(value[field])
        value["share_href"] = f"/a/owner/plans/{value['share_id']}"
        return value

    @staticmethod
    def _display_sections(answers: Mapping[str, Any]) -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        for section_key, eyebrow, title in PLAN_SECTIONS:
            rules = []
            for question in QUESTIONS:
                layer = "operations" if question["section"] == "execution" else question["section"]
                if layer != section_key:
                    continue
                if section_key == "setups" and (
                    question["id"].endswith(".rules") or question["id"] == "setup.enabled"
                ):
                    continue
                decision = answers.get(question["id"], {})
                raw_value = decision.get("value") if isinstance(decision, Mapping) else ""
                rules.append(
                    {
                        "id": question["id"],
                        "title": question["title"],
                        "value": ANSWER_VALUE_LABELS.get(str(raw_value), raw_value),
                    }
                )
            sections.append(
                {
                    "key": section_key,
                    "eyebrow": eyebrow,
                    "title": title,
                    "rules": rules,
                }
            )
        return sections
