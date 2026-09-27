from __future__ import annotations

from astock_backtester.ai.overfit import assess_overfit


def _combos(values, trades=None):
    rows = []
    for index, value in enumerate(values):
        metrics = {"total_return_pct": value}
        if trades is not None:
            metrics["trade_count"] = trades[index]
        rows.append({"metrics": metrics})
    return rows


def _codes(result):
    return {finding["code"] for finding in result["findings"]}


def test_few_trades_is_flagged():
    result = assess_overfit({"trade_count": 3, "total_return_pct": 0.2, "win_rate_pct": 1.0, "max_drawdown_pct": -0.01})
    codes = [finding["code"] for finding in result["findings"]]
    assert "few_trades" in codes
    assert result["level"] in ("warning", "critical")


def test_very_few_trades_is_critical():
    result = assess_overfit({"trade_count": 1, "total_return_pct": 0.05, "win_rate_pct": 1.0, "max_drawdown_pct": -0.01})
    assert result["level"] == "critical"


def test_perfect_win_rate_and_high_return_few_trades_flagged():
    metrics = {"trade_count": 8, "total_return_pct": 1.5, "win_rate_pct": 1.0, "max_drawdown_pct": -0.02}
    result = assess_overfit(metrics)
    codes = {finding["code"] for finding in result["findings"]}
    assert "perfect_win_rate" in codes
    assert "high_return_few_trades" in codes


def test_healthy_metrics_pass_clean():
    metrics = {"trade_count": 120, "total_return_pct": 0.18, "win_rate_pct": 0.54, "max_drawdown_pct": -0.12}
    result = assess_overfit(metrics)
    assert result["level"] == "none"
    assert result["findings"] == []


def test_grid_best_outlier_flagged():
    combos = [
        {"metrics": {"total_return_pct": value}}
        for value in (0.01, 0.02, 0.02, 0.03, 0.02, 0.01)
    ]
    combos.append({"metrics": {"total_return_pct": 0.30}})
    result = assess_overfit({"trade_count": 50}, combos=combos)
    codes = {finding["code"] for finding in result["findings"]}
    assert "grid_best_outlier" in codes


def test_grid_only_best_positive_flagged():
    combos = [{"metrics": {"total_return_pct": value}} for value in (-0.05, -0.02, -0.01, -0.03, 0.0, -0.04)]
    combos.append({"metrics": {"total_return_pct": 0.12}})
    result = assess_overfit({"trade_count": 50}, combos=combos)
    codes = {finding["code"] for finding in result["findings"]}
    assert "grid_only_best_positive" in codes


def test_grid_without_dispersion_passes():
    combos = [{"metrics": {"total_return_pct": value}} for value in (0.10, 0.11, 0.12, 0.11, 0.10, 0.12)]
    result = assess_overfit({"trade_count": 80}, combos=combos)
    assert result["level"] == "none"


def test_two_positive_combinations_are_not_reported_as_only_the_best_one():
    """The reproduced bad case: the median was negative, so the old rule claimed
    "only the best combination earns" while two of five actually did."""
    result = assess_overfit({"trade_count": 80}, combos=_combos([-0.30, -0.20, -0.10, 0.05, 0.10]))

    codes = _codes(result)
    assert "grid_only_best_positive" not in codes
    assert not any("只有最优组收益为正" in finding["message"] for finding in result["findings"])
    assert not any("其余均不赚钱" in finding["message"] for finding in result["findings"])


def test_several_profitable_combinations_are_counted_in_the_wording():
    result = assess_overfit({"trade_count": 80}, combos=_combos([0.02, 0.03, 0.05, 0.06, 0.40]))

    outlier = next(finding for finding in result["findings"] if finding["code"] == "grid_best_outlier")
    assert "5 组中有 5 组为正" in outlier["message"]
    assert outlier["level"] == "warning"


def test_single_positive_combination_reports_the_real_count():
    result = assess_overfit(
        {"trade_count": 80},
        combos=_combos([-0.30, -0.20, -0.10, -0.05, 0.01, -0.02, 0.0]),
    )

    finding = next(item for item in result["findings"] if item["code"] == "grid_only_best_positive")
    assert "只有 1 组收益为正" in finding["message"]
    assert "其余 6 组不赚钱" in finding["message"]
    assert result["level"] == "warning"


def test_all_losing_grid_states_it_is_not_an_overfit_signal():
    result = assess_overfit({"trade_count": 80}, combos=_combos([-0.05, -0.02, -0.01, -0.03, -0.04, -0.09]))

    assert "grid_no_positive_combination" in _codes(result)
    assert "grid_only_best_positive" not in _codes(result)
    assert result["level"] == "info"
    finding = next(item for item in result["findings"] if item["code"] == "grid_no_positive_combination")
    assert "6 组参数收益全部不为正" in finding["message"]


def test_thin_grid_is_labelled_as_small_sample():
    result = assess_overfit({"trade_count": 80}, combos=_combos([0.05, 0.11]))

    assert "grid_small_sample" in _codes(result)
    assert "grid_only_best_positive" not in _codes(result)
    assert result["level"] == "info"


def test_combinations_without_trades_are_not_read_as_results():
    combos = _combos([0.05, 0.06, 0.00, 0.07, 0.00], trades=[12, 9, 0, 11, 0])
    result = assess_overfit({"trade_count": 32}, combos=combos)

    finding = next(item for item in result["findings"] if item["code"] == "grid_combinations_without_trades")
    assert "5 组里有 2 组一笔成交都没有" in finding["message"]


def test_empty_grid_states_there_is_nothing_to_compare():
    result = assess_overfit({"trade_count": 80}, combos=[])

    assert _codes(result) == {"grid_no_usable_combinations"}
    assert result["level"] == "info"


def test_rejected_combinations_are_visible_in_the_dispersion_wording():
    result = assess_overfit({"trade_count": 80}, combos=_combos([0.05, 0.06, 0.07, 0.08, 0.09]), rejected=3)

    finding = next(item for item in result["findings"] if item["code"] == "grid_partial_failures")
    assert finding["level"] == "info"
    assert "3 个参数组合不合法" in finding["message"]
    assert "只覆盖剩下的组合" in finding["message"]


def test_absent_grid_is_not_treated_as_an_empty_one():
    assert assess_overfit({"trade_count": 80})["findings"] == []
