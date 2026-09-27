import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AiEquityChart } from "./AiEquityChart";

// 图表组件是懒加载的（echarts 不进首屏静态依赖），所以这里 mock 的是那个月
// 度加载的按需注册模块，而不是全量 "echarts"。
const chartInstance = vi.hoisted(() => ({
  setOption: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn()
}));
const initMock = vi.hoisted(() => vi.fn(() => chartInstance));

vi.mock("./echartsBundle", () => ({ init: initMock }));

const points = [
  { trade_date: "2026-01-05", equity: 1_000_000, cash: 800_000, market_value: 200_000, drawdown_pct: 0 },
  { trade_date: "2026-01-06", equity: 1_010_000, cash: 800_000, market_value: 210_000, drawdown_pct: -0.01 }
];

beforeEach(() => {
  chartInstance.setOption.mockClear();
  chartInstance.dispose.mockClear();
  chartInstance.resize.mockClear();
  initMock.mockClear();
});

describe("AiEquityChart", () => {
  it("renders the container with an accessible label even without a canvas backend", async () => {
    render(<AiEquityChart title="回测权益曲线" points={points} />);
    const node = await screen.findByRole("img", { name: "回测权益曲线" });
    expect(node.className).toContain("ai-chart");
  });

  it("shows a loading state until the chart module arrives", () => {
    render(<AiEquityChart title="回测权益曲线" points={points} />);
    expect(screen.getByText(/正在加载图表组件/)).toBeInTheDocument();
  });

  it("configures equity and drawdown series once the module resolves", async () => {
    render(<AiEquityChart title="回测权益曲线" points={points} />);

    await waitFor(() => expect(chartInstance.setOption).toHaveBeenCalled());
    expect(screen.queryByText(/正在加载图表组件/)).not.toBeInTheDocument();
    const option = chartInstance.setOption.mock.calls.at(-1)?.[0] as {
      series: Array<{ name: string; data: number[] }>;
      xAxis: { data: string[] };
    };
    expect(option.xAxis.data).toEqual(["2026-01-05", "2026-01-06"]);
    expect(option.series.map((series) => series.name)).toEqual(["权益", "回撤"]);
    expect(option.series[0].data).toEqual([1_000_000, 1_010_000]);
    expect(option.series[1].data).toEqual([0, -0.01]);
  });

  it("re-applies the option when the curve changes", async () => {
    const { rerender } = render(<AiEquityChart title="回测权益曲线" points={points} />);
    await waitFor(() => expect(chartInstance.setOption).toHaveBeenCalledTimes(1));

    rerender(<AiEquityChart title="回测权益曲线" points={[points[1]]} />);

    await waitFor(() => expect(chartInstance.setOption).toHaveBeenCalledTimes(2));
    const option = chartInstance.setOption.mock.calls.at(-1)?.[0] as { xAxis: { data: string[] } };
    expect(option.xAxis.data).toEqual(["2026-01-06"]);
  });

  it("does not call setOption with an empty point list", async () => {
    render(<AiEquityChart title="空曲线" points={[]} />);

    await waitFor(() => expect(initMock).toHaveBeenCalled());
    expect(chartInstance.setOption).not.toHaveBeenCalled();
  });

  it("disposes the chart and its resize observer on unmount", async () => {
    const { unmount } = render(<AiEquityChart title="回测权益曲线" points={points} />);
    await waitFor(() => expect(chartInstance.setOption).toHaveBeenCalled());

    unmount();

    expect(chartInstance.dispose).toHaveBeenCalledOnce();
  });

  it("reports a load failure instead of rendering an empty box", async () => {
    initMock.mockImplementationOnce(() => {
      throw new Error("cannot read properties of undefined (reading 'getContext')");
    });
    render(<AiEquityChart title="回测权益曲线" points={points} />);

    expect(await screen.findByText(/图表组件未能加载/)).toBeInTheDocument();
    expect(screen.queryByText(/正在加载图表组件/)).not.toBeInTheDocument();
  });
});
