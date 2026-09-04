"""Authoritative catalog for versioned trading-plan setups."""

PLAN_SETUP_DEFINITIONS: dict[str, dict[str, object]] = {
    "A1": {
        "name": "突破前入场",
        "rule_id": "A1.rules",
        "legacy_setup_method": "PRE_BREAKOUT_BASE",
    },
    "A3": {
        "name": "首次回调",
        "rule_id": "A3.rules",
        "legacy_setup_method": "POST_BREAKOUT_FIRST_PULLBACK",
    },
    "B1": {
        "name": "简单回调",
        "rule_id": "B1.rules",
        "legacy_setup_method": "PULLBACK_SUPPORT",
    },
    "B2": {
        "name": "复杂回调",
        "rule_id": "B2.rules",
        "legacy_setup_method": "COMPLEX_PULLBACK",
    },
    "C1": {
        "name": "区间失败测试",
        "rule_id": "C1.rules",
        "legacy_setup_method": "FAILED_TEST",
    },
    "C2": {
        "name": "底部区间突破后的首次回调",
        "rule_id": "C2.rules",
        # This column is retained only for compatibility with the legacy
        # constrained trade table.  The versioned setup id/code is authoritative.
        "legacy_setup_method": "POST_BREAKOUT_FIRST_PULLBACK",
    },
}

PLAN_SETUP_CODES = frozenset(PLAN_SETUP_DEFINITIONS)
PLAN_SETUP_BY_TRADE_SETUP: dict[str, str] = {}
for code, definition in PLAN_SETUP_DEFINITIONS.items():
    # A3 and C2 intentionally share a legacy compatibility value.  Preserve the
    # older A3 inference; C2 must be selected through its versioned setup id.
    PLAN_SETUP_BY_TRADE_SETUP.setdefault(str(definition["legacy_setup_method"]), code)
TRADE_SETUP_BY_PLAN_SETUP = {
    code: str(definition["legacy_setup_method"])
    for code, definition in PLAN_SETUP_DEFINITIONS.items()
}


def plan_setup_choices_text() -> str:
    return "、".join(PLAN_SETUP_DEFINITIONS)
