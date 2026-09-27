"""Strategy parameter grid search: deterministic sweep + one AI commentary.

``POST /ai/optimize`` streams NDJSON events (progress / combination / result)
while matching a bounded cartesian grid of whitelisted numeric settings knobs
against a frame whose indicators were computed once for the whole run. The AI
part is a single final commentary call; the sweep itself works without a
configured model.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterator
from typing import Any

from pydantic import ValidationError

from astock_backtester.ai.cancel import CancelToken
from astock_backtester.backtest_runner import enrich_for_strategy, run_prepared_backtest
from astock_backtester.models import BacktestMetrics, BacktestSettings, StrategyConfig

MAX_GRID_COMBINATIONS = 48
# Only vetted numeric knobs are grid-searchable; strategy conditions stay fixed.
GRID_KEYS = (
    "fixed_holding_days",
    "max_positions",
    "max_daily_buys",
    "position_size_pct",
    "take_profit_pct",
    "stop_loss_pct",
    "min_listing_days",
)
# Derived from the model so the integer knobs stay integer in the grid without a
# second constraint table that can drift away from BacktestSettings.
INT_GRID_KEYS = frozenset(
    key for key in GRID_KEYS if BacktestSettings.model_fields[key].annotation is int
)

GridValue = int | float


class GridTooLargeError(ValueError):
    pass


def _normalize_value(key: str, raw: object) -> GridValue:
    """Coerce one candidate into the shape the settings field actually declares.

    Legality itself is decided later by rebuilding ``BacktestSettings`` — this
    only keeps ``3`` from becoming ``3.0`` (and lets a fractional value for an
    integer knob survive long enough to be reported as a failed combination).
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"参数 {key} 的候选值必须是数字，收到 {raw!r}")
    number = float(raw)
    if number != number or abs(number) == float("inf"):
        raise ValueError(f"参数 {key} 的候选值必须是有限数字")
    if key in INT_GRID_KEYS and number.is_integer():
        return int(number)
    return number


def normalize_grid(payload_grid: dict[str, Any]) -> dict[str, list[GridValue]]:
    """Validate the requested grid: whitelisted keys, finite values, bounded size."""
    grid: dict[str, list[GridValue]] = {}
    for key, values in (payload_grid or {}).items():
        if key not in GRID_KEYS:
            raise ValueError(f"不支持寻优的参数：{key}（可选：{', '.join(GRID_KEYS)}）")
        if not isinstance(values, list) or not values:
            raise ValueError(f"参数 {key} 的候选值必须是非空数组")
        grid[key] = [_normalize_value(key, value) for value in values]
    total = 1
    for values in grid.values():
        total *= len(values)
    if total == 0:
        raise ValueError("网格为空，请至少为一个参数提供候选值")
    if total > MAX_GRID_COMBINATIONS:
        raise GridTooLargeError(f"网格组合数 {total} 超过上限 {MAX_GRID_COMBINATIONS}，请减少候选值")
    return grid


def iter_grid(grid: dict[str, list[GridValue]]) -> Iterator[dict[str, GridValue]]:
    keys = list(grid)
    for values in itertools.product(*(grid[key] for key in keys)):
        yield dict(zip(keys, values, strict=True))


def merge_settings(settings: BacktestSettings, overrides: dict[str, GridValue]) -> BacktestSettings:
    """Apply one combination and re-run every ``BacktestSettings`` constraint.

    ``model_copy(update=...)`` would skip validation and let an illegal
    combination through; rebuilding is what keeps the grid's legality identical
    to what a hand-written request could set.
    """
    return BacktestSettings.model_validate({**settings.model_dump(), **overrides})


def _validation_failure(exc: ValidationError, overrides: dict[str, GridValue]) -> str:
    """Human-readable reason for a rejected combination.

    Model-level validators (``mode="after"``) report an empty ``loc``, so the
    overridden knob names are the only useful pointer back to the combo.
    """
    errors = exc.errors()
    first = errors[0] if errors else {}
    field = ".".join(str(part) for part in first.get("loc", ())) or ", ".join(sorted(overrides))
    return f"{field}: {first.get('msg') or exc}"


def metrics_row(metrics: BacktestMetrics) -> dict[str, float | int]:
    return metrics.model_dump(mode="json")


def rank_combinations(combinations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best combination by total return among those with at least one trade."""
    candidates = [combo for combo in combinations if combo.get("metrics", {}).get("trade_count", 0) > 0]
    if not candidates:
        return None

    def sort_key(combo: dict[str, Any]) -> tuple[float, float]:
        metrics = combo["metrics"]
        return (float(metrics.get("total_return_pct", 0.0)), -abs(float(metrics.get("max_drawdown_pct", 0.0))))

    return max(candidates, key=sort_key)


def run_optimization(
    frame: Any,
    strategy: StrategyConfig,
    settings: BacktestSettings,
    grid: dict[str, list[GridValue]],
    on_event: Callable[[dict[str, Any]], None],
    cancel: CancelToken | None = None,
) -> dict[str, Any]:
    """Run every grid combination and emit progress/combination events.

    Combinations that break a ``BacktestSettings`` constraint are reported in
    ``failures`` with code ``invalid_combination`` and never enter
    ``combinations``, so they cannot be ranked or shown as a result.

    ``cancel`` 只作用在**组合边界**：已经在跑的回测一定跑完（中途放弃会留下写到
    一半的会话与缓存），之后的组合不再启动。
    """
    token = cancel if cancel is not None else CancelToken()
    combos = list(iter_grid(grid))
    combinations: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    # 指标增强在整个网格里是同一份：网格只扫 settings，不扫条件。
    prepared = enrich_for_strategy(frame, strategy)
    index = 0
    for overrides in combos:
        if token.cancelled:
            break
        index += 1
        try:
            combo_settings = merge_settings(settings, overrides)
            result = run_prepared_backtest(prepared, strategy, combo_settings)
        except ValidationError as exc:
            failures.append(
                {
                    "params": overrides,
                    "code": "invalid_combination",
                    "error": _validation_failure(exc, overrides),
                }
            )
            on_event({"type": "progress", "completed": index, "total": len(combos)})
            continue
        row = {
            "index": index,
            "params": overrides,
            "metrics": metrics_row(result.metrics),
        }
        combinations.append(row)
        on_event({"type": "combination", **row})
        on_event({"type": "progress", "completed": index, "total": len(combos)})
    best = rank_combinations(combinations)
    return {
        "combinations": combinations,
        "best": best,
        "failures": failures,
        "total": len(combos),
        "evaluated": len(combinations),
        "cancelled": token.cancelled,
    }


def build_optimize_insight_context(summary: dict[str, Any]) -> dict[str, Any]:
    rows = [
        {
            "params": combo["params"],
            "total_return_pct": combo["metrics"].get("total_return_pct"),
            "max_drawdown_pct": combo["metrics"].get("max_drawdown_pct"),
            "win_rate_pct": combo["metrics"].get("win_rate_pct"),
            "trade_count": combo["metrics"].get("trade_count"),
        }
        for combo in summary.get("combinations", [])
    ]
    return {
        "best": summary.get("best"),
        "combinations": rows[:48],
        "evaluated": summary.get("evaluated", 0),
        "failures": summary.get("failures", [])[:3],
    }
