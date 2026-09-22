import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DataCenter } from "./DataCenter";

const apiMocks = vi.hoisted(() => ({
  ensureDataService: vi.fn(),
  fetchCapitalFlow: vi.fn(),
  fetchDailyBars: vi.fn(),
  importDailyBars: vi.fn(),
  loadDailyBarsCoverage: vi.fn(),
  loadDataServiceHealth: vi.fn(),
  loadDataServiceLogs: vi.fn(),
  loadSyncJob: vi.fn(),
  startFullMarketSync: vi.fn()
}));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...apiMocks
}));

const coverage = [
  { dataset: "daily_bars", symbols: 3, start_date: "2024-01-02", end_date: "2024-01-03", missing_rows: 0 }
];

describe("DataCenter lifecycle badges", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.clearAllMocks();
    apiMocks.ensureDataService.mockResolvedValue({
      running: true,
      port: 9013,
      base_url: "http://127.0.0.1:9013",
      cache_dir: ".astock-cache",
      message: "local data service is ready"
    });
    apiMocks.loadDataServiceHealth.mockResolvedValue({
      ok: true,
      cache_path: "C:\\cache",
      port: 9013,
      coverage
    });
    apiMocks.loadDataServiceLogs.mockResolvedValue({ items: [] });
    apiMocks.loadDailyBarsCoverage.mockResolvedValue({
      summary: coverage,
      items: [
        {
          symbol: "600519",
          start_date: "2024-01-02",
          end_date: "2024-01-03",
          rows: 2,
          missing_trade_dates: [],
          missing_capital_flow_dates: [],
          missing_market_cap_dates: []
        },
        {
          symbol: "NEW001",
          start_date: "2024-01-03",
          end_date: "2024-01-03",
          rows: 1,
          missing_trade_dates: [],
          missing_capital_flow_dates: [],
          missing_market_cap_dates: [],
          listing_date: "2026-09-15",
          delisted_date: null,
          lifecycle_status: "listed"
        },
        {
          symbol: "DEAD1",
          start_date: "2024-01-02",
          end_date: "2024-01-02",
          rows: 1,
          missing_trade_dates: [],
          missing_capital_flow_dates: [],
          missing_market_cap_dates: [],
          listing_date: "2019-03-01",
          delisted_date: "2024-01-02",
          lifecycle_status: "delisted"
        }
      ]
    });
  });

  it("marks delisted and not-yet-listed symbols in the coverage details", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9013/);
    await user.clear(screen.getByLabelText("股票代码"));
    await user.type(screen.getByLabelText("股票代码"), "600519 NEW001 DEAD1");
    await user.click(screen.getByRole("button", { name: "刷新覆盖范围" }));

    await waitFor(() => expect(screen.getByText("已退市（2024-01-02）")).toBeInTheDocument());
    expect(screen.getByText("未上市（2026-09-15 起）")).toBeInTheDocument();
    const dead = screen.getByText("DEAD1").closest("article");
    expect(dead).not.toBeNull();
  });

  it("runs the AI coverage diagnosis on demand and shows the one-line result", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9013/);
    await user.click(screen.getByRole("button", { name: "AI 诊断缺失" }));

    expect(await screen.findByLabelText("AI 覆盖诊断")).toHaveTextContent("补齐资金流");
  });

  it("shows the source health card with realtime news finance rows", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9013/);
    await user.click(screen.getByText("数据源健康监控"));

    await waitFor(() => expect(screen.getByText("实时行情源")).toBeInTheDocument());
    expect(screen.getByText("市场新闻源")).toBeInTheDocument();
    expect(screen.getByText("财联社行情源")).toBeInTheDocument();
    expect(screen.getByText(/同花顺大盘评分读取失败/)).toBeInTheDocument();
  });
});
