import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { defaultSettings, defaultStrategy } from "../strategyDefaults";
import type { OptimizeSummary } from "../types";
import { StrategyOptimizer } from "./StrategyOptimizer";

const aiApi = vi.hoisted(() => ({ runAiOptimizeStream: vi.fn(), aiOverfitCheck: vi.fn() }));

vi.mock("../aiApi", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...aiApi
}));

const baseUrl = "http://127.0.0.1:9000";

function metrics(totalReturnPct: number) {
  return {
    total_return_pct: totalReturnPct,
    annualized_return_pct: totalReturnPct * 2,
    max_drawdown_pct: -0.03,
    win_rate_pct: 0.5,
    trade_count: 8,
    average_trade_return_pct: 0.004,
    average_position_pct: 0.35,
    max_position_pct: 0.5
  };
}

/** One evaluated combination plus one rejected for an illegal max_positions. */
function summaryWithFailures(): OptimizeSummary {
  const combinations = [
    { index: 2, params: { fixed_holding_days: 5, max_positions: 4 }, metrics: metrics(0.12) }
  ];
  return {
    combinations,
    best: combinations[0],
    failures: [
      {
        params: { fixed_holding_days: 5, max_positions: 0 },
        code: "invalid_combination",
        error: "max_positions: Value error, max_positions must be >= 1"
      }
    ],
    total: 2,
    evaluated: 1
  };
}

function emptySummary(): OptimizeSummary {
  return { combinations: [], best: null, failures: [], total: 0, evaluated: 0 };
}

/** Replay a real optimize stream: combination events first, then the result. */
function stubStream(summary: OptimizeSummary) {
  aiApi.runAiOptimizeStream.mockImplementation(
    async (
      _baseUrl: string,
      _request: unknown,
      handlers: {
        onCombination?: (c: OptimizeSummary["combinations"][number]) => void;
        onResult?: (s: OptimizeSummary) => void;
      }
    ) => {
      for (const combination of summary.combinations) {
        handlers.onCombination?.(combination);
      }
      handlers.onResult?.(summary);
    }
  );
}

function renderOptimizer() {
  return render(<StrategyOptimizer strategy={defaultStrategy} settings={defaultSettings} baseUrl={baseUrl} />);
}

async function clickRun() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "开始 AI 参数寻优" }));
}

beforeEach(() => {
  aiApi.runAiOptimizeStream.mockReset();
  aiApi.aiOverfitCheck.mockReset();
  aiApi.aiOverfitCheck.mockResolvedValue({ level: "none", findings: [] });
});

describe("StrategyOptimizer grid input", () => {
  it("sends the parsed grid with percent-scaled values divided by 100", async () => {
    stubStream(emptySummary());
    renderOptimizer();
    await clickRun();

    await waitFor(() => expect(aiApi.runAiOptimizeStream).toHaveBeenCalled());
    expect(aiApi.runAiOptimizeStream.mock.calls[0][1].grid).toEqual({
      fixed_holding_days: [3, 5, 8],
      take_profit_pct: [0.05, 0.08, 0.12]
    });
  });

  it("treats an empty candidate cell as an error instead of an implicit 0", async () => {
    renderOptimizer();
    const user = userEvent.setup();
    await user.clear(screen.getByLabelText("参数候选值 1"));
    await user.click(screen.getByRole("button", { name: "开始 AI 参数寻优" }));

    expect(await screen.findByText("参数“固定持仓天数”的候选值为空，请填写逗号分隔的数字。")).toBeInTheDocument();
    expect(aiApi.runAiOptimizeStream).not.toHaveBeenCalled();
  });

  it("names non-numeric tokens rather than dropping them silently", async () => {
    renderOptimizer();
    const user = userEvent.setup();
    await user.clear(screen.getByLabelText("参数候选值 1"));
    await user.type(screen.getByLabelText("参数候选值 1"), "3,abc,5");
    await user.click(screen.getByRole("button", { name: "开始 AI 参数寻优" }));

    expect(await screen.findByText(/不是数字的项：abc/)).toBeInTheDocument();
    expect(aiApi.runAiOptimizeStream).not.toHaveBeenCalled();
  });

  it("rejects two rows on the same parameter instead of letting the last one win", async () => {
    renderOptimizer();
    const user = userEvent.setup();
    await user.selectOptions(screen.getByLabelText("寻优参数 2"), "fixed_holding_days");
    await user.click(screen.getByRole("button", { name: "开始 AI 参数寻优" }));

    expect(await screen.findByText(/参数“固定持仓天数”重复登记了多行/)).toBeInTheDocument();
    expect(aiApi.runAiOptimizeStream).not.toHaveBeenCalled();
  });
});

describe("StrategyOptimizer rejected combinations", () => {
  it("shows rejected combinations outside the ranking table", async () => {
    const summary = summaryWithFailures();
    stubStream(summary);
    renderOptimizer();
    await clickRun();

    const rejected = await screen.findByText(/已拒绝的组合/);
    expect(rejected).toHaveTextContent("固定持仓天数 5 / 最大持仓数 0");
    expect(rejected).toHaveTextContent("max_positions must be >= 1");
    const table = screen.getByRole("table");
    expect(table).toHaveTextContent("固定持仓天数 5 / 最大持仓数 4");
    expect(table).not.toHaveTextContent("最大持仓数 0");
  });
});

describe("StrategyOptimizer overfit wiring", () => {
  it("feeds the real combinations and rejected count into the deterministic check", async () => {
    const summary = summaryWithFailures();
    stubStream(summary);
    aiApi.aiOverfitCheck.mockResolvedValue({
      level: "warning",
      findings: [
        { level: "warning", code: "grid_partial_failures", message: "网格里有 1 个参数组合不合法、已被剔除。" }
      ]
    });
    renderOptimizer();
    await clickRun();

    await waitFor(() => expect(aiApi.aiOverfitCheck).toHaveBeenCalled());
    const payload = aiApi.aiOverfitCheck.mock.calls[0][1];
    expect(payload.metrics).toEqual(summary.best?.metrics);
    expect(payload.combos).toEqual(summary.combinations);
    expect(payload.rejected_combinations).toBe(1);
    expect(await screen.findByText(/网格里有 1 个参数组合不合法/)).toBeInTheDocument();
  });

  it("keeps the grid result usable when the check is unavailable", async () => {
    const summary = summaryWithFailures();
    stubStream(summary);
    aiApi.aiOverfitCheck.mockRejectedValue(new Error("boom"));
    renderOptimizer();
    await clickRun();

    await waitFor(() => expect(aiApi.aiOverfitCheck).toHaveBeenCalled());
    expect(screen.queryByText(/过拟合/)).not.toBeInTheDocument();
    expect(screen.getByText("固定持仓天数 5 / 最大持仓数 4")).toBeInTheDocument();
  });
});
