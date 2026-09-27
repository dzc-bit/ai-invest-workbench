from __future__ import annotations

from collections.abc import Callable
from typing import Any

from astock_backtester.conditions import registered_conditions
from astock_backtester.engine import run_backtest
from astock_backtester.indicators import (
    add_capital_flow_positive_count,
    add_capital_flow_sum,
    add_macd,
    add_market_heat,
    add_moving_average,
    add_prior_high_low,
    add_returns,
    add_volume_ratio,
)
from astock_backtester.models import ConditionNode, StrategyConfig


def strategy_nodes(strategy: StrategyConfig) -> list[ConditionNode]:
    return [
        *strategy.market_filters,
        *(node for group in strategy.entry_groups for node in group.conditions),
        *strategy.exit_rules,
    ]


def window_params(strategy: StrategyConfig, condition_ids: set[str], defaults: set[int]) -> list[int]:
    windows = set(defaults)
    for node in strategy_nodes(strategy):
        if node.condition_id in condition_ids and "window" in node.params:
            windows.add(int(node.params["window"]))
    return sorted(windows)


def enrich_for_strategy(frame: Any, strategy: StrategyConfig) -> Any:
    ma_windows = window_params(strategy, {"close_above_ma", "close_below_ma"}, {3, 5, 10, 20, 60})
    return_windows = window_params(strategy, {"past_return_at_most", "past_return_between"}, {2, 3, 5, 10, 20})
    volume_windows = window_params(strategy, {"volume_ratio_between"}, {2, 3, 5, 10})
    flow_windows = window_params(strategy, {"capital_flow_n_day_sum_at_least", "capital_flow_n_day_sum_at_most"}, set())
    flow_positive_count_windows = window_params(strategy, {"capital_flow_n_day_positive_count_at_least"}, set())
    high_low_windows = window_params(
        strategy,
        {"breakout_above_n_day_high", "breakdown_below_n_day_low"},
        set(),
    )
    # Sort once upfront so each indicator's _sorted() becomes a no-op
    frame = frame.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    frame = add_moving_average(frame, ma_windows)
    frame = add_returns(frame, return_windows)
    frame = add_volume_ratio(frame, volume_windows)
    if flow_windows:
        frame = add_capital_flow_sum(frame, flow_windows)
    if flow_positive_count_windows:
        frame = add_capital_flow_positive_count(frame, flow_positive_count_windows)
    if high_low_windows:
        frame = add_prior_high_low(frame, high_low_windows)
    frame = add_macd(frame)
    return add_market_heat(frame)


def condition_definition_json(definition: Any) -> dict[str, Any]:
    return {
        "condition_id": definition.condition_id,
        "label": definition.label,
        "category": definition.category,
        "required_columns": list(definition.required_columns),
    }


def condition_definitions_json() -> list[dict[str, Any]]:
    return [condition_definition_json(item) for item in registered_conditions()]


def run_prepared_backtest(
    prepared: Any,
    strategy: StrategyConfig,
    settings: Any,
    on_trade_closed: Callable[[Any], None] | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> Any:
    """Match an already-enriched frame against one set of settings.

    Reuse across combinations is safe because enrichment is a pure function of
    ``(frame, strategy)`` — it never sees ``BacktestSettings`` — and
    ``run_backtest`` copies the frame before writing its own columns, so no
    combination can mutate the shared prepared data.
    """
    return run_backtest(
        prepared,
        strategy,
        settings,
        on_trade_closed=on_trade_closed,
        on_event=on_event,
    )


def run_configured_backtest(
    frame: Any,
    strategy: StrategyConfig,
    settings: Any,
    on_trade_closed: Callable[[Any], None] | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> Any:
    return run_prepared_backtest(
        enrich_for_strategy(frame, strategy),
        strategy,
        settings,
        on_trade_closed=on_trade_closed,
        on_event=on_event,
    )
