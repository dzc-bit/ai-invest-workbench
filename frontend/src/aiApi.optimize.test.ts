import { describe, expect, it, vi } from "vitest";
import type { OptimizeSummary } from "./types";
import { defaultSettings, defaultStrategy } from "./strategyDefaults";

// Mock only the transport: the real aiApi dispatch layer must unwrap the
// nested NDJSON event envelope before invoking handlers.
const consumeNdjsonStream = vi.hoisted(() => vi.fn());

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  consumeNdjsonStream
}));

vi.mock("./tauriRuntime", () => ({
  isTauriRuntime: () => true
}));

const { runAiOptimizeStream } = await import("./aiApi");

describe("runAiOptimizeStream dispatch", () => {
  it("unwraps the nested result event and forwards combination payloads", async () => {
    const summary: OptimizeSummary = {
      combinations: [
        {
          index: 1,
          params: { fixed_holding_days: 3 },
          metrics: {
            total_return_pct: 0.05,
            annualized_return_pct: 0.1,
            max_drawdown_pct: -0.03,
            win_rate_pct: 0.5,
            trade_count: 8,
            average_trade_return_pct: 0.004,
            average_position_pct: 0.35,
            max_position_pct: 0.5
          }
        }
      ],
      best: null,
      failures: [],
      total: 1,
      evaluated: 1,
      insight: "样本过少，注意过拟合。",
      insight_error: null
    };
    summary.best = summary.combinations[0];
    consumeNdjsonStream.mockImplementationOnce(
      async (_url: string, _init: unknown, _options: unknown, _timeout: number, onLine: (line: string) => void) => {
        onLine(JSON.stringify({ type: "phase", phase: "读取本地数据" }));
        onLine(JSON.stringify({ type: "combination", ...summary.combinations[0] }));
        onLine(JSON.stringify({ type: "progress", completed: 1, total: 1 }));
        onLine(JSON.stringify({ type: "result", result: summary }));
      }
    );
    const combinations: unknown[] = [];
    const results: OptimizeSummary[] = [];
    const progress: Array<{ completed: number; total: number }> = [];
    await runAiOptimizeStream(
      "http://127.0.0.1:9000",
      { strategy: defaultStrategy, settings: defaultSettings, grid: { fixed_holding_days: [3] } },
      {
        onCombination: (combination) => combinations.push(combination),
        onProgress: (event) => progress.push(event),
        onResult: (result) => results.push(result)
      }
    );

    // onResult receives the summary itself, not the {type:"result", result} envelope.
    expect(results).toHaveLength(1);
    expect(results[0].best?.index).toBe(1);
    expect(results[0].insight).toBe("样本过少，注意过拟合。");
    expect(combinations[0]).toMatchObject({ index: 1, params: { fixed_holding_days: 3 } });
    expect(progress).toEqual([{ completed: 1, total: 1 }]);
  });

  it("surfaces stream errors as BackendError with the backend code", async () => {
    consumeNdjsonStream.mockImplementationOnce(
      async (_url: string, _init: unknown, _options: unknown, _timeout: number, onLine: (line: string) => void) => {
        onLine(JSON.stringify({ type: "error", code: "grid_too_large", message: "组合数超限" }));
      }
    );
    await expect(
      runAiOptimizeStream("http://127.0.0.1:9000", {
        strategy: defaultStrategy,
        settings: defaultSettings,
        grid: { fixed_holding_days: [3] }
      })
    ).rejects.toMatchObject({ code: "grid_too_large" });
  });

  it("reports interruption when the grid stream ends without a result event", async () => {
    // 上一版会在这里静默成功：只收到 combination/progress 也当作寻优完成。
    consumeNdjsonStream.mockImplementationOnce(
      async (_url: string, _init: unknown, _options: unknown, _timeout: number, onLine: (line: string) => void) => {
        onLine(JSON.stringify({ type: "phase", phase: "读取本地数据" }));
        onLine(
          JSON.stringify({
            type: "combination",
            index: 1,
            params: { fixed_holding_days: 3 },
            metrics: { total_return_pct: 0.05 }
          })
        );
        onLine(JSON.stringify({ type: "progress", completed: 1, total: 6 }));
        onLine(JSON.stringify({ type: "heartbeat" }));
      }
    );
    const combinations: unknown[] = [];
    const results: unknown[] = [];

    await expect(
      runAiOptimizeStream(
        "http://127.0.0.1:9000",
        { strategy: defaultStrategy, settings: defaultSettings, grid: { fixed_holding_days: [3] } },
        {
          onCombination: (combination) => combinations.push(combination),
          onResult: (result) => results.push(result)
        }
      )
    ).rejects.toMatchObject({ code: "stream_incomplete" });

    // 已经算出来的组合不能因为中断被抹掉。
    expect(combinations).toHaveLength(1);
    expect(results).toHaveLength(0);
  });

  it("treats the result event as terminal even when the transport drops later lines", async () => {
    consumeNdjsonStream.mockImplementationOnce(
      async (_url: string, _init: unknown, _options: unknown, _timeout: number, onLine: (line: string) => void) => {
        onLine(JSON.stringify({ type: "result", result: { combinations: [], failures: [], total: 0, evaluated: 0 } }));
      }
    );
    await expect(
      runAiOptimizeStream("http://127.0.0.1:9000", {
        strategy: defaultStrategy,
        settings: defaultSettings,
        grid: { fixed_holding_days: [3] }
      })
    ).resolves.toBeUndefined();
  });
});
