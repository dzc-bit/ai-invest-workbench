"""Unit tests for the v1.5.0 AI light-route modules (no HTTP, no network)."""

from __future__ import annotations

from datetime import date

import pytest
from astock_backtester.ai.condition_dsl import _extract_json_object, parse_conditions_with_llm
from astock_backtester.ai.oneshot import compact_context
from astock_backtester.ai.optimizer import (
    GridTooLargeError,
    iter_grid,
    merge_settings,
    normalize_grid,
    run_optimization,
)
from astock_backtester.backtest_runner import enrich_for_strategy, run_configured_backtest, run_prepared_backtest
from astock_backtester.data.warehouse import lifecycle_bound
from astock_backtester.models import (
    BacktestSettings,
    ConditionGroup,
    ConditionNode,
    ConditionOperator,
    StrategyConfig,
)
from astock_backtester.sample_data import sample_daily_bars
from pydantic import ValidationError


class ScriptedModel:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, *, tools=None):
        self.calls += 1
        content = self.replies.pop(0) if self.replies else ""
        yield ("final", {"content": content, "tool_calls": None})

    def embed(self, texts):
        return [[0.0] for _ in texts]


def test_extract_json_object_handles_plain_fenced_and_embedded_payloads():
    assert _extract_json_object('{"entry_expressions": []}') == {"entry_expressions": []}
    assert _extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json_object('前置说明 {"a": {"b": 2}} 后置说明') == {"a": {"b": 2}}
    assert _extract_json_object("完全不是 JSON") is None
    assert _extract_json_object('{"a": 1') is None  # 截断的 JSON


def test_parse_conditions_reports_unsupported_indicator_as_dropped():
    import json

    broken = json.dumps(
        {"entry_expressions": ["KDJ金叉"], "exit_expressions": [], "approximations": []},
        ensure_ascii=False,
    )
    model = ScriptedModel([broken, broken, broken])

    result = parse_conditions_with_llm(model, "KDJ金叉买入")

    assert result["entry"] == []
    assert len(result["dropped"]) == 1
    assert result["dropped"][0]["kind"] == "entry"
    assert model.calls == 3  # 1 initial + 2 self-healing retries


def test_parse_conditions_accepts_valid_dsl_without_retry():
    import json

    model = ScriptedModel(
        [
            json.dumps(
                {
                    "entry_expressions": ["收盘价站上20日均线"],
                    "exit_expressions": ["MACD死叉"],
                    "approximations": [],
                },
                ensure_ascii=False,
            )
        ]
    )

    result = parse_conditions_with_llm(model, "站上20日线买，死叉卖")

    assert [node["condition_id"] for node in result["entry"]] == ["close_above_ma"]
    assert [node["condition_id"] for node in result["exit"]] == ["macd_dead_cross"]
    assert result["dropped"] == []
    assert model.calls == 1


def test_compact_context_truncates_and_scene_validation_rejects_unknown():
    from astock_backtester.ai.oneshot import ONESHOT_SCENES, insight_oneshot

    assert len(compact_context({"k": "x" * 5000})) <= 3000
    with pytest.raises(ValueError, match="未知点评场景"):
        insight_oneshot(ScriptedModel([]), "nope", {})
    assert set(ONESHOT_SCENES) == {"results_overview", "data_coverage", "risk_alerts"}


def test_normalize_grid_enforces_whitelist_bounds_and_finiteness():
    assert normalize_grid({"fixed_holding_days": [3, 5]}) == {"fixed_holding_days": [3, 5]}
    with pytest.raises(ValueError, match="不支持寻优的参数"):
        normalize_grid({"entry_window": [3, 5]})
    with pytest.raises(ValueError, match="非空数组"):
        normalize_grid({"fixed_holding_days": []})
    with pytest.raises(ValueError, match="有限数字"):
        normalize_grid({"max_positions": [float("nan")]})
    with pytest.raises(ValueError, match="有限数字"):
        normalize_grid({"max_positions": [float("inf")]})
    with pytest.raises(ValueError, match="必须是数字"):
        normalize_grid({"max_positions": ["3"]})
    with pytest.raises(ValueError, match="必须是数字"):
        normalize_grid({"max_positions": [True]})
    with pytest.raises(GridTooLargeError):
        normalize_grid({"fixed_holding_days": [1] * 7, "max_positions": [2] * 7})


def test_normalize_grid_keeps_integer_knobs_integral_and_ratios_fractional():
    """Integer knobs must not arrive as 3.0: ``model_copy`` used to accept the
    float, so the combination skipped every BacktestSettings constraint."""
    grid = normalize_grid({"fixed_holding_days": [3, 5], "position_size_pct": [20, 30]})

    assert [type(value) is int for value in grid["fixed_holding_days"]] == [True, True]
    assert grid["position_size_pct"] == [20.0, 30.0]
    # an integer knob given a fractional candidate keeps the fraction so the
    # combination is reported as rejected rather than silently rounded.
    assert normalize_grid({"max_positions": [2.5]}) == {"max_positions": [2.5]}


def test_iter_grid_yields_the_full_cartesian_product():
    combos = list(iter_grid({"a": [1, 2], "b": [3, 4]}))

    assert combos == [
        {"a": 1, "b": 3},
        {"a": 1, "b": 4},
        {"a": 2, "b": 3},
        {"a": 2, "b": 4},
    ]


def _optimize_settings():
    return BacktestSettings(
        start_date=date(2024, 1, 2),
        end_date=date(2024, 1, 8),
        initial_cash=1_000_000,
        max_positions=5,
        max_daily_buys=3,
        fixed_holding_days=2,
        min_listing_days=0,
    )


def _optimize_strategy():
    return StrategyConfig(
        name="寻优策略",
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[ConditionNode(id="c1", condition_id="close_above_ma", params={"window": 3})],
            )
        ],
    )


def _optimize_frame():
    return sample_daily_bars()


def _run_grid(grid):
    events: list[dict] = []
    summary = run_optimization(_optimize_frame(), _optimize_strategy(), _optimize_settings(), grid, events.append)
    return summary, events


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("max_positions", 0),
        ("position_size_pct", 1.5),
        ("fixed_holding_days", 2.5),
        ("stop_loss_pct", 0.1),
    ],
)
def test_optimize_rejects_combinations_the_settings_model_itself_rejects(key, value):
    """``model_copy(update=...)`` used to skip validation, so these four were
    evaluated as if they were legal parameters."""
    summary, events = _run_grid({key: [value]})

    assert summary["combinations"] == []
    assert summary["best"] is None
    assert summary["total"] == 1
    assert summary["evaluated"] == 0
    assert [event["type"] for event in events] == ["progress"]
    failure = summary["failures"][0]
    assert failure["params"] == {key: value}
    assert failure["code"] == "invalid_combination"
    assert key in failure["error"]
    # the grid refuses exactly what a hand-written request would be refused.
    with pytest.raises(ValidationError):
        merge_settings(_optimize_settings(), {key: value})


def test_optimize_keeps_illegal_combinations_out_of_the_ranking():
    summary, events = _run_grid({"max_positions": [0, 2]})

    assert summary["total"] == 2
    assert summary["evaluated"] == 1
    assert [combo["params"] for combo in summary["combinations"]] == [{"max_positions": 2}]
    assert len(summary["failures"]) == 1
    assert summary["best"] == summary["combinations"][0]
    assert [event["type"] for event in events] == ["progress", "combination", "progress"]


def test_optimize_legal_grid_metrics_match_per_combination_backtests():
    grid = {"fixed_holding_days": [1, 2, 3], "max_positions": [2, 3]}
    summary, _events = _run_grid(grid)

    expected = [
        run_configured_backtest(
            _optimize_frame(), _optimize_strategy(), merge_settings(_optimize_settings(), overrides)
        ).metrics.model_dump(mode="json")
        for overrides in iter_grid(grid)
    ]

    assert summary["failures"] == []
    assert summary["evaluated"] == 6
    assert [combo["metrics"] for combo in summary["combinations"]] == expected
    ranked = [combo for combo in summary["combinations"] if combo["metrics"]["trade_count"] > 0]
    assert summary["best"] in ranked
    assert summary["best"]["metrics"]["total_return_pct"] == max(
        combo["metrics"]["total_return_pct"] for combo in ranked
    )


def test_optimize_prepares_indicators_once_per_run(monkeypatch):
    import astock_backtester.ai.optimizer as optimizer_module

    real_enrich = optimizer_module.enrich_for_strategy
    calls: list[int] = []

    def counting_enrich(frame, strategy):
        calls.append(1)
        return real_enrich(frame, strategy)

    monkeypatch.setattr(optimizer_module, "enrich_for_strategy", counting_enrich)
    summary, _events = _run_grid({"fixed_holding_days": [1, 2, 3], "max_positions": [2, 3]})

    assert summary["evaluated"] == 6
    assert len(calls) == 1


def test_shared_prepared_frame_is_not_polluted_between_combinations():
    """Combinations share one enriched frame, so an earlier match must not be
    able to change what a later one sees."""
    frame, strategy, base = _optimize_frame(), _optimize_strategy(), _optimize_settings()
    prepared = enrich_for_strategy(frame, strategy)

    run_prepared_backtest(prepared, strategy, merge_settings(base, {"max_positions": 2}))
    shared = run_prepared_backtest(prepared, strategy, merge_settings(base, {"max_positions": 5}))
    fresh = run_configured_backtest(frame, strategy, merge_settings(base, {"max_positions": 5}))

    assert shared.model_dump() == fresh.model_dump()


def test_lifecycle_bound_parses_iso_and_ignores_garbage():
    import pandas as pd

    assert lifecycle_bound({"listing_date": "2024-01-03"}, "listing_date") == pd.Timestamp("2024-01-03")
    assert lifecycle_bound({"listing_date": "not-a-date"}, "listing_date") is None
    assert lifecycle_bound({"listing_date": None}, "listing_date") is None
    assert lifecycle_bound(None, "listing_date") is None
