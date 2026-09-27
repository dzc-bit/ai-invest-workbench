import { render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import type { BacktestResult } from "../types";
import { ResultsOverview } from "./ResultsOverview";

vi.mock("recharts", () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div data-testid="responsive-container">{children}</div>,
  LineChart: ({ children, data }: { children: ReactNode; data?: unknown[] }) => <div data-testid="line-chart" data-points={data?.length ?? 0}>{children}</div>,
  Tooltip: () => null,
  XAxis: () => null,
  YAxis: () => null,
  Line: () => null
}));

function shanghaiToday(): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).format(new Date());
}

function buildResult(overrides: Partial<BacktestResult> = {}): BacktestResult {
  return {
    metrics: {
      total_return_pct: 0.032,
      annualized_return_pct: 0.041,
      max_drawdown_pct: -0.018,
      win_rate_pct: 0.6,
      trade_count: 2,
      average_trade_return_pct: 0.011,
      average_position_pct: 0.48,
      max_position_pct: 0.5
    },
    equity_curve: [
      { trade_date: "2026-06-01", equity: 100000, cash: 100000, market_value: 0, drawdown_pct: 0 },
      { trade_date: "2026-06-02", equity: 101000, cash: 52000, market_value: 49000, drawdown_pct: 0 }
    ],
    trades: [],
    preflight_issues: [],
    ...overrides
  };
}

it("lists today's matched stocks after a strategy run", () => {
  const today = shanghaiToday();

  render(
    <ResultsOverview
      result={buildResult({
        latest_strategy_matches: {
          signal_date: today,
          trade_date: today,
          matches: [
            {
              symbol: "600519",
              name: "贵州茅台",
              close: 1688.8,
              change_pct: 0.0213,
              reasons: ["收盘价站上20日均线", "主力净流入放大"]
            },
            {
              symbol: "300750",
              name: "宁德时代",
              close: 218.32,
              change_pct: -0.008,
              reasons: ["量比2日介于1.2到2.5"]
            }
          ]
        }
      })}
      onRun={vi.fn()}
      onOpenRiskAlerts={vi.fn()}
    />
  );

  expect(screen.getByRole("region", { name: "策略命中" })).toBeInTheDocument();
  expect(screen.getByText("今日 user 模式候选")).toBeInTheDocument();
  expect(screen.getByText("当日符合用户策略的个股")).toBeInTheDocument();
  expect(screen.getByText(`信号日 ${today} / 展示日 ${today} / 本地回测快照`)).toBeInTheDocument();
  const cards = screen.getAllByRole("article").filter((item) => item.className.includes("matched-stock-card"));
  expect(cards[0]).toHaveTextContent("600519 贵州茅台");
  expect(cards[0]).toHaveTextContent("理由：收盘价站上20日均线；主力净流入放大");
  expect(cards[0]).not.toHaveTextContent("+2.13%");
  expect(cards[0]).not.toHaveTextContent("本地收盘价");
});

it("sorts latest strategy matches by rank score", () => {
  render(
    <ResultsOverview
      result={buildResult({
        latest_strategy_matches: {
          signal_date: "2026-06-03",
          trade_date: "2026-06-03",
          matches: [
            { symbol: "LOW", name: "低分", close: 10, change_pct: 0.01, rank_score: 0.3, reasons: ["低分原因"] },
            { symbol: "HIGH", name: "高分", close: 20, change_pct: 0.02, rank_score: 2.4, reasons: ["高分原因"] }
          ]
        }
      })}
      onRun={vi.fn()}
      onOpenRiskAlerts={vi.fn()}
    />
  );

  const cards = screen.getAllByRole("article").filter((item) => item.className.includes("matched-stock-card"));
  expect(cards[0]).toHaveTextContent("HIGH");
  expect(cards[0]).toHaveTextContent("评分 2.40");
});

it("shows matched stocks as symbol name score and reasons without ambiguous gain chips", () => {
  render(
    <ResultsOverview
      result={buildResult({
        latest_strategy_matches: {
          signal_date: "2026-05-25",
          trade_date: "2026-05-25",
          matches: [
            {
              symbol: "000518",
              name: "四环生物",
              close: 4.54,
              change_pct: 9.93,
              rank_score: 5.67,
              reasons: ["close 4.54 broke prior 20d high 4.13", "2d volume ratio 1.81 in [1.20, 2.50]"]
            }
          ]
        }
      })}
      onRun={vi.fn()}
      onOpenRiskAlerts={vi.fn()}
    />
  );

  const card = screen.getByRole("article");
  expect(card).toHaveTextContent("000518 四环生物");
  expect(card).toHaveTextContent("评分 5.67");
  expect(card).toHaveTextContent("理由：收盘价 4.54 突破前20日高点 4.13；2日量比 1.81 位于 [1.20, 2.50]");
  expect(card).not.toHaveTextContent("+993.00%");
  expect(card).not.toHaveTextContent("本地最近收盘价");
  expect(card.querySelector(".matched-stock-reasons span")).toBeNull();
});

it("omits placeholder name when a latest strategy match has no stock name", () => {
  render(
    <ResultsOverview
      result={buildResult({
        latest_strategy_matches: {
          signal_date: "2026-05-25",
          trade_date: "2026-05-25",
          matches: [
            {
              symbol: "000001",
              name: null,
              close: 10,
              change_pct: 0,
              rank_score: 1.23,
              reasons: ["custom reason"]
            }
          ]
        }
      })}
      onRun={vi.fn()}
      onOpenRiskAlerts={vi.fn()}
    />
  );

  const card = screen.getByRole("article");
  expect(card).toHaveTextContent("000001");
  expect(card).not.toHaveTextContent("000001 --");
  expect(card).not.toHaveTextContent("--");
});

it("shows a friendly empty state when no stocks match today", () => {
  const today = shanghaiToday();

  render(
    <ResultsOverview
      result={buildResult({
        latest_strategy_matches: {
          signal_date: today,
          trade_date: today,
          matches: []
        }
      })}
      onRun={vi.fn()}
      onOpenRiskAlerts={vi.fn()}
    />
  );

  expect(screen.getByText("今日 user 模式候选")).toBeInTheDocument();
  expect(screen.getByText("今日没有股票命中当前策略")).toBeInTheDocument();
});

it("shows annualized return beside the other backtest metrics", () => {
  render(<ResultsOverview result={buildResult()} onRun={vi.fn()} onOpenRiskAlerts={vi.fn()} />);

  expect(screen.getByText("总收益 3.20%")).toBeInTheDocument();
  expect(screen.getByText("年化收益 4.10%")).toBeInTheDocument();
  expect(screen.getByText("最大回撤 -1.80%")).toBeInTheDocument();
});

it("does not label old backtest matches as today's hits and separates the equity chart", () => {
  render(
    <ResultsOverview
      result={buildResult({
        latest_strategy_matches: {
          signal_date: "2024-03-13",
          trade_date: "2024-03-13",
          matches: []
        },
        equity_curve: [
          { trade_date: "2024-01-04", equity: 100000, cash: 100000, market_value: 0, drawdown_pct: 0 },
          { trade_date: "2024-03-13", equity: 90000, cash: 90000, market_value: 0, drawdown_pct: -0.1 }
        ]
      })}
      onRun={vi.fn()}
      onOpenRiskAlerts={vi.fn()}
    />
  );

  expect(screen.getByRole("region", { name: "策略命中" })).toBeInTheDocument();
  expect(screen.getByText("本地最近交易日候选")).toBeInTheDocument();
  expect(screen.queryByText("今日 user 模式候选")).not.toBeInTheDocument();
  expect(screen.getByText("历史权益曲线")).toBeInTheDocument();
  expect(screen.getByText("回测区间 2024-01-04 至 2024-03-13")).toBeInTheDocument();
});

it("normalizes the equity curve by date before rendering the chart", async () => {
  render(
    <ResultsOverview
      result={buildResult({
        equity_curve: [
          { trade_date: "2024-01-04", equity: 100000, cash: 100000, market_value: 0, drawdown_pct: 0 },
          { trade_date: "2024-01-04", equity: 99999, cash: 99999, market_value: 0, drawdown_pct: -0.00001 },
          { trade_date: "2024-01-06", equity: Number.NaN, cash: 0, market_value: 0, drawdown_pct: 0 },
          { trade_date: "2024-01-05", equity: 101000, cash: 51000, market_value: 50000, drawdown_pct: 0 }
        ]
      })}
      onRun={vi.fn()}
      onOpenRiskAlerts={vi.fn()}
    />
  );

  // 图表块 lazy 加载（recharts 不进首屏），断言前等 Suspense 落地。
  await waitFor(() => expect(screen.getByTestId("line-chart")).toBeInTheDocument());
  expect(screen.getByTestId("line-chart")).toHaveAttribute("data-points", "2");
  expect(screen.getByText("回测区间 2024-01-04 至 2024-01-05")).toBeInTheDocument();
});
