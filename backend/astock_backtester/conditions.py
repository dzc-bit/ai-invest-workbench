from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd

from astock_backtester.models import ConditionGroup, ConditionNode, ConditionOperator


@dataclass(frozen=True)
class ConditionDefinition:
    condition_id: str
    label: str
    category: str
    required_columns: tuple[str, ...]


@dataclass(frozen=True)
class ConditionResult:
    passed: bool
    reason: str
    observed_value: float | None = None


@dataclass(frozen=True)
class GroupResult:
    passed: bool
    reasons: list[str]
    score: float = 0.0


Evaluator = Callable[[ConditionNode, pd.Series, pd.DataFrame], ConditionResult]
MaskBuilder = Callable[[ConditionNode, pd.DataFrame], pd.Series]


def _all_true_mask(data: pd.DataFrame) -> pd.Series:
    return pd.Series(True, index=data.index)


def registered_conditions() -> list[ConditionDefinition]:
    return [
        ConditionDefinition("market_cap_between", "Float market cap range", "market_cap", ("float_market_cap",)),
        ConditionDefinition(
            "capital_flow_n_day_sum_at_least",
            "N-day main net inflow",
            "capital_flow",
            ("main_net_inflow",),
        ),
        ConditionDefinition(
            "capital_flow_n_day_sum_at_most",
            "N-day main net outflow",
            "capital_flow",
            ("main_net_inflow",),
        ),
        ConditionDefinition(
            "capital_flow_today_at_least",
            "Signal-day main net inflow",
            "capital_flow",
            ("main_net_inflow",),
        ),
        ConditionDefinition(
            "capital_flow_today_at_most",
            "Signal-day main net outflow",
            "capital_flow",
            ("main_net_inflow",),
        ),
        ConditionDefinition(
            "capital_flow_n_day_positive_count_at_least",
            "N-day positive main net inflow count",
            "capital_flow",
            ("main_net_inflow",),
        ),
        ConditionDefinition(
            "market_rising_ratio_at_least",
            "Market rising ratio",
            "market_heat",
            ("market_rising_ratio",),
        ),
        ConditionDefinition("close_above_ma", "Close above moving average", "trend", ()),
        ConditionDefinition("close_below_ma", "Close below moving average", "trend", ()),
        ConditionDefinition("turnover_between", "Turnover range", "volume", ("turnover_rate",)),
        ConditionDefinition("past_return_at_most", "Past return upper bound", "price_movement", ()),
        ConditionDefinition("past_return_between", "Past return range", "price_movement", ()),
        ConditionDefinition("volume_ratio_between", "Volume ratio range", "volume", ()),
        ConditionDefinition("macd_histogram_at_least", "MACD histogram floor", "technical", ("macd_hist",)),
        ConditionDefinition("macd_dead_cross", "MACD dead cross", "technical", ("macd_dif", "macd_dea")),
        ConditionDefinition("breakout_above_n_day_high", "Breakout above prior high", "pattern", ()),
        ConditionDefinition("breakdown_below_n_day_low", "Breakdown below prior low", "exit_pattern", ()),
    ]


def _market_cap_between(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    value = float(row["float_market_cap"])
    minimum = float(node.params["min"])
    maximum = float(node.params["max"])
    passed = minimum <= value <= maximum
    return ConditionResult(passed, f"float market cap {value:.0f} in [{minimum:.0f}, {maximum:.0f}]", value)


def _capital_flow_n_day_sum_at_least(
    node: ConditionNode,
    row: pd.Series,
    frame: pd.DataFrame,
) -> ConditionResult:
    window = int(node.params["window"])
    minimum = float(node.params["min"])
    precomputed_column = f"main_net_inflow_sum_{window}d"
    if precomputed_column in row.index:
        value = pd.to_numeric(row[precomputed_column], errors="coerce")
        if pd.isna(value):
            return ConditionResult(
                False,
                f"{window}d main net inflow unavailable before enough history",
                None,
            )
        value = float(value)
        return ConditionResult(value >= minimum, f"{window}d main net inflow {value:.0f} >= {minimum:.0f}", value)

    symbol_frame = frame[
        (frame["symbol"] == row["symbol"]) & (frame["trade_date"] <= row["trade_date"])
    ].sort_values("trade_date")
    if len(symbol_frame) < window:
        return ConditionResult(
            False,
            f"{window}d main net inflow unavailable before enough history",
            None,
        )
    value = float(symbol_frame.tail(window)["main_net_inflow"].sum())
    passed = value >= minimum
    return ConditionResult(passed, f"{window}d main net inflow {value:.0f} >= {minimum:.0f}", value)


def _capital_flow_n_day_sum_at_most(
    node: ConditionNode,
    row: pd.Series,
    frame: pd.DataFrame,
) -> ConditionResult:
    window = int(node.params["window"])
    maximum = float(node.params["max"])
    precomputed_column = f"main_net_inflow_sum_{window}d"
    if precomputed_column in row.index:
        value = pd.to_numeric(row[precomputed_column], errors="coerce")
        if pd.isna(value):
            return ConditionResult(
                False,
                f"{window}d main net inflow unavailable before enough history",
                None,
            )
        value = float(value)
        return ConditionResult(value <= maximum, f"{window}d main net inflow {value:.0f} <= {maximum:.0f}", value)

    symbol_frame = frame[
        (frame["symbol"] == row["symbol"]) & (frame["trade_date"] <= row["trade_date"])
    ].sort_values("trade_date")
    if len(symbol_frame) < window:
        return ConditionResult(
            False,
            f"{window}d main net inflow unavailable before enough history",
            None,
        )
    value = float(symbol_frame.tail(window)["main_net_inflow"].sum())
    passed = value <= maximum
    return ConditionResult(passed, f"{window}d main net inflow {value:.0f} <= {maximum:.0f}", value)


def _capital_flow_today_at_least(
    node: ConditionNode,
    row: pd.Series,
    frame: pd.DataFrame,
) -> ConditionResult:
    minimum = float(node.params["min"])
    value = pd.to_numeric(row["main_net_inflow"], errors="coerce")
    if pd.isna(value):
        return ConditionResult(False, "signal-day main net inflow unavailable", None)
    value = float(value)
    return ConditionResult(value >= minimum, f"signal-day main net inflow {value:.0f} >= {minimum:.0f}", value)


def _capital_flow_today_at_most(
    node: ConditionNode,
    row: pd.Series,
    frame: pd.DataFrame,
) -> ConditionResult:
    maximum = float(node.params["max"])
    value = pd.to_numeric(row["main_net_inflow"], errors="coerce")
    if pd.isna(value):
        return ConditionResult(False, "signal-day main net inflow unavailable", None)
    value = float(value)
    return ConditionResult(value <= maximum, f"signal-day main net inflow {value:.0f} <= {maximum:.0f}", value)


def _capital_flow_n_day_positive_count_at_least(
    node: ConditionNode,
    row: pd.Series,
    frame: pd.DataFrame,
) -> ConditionResult:
    window = int(node.params["window"])
    minimum = int(node.params["min_count"])
    precomputed_column = f"main_net_inflow_positive_count_{window}d"
    if precomputed_column in row.index:
        value = pd.to_numeric(row[precomputed_column], errors="coerce")
        if pd.isna(value):
            return ConditionResult(
                False,
                f"{window}d positive main net inflow days unavailable before enough history",
                None,
            )
        value = int(value)
        return ConditionResult(
            value >= minimum,
            f"{window}d positive main net inflow days {value} >= {minimum}",
            float(value),
        )

    symbol_frame = frame[
        (frame["symbol"] == row["symbol"]) & (frame["trade_date"] <= row["trade_date"])
    ].sort_values("trade_date")
    if len(symbol_frame) < window:
        return ConditionResult(
            False,
            f"{window}d positive main net inflow days unavailable before enough history",
            None,
        )
    value = int((symbol_frame.tail(window)["main_net_inflow"] > 0).sum())
    return ConditionResult(
        value >= minimum,
        f"{window}d positive main net inflow days {value} >= {minimum}",
        float(value),
    )


def _market_rising_ratio_at_least(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    value = float(row["market_rising_ratio"])
    minimum = float(node.params["min_ratio"])
    return ConditionResult(value >= minimum, f"market rising ratio {value:.2%} >= {minimum:.2%}", value)


def _close_above_ma(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    window = int(node.params["window"])
    value = float(row["close"] - row[f"ma_{window}"])
    return ConditionResult(value > 0, f"close {row['close']:.2f} above MA{window} {row[f'ma_{window}']:.2f}", value)


def _close_below_ma(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    window = int(node.params["window"])
    value = float(row["close"] - row[f"ma_{window}"])
    return ConditionResult(value < 0, f"close {row['close']:.2f} below MA{window} {row[f'ma_{window}']:.2f}", value)


def _turnover_ratio(value: float) -> float:
    """Normalize the warehouse's percent-scale ``turnover_rate`` to a fraction.

    ``turnover_rate`` 在仓库里是**百分数量纲**（实测 229 万行：中位数 0.38、最大
    98.28；``volume / 流通股 × 100`` 恰好等于该列），而条件参数是分数
    （``{"min": 0.02, "max": 0.08}`` = 换手率 2%~8%）。行级 evaluator 与向量化
    mask builder 必须共用这一处归一：二者给出相反结论时，engine 的 prefilter
    （走 MASK）会把行级判定为通过的股票全部提前丢掉，推荐策略的换手率条件会
    永远筛不出任何股票。
    """
    return value / 100.0 if value > 1 else value


def _turnover_between(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    value = _turnover_ratio(float(row["turnover_rate"]))
    minimum = float(node.params["min"])
    maximum = float(node.params["max"])
    return ConditionResult(
        minimum <= value <= maximum,
        f"turnover {value:.2%} in [{minimum:.2%}, {maximum:.2%}]",
        value,
    )


def _past_return_at_most(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    window = int(node.params["window"])
    value = float(row[f"return_{window}d"])
    maximum = float(node.params["max"])
    return ConditionResult(value <= maximum, f"{window}d return {value:.2%} <= {maximum:.2%}", value)


def _past_return_between(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    window = int(node.params["window"])
    value = float(row[f"return_{window}d"])
    minimum = float(node.params["min"])
    maximum = float(node.params["max"])
    return ConditionResult(
        minimum <= value <= maximum,
        f"{window}d return {value:.2%} in [{minimum:.2%}, {maximum:.2%}]",
        value,
    )


def _volume_ratio_between(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    window = int(node.params["window"])
    value = float(row[f"volume_ratio_{window}d"])
    minimum = float(node.params["min"])
    maximum = float(node.params["max"])
    return ConditionResult(
        minimum <= value <= maximum,
        f"{window}d volume ratio {value:.2f} in [{minimum:.2f}, {maximum:.2f}]",
        value,
    )


def _macd_histogram_at_least(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    value = float(row["macd_hist"])
    minimum = float(node.params["min"])
    return ConditionResult(value >= minimum, f"MACD histogram {value:.4f} >= {minimum:.4f}", value)


def _macd_dead_cross(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    dif = pd.to_numeric(row["macd_dif"], errors="coerce")
    dea = pd.to_numeric(row["macd_dea"], errors="coerce")
    if pd.isna(dif) or pd.isna(dea):
        return ConditionResult(False, "MACD dead cross unavailable", None)
    symbol_frame = frame[
        (frame["symbol"] == row["symbol"]) & (frame["trade_date"] < row["trade_date"])
    ].sort_values("trade_date")
    if symbol_frame.empty:
        return ConditionResult(False, "MACD dead cross unavailable before enough history", None)
    prev = symbol_frame.iloc[-1]
    prev_dif = pd.to_numeric(prev["macd_dif"], errors="coerce")
    prev_dea = pd.to_numeric(prev["macd_dea"], errors="coerce")
    if pd.isna(prev_dif) or pd.isna(prev_dea):
        return ConditionResult(False, "MACD dead cross unavailable before enough history", None)
    crossed = float(prev_dif) >= float(prev_dea) and float(dif) < float(dea)
    return ConditionResult(
        crossed,
        f"MACD dead cross {float(prev_dif):.4f}/{float(prev_dea):.4f} -> {float(dif):.4f}/{float(dea):.4f}",
        float(dif - dea),
    )


def _breakout_above_n_day_high(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    window = int(node.params["window"])
    precomputed_column = f"prior_high_{window}d"
    if precomputed_column in row.index:
        prior_high = pd.to_numeric(row[precomputed_column], errors="coerce")
        if pd.isna(prior_high):
            return ConditionResult(False, f"{window}d prior high unavailable before enough history", None)
        prior_high = float(prior_high)
        value = float(row["close"] - prior_high)
        return ConditionResult(value > 0, f"close {row['close']:.2f} broke prior {window}d high {prior_high:.2f}", value)

    symbol_frame = frame[
        (frame["symbol"] == row["symbol"]) & (frame["trade_date"] < row["trade_date"])
    ].sort_values("trade_date")
    if len(symbol_frame) < window:
        return ConditionResult(False, f"{window}d prior high unavailable before enough history", None)
    prior_high = float(symbol_frame.tail(window)["high"].max())
    value = float(row["close"] - prior_high)
    return ConditionResult(value > 0, f"close {row['close']:.2f} broke prior {window}d high {prior_high:.2f}", value)


def _breakdown_below_n_day_low(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    window = int(node.params["window"])
    precomputed_column = f"prior_low_{window}d"
    if precomputed_column in row.index:
        prior_low = pd.to_numeric(row[precomputed_column], errors="coerce")
        if pd.isna(prior_low):
            return ConditionResult(False, f"{window}d prior low unavailable before enough history", None)
        prior_low = float(prior_low)
        value = float(row["close"] - prior_low)
        return ConditionResult(value < 0, f"close {row['close']:.2f} broke prior {window}d low {prior_low:.2f}", value)

    symbol_frame = frame[
        (frame["symbol"] == row["symbol"]) & (frame["trade_date"] < row["trade_date"])
    ].sort_values("trade_date")
    if len(symbol_frame) < window:
        return ConditionResult(False, f"{window}d prior low unavailable before enough history", None)
    prior_low = float(symbol_frame.tail(window)["low"].min())
    value = float(row["close"] - prior_low)
    return ConditionResult(value < 0, f"close {row['close']:.2f} broke prior {window}d low {prior_low:.2f}", value)


def _mask_market_cap_between(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    return data["float_market_cap"].between(float(node.params["min"]), float(node.params["max"]), inclusive="both")


def _mask_capital_flow_n_day_sum_at_least(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    window = int(node.params["window"])
    column = f"main_net_inflow_sum_{window}d"
    if column not in data:
        return _all_true_mask(data)
    return data[column] >= float(node.params["min"])


def _mask_capital_flow_n_day_sum_at_most(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    window = int(node.params["window"])
    column = f"main_net_inflow_sum_{window}d"
    if column not in data:
        return _all_true_mask(data)
    return data[column] <= float(node.params["max"])


def _mask_capital_flow_today_at_least(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    return data["main_net_inflow"] >= float(node.params["min"])


def _mask_capital_flow_today_at_most(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    return data["main_net_inflow"] <= float(node.params["max"])


def _mask_capital_flow_n_day_positive_count_at_least(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    window = int(node.params["window"])
    column = f"main_net_inflow_positive_count_{window}d"
    if column not in data:
        return _all_true_mask(data)
    return data[column] >= int(node.params["min_count"])


def _mask_market_rising_ratio_at_least(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    return data["market_rising_ratio"] >= float(node.params["min_ratio"])


def _mask_close_above_ma(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    window = int(node.params["window"])
    return data["close"] > data[f"ma_{window}"]


def _mask_close_below_ma(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    window = int(node.params["window"])
    return data["close"] < data[f"ma_{window}"]


def _mask_turnover_between(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    # 与行级 ``_turnover_between`` 共用 ``_turnover_ratio``：百分数 >1 归一到分数。
    # 不归一时 MASK 会用分数区间去比百分数量纲，prefilter 把候选全部丢掉。
    values = pd.to_numeric(data["turnover_rate"], errors="coerce")
    normalized = values.where(values <= 1, values / 100.0)
    return normalized.between(float(node.params["min"]), float(node.params["max"]), inclusive="both")


def _mask_past_return_at_most(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    window = int(node.params["window"])
    return data[f"return_{window}d"] <= float(node.params["max"])


def _mask_past_return_between(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    window = int(node.params["window"])
    return data[f"return_{window}d"].between(float(node.params["min"]), float(node.params["max"]), inclusive="both")


def _mask_volume_ratio_between(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    window = int(node.params["window"])
    return data[f"volume_ratio_{window}d"].between(float(node.params["min"]), float(node.params["max"]), inclusive="both")


def _mask_macd_histogram_at_least(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    return data["macd_hist"] >= float(node.params["min"])


def _mask_macd_dead_cross(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    previous_dif = data.groupby("symbol")["macd_dif"].shift(1)
    previous_dea = data.groupby("symbol")["macd_dea"].shift(1)
    return (previous_dif >= previous_dea) & (data["macd_dif"] < data["macd_dea"])


def _mask_breakout_above_n_day_high(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    window = int(node.params["window"])
    column = f"prior_high_{window}d"
    if column not in data:
        return _all_true_mask(data)
    return data["close"] > data[column]


def _mask_breakdown_below_n_day_low(node: ConditionNode, data: pd.DataFrame) -> pd.Series:
    window = int(node.params["window"])
    column = f"prior_low_{window}d"
    if column not in data:
        return _all_true_mask(data)
    return data["close"] < data[column]


EVALUATORS: dict[str, Evaluator] = {
    "market_cap_between": _market_cap_between,
    "capital_flow_n_day_sum_at_least": _capital_flow_n_day_sum_at_least,
    "capital_flow_n_day_sum_at_most": _capital_flow_n_day_sum_at_most,
    "capital_flow_today_at_least": _capital_flow_today_at_least,
    "capital_flow_today_at_most": _capital_flow_today_at_most,
    "capital_flow_n_day_positive_count_at_least": _capital_flow_n_day_positive_count_at_least,
    "market_rising_ratio_at_least": _market_rising_ratio_at_least,
    "close_above_ma": _close_above_ma,
    "close_below_ma": _close_below_ma,
    "turnover_between": _turnover_between,
    "past_return_at_most": _past_return_at_most,
    "past_return_between": _past_return_between,
    "volume_ratio_between": _volume_ratio_between,
    "macd_histogram_at_least": _macd_histogram_at_least,
    "macd_dead_cross": _macd_dead_cross,
    "breakout_above_n_day_high": _breakout_above_n_day_high,
    "breakdown_below_n_day_low": _breakdown_below_n_day_low,
}


MASK_BUILDERS: dict[str, MaskBuilder] = {
    "market_cap_between": _mask_market_cap_between,
    "capital_flow_n_day_sum_at_least": _mask_capital_flow_n_day_sum_at_least,
    "capital_flow_n_day_sum_at_most": _mask_capital_flow_n_day_sum_at_most,
    "capital_flow_today_at_least": _mask_capital_flow_today_at_least,
    "capital_flow_today_at_most": _mask_capital_flow_today_at_most,
    "capital_flow_n_day_positive_count_at_least": _mask_capital_flow_n_day_positive_count_at_least,
    "market_rising_ratio_at_least": _mask_market_rising_ratio_at_least,
    "close_above_ma": _mask_close_above_ma,
    "close_below_ma": _mask_close_below_ma,
    "turnover_between": _mask_turnover_between,
    "past_return_at_most": _mask_past_return_at_most,
    "past_return_between": _mask_past_return_between,
    "volume_ratio_between": _mask_volume_ratio_between,
    "macd_histogram_at_least": _mask_macd_histogram_at_least,
    "macd_dead_cross": _mask_macd_dead_cross,
    "breakout_above_n_day_high": _mask_breakout_above_n_day_high,
    "breakdown_below_n_day_low": _mask_breakdown_below_n_day_low,
}


def evaluate_condition(node: ConditionNode, row: pd.Series, frame: pd.DataFrame) -> ConditionResult:
    if not node.enabled:
        return ConditionResult(True, f"{node.condition_id} disabled")
    data_lag_days = int(node.data_lag_days or 0)
    if data_lag_days > 0:
        if "symbol" not in frame.columns or "trade_date" not in frame.columns:
            return ConditionResult(False, f"{node.condition_id} unavailable for {data_lag_days}d data lag")
        symbol_frame = frame[
            (frame["symbol"] == row["symbol"]) & (frame["trade_date"] < row["trade_date"])
        ].sort_values("trade_date")
        if len(symbol_frame) < data_lag_days:
            return ConditionResult(False, f"{node.condition_id} unavailable for {data_lag_days}d data lag")
        row = symbol_frame.iloc[-data_lag_days]
    try:
        evaluator = EVALUATORS[node.condition_id]
    except KeyError as exc:
        raise ValueError(f"unknown condition_id: {node.condition_id}") from exc
    return evaluator(node, row, frame)


def evaluate_group(
    group: ConditionGroup,
    row: pd.Series,
    frame: pd.DataFrame,
    score_threshold: float | None = None,
) -> GroupResult:
    results = [evaluate_condition(node, row, frame) for node in group.conditions]
    reasons = [result.reason for result in results if result.passed]

    if group.operator == ConditionOperator.AND:
        return GroupResult(all(result.passed for result in results), reasons)
    if group.operator == ConditionOperator.OR:
        return GroupResult(any(result.passed for result in results), reasons)

    score = 0.0
    for node, result in zip(group.conditions, results, strict=True):
        if result.passed:
            score += float(node.weight or 0.0)
    threshold = float(score_threshold or 0.0)
    return GroupResult(score >= threshold, reasons, score)
