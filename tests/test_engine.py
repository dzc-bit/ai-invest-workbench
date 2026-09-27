import pandas as pd
import pytest
from astock_backtester import engine
from astock_backtester.engine import run_backtest
from astock_backtester.indicators import add_market_heat, add_moving_average, add_returns
from astock_backtester.models import (
    BacktestSettings,
    ConditionGroup,
    ConditionNode,
    ConditionOperator,
    StrategyConfig,
)
from astock_backtester.sample_data import sample_daily_bars


def enriched_data():
    frame = sample_daily_bars()
    frame = add_moving_average(frame, [3])
    frame = add_returns(frame, [2])
    frame = add_market_heat(frame)
    return frame


def simple_market_cap_strategy() -> StrategyConfig:
    return StrategyConfig(
        name="simple",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="cap",
                        condition_id="market_cap_between",
                        params={"min": 1_000_000_000, "max": 10_000_000_000},
                    )
                ],
            )
        ],
        exit_rules=[],
    )


def backtest_row(
    trade_date: str,
    *,
    symbol: str = "000001",
    open_price: float = 10.0,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    pre_close: float | None = None,
    is_st: bool = False,
    is_suspended: bool = False,
) -> dict:
    close_price = close if close is not None else open_price
    row = {
        "symbol": symbol,
        "trade_date": pd.Timestamp(trade_date),
        "open": open_price,
        "high": high if high is not None else max(open_price, close_price),
        "low": low if low is not None else min(open_price, close_price),
        "close": close_price,
        "volume": 1000,
        "is_suspended": is_suspended,
        "listing_days": 500,
        "float_market_cap": 2_000_000_000,
        "main_net_inflow": 0.0,
        "is_st": is_st,
    }
    if pre_close is not None:
        row["pre_close"] = pre_close
    return row


def test_backtest_buys_next_open_after_signal(basic_strategy, basic_settings):
    result = run_backtest(enriched_data(), basic_strategy, basic_settings)

    assert result.trades
    first = result.trades[0]
    assert str(first.buy_signal_date) == "2024-01-04"
    assert str(first.buy_date) == "2024-01-05"
    assert first.buy_price == pytest.approx(12.0 * (1 + basic_settings.slippage_rate))
    assert any("float market cap" in reason for reason in first.buy_reason)


def test_backtest_respects_max_daily_buys(basic_strategy, basic_settings):
    result = run_backtest(enriched_data(), basic_strategy, basic_settings)
    buys_by_day = {}
    for trade in result.trades:
        buys_by_day.setdefault(trade.buy_date, 0)
        buys_by_day[trade.buy_date] += 1

    assert max(buys_by_day.values()) <= 1


def test_backtest_result_reports_latest_trade_day_strategy_matches_without_daily_buy_limit():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    rows = []
    for symbol, name, close, volume, volume_ratio, change_pct in [
        ("AAA", "Alpha", 10.0, 1000, 1.1, 0.01),
        ("BBB", "Bravo", 11.0, 9000, 1.5, 0.03),
        ("CCC", "Charlie", 12.0, 5000, 1.2, -0.01),
    ]:
        for trade_date in dates:
            rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "trade_date": trade_date,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "change_pct": change_pct,
                    "volume": volume,
                    "volume_ratio_2d": volume_ratio,
                    "is_suspended": False,
                    "listing_days": 500,
                    "float_market_cap": 2_000_000_000,
                    "main_net_inflow": 0.0,
                }
            )
    frame = pd.DataFrame(rows)
    strategy = StrategyConfig(
        name="latest-matches",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="cap",
                        condition_id="market_cap_between",
                        params={"min": 1_000_000_000, "max": 3_000_000_000},
                    )
                ],
            )
        ],
        exit_rules=[],
    )
    settings = BacktestSettings(
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        initial_cash=100_000,
        max_positions=10,
        max_daily_buys=1,
        min_listing_days=0,
    )

    events = []

    result = run_backtest(frame, strategy, settings, on_event=lambda event: events.append(event))

    opened_trades = [event["trade"] for event in events if event["type"] == "trade_opened"]
    assert len(opened_trades) == 1
    assert opened_trades[0].symbol == "BBB"
    assert result.latest_strategy_matches is not None
    assert str(result.latest_strategy_matches.signal_date) == "2024-01-03"
    assert str(result.latest_strategy_matches.trade_date) == "2024-01-03"
    assert [match.symbol for match in result.latest_strategy_matches.matches] == ["BBB", "CCC", "AAA"]

    first_match = result.latest_strategy_matches.matches[0]
    assert first_match.name == "Bravo"
    assert str(first_match.signal_date) == "2024-01-03"
    assert str(first_match.trade_date) == "2024-01-03"
    assert first_match.close == 11.0
    assert first_match.change_pct == 0.03
    assert first_match.rank_score == 67.5
    assert any("float market cap" in reason for reason in first_match.reasons)


def test_backtest_result_reports_empty_matches_for_latest_trade_day_without_reusing_older_hits():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    rows = []
    for trade_date, market_cap in [
        (dates[0], 2_000_000_000),
        (dates[1], 2_000_000_000),
        (dates[2], 9_000_000_000),
    ]:
        rows.append(
            {
                "symbol": "AAA",
                "name": "Alpha",
                "trade_date": trade_date,
                "open": 10.0,
                "high": 10.2,
                "low": 9.8,
                "close": 10.0,
                "change_pct": 0.01,
                "volume": 1000,
                "is_suspended": False,
                "listing_days": 500,
                "float_market_cap": market_cap,
                "main_net_inflow": 0.0,
            }
        )
    frame = pd.DataFrame(rows)
    strategy = StrategyConfig(
        name="latest-empty-matches",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="cap",
                        condition_id="market_cap_between",
                        params={"min": 1_000_000_000, "max": 3_000_000_000},
                    )
                ],
            )
        ],
        exit_rules=[],
    )
    settings = BacktestSettings(
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        initial_cash=100_000,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
    )

    result = run_backtest(frame, strategy, settings)

    assert result.latest_strategy_matches is not None
    assert str(result.latest_strategy_matches.signal_date) == "2024-01-04"
    assert result.latest_strategy_matches.matches == []


def test_backtest_reports_metrics_and_equity_curve(basic_strategy, basic_settings):
    result = run_backtest(enriched_data(), basic_strategy, basic_settings)

    assert result.metrics.trade_count >= 1
    assert result.equity_curve
    assert result.metrics.max_drawdown_pct <= 0
    assert result.metrics.average_position_pct > 0
    assert result.metrics.max_position_pct >= result.metrics.average_position_pct


def test_equity_curve_does_not_deduct_next_day_buy_cash_on_signal_date():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=10.0, high=10.1, low=9.9, close=10.0, pre_close=10.0),
            backtest_row("2024-01-04", open_price=10.0, high=10.1, low=9.9, close=10.0, pre_close=10.0),
        ]
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-04").date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        position_sizing_mode="fixed_ratio",
        position_size_pct=0.5,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        take_profit_pct=None,
        stop_loss_pct=None,
    )

    events = []

    result = run_backtest(frame, simple_market_cap_strategy(), settings, on_event=lambda event: events.append(event))

    opened_trade = next(event["trade"] for event in events if event["type"] == "trade_opened")
    assert opened_trade.buy_signal_date.isoformat() == "2024-01-02"
    assert opened_trade.buy_date.isoformat() == "2024-01-03"
    assert result.equity_curve[0].trade_date.isoformat() == "2024-01-02"
    assert result.equity_curve[0].equity == pytest.approx(100_000)
    assert result.equity_curve[0].cash == pytest.approx(100_000)
    assert result.equity_curve[0].market_value == pytest.approx(0)
    assert result.equity_curve[1].cash < result.equity_curve[0].cash
    assert result.equity_curve[1].market_value > 0


def test_position_size_caps_each_stock_instead_of_total_portfolio_exposure():
    rows = []
    for symbol in ["AAA", "BBB", "CCC"]:
        rows.extend(
            [
                backtest_row("2024-01-02", symbol=symbol, close=10.0),
                backtest_row("2024-01-03", symbol=symbol, open_price=10.0, close=10.0, pre_close=10.0),
                backtest_row("2024-01-04", symbol=symbol, open_price=10.0, close=10.0, pre_close=10.0),
            ]
        )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-04").date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        max_positions=5,
        max_daily_buys=5,
        min_listing_days=0,
        position_sizing_mode="equal_slots",
        position_size_pct=0.2,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        conservative_execution=False,
    )

    result = run_backtest(pd.DataFrame(rows), simple_market_cap_strategy(), settings)

    opened = [trade for trade in result.trades if trade.buy_amount > 0]
    assert len(opened) == 3
    assert [trade.buy_amount for trade in opened] == [20_000, 20_000, 20_000]
    assert [trade.target_position_pct for trade in opened] == [0.2, 0.2, 0.2]
    max_curve_exposure = max(point.market_value / point.equity for point in result.equity_curve if point.equity)
    assert max_curve_exposure == pytest.approx(0.6)
    assert result.metrics.max_position_pct == pytest.approx(max_curve_exposure)


def test_exit_rules_sell_on_weak_recent_return_macd_dead_cross_and_capital_outflow():
    rows = [
        {
            **backtest_row("2024-01-02", close=10.0),
            "return_5d": 0.05,
            "macd_dif": 0.08,
            "macd_dea": 0.03,
            "main_net_inflow_sum_3d": 1_000_000.0,
        },
        {
            **backtest_row("2024-01-03", open_price=10.0, close=10.0, pre_close=10.0),
            "return_5d": 0.05,
            "macd_dif": 0.04,
            "macd_dea": 0.04,
            "main_net_inflow_sum_3d": 500_000.0,
        },
        {
            **backtest_row("2024-01-04", open_price=10.0, close=10.1, pre_close=10.0),
            "return_5d": 0.02,
            "macd_dif": -0.02,
            "macd_dea": 0.01,
            "main_net_inflow_sum_3d": -100_000.0,
        },
        {
            **backtest_row("2024-01-05", open_price=9.8, close=9.9, pre_close=10.1),
            "return_5d": 0.01,
            "macd_dif": -0.03,
            "macd_dea": 0.0,
            "main_net_inflow_sum_3d": -200_000.0,
        },
    ]
    strategy = StrategyConfig(
        name="exit-rich-rules",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="cap",
                        condition_id="market_cap_between",
                        params={"min": 1_000_000_000, "max": 10_000_000_000},
                    )
                ],
            )
        ],
        exit_rules=[
            ConditionNode(id="weak-return", condition_id="past_return_at_most", params={"window": 5, "max": 0.03}),
            ConditionNode(id="dead-cross", condition_id="macd_dead_cross", params={}),
            ConditionNode(id="outflow", condition_id="capital_flow_n_day_sum_at_most", params={"window": 3, "max": 0}),
        ],
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-05").date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        take_profit_pct=None,
        stop_loss_pct=None,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        conservative_execution=False,
    )

    result = run_backtest(pd.DataFrame(rows), strategy, settings)

    trade = result.trades[0]
    assert trade.sell_signal_date.isoformat() == "2024-01-04"
    assert trade.sell_date.isoformat() == "2024-01-05"
    assert any("5d return" in reason for reason in trade.sell_reason)
    assert any("MACD dead cross" in reason for reason in trade.sell_reason)
    assert any("main net inflow" in reason for reason in trade.sell_reason)


def test_preflight_reports_missing_capital_flow_when_required(basic_strategy, basic_settings):
    data = enriched_data().drop(columns=["main_net_inflow"])

    result = run_backtest(data, basic_strategy, basic_settings)

    assert any(issue.dataset == "capital_flow" and issue.severity == "error" for issue in result.preflight_issues)
    assert result.trades == []


def test_preflight_reports_empty_capital_flow_values_when_required(basic_strategy, basic_settings):
    data = enriched_data()
    data["main_net_inflow"] = float("nan")

    result = run_backtest(data, basic_strategy, basic_settings)

    assert any(issue.code == "empty_capital_flow" and issue.severity == "error" for issue in result.preflight_issues)
    assert result.trades == []


def test_preflight_reports_empty_capital_flow_for_positive_count_strategy(basic_settings):
    data = enriched_data()
    data["main_net_inflow"] = float("nan")
    strategy = StrategyConfig(
        name="flow-positive-days",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="flow-days",
                        condition_id="capital_flow_n_day_positive_count_at_least",
                        params={"window": 3, "min_count": 2},
                    )
                ],
            )
        ],
        exit_rules=[],
    )

    result = run_backtest(data, strategy, basic_settings)

    assert any(issue.code == "empty_capital_flow" and issue.severity == "error" for issue in result.preflight_issues)
    assert result.trades == []


def test_backtest_filters_by_today_and_positive_count_capital_flow_conditions():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    rows = []
    for symbol, flows in {
        "AAA": [1_000_000, -1_000_000, 2_000_000, 3_000_000],
        "BBB": [1_000_000, -1_000_000, -2_000_000, 4_000_000],
    }.items():
        for trade_date, flow in zip(dates, flows, strict=True):
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "open": 10.0,
                    "high": 10.3,
                    "low": 9.8,
                    "close": 10.0,
                    "volume": 1000,
                    "is_suspended": False,
                    "listing_days": 500,
                    "float_market_cap": 2_000_000_000,
                    "main_net_inflow": flow,
                    "market_rising_ratio": 1.0,
                    "main_net_inflow_positive_count_3d": (
                        float("nan")
                        if trade_date < pd.Timestamp("2024-01-04")
                        else sum(item > 0 for item in flows[: dates.get_loc(trade_date) + 1][-3:])
                    ),
                }
            )
    frame = pd.DataFrame(rows)
    strategy = StrategyConfig(
        name="flow-prefilter",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(id="flow-today", condition_id="capital_flow_today_at_least", params={"min": 2_500_000}),
                    ConditionNode(
                        id="flow-days",
                        condition_id="capital_flow_n_day_positive_count_at_least",
                        params={"window": 3, "min_count": 2},
                    ),
                ],
            )
        ],
        exit_rules=[],
    )
    settings = BacktestSettings(
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        initial_cash=100_000,
        fixed_holding_days=3,
        max_positions=2,
        max_daily_buys=2,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
    )

    result = run_backtest(frame, strategy, settings)

    assert result.latest_strategy_matches is not None
    assert [match.symbol for match in result.latest_strategy_matches.matches] == ["AAA"]
    assert any("positive main net inflow days" in reason for reason in result.latest_strategy_matches.matches[0].reasons)


def test_market_cap_strategy_requires_non_empty_market_cap(basic_strategy, basic_settings):
    frame = pd.DataFrame(
        {
            "symbol": ["AAA"],
            "trade_date": pd.to_datetime(["2024-01-02"]),
            "open": [10.0],
            "high": [10.5],
            "low": [9.8],
            "close": [10.2],
            "volume": [1000],
            "is_suspended": [False],
            "listing_days": [100],
            "float_market_cap": [float("nan")],
            "main_net_inflow": [1000000.0],
        }
    )

    result = run_backtest(frame, basic_strategy, basic_settings)

    assert any(issue.code == "empty_market_cap" for issue in result.preflight_issues)


def test_backtest_filters_to_custom_stock_pool(basic_strategy, basic_settings):
    basic_settings.stock_pool = "custom"
    basic_settings.custom_symbols = ["BBB"]

    result = run_backtest(enriched_data(), basic_strategy, basic_settings)

    assert result.trades == []
    assert len(result.equity_curve) == 5


def test_backtest_excludes_st_symbols_when_enabled(basic_strategy, basic_settings):
    data = enriched_data()
    data.loc[data["symbol"] == "AAA", "is_st"] = True

    result = run_backtest(data, basic_strategy, basic_settings)

    assert result.trades == []


def test_backtest_candidate_selection_is_not_biased_to_low_symbol_codes():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"])
    rows = []
    for symbol, close, volume, market_cap in [
        ("000001", 10.0, 1000, 2_000_000_000),
        ("300001", 11.0, 9000, 9_000_000_000),
        ("600001", 12.0, 5000, 5_000_000_000),
    ]:
        for date in dates:
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": date,
                    "open": close,
                    "high": close * 1.01,
                    "low": close * 0.99,
                    "close": close,
                    "volume": volume,
                    "is_suspended": False,
                    "listing_days": 500,
                    "float_market_cap": market_cap,
                    "main_net_inflow": 0.0,
                    "market_rising_ratio": 1.0,
                    "volume_ratio_2d": 1.2,
                    "ma_3": close,
                }
            )
    frame = pd.DataFrame(rows)
    strategy = StrategyConfig(
        name="ranking",
        market_filters=[
            ConditionNode(id="market", condition_id="market_rising_ratio_at_least", params={"min_ratio": 0.5})
        ],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="cap",
                        condition_id="market_cap_between",
                        params={"min": 1_000_000_000, "max": 10_000_000_000},
                    ),
                    ConditionNode(
                        id="volume",
                        condition_id="volume_ratio_between",
                        params={"window": 2, "min": 1.0, "max": 2.0},
                    ),
                ],
            )
        ],
        exit_rules=[ConditionNode(id="exit", condition_id="close_below_ma", params={"window": 3})],
    )
    settings = BacktestSettings(
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        initial_cash=100_000,
        fixed_holding_days=2,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
    )

    result = run_backtest(frame, strategy, settings)

    assert result.trades
    assert result.trades[0].symbol == "300001"


def test_backtest_candidate_selection_prefers_balanced_internal_score():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    rows = []
    for symbol, name, volume_ratio, return_2d, turnover_rate, volume, main_net_inflow, market_cap in [
        ("600001", "Spike", 9.0, 0.01, 0.01, 1_000, 10_000, 1_000_000_000),
        ("600002", "Balanced", 3.0, 0.09, 0.09, 9_000, 90_000, 5_000_000_000),
    ]:
        for date in dates:
            rows.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "trade_date": date,
                    "open": 10.0,
                    "high": 10.2,
                    "low": 9.8,
                    "close": 10.0,
                    "change_pct": return_2d,
                    "volume": volume,
                    "is_suspended": False,
                    "listing_days": 500,
                    "float_market_cap": market_cap,
                    "main_net_inflow": main_net_inflow,
                    "market_rising_ratio": 1.0,
                    "volume_ratio_2d": volume_ratio,
                    "return_2d": return_2d,
                    "turnover_rate": turnover_rate,
                    "ma_3": 10.0,
                }
            )
    strategy = StrategyConfig(
        name="balanced-ranking",
        market_filters=[
            ConditionNode(id="market", condition_id="market_rising_ratio_at_least", params={"min_ratio": 0.5})
        ],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="cap",
                        condition_id="market_cap_between",
                        params={"min": 500_000_000, "max": 10_000_000_000},
                    ),
                    ConditionNode(
                        id="volume",
                        condition_id="volume_ratio_between",
                        params={"window": 2, "min": 1.0, "max": 10.0},
                    ),
                ],
            )
        ],
        exit_rules=[],
    )
    settings = BacktestSettings(
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        initial_cash=100_000,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
    )

    result = run_backtest(pd.DataFrame(rows), strategy, settings)

    assert result.trades
    assert result.trades[0].symbol == "600002"
    assert result.latest_strategy_matches is not None
    assert [match.symbol for match in result.latest_strategy_matches.matches] == ["600002", "600001"]
    assert result.latest_strategy_matches.matches[0].rank_score > result.latest_strategy_matches.matches[1].rank_score


def test_backtest_continues_past_blocked_top_candidates_to_fill_daily_buys():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"])
    rows = []
    for symbol, volume_ratio, open_price, pre_close in [
        ("600001", 9.0, 11.0, 10.0),
        ("600002", 5.0, 10.0, 10.0),
    ]:
        for date in dates:
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": date,
                    "open": open_price if date == dates[1] else 10.0,
                    "high": 11.0 if symbol == "600001" and date == dates[1] else 10.2,
                    "low": 9.8,
                    "close": 10.0,
                    "pre_close": pre_close,
                    "volume": 5_000,
                    "is_suspended": False,
                    "listing_days": 500,
                    "float_market_cap": 2_000_000_000,
                    "main_net_inflow": 0.0,
                    "market_rising_ratio": 1.0,
                    "volume_ratio_2d": volume_ratio,
                    "return_2d": 0.02,
                    "turnover_rate": 0.02,
                    "ma_3": 10.0,
                }
            )
    strategy = StrategyConfig(
        name="fill-after-blocked",
        market_filters=[
            ConditionNode(id="market", condition_id="market_rising_ratio_at_least", params={"min_ratio": 0.5})
        ],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="cap",
                        condition_id="market_cap_between",
                        params={"min": 1_000_000_000, "max": 3_000_000_000},
                    )
                ],
            )
        ],
        exit_rules=[],
    )
    settings = BacktestSettings(
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        initial_cash=100_000,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        limit_up_blocks_buy=True,
    )
    events = []

    result = run_backtest(pd.DataFrame(rows), strategy, settings, on_event=lambda event: events.append(event))

    assert [trade.symbol for trade in result.trades if trade.shares > 0] == ["600002"]
    blocked = [event["trade"] for event in events if event["type"] == "trade_blocked"]
    assert [trade.symbol for trade in blocked] == ["600001"]


def test_backtest_emits_progress_and_open_trade_events_before_close(basic_strategy, basic_settings):
    events = []

    result = run_backtest(
        enriched_data(),
        basic_strategy,
        basic_settings,
        on_event=lambda event: events.append(event),
    )

    assert result.trades
    assert any(event["type"] == "progress" for event in events)
    opened_index = next(index for index, event in enumerate(events) if event["type"] == "trade_opened")
    closed_index = next(index for index, event in enumerate(events) if event["type"] == "trade_closed")
    assert opened_index < closed_index
    assert events[opened_index]["trade"].symbol == "AAA"
    assert events[closed_index]["trade"].symbol == "AAA"


def test_backtest_never_sells_on_the_buy_date_under_t_plus_one(basic_strategy, basic_settings):
    settings = basic_settings.model_copy(
        update={
            "fixed_holding_days": 1,
            "take_profit_pct": 0.001,
            "stop_loss_pct": -0.001,
        }
    )

    result = run_backtest(enriched_data(), basic_strategy, settings)

    assert result.trades
    same_day_exits = [
        trade for trade in result.trades
        if trade.sell_date is not None and trade.sell_date <= trade.buy_date
    ]
    assert same_day_exits == []


def test_backtest_applies_single_position_ratio_and_board_lot_rounding(basic_strategy, basic_settings):
    settings = basic_settings.model_copy(
        update={
            "position_sizing_mode": "fixed_ratio",
            "position_size_pct": 0.15,
            "slippage_rate": 0,
            "fee_rate": 0,
            "stamp_tax_rate": 0,
        }
    )

    result = run_backtest(enriched_data(), basic_strategy, settings)

    assert result.trades
    trade = result.trades[0]
    assert trade.shares == 1200
    assert trade.planned_amount == 15000
    assert trade.buy_amount == 14400
    assert trade.target_position_pct == 0.15
    assert trade.actual_position_pct == 0.144


def test_exit_rule_can_sell_when_price_breaks_prior_low():
    dates = pd.to_datetime(
        [
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
            "2024-01-08",
            "2024-01-09",
            "2024-01-10",
        ]
    )
    rows = []
    closes = [10.0, 10.1, 10.3, 10.4, 10.5, 9.4, 9.1]
    lows = [9.8, 9.9, 10.0, 10.2, 10.3, 9.2, 9.0]
    for date, close, low in zip(dates, closes, lows, strict=True):
        rows.append(
            {
                "symbol": "AAA",
                "trade_date": date,
                "open": close,
                "high": close + 0.3,
                "low": low,
                "close": close,
                "volume": 1000,
                "is_suspended": False,
                "listing_days": 500,
                "float_market_cap": 5_000_000_000,
                "main_net_inflow": 1_000_000,
                "market_rising_ratio": 1.0,
                "ma_3": close - 0.1,
            }
        )
    frame = pd.DataFrame(rows)
    strategy = StrategyConfig(
        name="prior-low-exit",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="cap",
                        condition_id="market_cap_between",
                        params={"min": 1_000_000_000, "max": 10_000_000_000},
                    )
                ],
            )
        ],
        exit_rules=[
            ConditionNode(
                id="exit-low",
                condition_id="breakdown_below_n_day_low",
                params={"window": 3},
                expression="突破3日最低",
            )
        ],
    )
    settings = BacktestSettings(
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        take_profit_pct=None,
        stop_loss_pct=None,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
    )

    result = run_backtest(frame, strategy, settings)

    assert result.trades
    trade = result.trades[0]
    assert str(trade.buy_date) == "2024-01-03"
    assert str(trade.sell_signal_date) == "2024-01-09"
    assert str(trade.sell_date) == "2024-01-10"
    assert any("prior 3d low" in reason for reason in trade.sell_reason)


def test_extreme_chasing_strategy_can_surface_large_loss():
    dates = pd.to_datetime(
        [
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
            "2024-01-08",
        ]
    )
    rows = []
    opens = [10.0, 10.5, 12.0, 12.5, 7.0]
    closes = [10.0, 10.5, 12.0, 8.0, 7.5]
    volumes = [1000, 1200, 6000, 7000, 6500]
    for date, open_price, close, volume in zip(dates, opens, closes, volumes, strict=True):
        rows.append(
            {
                "symbol": "300999",
                "trade_date": date,
                "open": open_price,
                "high": max(open_price, close) * 1.02,
                "low": min(open_price, close) * 0.98,
                "close": close,
                "volume": volume,
                "is_suspended": False,
                "listing_days": 500,
                "float_market_cap": 2_000_000_000,
                "main_net_inflow": 0.0,
                "market_rising_ratio": 1.0,
                "ma_3": close,
                "volume_ratio_2d": 2.8,
                "return_2d": 0.2,
            }
        )
    frame = pd.DataFrame(rows)
    strategy = StrategyConfig(
        name="极端追高压力测试",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(id="breakout", condition_id="breakout_above_n_day_high", params={"window": 2}),
                    ConditionNode(id="volume", condition_id="volume_ratio_between", params={"window": 2, "min": 2.0, "max": 5.0}),
                ],
            )
        ],
        exit_rules=[],
    )
    settings = BacktestSettings(
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        initial_cash=100_000,
        fixed_holding_days=2,
        take_profit_pct=None,
        stop_loss_pct=None,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
    )

    result = run_backtest(frame, strategy, settings)

    assert result.trades
    assert result.metrics.total_return_pct < -0.08
    assert result.trades[0].pnl_pct is not None
    assert result.trades[0].pnl_pct < -0.3


def test_backtest_uses_precomputed_breakout_and_breakdown_columns():
    dates = pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-08", "2024-01-09"])
    frame = pd.DataFrame(
        {
            "symbol": ["AAA"] * 6,
            "trade_date": dates,
            "open": [10.0, 10.2, 10.8, 10.0, 9.7, 9.0],
            "high": [10.2, 10.4, 11.0, 10.1, 9.9, 9.2],
            "low": [9.8, 10.0, 10.6, 9.5, 9.2, 8.8],
            "close": [10.0, 10.2, 10.9, 10.0, 9.4, 9.1],
            "volume": [1000, 1200, 1400, 1300, 1200, 1100],
            "is_suspended": [False] * 6,
            "listing_days": [500] * 6,
            "float_market_cap": [2_000_000_000] * 6,
            "main_net_inflow": [0.0] * 6,
            "market_rising_ratio": [1.0] * 6,
            "prior_high_2d": [float("nan"), float("nan"), 10.4, 11.0, 11.0, 10.1],
            "prior_low_2d": [float("nan"), float("nan"), 9.8, 10.0, 9.5, 9.2],
            "volume_ratio_2d": [float("nan"), float("nan"), 1.27, 1.0, 0.92, 0.9],
        }
    )
    strategy = StrategyConfig(
        name="precomputed",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(id="breakout", condition_id="breakout_above_n_day_high", params={"window": 2}),
                    ConditionNode(id="volume", condition_id="volume_ratio_between", params={"window": 2, "min": 1.0, "max": 2.0}),
                ],
            )
        ],
        exit_rules=[ConditionNode(id="exit-low", condition_id="breakdown_below_n_day_low", params={"window": 2})],
    )
    settings = BacktestSettings(
        start_date=dates[0].date(),
        end_date=dates[-1].date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        take_profit_pct=None,
        stop_loss_pct=None,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
    )

    result = run_backtest(frame, strategy, settings)

    assert result.trades
    trade = result.trades[0]
    assert str(trade.buy_signal_date) == "2024-01-04"
    assert str(trade.buy_date) == "2024-01-05"
    assert any("prior 2d high" in reason for reason in trade.buy_reason)
    assert str(trade.sell_signal_date) == "2024-01-08"
    assert str(trade.sell_date) == "2024-01-09"
    assert any("prior 2d low" in reason for reason in trade.sell_reason)


def test_annualized_return_uses_equity_curve_date_span_not_total_return():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=10.0, close=10.0),
            backtest_row("2024-01-22", open_price=12.0, close=12.0),
        ]
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-22").date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings)

    assert result.metrics.total_return_pct == pytest.approx(0.04)
    assert result.metrics.annualized_return_pct == pytest.approx((1.04 ** (365 / 20)) - 1)
    assert result.metrics.annualized_return_pct != result.metrics.total_return_pct


def test_limit_up_blocks_next_day_buy_and_records_chinese_reason():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=11.0, high=11.0, low=11.0, close=11.0, pre_close=10.0),
            backtest_row("2024-01-04", open_price=12.0, close=12.0, pre_close=11.0),
        ]
    )
    events = []
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-03").date(),
        initial_cash=100_000,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        limit_up_blocks_buy=True,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings, on_event=lambda event: events.append(event))

    assert result.trades == []
    blocked_events = [event for event in events if event["type"] == "trade_blocked"]
    assert blocked_events
    blocked_trade = blocked_events[0]["trade"]
    assert blocked_trade.blocked_reason == "次日开盘接近涨停，未买入：000001"
    assert blocked_trade.shares == 0
    assert blocked_trade.buy_amount == 0
    assert blocked_trade.pnl_pct is None
    assert result.metrics.trade_count == 0


def test_missing_pre_close_does_not_guess_limit_up_block():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=99.0, high=99.0, low=99.0, close=99.0),
            backtest_row("2024-01-04", open_price=99.0, close=99.0),
            backtest_row("2024-01-05", open_price=99.0, close=99.0),
        ]
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-04").date(),
        initial_cash=100_000,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        limit_up_blocks_buy=True,
        fixed_holding_days=1,
        exclude_st=False,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings)

    assert result.trades
    assert result.trades[0].buy_price == 99.0


def test_limit_down_blocks_sell_keeps_position_and_reports_reason():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=10.0, close=10.0, pre_close=10.0),
            backtest_row("2024-01-04", open_price=9.0, high=9.0, low=9.0, close=9.0, pre_close=10.0),
            backtest_row("2024-01-05", open_price=9.5, close=9.5, pre_close=9.0),
        ]
    )
    events = []
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-05").date(),
        initial_cash=100_000,
        fixed_holding_days=1,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        limit_down_blocks_sell=True,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings, on_event=lambda event: events.append(event))

    assert result.trades
    trade = result.trades[0]
    assert str(trade.sell_date) == "2024-01-05"
    assert trade.sell_price == 9.5
    blocked_events = [event for event in events if event["type"] == "trade_blocked"]
    assert blocked_events
    assert blocked_events[0]["trade"].blocked_reason == "卖出日开盘接近跌停，暂不卖出：000001"


@pytest.mark.parametrize(
    ("symbol", "is_st", "open_price", "pre_close", "should_block"),
    [
        ("600001", False, 9.0, 10.0, True),
        ("000001", True, 9.5, 10.0, True),
        ("300001", False, 9.0, 10.0, False),
        ("300001", False, 8.0, 10.0, True),
        ("688001", False, 8.0, 10.0, True),
    ],
)
def test_limit_down_block_uses_board_specific_thresholds(symbol, is_st, open_price, pre_close, should_block):
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", symbol=symbol, close=10.0, is_st=is_st),
            backtest_row("2024-01-03", symbol=symbol, open_price=10.0, close=10.0, pre_close=10.0, is_st=is_st),
            backtest_row(
                "2024-01-04",
                symbol=symbol,
                open_price=open_price,
                high=max(open_price, 9.8),
                low=open_price,
                close=open_price,
                pre_close=pre_close,
                is_st=is_st,
            ),
            backtest_row("2024-01-05", symbol=symbol, open_price=9.8, close=9.8, pre_close=open_price, is_st=is_st),
        ]
    )
    events = []
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-05").date(),
        initial_cash=100_000,
        fixed_holding_days=1,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        limit_down_blocks_sell=True,
        exclude_st=False,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings, on_event=lambda event: events.append(event))
    blocked_events = [event for event in events if event["type"] == "trade_blocked"]

    assert bool(blocked_events) is should_block
    assert bool(result.trades) is True
    if should_block:
        assert result.trades[0].sell_date.isoformat() == "2024-01-05"
    else:
        assert result.trades[0].sell_date.isoformat() == "2024-01-04"


@pytest.mark.parametrize(
    ("symbol", "is_st", "expected"),
    [
        # 主板 10%，ST 在主板基础上降到 5%。
        ("600001", False, 0.10),
        ("000001", True, 0.05),
        # 创业板（含后来的 301/302 新股段）与科创板 20%：
        # 先判 ST 会把创业板/科创板 ST 股错按 5% 算。
        ("300001", False, 0.20),
        ("301001", False, 0.20),
        ("302001", False, 0.20),
        ("688001", False, 0.20),
        ("300001", True, 0.20),
        ("688001", True, 0.20),
        # 北交所 30%，且不适用 ST 降幅规则。
        ("920001", False, 0.30),
        ("830001", False, 0.30),
        ("920001", True, 0.30),
    ],
)
def test_stock_limit_pct_follows_board_before_st(symbol, is_st, expected):
    """涨跌停阈值必须先看板块再看 ST，并覆盖 301/302 与北交所。

    旧实现 ``if is_st: return 0.05`` 在板块判断之前，创业板/科创板 ST 股实际
    仍是 20% 却按 5% 判涨跌停，``limit_up_blocks_buy`` 会错误拦截；板块只认
    ``300/688``，301/302 创业板新股被按主板 10% 算；北交所 30% 完全缺失。
    """
    row = pd.Series({"symbol": symbol, "is_st": is_st, "pre_close": 10.0})

    assert engine._stock_limit_pct(row) == pytest.approx(expected)


def test_stock_pool_gem_includes_later_gem_segments():
    """创业板池必须含 301/302：只认 300 会把创业板新股排除在池外。"""
    frame = pd.DataFrame({"symbol": ["300001", "301001", "302001", "688001", "600001"]})
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-05").date(),
        initial_cash=100_000,
        stock_pool="gem",
    )

    mask = engine._stock_pool_mask(frame, settings)

    assert mask.tolist() == [True, True, True, False, False]


def test_conservative_execution_records_actual_buy_and_sell_prices_and_amounts():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=10.0, high=11.0, low=9.0, close=10.5, pre_close=10.0),
            backtest_row("2024-01-04", open_price=12.0, high=12.5, low=11.5, close=12.0, pre_close=10.5),
        ]
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-04").date(),
        initial_cash=100_000,
        fixed_holding_days=1,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0.01,
        fee_rate=0.001,
        stamp_tax_rate=0.002,
        conservative_execution=True,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings)

    trade = result.trades[0]
    assert trade.buy_price == pytest.approx(10.1)
    assert trade.sell_price == pytest.approx(11.88)
    assert trade.buy_amount == pytest.approx(trade.buy_price * trade.shares * 1.001)
    assert trade.sell_amount == pytest.approx(trade.sell_price * trade.shares * (1 - 0.001 - 0.002))
    assert trade.pnl == pytest.approx(trade.sell_amount - trade.buy_amount)
    assert trade.pnl_pct == pytest.approx(trade.sell_amount / trade.buy_amount - 1)


def test_take_profit_triggers_exit_at_threshold_price_when_open_is_below_threshold():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=10.0, high=10.1, low=9.9, close=10.0, pre_close=10.0),
            backtest_row("2024-01-04", open_price=10.1, high=11.0, low=10.0, close=10.5, pre_close=10.0),
        ]
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-04").date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        take_profit_pct=0.08,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        conservative_execution=False,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings)

    trade = result.trades[0]
    assert trade.sell_price == pytest.approx(10.8)
    assert any("止盈触发" in reason for reason in trade.sell_reason)


def test_take_profit_uses_open_price_when_open_gaps_beyond_threshold():
    rows = [
        backtest_row("2024-01-02", symbol="AAA", close=10.0),
        backtest_row("2024-01-02", symbol="BBB", close=20.0),
        backtest_row("2024-01-03", symbol="AAA", open_price=10.0, high=10.2, low=9.9, close=10.0, pre_close=10.0),
        backtest_row("2024-01-03", symbol="BBB", open_price=20.0, high=20.3, low=19.8, close=20.0, pre_close=20.0),
        backtest_row("2024-01-04", symbol="AAA", open_price=10.9, high=11.0, low=10.8, close=10.9, pre_close=10.0),
        backtest_row("2024-01-04", symbol="BBB", open_price=23.0, high=24.0, low=22.8, close=23.5, pre_close=20.0),
    ]
    frame = pd.DataFrame(rows)
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-04").date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        take_profit_pct=0.08,
        max_positions=2,
        max_daily_buys=2,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        conservative_execution=False,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings)

    trades_by_symbol = {trade.symbol: trade for trade in result.trades}
    assert trades_by_symbol["AAA"].sell_price == pytest.approx(10.9)
    assert trades_by_symbol["BBB"].sell_price == pytest.approx(23.0)
    assert trades_by_symbol["AAA"].pnl_pct == pytest.approx(0.09)
    assert trades_by_symbol["BBB"].pnl_pct == pytest.approx(0.15)
    assert len({round(trade.pnl_pct or 0, 4) for trade in result.trades}) == 2


def test_stop_loss_triggers_exit_at_threshold_price_when_open_is_above_threshold():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=10.0, high=10.1, low=9.9, close=10.0, pre_close=10.0),
            backtest_row("2024-01-04", open_price=9.9, high=10.0, low=9.2, close=9.5, pre_close=10.0),
        ]
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-04").date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        stop_loss_pct=-0.06,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        conservative_execution=False,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings)

    trade = result.trades[0]
    assert trade.sell_price == pytest.approx(9.4)
    assert any("止损触发" in reason for reason in trade.sell_reason)


def test_close_based_exit_sells_next_open_after_signal_day():
    frame = pd.DataFrame(
        [
            {**backtest_row("2024-01-02", close=10.0), "ma_3": 9.0},
            {**backtest_row("2024-01-03", open_price=10.0, close=10.0, pre_close=10.0), "ma_3": 9.0},
            {**backtest_row("2024-01-04", open_price=9.0, close=8.0, pre_close=10.0), "ma_3": 8.5},
            {**backtest_row("2024-01-05", open_price=7.5, close=7.6, pre_close=8.0), "ma_3": 8.0},
        ]
    )
    strategy = StrategyConfig(
        name="close-exit-next-open",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="cap",
                        condition_id="market_cap_between",
                        params={"min": 1_000_000_000, "max": 10_000_000_000},
                    )
                ],
            )
        ],
        exit_rules=[ConditionNode(id="exit-ma", condition_id="close_below_ma", params={"window": 3})],
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-05").date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        take_profit_pct=None,
        stop_loss_pct=None,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        conservative_execution=False,
    )

    result = run_backtest(frame, strategy, settings)

    trade = result.trades[0]
    assert trade.sell_signal_date.isoformat() == "2024-01-04"
    assert trade.sell_date.isoformat() == "2024-01-05"
    assert trade.sell_price == pytest.approx(7.5)


def test_suspended_buy_day_blocks_entry():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=10.0, close=10.0, is_suspended=True),
            backtest_row("2024-01-04", open_price=10.5, close=10.5),
        ]
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-04").date(),
        initial_cash=100_000,
        fixed_holding_days=1,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
    )
    events = []

    result = run_backtest(frame, simple_market_cap_strategy(), settings, on_event=lambda event: events.append(event))

    assert result.trades == []
    assert not any(event["type"] == "trade_opened" for event in events)
    blocked = [event for event in events if event["type"] == "trade_blocked"]
    assert blocked[0]["trade"].blocked_reason == "买入日停牌，未买入：000001"


def test_suspended_sell_day_keeps_position_until_next_tradable_open():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=10.0, close=10.0),
            backtest_row("2024-01-04", open_price=9.0, close=9.0, is_suspended=True),
            backtest_row("2024-01-05", open_price=8.5, close=8.5),
        ]
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-05").date(),
        initial_cash=100_000,
        fixed_holding_days=1,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        conservative_execution=False,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings)

    trade = result.trades[0]
    assert trade.sell_date.isoformat() == "2024-01-05"
    assert trade.sell_price == pytest.approx(8.5)


def test_missing_holding_quote_uses_last_close_for_equity_curve():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", symbol="AAA", close=10.0),
            backtest_row("2024-01-03", symbol="AAA", open_price=10.0, close=11.0),
            backtest_row("2024-01-04", symbol="BBB", open_price=20.0, close=20.0),
        ]
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-04").date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        conservative_execution=False,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings)

    jan4 = next(point for point in result.equity_curve if point.trade_date.isoformat() == "2024-01-04")
    assert jan4.market_value == pytest.approx(22_000)
    assert jan4.equity == pytest.approx(102_000)


def test_condition_data_lag_days_uses_prior_symbol_row():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=10.0, close=10.0),
            backtest_row("2024-01-04", open_price=10.0, close=10.0),
            backtest_row("2024-01-05", open_price=10.0, close=10.0),
        ]
    )
    frame.loc[frame["trade_date"] == pd.Timestamp("2024-01-02"), "main_net_inflow"] = 200.0
    frame.loc[frame["trade_date"] != pd.Timestamp("2024-01-02"), "main_net_inflow"] = 0.0
    strategy = StrategyConfig(
        name="lagged-flow",
        market_filters=[],
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="flow",
                        condition_id="capital_flow_today_at_least",
                        params={"min": 100},
                        data_lag_days=1,
                    )
                ],
            )
        ],
        exit_rules=[],
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-05").date(),
        initial_cash=100_000,
        fixed_holding_days=1,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        conservative_execution=False,
    )

    result = run_backtest(frame, strategy, settings)

    trade = result.trades[0]
    assert trade.buy_signal_date.isoformat() == "2024-01-03"
    assert trade.buy_date.isoformat() == "2024-01-04"


def test_result_includes_open_positions_marked_as_holding():
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", close=10.0),
            backtest_row("2024-01-03", open_price=10.0, close=11.0),
            backtest_row("2024-01-04", open_price=11.0, close=12.0),
        ]
    )
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-04").date(),
        initial_cash=100_000,
        fixed_holding_days=20,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        conservative_execution=False,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings)

    assert len(result.trades) == 1
    assert result.trades[0].sell_date is None
    assert result.trades[0].pnl_pct is None


@pytest.mark.parametrize(
    ("symbol", "is_st", "open_price", "pre_close", "should_block"),
    [
        ("600001", False, 11.0, 10.0, True),
        ("000001", True, 10.5, 10.0, True),
        ("300001", False, 11.0, 10.0, False),
        ("300001", False, 12.0, 10.0, True),
        ("688001", False, 12.0, 10.0, True),
    ],
)
def test_limit_up_block_uses_board_specific_thresholds(symbol, is_st, open_price, pre_close, should_block):
    frame = pd.DataFrame(
        [
            backtest_row("2024-01-02", symbol=symbol, close=10.0, is_st=is_st),
            backtest_row(
                "2024-01-03",
                symbol=symbol,
                open_price=open_price,
                high=open_price,
                low=open_price,
                close=open_price,
                pre_close=pre_close,
                is_st=is_st,
            ),
            backtest_row("2024-01-04", symbol=symbol, open_price=open_price, close=open_price, pre_close=open_price, is_st=is_st),
        ]
    )
    events = []
    settings = BacktestSettings(
        start_date=pd.Timestamp("2024-01-02").date(),
        end_date=pd.Timestamp("2024-01-03").date(),
        initial_cash=100_000,
        max_positions=1,
        max_daily_buys=1,
        min_listing_days=0,
        slippage_rate=0,
        fee_rate=0,
        stamp_tax_rate=0,
        limit_up_blocks_buy=True,
        fixed_holding_days=1,
        exclude_st=False,
    )

    result = run_backtest(frame, simple_market_cap_strategy(), settings, on_event=lambda event: events.append(event))
    blocked_events = [event for event in events if event["type"] == "trade_blocked"]

    assert bool(blocked_events) is should_block
    assert bool(result.trades) is (not should_block)
