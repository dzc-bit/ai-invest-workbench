import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DataCenter } from "./DataCenter";

const apiMocks = vi.hoisted(() => ({
  cancelSyncJob: vi.fn(),
  ensureDataService: vi.fn(),
  fetchCapitalFlow: vi.fn(),
  fetchDailyBars: vi.fn(),
  importDailyBars: vi.fn(),
  loadDataServiceHealth: vi.fn(),
  loadDataServiceLogs: vi.fn(),
  loadDailyBarsCoverage: vi.fn(),
  loadSyncJob: vi.fn(),
  startFullMarketSync: vi.fn(),
  startMissingOnlySync: vi.fn()
}));

// Spread the real api module so newly added exports (e.g. BackendError) stay
// available to the component under test; mocked functions always win.
vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...apiMocks
}));

const coverage = [
  { dataset: "daily_bars", symbols: 1, start_date: "2024-01-02", end_date: "2024-01-03", missing_rows: 2, suspension_rows: 0 },
  { dataset: "capital_flow", symbols: 1, start_date: "2024-01-03", end_date: "2024-01-03", missing_rows: 1, suspension_rows: 0 }
];

const staleRecentCoverage = [
  { dataset: "daily_bars", symbols: 5000, start_date: "2015-01-05", end_date: "2026-05-26", missing_rows: 0, suspension_rows: 0 },
  { dataset: "capital_flow", symbols: 4900, start_date: "2015-01-05", end_date: "2026-05-26", missing_rows: 0, suspension_rows: 0 }
];

describe("DataCenter", () => {
  const setupUser = () => userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.clearAllMocks();
    vi.setSystemTime(new Date("2026-06-07T10:00:00+08:00"));
    apiMocks.ensureDataService.mockResolvedValue({
      running: true,
      port: 9011,
      base_url: "http://127.0.0.1:9011",
      cache_dir: ".astock-cache",
      message: "local data service is ready"
    });
    apiMocks.loadDailyBarsCoverage.mockResolvedValue({
      summary: coverage,
      items: [
        {
          symbol: "600519",
          start_date: "2024-01-02",
          end_date: "2024-01-03",
          rows: 2,
          missing_trade_dates: ["2024-01-04"],
          missing_capital_flow_dates: ["2024-01-02"],
          missing_market_cap_dates: []
        }
      ]
    });
    apiMocks.loadDataServiceHealth.mockResolvedValue({
      ok: true,
      cache_path: "C:\\cache",
      port: 9011,
      coverage
    });
    apiMocks.loadDataServiceLogs.mockResolvedValue({ items: [] });
    apiMocks.fetchDailyBars.mockResolvedValue({
      status: "ok",
      imported_rows: 3,
      requested_symbols: ["600519"],
      fetched_symbols: ["600519"],
      missing_symbols: [],
      coverage,
      logs: [{ level: "info", message: "Fetched 3 daily bar rows" }]
    });
    apiMocks.fetchCapitalFlow.mockResolvedValue({
      status: "ok",
      imported_rows: 1,
      requested_symbols: ["600519"],
      fetched_symbols: ["600519"],
      missing_symbols: [],
      coverage,
      logs: [{ level: "info", message: "Capital-flow crawler merged 1 rows as primary main_net_inflow source" }],
      diagnostics: [{ code: "capital_flow_crawler_merge", merged_rows: 1, source: "capital_flow_crawler" }],
      failures: []
    });
    apiMocks.importDailyBars.mockResolvedValue({
      status: "ok",
      imported_rows: 2,
      coverage,
      logs: [{ level: "info", message: "Imported daily bars from sample" }]
    });
    apiMocks.loadSyncJob.mockResolvedValue({
      job: {
        job_id: "job-1",
        mode: "full_market_bootstrap",
        status: "completed",
        total_symbols: 2,
        completed_symbols: 2,
        failed_symbols: 0,
        imported_rows: 20,
        current_symbol: null,
        start_date: "2015-01-01",
        end_date: "2026-05-26",
        errors: []
      }
    });
  });

  it("starts the managed service and refreshes daily-bar coverage", async () => {
    const onCoverageChange = vi.fn();
    render(<DataCenter cacheDir=".astock-cache" coverage={[]} onCoverageChange={onCoverageChange} />);

    expect(await screen.findByText(/http:\/\/127\.0\.0\.1:9011/)).toBeInTheDocument();
    expect(apiMocks.ensureDataService).toHaveBeenCalledWith(".astock-cache");
    expect(apiMocks.loadDataServiceHealth).toHaveBeenCalledWith("http://127.0.0.1:9011");
    expect(onCoverageChange).toHaveBeenCalledWith(coverage);
    expect(apiMocks.loadDailyBarsCoverage).not.toHaveBeenCalled();
  });

  it("shows string errors returned by the desktop service command", async () => {
    apiMocks.ensureDataService.mockRejectedValueOnce(
      "localhost data service did not become healthy before the startup deadline"
    );

    render(<DataCenter cacheDir=".astock-cache" coverage={[]} onCoverageChange={vi.fn()} />);

    const status = screen.getByRole("status", { name: "数据中心状态" });
    await waitFor(() =>
      expect(status).toHaveTextContent(
        "localhost data service did not become healthy before the startup deadline"
      )
    );
    expect(screen.getByText("本地服务未连接")).toBeInTheDocument();
  });

  it("does not block service readiness while health coverage refresh continues in the background", async () => {
    const refreshedCoverage = [
      { dataset: "daily_bars", symbols: 5000, start_date: "2015-01-05", end_date: "2026-06-05", missing_rows: 0, suspension_rows: 0 }
    ];
    const onCoverageChange = vi.fn();
    apiMocks.loadDataServiceHealth
      .mockResolvedValueOnce({
        ok: true,
        cache_path: "C:\\cache",
        port: 9011,
        coverage,
        coverage_refreshing: true
      })
      .mockResolvedValueOnce({
        ok: true,
        cache_path: "C:\\cache",
        port: 9011,
        coverage: refreshedCoverage,
        coverage_refreshing: false
      });

    render(<DataCenter cacheDir=".astock-cache" coverage={[]} onCoverageChange={onCoverageChange} />);

    expect(await screen.findByText(/http:\/\/127\.0\.0\.1:9011/)).toBeInTheDocument();
    expect(onCoverageChange).toHaveBeenCalledWith(coverage);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1300);
    });

    await waitFor(() => expect(onCoverageChange).toHaveBeenLastCalledWith(refreshedCoverage));
    expect(apiMocks.loadDataServiceHealth).toHaveBeenCalledTimes(2);
  });

  it("keeps existing coverage when health returns an empty refreshing snapshot", async () => {
    const emptyRefreshingCoverage = [
      { dataset: "daily_bars", symbols: 0, start_date: null, end_date: null, missing_rows: 0, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: 0, start_date: null, end_date: null, missing_rows: 0, suspension_rows: 0 },
      { dataset: "market_cap", symbols: 0, start_date: null, end_date: null, missing_rows: 0, suspension_rows: 0 }
    ];
    const refreshedCoverage = [
      { dataset: "daily_bars", symbols: 5000, start_date: "2015-01-05", end_date: "2026-06-05", missing_rows: 120, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: 4800, start_date: "2015-01-05", end_date: "2026-06-05", missing_rows: 300, suspension_rows: 0 },
      { dataset: "market_cap", symbols: 5000, start_date: "2015-01-05", end_date: "2026-06-05", missing_rows: 0, suspension_rows: 0 }
    ];
    const onCoverageChange = vi.fn();
    apiMocks.loadDataServiceHealth
      .mockResolvedValueOnce({
        ok: true,
        cache_path: "C:\\cache",
        port: 9011,
        coverage: emptyRefreshingCoverage,
        coverage_refreshing: true
      })
      .mockResolvedValueOnce({
        ok: true,
        cache_path: "C:\\cache",
        port: 9011,
        coverage: refreshedCoverage,
        coverage_refreshing: false
      });

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={onCoverageChange} />);

    expect(await screen.findByText(/http:\/\/127\.0\.0\.1:9011/)).toBeInTheDocument();
    expect(onCoverageChange).not.toHaveBeenCalledWith(emptyRefreshingCoverage);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1300);
    });

    await waitFor(() => expect(onCoverageChange).toHaveBeenLastCalledWith(refreshedCoverage));
  });

  it("keeps polling while a large warehouse coverage refresh is still running", async () => {
    const emptyRefreshingCoverage = [
      { dataset: "daily_bars", symbols: 0, start_date: null, end_date: null, missing_rows: 0, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: 0, start_date: null, end_date: null, missing_rows: 0, suspension_rows: 0 },
      { dataset: "market_cap", symbols: 0, start_date: null, end_date: null, missing_rows: 0, suspension_rows: 0 }
    ];
    const refreshedCoverage = [
      { dataset: "daily_bars", symbols: 5469, start_date: "2015-01-05", end_date: "2026-06-18", missing_rows: 2930, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: 5530, start_date: "2015-01-05", end_date: "2026-06-18", missing_rows: 19338, suspension_rows: 0 },
      { dataset: "market_cap", symbols: 5469, start_date: "2015-01-05", end_date: "2026-06-18", missing_rows: 48959, suspension_rows: 0 }
    ];
    const onCoverageChange = vi.fn();
    apiMocks.loadDataServiceHealth
      .mockResolvedValueOnce({
        ok: true,
        cache_path: "C:\\cache",
        port: 9011,
        coverage: emptyRefreshingCoverage,
        coverage_refreshing: true
      });
    for (let index = 0; index < 8; index += 1) {
      apiMocks.loadDataServiceHealth.mockResolvedValueOnce({
        ok: true,
        cache_path: "C:\\cache",
        port: 9011,
        coverage: emptyRefreshingCoverage,
        coverage_refreshing: true
      });
    }
    apiMocks.loadDataServiceHealth.mockResolvedValueOnce({
      ok: true,
      cache_path: "C:\\cache",
      port: 9011,
      coverage: refreshedCoverage,
      coverage_refreshing: false
    });

    render(<DataCenter cacheDir=".astock-cache" coverage={staleRecentCoverage} onCoverageChange={onCoverageChange} />);

    expect(await screen.findByText(/http:\/\/127\.0\.0\.1:9011/)).toBeInTheDocument();
    expect(onCoverageChange).not.toHaveBeenCalledWith(emptyRefreshingCoverage);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 1200);
    });

    await waitFor(() => expect(onCoverageChange).toHaveBeenLastCalledWith(refreshedCoverage));
    expect(apiMocks.loadDataServiceHealth).toHaveBeenCalledTimes(10);
  });

  it("keeps polling after sync completion until the refreshed coverage snapshot arrives", async () => {
    const user = setupUser();
    const oldCoverage = [
      { dataset: "daily_bars", symbols: 5469, start_date: "2015-01-05", end_date: "2026-06-18", missing_rows: 2930, suspension_rows: 0 },
      { dataset: "market_cap", symbols: 5469, start_date: "2015-01-05", end_date: "2026-06-18", missing_rows: 48959, suspension_rows: 0 }
    ];
    const refreshedCoverage = [
      { dataset: "daily_bars", symbols: 5469, start_date: "2015-01-05", end_date: "2026-06-18", missing_rows: 2704, suspension_rows: 0 },
      { dataset: "market_cap", symbols: 5469, start_date: "2015-01-05", end_date: "2026-06-18", missing_rows: 48718, suspension_rows: 0 }
    ];
    const onCoverageChange = vi.fn();
    apiMocks.loadDataServiceHealth.mockReset();
    apiMocks.loadDataServiceHealth
      .mockResolvedValueOnce({ ok: true, cache_path: "C:\\cache", port: 9011, coverage: oldCoverage })
      .mockResolvedValueOnce({
        ok: true,
        cache_path: "C:\\cache",
        port: 9011,
        coverage: oldCoverage,
        coverage_refreshing: true
      });
    for (let index = 0; index < 35; index += 1) {
      apiMocks.loadDataServiceHealth.mockResolvedValueOnce({
        ok: true,
        cache_path: "C:\\cache",
        port: 9011,
        coverage: oldCoverage,
        coverage_refreshing: true
      });
    }
    apiMocks.loadDataServiceHealth.mockResolvedValueOnce({
      ok: true,
      cache_path: "C:\\cache",
      port: 9011,
      coverage: refreshedCoverage,
      coverage_refreshing: false
    });
    apiMocks.startFullMarketSync.mockResolvedValue({
      job: {
        job_id: "slow-coverage-sync",
        mode: "full_market_bootstrap",
        status: "running",
        total_symbols: 5532,
        completed_symbols: 2600,
        failed_symbols: 0,
        processed_symbols: 2600,
        skipped_symbols: 0,
        imported_rows: 60,
        returned_rows: 60,
        filled_missing_rows: 15,
        filled_daily_rows: 9,
        filled_market_cap_rows: 6,
        current_symbol: "000001",
        start_date: "2026-06-12",
        end_date: "2026-06-18",
        errors: []
      }
    });
    apiMocks.loadSyncJob.mockResolvedValue({
      job: {
        job_id: "slow-coverage-sync",
        mode: "full_market_bootstrap",
        status: "completed",
        total_symbols: 5532,
        completed_symbols: 5532,
        failed_symbols: 0,
        processed_symbols: 5532,
        skipped_symbols: 0,
        imported_rows: 60,
        returned_rows: 60,
        filled_missing_rows: 15,
        filled_daily_rows: 9,
        filled_market_cap_rows: 6,
        current_symbol: null,
        start_date: "2026-06-12",
        end_date: "2026-06-18",
        errors: []
      }
    });

    render(<DataCenter cacheDir=".astock-cache" coverage={oldCoverage} onCoverageChange={onCoverageChange} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.click(screen.getByRole("button", { name: "补全缺失数据" }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    await waitFor(() => expect(apiMocks.loadSyncJob).toHaveBeenCalledWith("http://127.0.0.1:9011", "slow-coverage-sync"));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(42 * 1200);
    });

    await waitFor(() => expect(onCoverageChange).toHaveBeenLastCalledWith(refreshedCoverage));
  });

  it("shows recent service logs when a fetch fails", async () => {
    const user = setupUser();
    apiMocks.fetchDailyBars.mockRejectedValue(new Error("HTTP 400: request_failed - boom"));
    apiMocks.loadDataServiceLogs.mockResolvedValue({
      items: [
        { level: "error", message: "Baidu daily source returned malformed kline", timestamp: "2026-05-26T05:00:00Z" }
      ]
    });

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.click(screen.getByRole("button", { name: "补全缺失数据" }));

    expect(await screen.findByText(/Baidu daily source returned malformed kline/)).toBeInTheDocument();
  });

  it("refreshes logs from the reconnected service after an import timeout", async () => {
    const user = setupUser();
    apiMocks.ensureDataService.mockClear();
    apiMocks.importDailyBars.mockClear();
    apiMocks.loadDataServiceHealth.mockClear();
    apiMocks.loadDataServiceLogs.mockClear();
    apiMocks.ensureDataService
      .mockResolvedValueOnce({
        running: true,
        port: 9011,
        base_url: "http://127.0.0.1:9011",
        cache_dir: ".astock-cache",
        message: "local data service is ready"
      })
      .mockResolvedValueOnce({
        running: true,
        port: 9012,
        base_url: "http://127.0.0.1:9012",
        cache_dir: ".astock-cache",
        message: "local data service restarted"
      });
    apiMocks.importDailyBars.mockRejectedValue(
      new Error("本地数据服务请求超时，请稍后重试或重新连接本地服务。")
    );

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.click(screen.getByRole("button", { name: "导入示例数据" }));

    await waitFor(() => expect(apiMocks.ensureDataService).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(apiMocks.loadDataServiceHealth).toHaveBeenLastCalledWith("http://127.0.0.1:9012"));
    await waitFor(() => expect(apiMocks.loadDataServiceLogs).toHaveBeenLastCalledWith("http://127.0.0.1:9012"));
    expect(screen.getByRole("status", { name: "数据中心状态" })).toHaveTextContent("本地数据服务请求超时");
  });

  it("fetches missing daily bars through the service and refreshes parent coverage", async () => {
    const user = setupUser();
    const onCoverageChange = vi.fn();

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={onCoverageChange} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.clear(screen.getByLabelText("股票代码"));
    await user.type(screen.getByLabelText("股票代码"), "600519 000001");
    await user.click(screen.getByRole("button", { name: "补全缺失数据" }));

    await waitFor(() => expect(apiMocks.fetchDailyBars).toHaveBeenCalledWith(
      "http://127.0.0.1:9011",
      ["600519", "000001"],
      "2026-06-01",
      "2026-06-05"
    ));
    expect(onCoverageChange).toHaveBeenCalledWith(coverage);
    expect(await screen.findByText("Fetched 3 daily bar rows")).toBeInTheDocument();
    expect(screen.getAllByText("建议补齐").length).toBeGreaterThan(0);
  });

  it("defaults missing-data backfill to full-market sync when no symbols are entered", async () => {
    const user = setupUser();

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    expect(screen.getByLabelText("股票代码")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "补全缺失数据" }));

    await waitFor(() => expect(apiMocks.startFullMarketSync).toHaveBeenCalledWith(
      "http://127.0.0.1:9011",
      "2026-06-01",
      "2026-06-05"
    ));
    expect(apiMocks.fetchDailyBars).not.toHaveBeenCalled();
  });

  it("keeps full-market missing rows authoritative while syncing", async () => {
    const user = setupUser();
    const missingCoverage = [
      { dataset: "daily_bars", symbols: 5000, start_date: "2015-01-05", end_date: "2026-06-01", missing_rows: 100, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: 5000, start_date: "2015-01-05", end_date: "2026-06-01", missing_rows: 60, suspension_rows: 0 }
    ];
    apiMocks.loadDataServiceHealth.mockResolvedValue({
      ok: true,
      cache_path: "C:\\cache",
      port: 9011,
      coverage: missingCoverage
    });
    apiMocks.startFullMarketSync.mockResolvedValue({
      job: {
        job_id: "job-rows",
        mode: "full_market_bootstrap",
        status: "running",
        total_symbols: 10,
        completed_symbols: 2,
        failed_symbols: 0,
        imported_rows: 25,
        filled_missing_rows: 8,
        filled_daily_rows: 3,
        filled_market_cap_rows: 5,
        current_symbol: "000002",
        start_date: "2026-06-01",
        end_date: "2026-06-05",
        errors: []
      }
    });

    render(<DataCenter cacheDir=".astock-cache" coverage={missingCoverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.click(screen.getByRole("button", { name: "补全缺失数据" }));

    const rows = screen.getAllByRole("row");
    expect(within(rows[1]).getByText("100")).toBeInTheDocument();
    expect(screen.queryByText("75")).not.toBeInTheDocument();
    expect(screen.getByText("已写入 25 行，补齐日线 3 行，补齐市值 5 行，等待覆盖刷新确认")).toBeInTheDocument();
  });

  it("keeps capital-flow missing rows authoritative while syncing", async () => {
    const user = setupUser();
    const missingCoverage = [
      { dataset: "daily_bars", symbols: 5000, start_date: "2015-01-05", end_date: "2026-06-01", missing_rows: 100, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: 5000, start_date: "2015-01-05", end_date: "2026-06-01", missing_rows: 60, suspension_rows: 0 }
    ];
    apiMocks.loadDataServiceHealth.mockResolvedValue({
      ok: true,
      cache_path: "C:\\cache",
      port: 9011,
      coverage: missingCoverage
    });
    apiMocks.fetchCapitalFlow.mockResolvedValue({
      status: "ok",
      imported_rows: 0,
      returned_rows: 0,
      requested_symbols: [],
      fetched_symbols: [],
      missing_symbols: [],
      skipped_symbols: [],
      coverage: missingCoverage,
      logs: [{ level: "info", message: "Capital-flow backfill started for all symbols" }],
      diagnostics: [{ code: "capital_flow_backfill_job_started", source: "capital_flow_crawler" }],
      failures: [],
      job: {
        job_id: "flow-job-progress",
        mode: "capital_flow_backfill",
        status: "running",
        total_symbols: 10,
        completed_symbols: 2,
        failed_symbols: 0,
        processed_symbols: 2,
        skipped_symbols: 0,
        imported_rows: 25,
        filled_missing_rows: 11,
        filled_daily_rows: 0,
        filled_market_cap_rows: 0,
        returned_rows: 30,
        current_symbol: "000003",
        start_date: "2026-06-01",
        end_date: "2026-06-05",
        errors: []
      }
    });

    render(<DataCenter cacheDir=".astock-cache" coverage={missingCoverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    const capitalFlowButton = screen.getByRole("button", { name: "补齐资金流" });
    await user.click(capitalFlowButton);

    await waitFor(() => expect(apiMocks.fetchCapitalFlow).toHaveBeenCalled());
    const rows = screen.getAllByRole("row");
    expect(within(rows[1]).getByText("100")).toBeInTheDocument();
    expect(within(rows[2]).getByText("60")).toBeInTheDocument();
    expect(screen.queryByText("35")).not.toBeInTheDocument();
    expect(screen.getByText("已写入 25 行，等待覆盖刷新确认")).toBeInTheDocument();
  });

  it("does not refresh health immediately after starting an async capital-flow backfill", async () => {
    const user = setupUser();
    apiMocks.fetchCapitalFlow.mockResolvedValue({
      status: "ok",
      imported_rows: 0,
      returned_rows: 0,
      requested_symbols: [],
      fetched_symbols: [],
      missing_symbols: [],
      skipped_symbols: [],
      coverage,
      logs: [{ level: "info", message: "Capital-flow backfill started for all symbols" }],
      diagnostics: [{ code: "capital_flow_backfill_job_started", source: "capital_flow_crawler" }],
      failures: [],
      job: {
        job_id: "flow-job-running",
        mode: "capital_flow_backfill",
        status: "running",
        total_symbols: 10,
        completed_symbols: 0,
        failed_symbols: 0,
        processed_symbols: 0,
        skipped_symbols: 0,
        imported_rows: 0,
        returned_rows: 0,
        current_symbol: "000001",
        start_date: "2026-06-01",
        end_date: "2026-06-05",
        errors: []
      }
    });

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    apiMocks.loadDataServiceHealth.mockClear();
    await user.click(screen.getByRole("button", { name: "补齐资金流" }));

    await waitFor(() => expect(apiMocks.fetchCapitalFlow).toHaveBeenCalled());
    expect(apiMocks.loadDataServiceHealth).not.toHaveBeenCalled();
    expect(screen.getByRole("status", { name: "数据中心状态" })).toHaveTextContent("正在补齐全市场资金流");
  });

  it("backfills capital flow through the service crawler boundary", async () => {
    const user = setupUser();
    const onCoverageChange = vi.fn();

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={onCoverageChange} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.clear(screen.getByLabelText("\u80a1\u7968\u4ee3\u7801"));
    await user.type(screen.getByLabelText("\u80a1\u7968\u4ee3\u7801"), "600519 000001");
    await user.click(screen.getByRole("button", { name: "补齐资金流" }));

    await waitFor(() => expect(apiMocks.fetchCapitalFlow).toHaveBeenCalledWith(
      "http://127.0.0.1:9011",
      ["600519", "000001"],
      "2026-06-01",
      "2026-06-05"
    ));
    expect(onCoverageChange).toHaveBeenCalledWith(coverage);
    expect(await screen.findByText(/Capital-flow crawler merged 1 rows/)).toBeInTheDocument();
  });

  it("starts capital-flow backfill without typed symbols and refreshes coverage after the operation", async () => {
    const user = setupUser();
    const onCoverageChange = vi.fn();
    const refreshedCoverage = [
      { dataset: "daily_bars", symbols: 1, start_date: "2024-01-02", end_date: "2024-01-03", missing_rows: 2, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-03", missing_rows: 0, suspension_rows: 0 }
    ];
    apiMocks.loadDataServiceHealth
      .mockResolvedValueOnce({
        ok: true,
        cache_path: "C:\\cache",
        port: 9011,
        coverage
      })
      .mockResolvedValueOnce({
        ok: true,
        cache_path: "C:\\cache",
        port: 9011,
        coverage: refreshedCoverage
      });

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={onCoverageChange} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    expect(screen.getByLabelText("股票代码")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "补齐资金流" }));

    await waitFor(() => expect(apiMocks.fetchCapitalFlow).toHaveBeenCalledWith(
      "http://127.0.0.1:9011",
      [],
      "2026-06-01",
      "2026-06-05"
    ));
    await waitFor(() => expect(apiMocks.loadDataServiceHealth).toHaveBeenLastCalledWith("http://127.0.0.1:9011"));
    expect(onCoverageChange).toHaveBeenLastCalledWith(refreshedCoverage);
    expect(await screen.findByText(/Capital-flow crawler merged 1 rows/)).toBeInTheDocument();
  });

  it("surfaces capital-flow crawler failures in the operation status", async () => {
    const user = setupUser();
    apiMocks.fetchCapitalFlow.mockResolvedValue({
      status: "partial",
      imported_rows: 1,
      requested_symbols: ["600519", "000001"],
      fetched_symbols: ["600519"],
      missing_symbols: ["000001"],
      coverage,
      logs: [{ level: "warning", message: "Capital-flow crawler failed for symbols: 000001" }],
      diagnostics: [{ symbol: "000001", code: "network_error", message: "remote disconnected" }],
      failures: [{ symbol: "000001", code: "network_error", error: "remote disconnected" }]
    });

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.clear(screen.getByLabelText("股票代码"));
    await user.type(screen.getByLabelText("股票代码"), "600519 000001");
    await user.click(screen.getByRole("button", { name: "补齐资金流" }));

    expect(await screen.findByRole("status", { name: "数据中心状态" })).toHaveTextContent(
      "部分失败: 000001"
    );
    expect(screen.getByRole("status", { name: "数据中心状态" })).toHaveTextContent("network_error");
  });

  it("renders the missing-data monitor with stale distribution details", async () => {
    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    expect(await screen.findByText(/停更 5389 只/)).toBeTruthy();
    expect(await screen.findByText("3084 只股票的数据停在 2026-07-14")).toBeTruthy();
    expect(await screen.findByText(/2026-09-07 仅有 74 行/)).toBeTruthy();
    expect(await screen.findByText(/市值 1222 只停在 2026-09-04/)).toBeTruthy();
  });

  it("cancels a running capital-flow backfill job and refreshes coverage", async () => {
    const user = setupUser();
    const onCoverageChange = vi.fn();
    const refreshedCoverage = [
      { dataset: "daily_bars", symbols: 1, start_date: "2024-01-02", end_date: "2024-01-03", missing_rows: 2, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-03", missing_rows: 0, suspension_rows: 0 }
    ];
    apiMocks.fetchCapitalFlow.mockResolvedValue({
      status: "ok",
      imported_rows: 0,
      returned_rows: 0,
      requested_symbols: [],
      fetched_symbols: [],
      missing_symbols: [],
      skipped_symbols: [],
      coverage,
      logs: [{ level: "info", message: "Capital-flow backfill started for all symbols" }],
      diagnostics: [{ code: "capital_flow_backfill_job_started", source: "capital_flow_crawler" }],
      failures: [],
      job: {
        job_id: "flow-job",
        mode: "capital_flow_backfill",
        status: "running",
        total_symbols: 3,
        completed_symbols: 1,
        failed_symbols: 0,
        processed_symbols: 1,
        skipped_symbols: 0,
        imported_rows: 5,
        returned_rows: 8,
        current_symbol: "000002",
        start_date: "2026-06-01",
        end_date: "2026-06-05",
        errors: []
      }
    });
    apiMocks.cancelSyncJob.mockResolvedValue({
      job: {
        job_id: "flow-job",
        mode: "capital_flow_backfill",
        status: "cancelling",
        total_symbols: 3,
        completed_symbols: 1,
        failed_symbols: 0,
        processed_symbols: 1,
        skipped_symbols: 0,
        imported_rows: 5,
        returned_rows: 8,
        current_symbol: "000002",
        start_date: "2026-06-01",
        end_date: "2026-06-05",
        errors: []
      }
    });
    apiMocks.loadSyncJob.mockResolvedValueOnce({
      job: {
        job_id: "flow-job",
        mode: "capital_flow_backfill",
        status: "cancelled",
        total_symbols: 3,
        completed_symbols: 1,
        failed_symbols: 0,
        processed_symbols: 1,
        skipped_symbols: 0,
        imported_rows: 5,
        returned_rows: 8,
        current_symbol: null,
        start_date: "2026-06-01",
        end_date: "2026-06-05",
        errors: []
      }
    });
    apiMocks.loadDataServiceHealth
      .mockResolvedValueOnce({ ok: true, cache_path: "C:\\cache", port: 9011, coverage })
      .mockResolvedValueOnce({ ok: true, cache_path: "C:\\cache", port: 9011, coverage: refreshedCoverage });

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={onCoverageChange} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.click(screen.getByRole("button", { name: "补齐资金流" }));

    expect(await screen.findByRole("button", { name: "停止任务" })).toBeInTheDocument();
    expect(screen.getByText(/接口返回 8 行，写入 5 行/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "停止任务" }));

    await waitFor(() => expect(apiMocks.cancelSyncJob).toHaveBeenCalledWith("http://127.0.0.1:9011", "flow-job"));
    expect(screen.getByRole("status", { name: "数据中心状态" })).toHaveTextContent("正在停止任务");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });

    await waitFor(() => expect(onCoverageChange).toHaveBeenLastCalledWith(refreshedCoverage));
    expect(screen.getByRole("status", { name: "数据中心状态" })).toHaveTextContent("资金流补齐已停止");
  });

  it("shows recent per-symbol failures for a running capital-flow backfill job", async () => {
    const user = setupUser();
    const runningJob = {
      job_id: "flow-job-failures",
      mode: "capital_flow_backfill",
      status: "running",
      total_symbols: 5,
      completed_symbols: 2,
      failed_symbols: 2,
      processed_symbols: 4,
      skipped_symbols: 0,
      imported_rows: 12,
      returned_rows: 20,
      current_symbol: "000005",
      start_date: "2026-06-01",
      end_date: "2026-06-05",
      errors: [],
      last_error: "000004 date coverage shortfall",
      recent_failures: [
        { symbol: "000003", code: "network_error", error: "remote disconnected" },
        { symbol: "000004", code: "date_coverage_shortfall", message: "only returned 2026-06-05" }
      ]
    };
    apiMocks.fetchCapitalFlow.mockResolvedValue({
      status: "ok",
      imported_rows: 0,
      returned_rows: 0,
      requested_symbols: [],
      fetched_symbols: [],
      missing_symbols: [],
      skipped_symbols: [],
      coverage,
      logs: [{ level: "info", message: "Capital-flow backfill started for all symbols" }],
      diagnostics: [{ code: "capital_flow_backfill_job_started", source: "capital_flow_crawler" }],
      failures: [],
      job: runningJob
    });
    apiMocks.loadSyncJob.mockResolvedValue({
      job: runningJob
    });

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.click(screen.getByRole("button", { name: "补齐资金流" }));

    const failures = await screen.findByLabelText("最近失败原因");
    expect(within(failures).getByText(/000003/)).toHaveTextContent("network_error");
    expect(within(failures).getByText(/000003/)).toHaveTextContent("remote disconnected");
    expect(within(failures).getByText(/000004/)).toHaveTextContent("date_coverage_shortfall");
    expect(within(failures).getByText(/000004/)).toHaveTextContent("only returned 2026-06-05");
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows immediate busy feedback for data operations", async () => {
    const user = setupUser();
    let resolveFetch: (value: Awaited<ReturnType<typeof apiMocks.fetchDailyBars>>) => void = () => {};
    apiMocks.fetchDailyBars.mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve;
      })
    );

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.type(screen.getByLabelText("股票代码"), "600519");
    await user.click(screen.getByRole("button", { name: "补全缺失数据" }));

    expect(screen.getByRole("button", { name: "正在补全缺失数据" })).toBeDisabled();
    expect(screen.getByRole("status", { name: "数据中心状态" })).toHaveTextContent("正在补全缺失数据");

    resolveFetch({
      status: "ok",
      imported_rows: 3,
      requested_symbols: ["600519"],
      fetched_symbols: ["600519"],
      missing_symbols: [],
      coverage,
      logs: [{ level: "info", message: "Fetched 3 daily bar rows" }]
    });

    expect(await screen.findByText("Fetched 3 daily bar rows")).toBeInTheDocument();
  });

  it("starts a full-market sync job and shows progress", async () => {
    const user = setupUser();
    apiMocks.startFullMarketSync.mockResolvedValue({
      job: {
        job_id: "job-1",
        mode: "full_market_bootstrap",
        status: "running",
        total_symbols: 2,
        completed_symbols: 1,
        failed_symbols: 0,
        imported_rows: 10,
        current_symbol: "000002",
        start_date: "2015-01-01",
        end_date: "2026-05-26",
        errors: []
      }
    });

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.click(screen.getByRole("button", { name: "下载全市场历史数据" }));

    expect(screen.getByText(/当前 000002/)).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });
    await waitFor(() => expect(apiMocks.loadSyncJob).toHaveBeenCalledWith("http://127.0.0.1:9011", "job-1"));
    await waitFor(() => expect(screen.getByRole("status", { name: "数据中心状态" })).toHaveTextContent("全市场下载完成"));
    expect(screen.getByRole("status", { name: "数据中心状态" })).toHaveTextContent("写入 20 行");
  });

  it("uses a recent business-day range and moves the date inputs after successful fetch coverage", async () => {
    const user = setupUser();
    const updatedCoverage = [
      { dataset: "daily_bars", symbols: 1, start_date: "2024-01-02", end_date: "2026-06-05", missing_rows: 0, suspension_rows: 0 }
    ];
    apiMocks.fetchDailyBars.mockResolvedValue({
      status: "ok",
      imported_rows: 5,
      requested_symbols: ["600519"],
      fetched_symbols: ["600519"],
      missing_symbols: [],
      coverage: updatedCoverage,
      logs: [{ level: "info", message: "Fetched 5 recent daily bar rows" }]
    });
    const onCoverageChange = vi.fn();

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={onCoverageChange} />);

    expect(await screen.findByLabelText("开始日期")).toHaveValue("2026-06-01");
    expect(screen.getByLabelText("结束日期")).toHaveValue("2026-06-05");
    await user.type(screen.getByLabelText("股票代码"), "600519");
    await user.click(screen.getByRole("button", { name: "补全缺失数据" }));

    await waitFor(() => expect(apiMocks.fetchDailyBars).toHaveBeenCalledWith(
      "http://127.0.0.1:9011",
      ["600519"],
      "2026-06-01",
      "2026-06-05"
    ));
    expect(onCoverageChange).toHaveBeenCalledWith(updatedCoverage);
    expect(screen.getByLabelText("结束日期")).toHaveValue("2026-06-05");
    expect(await screen.findByText("Fetched 5 recent daily bar rows")).toBeInTheDocument();
  });

  it("uses the latest A-share trading day instead of a market holiday for default fills", async () => {
    const user = setupUser();
    vi.setSystemTime(new Date("2026-06-19T10:00:00+08:00"));

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    expect(await screen.findByLabelText("开始日期")).toHaveValue("2026-06-12");
    expect(screen.getByLabelText("结束日期")).toHaveValue("2026-06-18");
    await user.click(screen.getByRole("button", { name: "补全缺失数据" }));

    await waitFor(() => expect(apiMocks.startFullMarketSync).toHaveBeenCalledWith(
      "http://127.0.0.1:9011",
      "2026-06-12",
      "2026-06-18"
    ));
  });

  it("fills from the local coverage end date to the latest open day when coverage is stale", async () => {
    const user = setupUser();
    apiMocks.loadDataServiceHealth.mockResolvedValue({
      ok: true,
      cache_path: "C:\\cache",
      port: 9011,
      coverage: staleRecentCoverage
    });
    apiMocks.loadDailyBarsCoverage.mockResolvedValue({
      summary: staleRecentCoverage,
      items: []
    });

    render(<DataCenter cacheDir=".astock-cache" coverage={staleRecentCoverage} onCoverageChange={vi.fn()} />);

    expect(await screen.findByLabelText("开始日期")).toHaveValue("2026-05-26");
    expect(screen.getByLabelText("结束日期")).toHaveValue("2026-06-05");
    await user.type(screen.getByLabelText("股票代码"), "600519");
    await user.click(screen.getByRole("button", { name: "补全缺失数据" }));

    await waitFor(() => expect(apiMocks.fetchDailyBars).toHaveBeenCalledWith(
      "http://127.0.0.1:9011",
      ["600519"],
      "2026-05-26",
      "2026-06-05"
    ));
  });

  it("keeps manually edited dates aligned with coverage details after a successful fetch", async () => {
    const user = setupUser();
    const updatedCoverage = [
      { dataset: "daily_bars", symbols: 5000, start_date: "2015-01-05", end_date: "2026-06-05", missing_rows: 0, suspension_rows: 0 }
    ];
    apiMocks.fetchDailyBars.mockResolvedValue({
      status: "ok",
      imported_rows: 12,
      requested_symbols: ["600519"],
      fetched_symbols: ["600519"],
      missing_symbols: [],
      coverage: updatedCoverage,
      logs: [{ level: "info", message: "Fetched manual date range rows" }]
    });

    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.type(screen.getByLabelText("股票代码"), "600519");
    await user.clear(screen.getByLabelText("开始日期"));
    await user.type(screen.getByLabelText("开始日期"), "2026-05-26");
    await user.click(screen.getByRole("button", { name: "补全缺失数据" }));

    await waitFor(() => expect(apiMocks.fetchDailyBars).toHaveBeenCalledWith(
      "http://127.0.0.1:9011",
      ["600519"],
      "2026-05-26",
      "2026-06-05"
    ));
    expect(screen.getByLabelText("开始日期")).toHaveValue("2026-05-26");
    await waitFor(() => expect(apiMocks.loadDailyBarsCoverage).toHaveBeenLastCalledWith(
      "http://127.0.0.1:9011",
      ["600519"],
      "2026-05-26",
      "2026-06-05"
    ));
  });

  it("覆盖表 missing_rows 只能来自刷新后的真实仓库 coverage，绝不能用本次 imported_rows 抵扣", async () => {
    // §9 红线（被踩过两次）：fetch 报告 imported_rows=3，但刷新后的真实 coverage
    // 仍有 missing_rows=2——覆盖表必须按仓库口径显示 2。旧实现曾用本次
    // imported_rows 抵扣缺失数，出现"任务没补完却显示缺失为 0"的错误。
    const user = setupUser();
    const onCoverageChange = vi.fn();
    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={onCoverageChange} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.type(screen.getByLabelText("股票代码"), "600519");
    await user.click(screen.getByRole("button", { name: "补全缺失数据" }));

    await waitFor(() => expect(apiMocks.fetchDailyBars).toHaveBeenCalled());
    expect(onCoverageChange).toHaveBeenCalledWith(coverage);

    // 覆盖表按 coverage 原样展示：日线缺失 2（不被 3 行 imported 抵扣成 0）
    const dailyRow = screen.getByText("日线行情", { selector: "strong" }).closest("tr");
    expect(dailyRow).not.toBeNull();
    expect(within(dailyRow!).getByText("2")).toBeTruthy();
    expect(within(dailyRow!).queryByText("0")).toBeNull();
  });

  it("「只补缺口」入口以缺口名单发起，完成后显示缺口数量", async () => {
    apiMocks.startMissingOnlySync.mockResolvedValue({
      started: true,
      missing_symbols: 3,
      start_date: "2026-05-26",
      end_date: "2026-06-05"
    });
    const user = setupUser();
    render(<DataCenter cacheDir=".astock-cache" coverage={coverage} onCoverageChange={vi.fn()} />);

    await screen.findByText(/http:\/\/127\.0\.0\.1:9011/);
    await user.click(screen.getByRole("button", { name: "只补缺口" }));

    await waitFor(() => expect(apiMocks.startMissingOnlySync).toHaveBeenCalledWith(
      "http://127.0.0.1:9011",
      expect.any(String),
      expect.any(String)
    ));
  });
});
