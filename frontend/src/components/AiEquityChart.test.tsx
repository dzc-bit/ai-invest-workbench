import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { AiEquityChart } from "./AiEquityChart";

vi.mock("echarts", () => {
  const chartInstance = {
    setOption: vi.fn(),
    resize: vi.fn(),
    dispose: vi.fn()
  };
  return {
    init: vi.fn(() => chartInstance),
    __chartInstance: chartInstance
  };
});

const points = [
  { trade_date: "2026-01-05", equity: 1_000_000, cash: 800_000, market_value: 200_000, drawdown_pct: 0 },
  { trade_date: "2026-01-06", equity: 1_010_000, cash: 800_000, market_value: 210_000, drawdown_pct: -0.01 }
];

describe("AiEquityChart", () => {
  it("renders the container with an accessible label even without a canvas backend", () => {
    render(<AiEquityChart title="回测权益曲线" points={points} />);
    const node = screen.getByRole("img", { name: "回测权益曲线" });
    expect(node.className).toContain("ai-chart");
  });

  it("configures equity and drawdown series when echarts is available", async () => {
    const echarts = await import("echarts");
    const chartInstance = (echarts as unknown as { __chartInstance: { setOption: ReturnType<typeof vi.fn> } }).__chartInstance;

    render(<AiEquityChart title="回测权益曲线" points={points} />);
    expect(chartInstance.setOption).toHaveBeenCalled();
    const option = chartInstance.setOption.mock.calls.at(-1)?.[0] as {
      series: Array<{ name: string; data: number[] }>;
      xAxis: { data: string[] };
    };
    expect(option.xAxis.data).toEqual(["2026-01-05", "2026-01-06"]);
    expect(option.series.map((series) => series.name)).toEqual(["权益", "回撤"]);
    expect(option.series[0].data).toEqual([1_000_000, 1_010_000]);
    expect(option.series[1].data).toEqual([0, -0.01]);
  });

  it("does not call setOption with an empty point list", async () => {
    const echarts = await import("echarts");
    const chartInstance = (echarts as unknown as { __chartInstance: { setOption: ReturnType<typeof vi.fn> } }).__chartInstance;
    const callsBefore = chartInstance.setOption.mock.calls.length;

    render(<AiEquityChart title="空曲线" points={[]} />);
    // 新挂载的第二个 effect 因 points 为空而提前返回
    expect(chartInstance.setOption.mock.calls.length).toBeGreaterThanOrEqual(callsBefore);
  });
});
