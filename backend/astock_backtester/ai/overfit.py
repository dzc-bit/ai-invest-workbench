"""Deterministic backtest overfit checks (no model needed).

``assess_overfit`` inspects a finished backtest's metrics plus — when a
parameter grid was swept — the dispersion across combinations, and returns
machine-readable findings.  The frontend shows them next to the result and
feeds them into the AI one-shot commentary as context; the check itself must
stay cheap, explainable and runnable with AI unconfigured.
"""

from __future__ import annotations

import statistics
from typing import Any

MIN_RELIABLE_TRADES = 10
# 指标口径：BacktestMetrics 里的 *_pct 是小数比例（0.05 = +5%）。
SUSPICIOUS_WIN_RATE = 0.999
SUSPICIOUS_RETURN = 1.0
LOW_TRADE_HIGH_RETURN_TRADES = 30
SMOOTH_DRAWDOWN = 0.005
SMOOTH_RETURN = 0.5
PARAM_DISPERSION_RATIO = 3.0
# Below this many comparable combinations the distribution is too thin to call
# anything a warning; the check still reports what it sees, labelled as such.
MIN_GRID_SAMPLES = 5


def _finding(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}


def _grid_sample(combos: list[dict[str, Any]]) -> tuple[list[float], int]:
    """Returns of the combinations that report a numeric total return, plus how
    many of them never traded at all (a 0% return there is not a result)."""
    returns: list[float] = []
    tradeless = 0
    for combo in combos:
        metrics = combo.get("metrics") or {}
        value = metrics.get("total_return_pct")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        returns.append(float(value))
        trades = metrics.get("trade_count")
        if isinstance(trades, (int, float)) and not isinstance(trades, bool) and trades == 0:
            tradeless += 1
    return returns, tradeless


def _param_dispersion_findings(
    combos: list[dict[str, Any]], *, rejected: int = 0
) -> list[dict[str, str]]:
    """Compare the best combination with the rest of the grid.

    A best that is far above its neighbours, or the only positive one, is a
    classic overfit signature — but only the count of positive combinations can
    say that, so it is counted rather than inferred from the median.
    """
    findings: list[dict[str, str]] = []
    if rejected > 0:
        findings.append(
            _finding(
                "info",
                "grid_partial_failures",
                f"网格里有 {rejected} 个参数组合不合法、已被剔除，下面的分布判断只覆盖剩下的组合。",
            )
        )
    returns, tradeless = _grid_sample(combos)
    if not returns:
        findings.append(
            _finding("info", "grid_no_usable_combinations", "参数网格没有产出任何可比较的组合，无法判断稳健性。")
        )
        return findings

    total = len(returns)
    positive = sum(1 for value in returns if value > 0)
    best = max(returns)
    if total < MIN_GRID_SAMPLES:
        findings.append(
            _finding(
                "info",
                "grid_small_sample",
                f"网格只有 {total} 组可比结果，样本偏少，以下关于分布的判断仅供参考。",
            )
        )

    if positive == 0:
        findings.append(
            _finding(
                "info",
                "grid_no_positive_combination",
                f"{total} 组参数收益全部不为正（最优 {best:+.1%}）；这不是过拟合特征，而是策略在该区间没有产出。",
            )
        )
    elif positive == 1:
        findings.append(
            _finding(
                "warning" if total >= MIN_GRID_SAMPLES else "info",
                "grid_only_best_positive",
                f"{total} 组里实测只有 1 组收益为正（{best:+.1%}），其余 {total - 1} 组不赚钱；"
                "最优组可能只是踩中参数噪声，稳健性存疑。",
            )
        )
    elif total >= MIN_GRID_SAMPLES:
        median = float(statistics.median(returns))
        if median > 0 and best > 0 and best >= median * PARAM_DISPERSION_RATIO:
            findings.append(
                _finding(
                    "warning",
                    "grid_best_outlier",
                    f"最优组合收益 {best:+.1%} 是网格中位数（{median:+.1%}）的 {best / median:.1f} 倍，"
                    f"{total} 组中有 {positive} 组为正；业绩集中在个别参数上，稳健性存疑。",
                )
            )
    if tradeless:
        findings.append(
            _finding(
                "info",
                "grid_combinations_without_trades",
                f"{total} 组里有 {tradeless} 组一笔成交都没有，其 0% 收益不代表该参数可用。",
            )
        )
    return findings


def assess_overfit(
    metrics: dict[str, Any],
    *,
    combos: list[dict[str, Any]] | None = None,
    rejected: int = 0,
) -> dict[str, Any]:
    findings: list[dict[str, str]] = []
    trade_count = metrics.get("trade_count")
    total_return = metrics.get("total_return_pct")
    win_rate = metrics.get("win_rate_pct")
    max_drawdown = metrics.get("max_drawdown_pct")

    if isinstance(trade_count, (int, float)) and trade_count < MIN_RELIABLE_TRADES:
        findings.append(
            _finding(
                "warning" if trade_count >= 5 else "critical",
                "few_trades",
                f"仅 {int(trade_count)} 笔交易，样本太少，收益/胜率统计意义不足；建议拉长回测区间或放宽条件。",
            )
        )
    if (
        isinstance(win_rate, (int, float))
        and win_rate >= SUSPICIOUS_WIN_RATE
        and isinstance(trade_count, (int, float))
        and trade_count >= 5
    ):
        findings.append(
            _finding(
                "warning",
                "perfect_win_rate",
                "胜率接近 100%：先检查是否用了未来函数、止损从未被触发，或数据本身存在缺口。",
            )
        )
    if (
        isinstance(total_return, (int, float))
        and total_return >= SUSPICIOUS_RETURN
        and isinstance(trade_count, (int, float))
        and trade_count < LOW_TRADE_HIGH_RETURN_TRADES
    ):
        findings.append(
            _finding(
                "warning",
                "high_return_few_trades",
                f"总收益 {total_return:+.1%} 却只来自 {int(trade_count)} 笔交易，收益高度依赖个别行情，实盘外推风险大。",
            )
        )
    if (
        isinstance(max_drawdown, (int, float))
        and abs(max_drawdown) <= SMOOTH_DRAWDOWN
        and isinstance(total_return, (int, float))
        and total_return >= SMOOTH_RETURN
    ):
        findings.append(
            _finding(
                "info",
                "suspiciously_smooth",
                "收益很高而回撤极浅：确认是否真实逐日撮合，警惕成交价按理想价成交的乐观假设。",
            )
        )
    if combos is not None or rejected:
        findings.extend(_param_dispersion_findings(combos or [], rejected=rejected))

    level = "none"
    if any(item["level"] == "critical" for item in findings):
        level = "critical"
    elif any(item["level"] == "warning" for item in findings):
        level = "warning"
    elif findings:
        level = "info"
    return {"level": level, "findings": findings}
