"""Shared numeric parsing helpers for upstream crawler payloads.

Consolidates the float-parsing variants that were previously duplicated across
``realtime_parsers``, ``capital_flow_crawler`` and ``briefing``.
"""

from __future__ import annotations

import re
from typing import Any

_BLANK_PLACEHOLDERS = (None, "", "-", "--")

# 中文数量级后缀 → 倍率。上游资金流接口（百度）用 "1.2亿" / "3298.68万" 这类
# 人类可读金额，必须先还原成"元"再入库，否则同一列会混进差 1e4/1e8 的量级。
_CN_MONEY_MULTIPLIERS: tuple[tuple[str, float], ...] = (
    ("亿", 100_000_000.0),
    ("万", 10_000.0),
)

# 科学计数法必须整体匹配：``1.2e8`` 只取 ``1.2`` 会丢掉指数，静默差 8 个数量级。
_MONEY_NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def parse_float(value: Any) -> float | None:
    """Parse a numeric payload value leniently.

    Strips ``+``/``%``/thousand separators; blank placeholders (``None``, ``""``,
    ``-``, ``--``) and non-numeric text return ``None``.
    """
    if value in _BLANK_PLACEHOLDERS:
        return None
    text = str(value).strip().replace(",", "").replace("+", "").replace("%", "")
    if not text or text in ("-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def is_blank_numeric(value: Any) -> bool:
    """Return ``True`` for placeholder values that mean "no number supplied"."""
    if value in _BLANK_PLACEHOLDERS:
        return True
    text = str(value).strip().replace(",", "").replace("+", "").replace("%", "")
    return text in ("", "-", "--")


def parse_money_amount(value: Any) -> float | None:
    """Parse a money amount that may carry a Chinese magnitude suffix.

    上游（百度资金流）返回 ``"+3298.68万"`` / ``"-1.81亿"`` / ``"300"``，单位一律
    归一到**元**，与东财 ``f52``、新浪 ``netamount`` 的口径一致（实测三源同日同值）。
    带 ``元``/``万``/``亿`` 后缀、千分位逗号与 ``+`` 号都会被清掉；科学计数法
    （``1.2e8``）整体解析而不是被截成 ``1.2``。无法解析时返回 ``None``。
    """
    if is_blank_numeric(value):
        return None
    text = str(value).strip().replace(",", "").replace("+", "")
    if not text:
        return None
    multiplier = 1.0
    for suffix, factor in _CN_MONEY_MULTIPLIERS:
        if suffix in text:
            multiplier = factor
            break
    for suffix, _factor in _CN_MONEY_MULTIPLIERS:
        text = text.replace(suffix, "")
    text = text.replace("元", "").strip()
    match = _MONEY_NUMBER_PATTERN.search(text)
    if match is None:
        return None
    try:
        return float(match.group(0)) * multiplier
    except ValueError:
        return None
