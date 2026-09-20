from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from astock_backtester import indicators
from astock_backtester.condition_parser import (
    condition_examples,
    exit_condition_examples,
    validate_condition_text,
    validate_exit_condition_text,
)
from astock_backtester.conditions import (
    EVALUATORS,
    MASK_BUILDERS,
    evaluate_condition,
    evaluate_group,
    registered_conditions,
)
from astock_backtester.data.trading_calendar import a_share_trade_dates
from astock_backtester.indicators import (
    add_capital_flow_sum,
    add_macd,
    add_market_heat,
    add_moving_average,
    add_prior_high_low,
    add_returns,
    add_volume_ratio,
)
from astock_backtester.models import (
    BacktestSettings,
    ClsFinanceAnchor,
    ClsFinanceEmotion,
    ClsFinancePoolItem,
    ClsFinanceResponse,
    ClsFinanceTlinePoint,
    ConditionGroup,
    ConditionNode,
    ConditionOperator,
    MarketBreadth,
    StrategyConfig,
    SyncJobStatus,
)
from astock_backtester.sample_data import sample_daily_bars
from pydantic import ValidationError

# Merged from: test_indicators.py, test_conditions.py, test_models.py, test_trading_calendar.py


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------


def test_sample_daily_bars_contract():
    df = sample_daily_bars()
    required_columns = {
        "symbol",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover_rate",
        "float_market_cap",
        "main_net_inflow",
        "is_st",
        "is_suspended",
        "listing_days",
    }

    assert set(df["symbol"]) == {"AAA", "BBB"}
    assert pd.api.types.is_datetime64_any_dtype(df["trade_date"])
    assert required_columns.issubset(df.columns)
    assert df.equals(df.sort_values(["symbol", "trade_date"]).reset_index(drop=True))


def test_add_moving_average_uses_symbol_boundaries():
    df = sample_daily_bars()
    result = add_moving_average(df, windows=[3])
    aaa = result[result["symbol"] == "AAA"].reset_index(drop=True)
    bbb = result[result["symbol"] == "BBB"].reset_index(drop=True)

    assert pd.isna(aaa.loc[1, "ma_3"])
    assert pd.isna(bbb.loc[0, "ma_3"])
    assert pd.isna(bbb.loc[1, "ma_3"])
    assert aaa.loc[2, "ma_3"] == 11.0
    assert bbb.loc[2, "ma_3"] == 21.0


def test_add_returns_calculates_past_gain_without_future_rows():
    df = sample_daily_bars()
    result = add_returns(df, windows=[2])
    aaa = result[result["symbol"] == "AAA"].reset_index(drop=True)
    bbb = result[result["symbol"] == "BBB"].reset_index(drop=True)

    assert round(aaa.loc[2, "return_2d"], 6) == round((12 / 10) - 1, 6)
    assert pd.isna(bbb.loc[0, "return_2d"])
    assert pd.isna(bbb.loc[1, "return_2d"])
    assert round(bbb.loc[2, "return_2d"], 6) == round((22 / 20) - 1, 6)


def test_add_macd_outputs_expected_columns():
    result = add_macd(sample_daily_bars())

    assert {"macd_dif", "macd_dea", "macd_hist"}.issubset(result.columns)
    assert result["macd_hist"].notna().any()


def test_add_market_heat_computes_rising_ratio_by_date():
    result = add_market_heat(sample_daily_bars())
    heat = result[["trade_date", "market_rising_ratio"]].drop_duplicates()
    row = heat[heat["trade_date"] == pd.Timestamp("2024-01-03")].iloc[0]

    assert row["market_rising_ratio"] == 1.0


def test_add_volume_ratio_uses_prior_window_only():
    result = add_volume_ratio(sample_daily_bars(), windows=[2])
    aaa = result[result["symbol"] == "AAA"].reset_index(drop=True)

    assert pd.isna(aaa.loc[0, "volume_ratio_2d"])
    assert round(aaa.loc[2, "volume_ratio_2d"], 6) == round(2200 / ((1000 + 1500) / 2), 6)


def test_add_prior_high_low_uses_only_previous_rows():
    result = add_prior_high_low(sample_daily_bars(), windows=[2])
    aaa = result[result["symbol"] == "AAA"].reset_index(drop=True)

    assert pd.isna(aaa.loc[1, "prior_high_2d"])
    assert aaa.loc[2, "prior_high_2d"] == 11.2
    assert aaa.loc[2, "prior_low_2d"] == 9.8


def test_add_capital_flow_sum_uses_rolling_symbol_window():
    result = add_capital_flow_sum(sample_daily_bars(), windows=[3])
    aaa = result[result["symbol"] == "AAA"].reset_index(drop=True)

    assert pd.isna(aaa.loc[1, "main_net_inflow_sum_3d"])
    assert aaa.loc[2, "main_net_inflow_sum_3d"] == 9_000_000


def test_add_capital_flow_positive_count_uses_rolling_symbol_window():
    assert hasattr(indicators, "add_capital_flow_positive_count")

    result = indicators.add_capital_flow_positive_count(sample_daily_bars(), windows=[3])
    aaa = result[result["symbol"] == "AAA"].reset_index(drop=True)
    bbb = result[result["symbol"] == "BBB"].reset_index(drop=True)

    assert pd.isna(aaa.loc[1, "main_net_inflow_positive_count_3d"])
    assert aaa.loc[2, "main_net_inflow_positive_count_3d"] == 3
    assert aaa.loc[3, "main_net_inflow_positive_count_3d"] == 2
    assert bbb.loc[4, "main_net_inflow_positive_count_3d"] == 2


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def enriched_frame() -> pd.DataFrame:
    frame = add_market_heat(add_moving_average(sample_daily_bars(), [3]))
    frame = add_returns(frame, [2])
    frame = add_macd(frame)
    frame = add_volume_ratio(frame, [2])
    return frame


def test_registry_contains_core_first_version_conditions():
    ids = {item.condition_id for item in registered_conditions()}

    assert "market_cap_between" in ids
    assert "capital_flow_n_day_sum_at_least" in ids
    assert "capital_flow_n_day_sum_at_most" in ids
    assert "capital_flow_today_at_least" in ids
    assert "capital_flow_today_at_most" in ids
    assert "capital_flow_n_day_positive_count_at_least" in ids
    assert "market_rising_ratio_at_least" in ids
    assert "close_above_ma" in ids
    assert "macd_histogram_at_least" in ids
    assert "macd_dead_cross" in ids
    assert "volume_ratio_between" in ids
    assert "past_return_between" in ids
    assert "breakout_above_n_day_high" in ids
    assert "breakdown_below_n_day_low" in ids


def test_market_cap_between_uses_signal_day_value():
    df = enriched_frame()
    row = df[(df["symbol"] == "AAA") & (df["trade_date"] == pd.Timestamp("2024-01-04"))].iloc[0]
    node = ConditionNode(
        id="cap",
        condition_id="market_cap_between",
        params={"min": 8_000_000_000, "max": 10_000_000_000},
    )

    result = evaluate_condition(node, row, df)

    assert result.passed is True
    assert "float market cap" in result.reason


def test_turnover_between_accepts_percent_unit_values():
    row = pd.Series({"turnover_rate": 4.5})
    node = ConditionNode(
        id="turnover",
        condition_id="turnover_between",
        params={"min": 0.02, "max": 0.08},
    )

    result = evaluate_condition(node, row, pd.DataFrame([row]))

    assert result.passed is True
    assert result.observed_value == 0.045
    assert "turnover 4.50%" in result.reason


def test_capital_flow_rolling_sum_is_date_bound():
    df = enriched_frame()
    row = df[(df["symbol"] == "AAA") & (df["trade_date"] == pd.Timestamp("2024-01-04"))].iloc[0]
    node = ConditionNode(
        id="flow",
        condition_id="capital_flow_n_day_sum_at_least",
        params={"window": 3, "min": 9_000_000},
    )

    result = evaluate_condition(node, row, df)

    assert result.passed is True
    assert result.observed_value == 9_000_000


def test_capital_flow_today_condition_uses_signal_day_value():
    df = enriched_frame()
    row = df[(df["symbol"] == "AAA") & (df["trade_date"] == pd.Timestamp("2024-01-04"))].iloc[0]
    node = ConditionNode(id="flow-today", condition_id="capital_flow_today_at_least", params={"min": 4_000_000})

    result = evaluate_condition(node, row, df)

    assert result.passed is True
    assert result.observed_value == 4_000_000


def test_capital_flow_positive_count_uses_precomputed_column():
    assert hasattr(indicators, "add_capital_flow_positive_count")

    df = indicators.add_capital_flow_positive_count(enriched_frame(), [3])
    row = df[(df["symbol"] == "AAA") & (df["trade_date"] == pd.Timestamp("2024-01-05"))].iloc[0]
    node = ConditionNode(
        id="flow-days",
        condition_id="capital_flow_n_day_positive_count_at_least",
        params={"window": 3, "min_count": 2},
    )

    result = evaluate_condition(node, row, df)

    assert result.passed is True
    assert result.observed_value == 2


def test_capital_flow_positive_count_requires_enough_history():
    assert hasattr(indicators, "add_capital_flow_positive_count")

    df = indicators.add_capital_flow_positive_count(enriched_frame(), [3])
    row = df[(df["symbol"] == "AAA") & (df["trade_date"] == pd.Timestamp("2024-01-03"))].iloc[0]
    node = ConditionNode(
        id="flow-days",
        condition_id="capital_flow_n_day_positive_count_at_least",
        params={"window": 3, "min_count": 2},
    )

    result = evaluate_condition(node, row, df)

    assert result.passed is False
    assert result.observed_value is None


def test_and_group_requires_all_conditions():
    df = enriched_frame()
    row = df[(df["symbol"] == "AAA") & (df["trade_date"] == pd.Timestamp("2024-01-04"))].iloc[0]
    group = ConditionGroup(
        id="entry",
        operator=ConditionOperator.AND,
        conditions=[
            ConditionNode(id="cap", condition_id="market_cap_between", params={"min": 1, "max": 10_000_000_000}),
            ConditionNode(id="ma", condition_id="close_above_ma", params={"window": 3}),
        ],
    )

    result = evaluate_group(group, row, df)

    assert result.passed is True
    assert len(result.reasons) == 2


def test_score_group_requires_threshold():
    df = enriched_frame()
    row = df[(df["symbol"] == "AAA") & (df["trade_date"] == pd.Timestamp("2024-01-04"))].iloc[0]
    group = ConditionGroup(
        id="score",
        operator=ConditionOperator.SCORE,
        conditions=[
            ConditionNode(id="cap", condition_id="market_cap_between", params={"min": 1, "max": 10_000_000_000}, weight=20),
            ConditionNode(id="hot", condition_id="market_rising_ratio_at_least", params={"min_ratio": 0.5}, weight=15),
        ],
    )

    result = evaluate_group(group, row, df, score_threshold=30)

    assert result.passed is True
    assert result.score == 35


def test_volume_ratio_between_uses_prior_average_volume():
    df = enriched_frame()
    row = df[(df["symbol"] == "AAA") & (df["trade_date"] == pd.Timestamp("2024-01-04"))].iloc[0]
    node = ConditionNode(id="vr", condition_id="volume_ratio_between", params={"window": 2, "min": 1.7, "max": 2.0})

    result = evaluate_condition(node, row, df)

    assert result.passed is True
    assert round(result.observed_value or 0, 4) == round(2200 / ((1000 + 1500) / 2), 4)


def test_macd_histogram_condition_evaluates_registered_indicator():
    df = enriched_frame()
    row = df[(df["symbol"] == "AAA") & (df["trade_date"] == pd.Timestamp("2024-01-04"))].iloc[0]
    node = ConditionNode(id="macd", condition_id="macd_histogram_at_least", params={"min": 0})

    result = evaluate_condition(node, row, df)

    assert result.passed is True
    assert "MACD histogram" in result.reason


def test_macd_dead_cross_condition_uses_prior_signal_cross():
    df = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA", "AAA"],
            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "macd_dif": [0.08, 0.04, -0.02],
            "macd_dea": [0.03, 0.04, 0.01],
        }
    )
    row = df[df["trade_date"] == pd.Timestamp("2024-01-04")].iloc[0]
    node = ConditionNode(id="macd-dead", condition_id="macd_dead_cross", params={})

    result = evaluate_condition(node, row, df)

    assert result.passed is True
    assert result.observed_value is not None
    assert "MACD dead cross" in result.reason


def test_capital_flow_outflow_conditions_support_today_and_rolling_sum():
    df = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA", "AAA"],
            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
            "main_net_inflow": [800_000.0, -1_200_000.0, -900_000.0],
            "main_net_inflow_sum_3d": [float("nan"), float("nan"), -1_300_000.0],
        }
    )
    row = df[df["trade_date"] == pd.Timestamp("2024-01-04")].iloc[0]

    today = evaluate_condition(
        ConditionNode(id="flow-today-out", condition_id="capital_flow_today_at_most", params={"max": 0}),
        row,
        df,
    )
    rolling = evaluate_condition(
        ConditionNode(id="flow-rolling-out", condition_id="capital_flow_n_day_sum_at_most", params={"window": 3, "max": 0}),
        row,
        df,
    )

    assert today.passed is True
    assert today.observed_value == -900_000
    assert rolling.passed is True
    assert rolling.observed_value == -1_300_000


def test_past_return_between_supports_prior_gain_ranges():
    df = enriched_frame()
    row = df[(df["symbol"] == "AAA") & (df["trade_date"] == pd.Timestamp("2024-01-04"))].iloc[0]
    node = ConditionNode(
        id="gain",
        condition_id="past_return_between",
        params={"window": 2, "min": 0.15, "max": 0.25},
    )

    result = evaluate_condition(node, row, df)

    assert result.passed is True
    assert round(result.observed_value or 0, 6) == round((12 / 10) - 1, 6)


def test_breakout_above_n_day_high_uses_prior_highs_only():
    df = enriched_frame()
    row = df[(df["symbol"] == "AAA") & (df["trade_date"] == pd.Timestamp("2024-01-04"))].iloc[0]
    node = ConditionNode(id="breakout", condition_id="breakout_above_n_day_high", params={"window": 2})

    result = evaluate_condition(node, row, df)

    assert result.passed is True
    assert result.observed_value == 12.0 - 11.2


def test_breakdown_below_n_day_low_uses_prior_lows_only():
    df = pd.DataFrame(
        {
            "symbol": ["AAA", "AAA", "AAA", "AAA"],
            "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]),
            "open": [10.0, 10.3, 10.1, 9.5],
            "high": [10.5, 10.6, 10.4, 9.8],
            "low": [9.8, 9.9, 10.0, 9.4],
            "close": [10.2, 10.1, 10.2, 9.6],
            "volume": [1000, 1000, 1000, 1000],
        }
    )
    row = df[df["trade_date"] == pd.Timestamp("2024-01-05")].iloc[0]
    node = ConditionNode(id="breakdown", condition_id="breakdown_below_n_day_low", params={"window": 3})

    result = evaluate_condition(node, row, df)

    assert result.passed is True
    assert round(result.observed_value or 0, 6) == round(9.6 - 9.8, 6)
    assert "prior 3d low" in result.reason


def test_breakout_breakdown_and_capital_flow_use_precomputed_columns():
    row = pd.Series(
        {
            "symbol": "AAA",
            "trade_date": pd.Timestamp("2024-01-04"),
            "close": 12.0,
            "prior_high_2d": 11.2,
            "prior_low_2d": 10.8,
            "main_net_inflow_sum_3d": 9_000_000,
        }
    )
    frame_without_history = pd.DataFrame([row])

    breakout = evaluate_condition(
        ConditionNode(id="breakout", condition_id="breakout_above_n_day_high", params={"window": 2}),
        row,
        frame_without_history,
    )
    breakdown = evaluate_condition(
        ConditionNode(id="breakdown", condition_id="breakdown_below_n_day_low", params={"window": 2}),
        row,
        frame_without_history,
    )
    flow = evaluate_condition(
        ConditionNode(id="flow", condition_id="capital_flow_n_day_sum_at_least", params={"window": 3, "min": 8_000_000}),
        row,
        frame_without_history,
    )

    assert breakout.passed is True
    assert round(breakout.observed_value or 0, 6) == round(12.0 - 11.2, 6)
    assert breakdown.passed is False
    assert round(breakdown.observed_value or 0, 6) == round(12.0 - 10.8, 6)
    assert flow.passed is True
    assert flow.observed_value == 9_000_000


def test_user_written_condition_text_is_validated_into_executable_node():
    result = validate_condition_text("收盘价站上20日均线")

    assert result.ok is True
    assert result.condition is not None
    assert result.condition.condition_id == "close_above_ma"
    assert result.condition.params == {"window": 20}
    assert result.normalized_text == "收盘价站上20日均线"
    assert result.errors == []


def test_user_written_exit_condition_supports_breaking_prior_low_text():
    result = validate_exit_condition_text("突破20日最低")

    assert result.ok is True
    assert result.condition is not None
    assert result.condition.condition_id == "breakdown_below_n_day_low"
    assert result.condition.params == {"window": 20}
    assert result.normalized_text == "突破20日最低"
    assert "跌破20日低点" in result.examples
    assert exit_condition_examples()


def test_user_written_exit_condition_supports_return_dead_cross_and_capital_outflow_text():
    weak_return = validate_exit_condition_text("近五日涨幅小于3%")
    dead_cross = validate_exit_condition_text("MACD死叉")
    rolling_outflow = validate_exit_condition_text("近3日主力净流出")
    today_outflow = validate_exit_condition_text("资金流出")

    assert weak_return.ok is True
    assert weak_return.condition is not None
    assert weak_return.condition.condition_id == "past_return_at_most"
    assert weak_return.condition.params == {"window": 5, "max": 0.03}
    assert dead_cross.ok is True
    assert dead_cross.condition is not None
    assert dead_cross.condition.condition_id == "macd_dead_cross"
    assert rolling_outflow.ok is True
    assert rolling_outflow.condition is not None
    assert rolling_outflow.condition.condition_id == "capital_flow_n_day_sum_at_most"
    assert rolling_outflow.condition.params == {"window": 3, "max": 0.0}
    assert today_outflow.ok is True
    assert today_outflow.condition is not None
    assert today_outflow.condition.condition_id == "capital_flow_today_at_most"


def test_user_written_exit_condition_has_exit_specific_error_message():
    result = validate_exit_condition_text("突破20日前高")

    assert result.ok is False
    assert result.condition is None
    assert result.errors[0].code == "unrecognized_exit_condition"
    assert "离场条件" in result.errors[0].message
    assert "跌破N日低点" in result.errors[0].message


def test_user_written_condition_supports_market_cap_and_percent_ranges():
    cap = validate_condition_text("流通市值10亿到300亿")
    turnover = validate_condition_text("换手率2%到8%")

    assert cap.ok is True
    assert cap.condition is not None
    assert cap.condition.condition_id == "market_cap_between"
    assert cap.condition.params == {"min": 1_000_000_000, "max": 30_000_000_000}
    assert turnover.ok is True
    assert turnover.condition is not None
    assert turnover.condition.condition_id == "turnover_between"
    assert turnover.condition.params == {"min": 0.02, "max": 0.08}


def test_user_written_condition_reports_examples_for_unrecognized_text():
    result = validate_condition_text("随便乱写一个无法执行的条件")

    assert result.ok is False
    assert result.condition is None
    assert result.errors[0].code == "unrecognized_condition"
    assert "无法识别" in result.errors[0].message
    assert "收盘价站上20日均线" in result.examples
    assert condition_examples()


def test_user_written_condition_reports_supported_templates_for_unrecognized_text():
    result = validate_condition_text("随便乱写一个无法执行的条件")

    assert result.ok is False
    assert result.condition is None
    assert "收盘价站上N日均线" in result.errors[0].message
    assert "量比N日介于A到B" in result.errors[0].message
    assert "近N日主力净流入大于X万/亿" in result.errors[0].message


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def make_backtest_settings(**overrides):
    settings = {
        "start_date": date(2024, 1, 2),
        "end_date": date(2024, 1, 10),
        "initial_cash": 100_000,
    }
    settings.update(overrides)
    return BacktestSettings(**settings)


def test_strategy_config_rejects_empty_entry_groups():
    with pytest.raises(ValidationError):
        StrategyConfig(
            name="bad",
            market_filters=[],
            entry_groups=[],
            exit_rules=[],
            score_threshold=None,
        )


def test_condition_registry_stays_in_sync():
    """Every registered condition must have both a row evaluator and a
    vectorized mask builder, otherwise the engine prefilter silently drifts
    away from the row-wise semantics."""
    registry_ids = {definition.condition_id for definition in registered_conditions()}
    assert set(EVALUATORS) == registry_ids
    assert set(MASK_BUILDERS) == registry_ids


def test_backtest_settings_defaults_to_conservative_execution():
    settings = BacktestSettings(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 10),
        initial_cash=100_000,
    )

    assert settings.conservative_execution is True
    assert settings.buy_price == "next_open"
    assert settings.limit_up_blocks_buy is True
    assert settings.limit_down_blocks_sell is True
    assert settings.stock_pool == "all"
    assert settings.custom_symbols == []
    assert settings.position_sizing_mode == "equal_slots"
    assert settings.position_size_pct == 0.2


def test_backtest_settings_rejects_negative_fee_rate():
    with pytest.raises(ValidationError):
        make_backtest_settings(fee_rate=-0.0001)


def test_backtest_settings_rejects_negative_stamp_tax_rate():
    with pytest.raises(ValidationError):
        make_backtest_settings(stamp_tax_rate=-0.0001)


def test_backtest_settings_rejects_negative_slippage_rate():
    with pytest.raises(ValidationError):
        make_backtest_settings(slippage_rate=-0.0001)


def test_backtest_settings_rejects_negative_min_listing_days():
    with pytest.raises(ValidationError):
        make_backtest_settings(min_listing_days=-1)


def test_condition_node_rejects_negative_data_lag_days():
    with pytest.raises(ValidationError):
        ConditionNode(
            id="cap-small",
            condition_id="market_cap_between",
            data_lag_days=-1,
        )


def test_condition_group_rejects_empty_conditions():
    with pytest.raises(ValidationError):
        ConditionGroup(id="entry", operator=ConditionOperator.AND, conditions=[])


def test_cls_finance_response_serializes_market_board_data():
    response = ClsFinanceResponse(
        updated_at="2026-06-09T09:31:00+08:00",
        source="cls-finance",
        source_url="https://www.cls.cn/finance",
        preclose_px=3959.337,
        tline=[ClsFinanceTlinePoint(date=20260609, minute=930, last_px=3977.539, change=0.0047)],
        anchors=[
            ClsFinanceAnchor(
                code="cls80025",
                name="PCB",
                article_id=2394344,
                c_time="2026-06-09 09:31:30",
                direction="up",
                url="https://www.cls.cn/plate?code=cls80025",
            )
        ],
        emotion=ClsFinanceEmotion(
            market_degree=56.0,
            market_degree_source="ths-market-summary",
            market_degree_label="同花顺大盘评级",
            breadth=MarketBreadth(up=3322, down=2049, flat=156, total=5527, source="cls-finance-emotion"),
            up_limit=130,
            open_limit=25,
            performance="1.74%",
        ),
        up_pool=[
            ClsFinancePoolItem(
                symbol="601869",
                name="长飞光纤",
                change_pct=0.1,
                last=484.33,
                time="2026-06-09 13:34:47",
                reason="光纤",
                limit_up_days=1,
            )
        ],
    )

    payload = response.model_dump(mode="json")

    assert payload["source"] == "cls-finance"
    assert payload["anchors"][0]["name"] == "PCB"
    assert payload["emotion"]["market_degree"] == 56.0
    assert payload["emotion"]["market_degree_source"] == "ths-market-summary"
    assert payload["emotion"]["market_degree_label"] == "同花顺大盘评级"
    assert payload["up_pool"][0]["symbol"] == "601869"


def test_sync_job_status_tracks_batch_progress_and_cancellation():
    status = SyncJobStatus(
        job_id="job-flow",
        mode="capital_flow_backfill",
        status="cancelled",
        total_symbols=5,
        completed_symbols=2,
        failed_symbols=1,
        processed_symbols=3,
        imported_rows=12,
        returned_rows=18,
        filled_missing_rows=7,
        skipped_symbols=1,
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        last_error="000003: remote disconnected",
        recent_failures=[{"symbol": "000003", "reason": "remote disconnected"}],
    )

    assert status.status == "cancelled"
    assert status.processed_symbols == 3
    assert status.returned_rows == 18
    assert status.filled_missing_rows == 7
    assert status.skipped_symbols == 1
    assert status.last_error == "000003: remote disconnected"


def test_backtest_settings_accepts_controlled_stock_pool_and_custom_symbols():
    settings = make_backtest_settings(stock_pool="custom", custom_symbols=["600519", "000001"])

    assert settings.stock_pool == "custom"
    assert settings.custom_symbols == ["600519", "000001"]


def test_backtest_settings_accepts_position_sizing_fields():
    settings = make_backtest_settings(position_sizing_mode="equal_slots", position_size_pct=0.25)

    assert settings.position_sizing_mode == "equal_slots"
    assert settings.position_size_pct == 0.25


def test_backtest_settings_rejects_invalid_position_size_pct():
    with pytest.raises(ValidationError):
        make_backtest_settings(position_size_pct=0)

    with pytest.raises(ValidationError):
        make_backtest_settings(position_size_pct=1.2)


def test_custom_stock_pool_requires_symbols():
    with pytest.raises(ValidationError):
        make_backtest_settings(stock_pool="custom", custom_symbols=[])


# ---------------------------------------------------------------------------
# Trading Calendar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "holiday",
    [
        "2027-02-08",
        "2027-10-04",
        "2028-01-26",
        "2028-10-02",
    ],
)
def test_a_share_trade_dates_excludes_2027_and_2028_holidays(holiday):
    assert pd.Timestamp(holiday) not in a_share_trade_dates(holiday, holiday)


@pytest.mark.parametrize(
    "holiday",
    [
        # 2015-2023 补录（此前表只从 2024 起，这些休市日曾被当成交易日
        # 计入缺口，制造出数十万“节假日幽灵缺口”）。
        "2015-02-19",
        "2015-09-03",
        "2016-10-04",
        "2017-10-05",
        "2018-02-19",
        "2019-10-02",
        "2020-01-28",
        "2021-02-15",
        "2022-10-04",
        "2023-05-01",
        "2023-09-29",
        "2023-10-04",
    ],
)
def test_a_share_trade_dates_excludes_2015_to_2023_holidays(holiday):
    assert pd.Timestamp(holiday) not in a_share_trade_dates(holiday, holiday)


@pytest.mark.parametrize(
    "trade_day",
    [
        "2015-01-05",
        "2015-09-07",
        "2016-02-15",
        "2022-10-10",
        "2023-10-09",
        "2023-10-30",
    ],
)
def test_a_share_trade_dates_keeps_adjacent_trade_days(trade_day):
    """补录节假日不得把相邻的真实交易日误删。"""
    assert pd.Timestamp(trade_day) in a_share_trade_dates(trade_day, trade_day)
