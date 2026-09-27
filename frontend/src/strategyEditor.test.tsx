import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "./App";

const apiMocks = vi.hoisted(() => ({
  ensureDataService: vi.fn(),
  fetchDailyBars: vi.fn(),
  importDailyBars: vi.fn(),
  loadDataServiceHealth: vi.fn(),
  loadDataServiceLogs: vi.fn(),
  loadDailyBarsCoverage: vi.fn(),
  loadMarketBriefing: vi.fn(),
  loadClsFinance: vi.fn(),
  loadMarketNews: vi.fn(),
  loadNewsSummary: vi.fn(),
  loadRealtimeMarketSnapshot: vi.fn(),
  loadRealtimeMarketSnapshotStream: vi.fn(),
  loadRecommendedStrategies: vi.fn(),
  loadRiskAlerts: vi.fn(),
  loadSyncJob: vi.fn(),
  runBacktestStreamWithDataService: vi.fn(),
  runConfiguredBacktest: vi.fn(),
  validateConditionExpression: vi.fn(),
  validateStockSymbols: vi.fn()
}));

// Spread the real api module so newly added exports (e.g. BackendError) stay
// available to the component under test; mocked functions always win.
vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...apiMocks
}));

const demoResult = {
  metrics: {
    total_return_pct: 0.032,
    annualized_return_pct: 0.032,
    max_drawdown_pct: -0.018,
    win_rate_pct: 0.6,
    trade_count: 1,
    average_trade_return_pct: 0.011,
    average_position_pct: 0.48,
    max_position_pct: 0.48
  },
  equity_curve: [
    { trade_date: "2024-01-02", equity: 100000, cash: 100000, market_value: 0, drawdown_pct: 0 },
    { trade_date: "2024-01-08", equity: 103200, cash: 103200, market_value: 0, drawdown_pct: 0 }
  ],
  trades: [
    {
      symbol: "AAA",
      buy_signal_date: "2024-01-04",
      buy_date: "2024-01-05",
      sell_date: "2024-01-08",
      buy_price: 12,
      sell_price: 10.2,
      shares: 4000,
      planned_amount: 50000,
      buy_amount: 48000,
      sell_amount: 40800,
      target_position_pct: 0.5,
      actual_position_pct: 0.48,
      buy_reason: [
        "float market cap 8000000000 in [1000000000, 30000000000]",
        "3d main net inflow 6000000 >= 3000000",
        "2d volume ratio 1.20 in [1.00, 2.50]",
        "turnover 4.00% in [2.00%, 8.00%]",
        "MACD histogram 0.0310 >= 0.0000",
        "5d return 8.00% in [0.00%, 20.00%]",
        "close 12.00 broke prior 20d high 11.80"
      ],
      sell_reason: ["fixed holding days reached"],
      pnl: -7200,
      pnl_pct: -0.15
    }
  ],
  preflight_issues: []
};

async function flushAsyncEffects(): Promise<void> {
  await act(async () => {
    for (let index = 0; index < 10; index += 1) {
      await Promise.resolve();
    }
  });
}

// 手工条件编辑器（校验→添加→模板）已收进"高级模式"折叠区，默认收起；
// 交互前先展开，语义与折叠前保持一致。
async function openAdvancedMode(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  const toggle = screen.getByRole("button", { name: /高级模式/ });
  if (toggle.getAttribute("aria-expanded") === "false") {
    await user.click(toggle);
  }
}

describe("A 股回测工作台界面", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    apiMocks.ensureDataService.mockResolvedValue({
      running: true,
      port: 9010,
      base_url: "http://127.0.0.1:9010",
      cache_dir: ".astock-cache",
      message: "browser preview uses mock local service"
    });
    apiMocks.loadDailyBarsCoverage.mockResolvedValue({
      items: []
    });
    apiMocks.validateStockSymbols.mockResolvedValue({
      ok: true,
      valid_symbols: ["600519", "000001"],
      invalid_symbols: [],
      normalized_symbols: ["600519", "000001"],
      source: "local-warehouse"
    });
    apiMocks.loadDataServiceHealth.mockResolvedValue({
      ok: true,
      cache_path: "C:\\cache",
      port: 9010,
      coverage: [
        { dataset: "daily_bars", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-08", missing_rows: 0 }
      ]
    });
    apiMocks.loadDataServiceLogs.mockResolvedValue({ items: [] });
    apiMocks.loadRealtimeMarketSnapshot.mockResolvedValue({
      status: "live",
      source: "ashare-sina",
      updated_at: "2026-05-27T10:30:00Z",
      indexes: [
        {
          symbol: "sh000001",
          name: "上证指数",
          last: 3120.5,
          previous_close: 3100,
          change: 20.5,
          change_pct: 0.0066129,
          source: "ashare-sina",
          updated_at: "2026-05-27T10:30:00Z"
        },
        {
          symbol: "sz399001",
          name: "深证成指",
          last: 9800.2,
          previous_close: 9700,
          change: 100.2,
          change_pct: 0.0103298,
          source: "ashare-sina",
          updated_at: "2026-05-27T10:30:00Z"
        }
      ],
      breadth: { up: 3200, down: 1700, flat: 200, total: 5100, source: "local-latest" },
      strong_sectors: [
        { name: "半导体", change_pct: 0.036, leading_symbol: "688001", source: "local-latest" },
        { name: "电力设备", change_pct: 0.024, leading_symbol: "300750", source: "local-latest" }
      ],
      yesterday_strong_sectors: [
        { name: "半导体", change_pct: 0.031, leading_symbol: "688001", source: "local-yesterday-group" },
        { name: "机器人", change_pct: 0.022, leading_symbol: "300024", source: "local-yesterday-group" }
      ],
      message: "实时行情已更新"
    });
    apiMocks.loadRealtimeMarketSnapshotStream.mockImplementation(async (_baseUrl, handlers = {}) => {
      const snapshot = await apiMocks.loadRealtimeMarketSnapshot();
      handlers.onSnapshot?.(snapshot);
      return snapshot;
    });
    apiMocks.loadMarketNews.mockResolvedValue({
      updated_at: "2026-05-27T10:30:00Z",
      source: "eastmoney",
      items: [
        {
          title: "政策利好推动科技板块走强",
          summary: "半导体、AI 应用方向盘中活跃。",
          source: "东方财富",
          published_at: "2026-05-27T10:20:00Z",
          url: "https://example.test/news",
          tags: ["科技", "政策"],
          sentiment: "positive"
        }
      ]
    });
    apiMocks.loadMarketBriefing.mockImplementation((_baseUrl, kind) =>
      Promise.resolve({
        kind,
        updated_at: "2026-05-27T10:30:00Z",
        source: kind === "fupan" ? "ths-fupan" : "ths-zaopan",
        source_url:
          kind === "fupan" ? "https://stock.10jqka.com.cn/fupan/" : "https://stock.10jqka.com.cn/zaopan/",
        summary: kind === "fupan" ? "同花顺复盘摘要" : "同花顺早盘摘要",
        sections: [],
        diagnostics: []
      })
    );
    apiMocks.loadClsFinance.mockResolvedValue({
      updated_at: "2026-06-09T07:05:00Z",
      source: "cls-finance",
      source_url: "https://www.cls.cn/finance",
      preclose_px: 3959.337,
      tline: [
        { date: 20260609, minute: 930, last_px: 3977.539, change: 0.0047 },
        { date: 20260609, minute: 1500, last_px: 4015.5, change: 0.0142 }
      ],
      anchors: [
        { code: "cls80025", name: "PCB", article_id: 2394344, c_time: "2026-06-09 09:31:30", direction: "up", url: "https://www.cls.cn/plate?code=cls80025" },
        { code: "cls80081", name: "油气设服", article_id: 2394352, c_time: "2026-06-09 09:39:24", direction: "down", url: "https://www.cls.cn/plate?code=cls80081" }
      ],
      emotion: {
        market_degree: 56,
        shsz_balance: "2.64万亿",
        shsz_balance_change: "-1524亿",
        up_limit: 130,
        open_limit: 25,
        performance: "1.74%",
        breadth: { up: 3322, down: 2049, flat: 156, total: 5527, source: "cls-finance-emotion", distribution: { suspend: 12 } }
      },
      up_pool: [
        {
          symbol: "601869",
          name: "长飞光纤",
          change_pct: 0.1,
          last: 484.33,
          time: "2026-06-09 13:34:47",
          reason: "光纤|全球光纤光缆行业领先企业。",
          limit_up_days: 1,
          plates: [{ code: "cls81670", name: "光纤光缆", change_pct: 0.0393 }]
        }
      ],
      diagnostics: []
    });
    apiMocks.loadNewsSummary.mockResolvedValue({
      updated_at: "2026-05-27T10:30:00Z",
      source: "test-summary",
      item_count: 1,
      themes: [
        {
          title: "科技",
          summary: "科技方向消息集中。",
          sentiment: "positive",
          source_count: 1,
          headlines: ["政策利好推动科技板块走强"]
        }
      ],
      highlights: ["政策利好推动科技板块走强"],
      risks: ["高位题材分化加快。"],
      diagnostics: []
    });
    apiMocks.loadRiskAlerts.mockResolvedValue({
      updated_at: "2026-05-27T10:30:00Z",
      source: "adata",
      diagnostics: [],
      items: [
        {
          symbol: "000001",
          name: "*ST示例",
          risk_type: "ST风险",
          reason: "股票名称包含 *ST，存在退市风险警示。",
          severity: "high",
          source: "adata",
          detected_at: "2026-05-27T10:30:00Z"
        }
      ]
    });
    apiMocks.loadRecommendedStrategies.mockResolvedValue({
      items: [
        {
          id: "volume-breakout",
          name: "放量突破",
          description: "价格突破前高并伴随量能放大。",
          suitable_market: "指数温和上行、题材活跃时使用。",
          risk_note: "避免连续大涨后追高。",
          example_conditions: ["突破20日新高", "量比2日介于1.2到2.5"],
          strategy: {
            name: "放量突破",
            market_filters: [],
            entry_groups: [
              {
                id: "entry",
                operator: "and",
                conditions: [
                  {
                    id: "preset-breakout",
                    condition_id: "breakout_above_n_day_high",
                    enabled: true,
                    params: { window: 20 },
                    data_lag_days: 0,
                    expression: "突破20日新高"
                  }
                ]
              }
            ],
            exit_rules: [],
            score_threshold: null
          }
        }
      ]
    });
    apiMocks.validateConditionExpression.mockImplementation(async (_baseUrl, text, mode) => {
      if (text === "收盘价站上20日均线") {
        return {
          ok: true,
          normalized_text: text,
          errors: [],
          examples: ["收盘价站上20日均线"],
          condition: {
            id: "custom-close-above-ma",
            condition_id: "close_above_ma",
            enabled: true,
            params: { window: 20 },
            data_lag_days: 0,
            expression: text
          }
        };
      }
      if (text === "流通市值50亿到300亿") {
        return {
          ok: true,
          normalized_text: text,
          errors: [],
          examples: ["流通市值10亿到300亿", "量比2日介于1.2到2.5"],
          condition: {
            id: "custom-market-cap",
            condition_id: "market_cap_between",
            enabled: true,
            params: { min: 5000000000, max: 30000000000 },
            data_lag_days: 0,
            expression: text
          }
        };
      }
      if (text === "量比2日介于1.2到2.5") {
        return {
          ok: true,
          normalized_text: text,
          errors: [],
          examples: ["流通市值10亿到300亿", "量比2日介于1.2到2.5"],
          condition: {
            id: "custom-volume-ratio",
            condition_id: "volume_ratio_between",
            enabled: true,
            params: { window: 2, min: 1.2, max: 2.5 },
            data_lag_days: 0,
            expression: text
          }
        };
      }
      if (text === "突破20日最低" && mode === "exit") {
        return {
          ok: true,
          normalized_text: text,
          errors: [],
          examples: ["收盘价跌破3日均线", "跌破20日低点", "突破20日最低"],
          condition: {
            id: "custom-breakdown-low",
            condition_id: "breakdown_below_n_day_low",
            enabled: true,
            params: { window: 20 },
            data_lag_days: 0,
            expression: text
          }
        };
      }
      return {
        ok: false,
        normalized_text: text,
        condition: null,
        errors: [{ code: "unrecognized_condition", message: "无法识别条件，请参考样例改写。" }],
        examples: ["收盘价站上20日均线"]
      };
    });
    apiMocks.loadSyncJob.mockResolvedValue({
      job: {
        job_id: "preview",
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
    apiMocks.fetchDailyBars.mockResolvedValue({
      status: "ok",
      imported_rows: 0,
      requested_symbols: [],
      fetched_symbols: [],
      missing_symbols: [],
      coverage: [],
      logs: []
    });
    apiMocks.importDailyBars.mockResolvedValue({
      status: "ok",
      imported_rows: 0,
      coverage: [],
      logs: []
    });
    apiMocks.runBacktestStreamWithDataService.mockImplementation(async (_baseUrl, _strategy, _settings, handlers) => {
      handlers.onPhase("校验参数");
      handlers.onPhase("读取本地数据");
      handlers.onTrade(demoResult.trades[0]);
      handlers.onResult(demoResult);
      return demoResult;
    });
    apiMocks.runConfiguredBacktest.mockResolvedValue(demoResult);
  });

  it("renders the Chinese workstation areas", async () => {
    const user = userEvent.setup();
    render(<App />);

    await openAdvancedMode(user);

    expect(screen.getByRole("heading", { name: "A股策略回测工作台" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "检查更新" })).toBeInTheDocument();
    expect(screen.getByText(/当前版本/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "数据中心" })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /基础配置/ })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /AI 推荐/ })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "回测范围" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "写入条件" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "收益概览" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "交易明细" })).toBeInTheDocument();
    expect(screen.getAllByText("市场热度").length).toBeGreaterThan(0);
    expect(screen.getAllByText("大盘评分").length).toBeGreaterThan(0);
    expect(screen.getAllByText("风险提示").length).toBeGreaterThan(0);
    expect(await screen.findByText("今日实时行情")).toBeInTheDocument();
    expect(await screen.findByText("上证指数")).toBeInTheDocument();
    expect(screen.getByText("红 3200")).toBeInTheDocument();
    expect(screen.getByText("绿 1700")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "财联社看盘" })).toBeInTheDocument();
    expect(screen.getAllByText("半导体").length).toBeGreaterThan(0);
    expect(await screen.findByRole("heading", { name: "资讯与事件" })).toBeInTheDocument();
    expect(screen.getAllByText("政策利好推动科技板块走强").length).toBeGreaterThan(0);
    expect(await screen.findByText("日线行情")).toBeInTheDocument();
  });

  it("不会让慢速推荐策略阻塞其他首屏行情板块", async () => {
    apiMocks.loadRecommendedStrategies.mockImplementation(() => new Promise(() => undefined));

    render(<App />);

    await waitFor(() => expect(apiMocks.loadClsFinance).toHaveBeenCalled());
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("PCB")).toBeInTheDocument();
    expect(screen.getAllByText("政策利好推动科技板块走强").length).toBeGreaterThan(0);
  });

  it("会独立重试失败的推荐策略，并在覆盖更新后重新读取", async () => {
    vi.useRealTimers();
    vi.useFakeTimers();
    vi.clearAllMocks();
    let releaseHealth: () => void = () => {};
    apiMocks.loadDataServiceHealth.mockImplementationOnce(() => new Promise((resolve) => {
      releaseHealth = () => resolve({
        ok: true,
        cache_path: "C:\\cache",
        port: 9010,
        coverage: [
          { dataset: "daily_bars", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-08", missing_rows: 0 }
        ]
      });
    }));
    apiMocks.loadRecommendedStrategies.mockRejectedValueOnce(new Error("recommendations starting"));

    render(<App />);

    await flushAsyncEffects();
    expect(apiMocks.loadRecommendedStrategies).toHaveBeenCalledTimes(1);
    expect(apiMocks.loadMarketNews).toHaveBeenCalledTimes(1);
    expect(apiMocks.loadClsFinance).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_999);
    });
    expect(apiMocks.loadRecommendedStrategies).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    await flushAsyncEffects();
    expect(apiMocks.loadRecommendedStrategies).toHaveBeenCalledTimes(2);

    await act(async () => {
      releaseHealth();
      await Promise.resolve();
    });
    await flushAsyncEffects();
    expect(apiMocks.loadRecommendedStrategies).toHaveBeenCalledTimes(3);
  });

  it("在昨日追踪运行时保持正常轮询，并在后台刷新期间保留完整快照", async () => {
    vi.useRealTimers();
    vi.useFakeTimers();
    vi.clearAllMocks();
    const initialSnapshot = {
      status: "live",
      source: "test",
      updated_at: "2026-05-27T10:30:00Z",
      market_phase: "trading",
      indexes: [
        {
          symbol: "sh000001",
          name: "上证指数",
          last: 3120.5,
          previous_close: 3100,
          change: 20.5,
          change_pct: 0.0066129,
          source: "test",
          updated_at: "2026-05-27T10:30:00Z"
        }
      ],
      breadth: { up: 3200, down: 1700, flat: 200, total: 5100, source: "test" },
      strong_sectors: [{ name: "半导体", change_pct: 0.036, leading_symbol: "688001", source: "test" }],
      yesterday_strong_sectors: [],
      message: "昨日涨停板块仍在加载",
      diagnostics: ["eastmoney-yesterday-limit-up tracking refresh scheduled in background."]
    };
    const partialSnapshot = {
      ...initialSnapshot,
      status: "stale",
      source: "stream-partial",
      breadth: null,
      strong_sectors: [],
      message: "实时指数已返回，继续加载红绿家数和板块"
    };
    const completedSnapshot = {
      ...initialSnapshot,
      updated_at: "2026-05-27T10:31:00Z",
      breadth: { up: 3210, down: 1690, flat: 200, total: 5100, source: "test" },
      message: "实时行情已更新",
      diagnostics: []
    };
    let requestCount = 0;
    let completeSecondRefresh: (snapshot: typeof completedSnapshot) => void = () => undefined;
    const secondRefresh = new Promise<typeof completedSnapshot>((resolve) => {
      completeSecondRefresh = resolve;
    });
    apiMocks.loadRealtimeMarketSnapshot.mockResolvedValue(initialSnapshot);
    apiMocks.loadRealtimeMarketSnapshotStream.mockImplementation(async (_baseUrl, handlers = {}) => {
      requestCount += 1;
      if (requestCount === 1) {
        handlers.onSnapshot?.(initialSnapshot);
        return initialSnapshot;
      }
      handlers.onSnapshot?.(partialSnapshot);
      return secondRefresh;
    });

    render(<App />);

    await flushAsyncEffects();
    expect(apiMocks.loadRealtimeMarketSnapshotStream).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(apiMocks.loadRealtimeMarketSnapshotStream).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(57_000);
    });
    await flushAsyncEffects();
    expect(apiMocks.loadRealtimeMarketSnapshotStream).toHaveBeenCalledTimes(2);
    const market = screen.getByRole("region", { name: "今日实时行情" });
    expect(within(market).getByText("红 3200")).toBeInTheDocument();
    expect(within(market).getByText("半导体")).toBeInTheDocument();
    expect(within(market).queryByText("刷新中")).not.toBeInTheDocument();

    await act(async () => {
      completeSecondRefresh(completedSnapshot);
      await Promise.resolve();
    });
    await flushAsyncEffects();
    expect(within(market).getByText("红 3210")).toBeInTheDocument();
  });

  it("retries failed independent first-screen modules after three seconds without duplicate requests", async () => {
    vi.useRealTimers();
    vi.useFakeTimers();
    vi.clearAllMocks();
    apiMocks.loadMarketNews.mockRejectedValueOnce(new Error("news starting"));
    apiMocks.loadClsFinance.mockRejectedValueOnce(new Error("finance starting"));
    apiMocks.loadNewsSummary.mockRejectedValueOnce(new Error("summary starting"));
    apiMocks.loadRiskAlerts.mockRejectedValueOnce(new Error("risk starting"));
    const failedBriefings = new Set(["fupan", "zaopan"]);
    apiMocks.loadMarketBriefing.mockImplementation((_baseUrl, kind) => {
      if (failedBriefings.delete(kind)) {
        return Promise.reject(new Error(`${kind} starting`));
      }
      return Promise.resolve({
        kind,
        updated_at: "2026-05-27T10:30:00Z",
        source: "retry-test",
        summary: "",
        sections: [],
        diagnostics: []
      });
    });

    render(<App />);

    await flushAsyncEffects();
    expect(apiMocks.loadMarketNews).toHaveBeenCalledTimes(1);
    expect(apiMocks.loadMarketBriefing).toHaveBeenCalledTimes(2);
    expect(apiMocks.loadClsFinance).toHaveBeenCalledTimes(1);
    expect(apiMocks.loadNewsSummary).toHaveBeenCalledTimes(1);
    expect(apiMocks.loadRiskAlerts).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_999);
    });
    expect(apiMocks.loadMarketNews).toHaveBeenCalledTimes(1);
    expect(apiMocks.loadMarketBriefing).toHaveBeenCalledTimes(2);
    expect(apiMocks.loadClsFinance).toHaveBeenCalledTimes(1);
    expect(apiMocks.loadNewsSummary).toHaveBeenCalledTimes(1);
    expect(apiMocks.loadRiskAlerts).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    await flushAsyncEffects();
    expect(apiMocks.loadMarketNews).toHaveBeenCalledTimes(2);
    expect(apiMocks.loadMarketBriefing).toHaveBeenCalledTimes(4);
    expect(apiMocks.loadClsFinance).toHaveBeenCalledTimes(2);
    expect(apiMocks.loadNewsSummary).toHaveBeenCalledTimes(2);
    expect(apiMocks.loadRiskAlerts).toHaveBeenCalledTimes(2);
  });

  it("aborts the active realtime stream when the workspace unmounts", async () => {
    let requestSignal: AbortSignal | undefined;
    apiMocks.loadRealtimeMarketSnapshotStream.mockImplementation(
      async (_baseUrl, _handlers, options) => new Promise((_resolve, reject) => {
        requestSignal = options?.signal;
        requestSignal?.addEventListener(
          "abort",
          () => reject(new Error("stream request cancelled")),
          { once: true }
        );
      })
    );

    const { unmount } = render(<App />);

    await waitFor(() => expect(requestSignal).toBeDefined());
    expect(requestSignal?.aborted).toBe(false);
    unmount();

    expect(requestSignal?.aborted).toBe(true);
  });

  it("shows the Tonghuashun score in the overview band instead of a capital-flow card", async () => {
    apiMocks.loadClsFinance.mockResolvedValueOnce({
      updated_at: "2026-06-09T07:05:00Z",
      source: "cls-finance",
      source_url: "https://www.cls.cn/finance",
      tline: [],
      anchors: [],
      emotion: {
        market_degree: 7.3,
        market_degree_source: "ths-market-summary",
        market_degree_label: "同花顺大盘评级",
        up_limit: 130,
        open_limit: 25
      },
      up_pool: [],
      diagnostics: ["同花顺大盘评分读取成功：7.3"]
    });

    render(<App />);

    await screen.findByText("日线行情");
    await screen.findAllByText("7.3");
    const overview = screen.getByLabelText("工作台概览");
    expect(within(overview).getByText("大盘评分")).toBeInTheDocument();
    expect(within(overview).getByText("7.3")).toBeInTheDocument();
    expect(within(overview).getByText("同花顺大盘评级")).toBeInTheDocument();
    expect(within(overview).queryByText("资金流向")).not.toBeInTheDocument();
    expect(within(overview).queryByText("待导入")).not.toBeInTheDocument();
  });

  it("uses A-share red for high Tonghuashun score and green for low score in the overview band", async () => {
    apiMocks.loadClsFinance.mockResolvedValueOnce({
      updated_at: "2026-06-09T07:05:00Z",
      source: "cls-finance",
      source_url: "https://www.cls.cn/finance",
      tline: [],
      anchors: [],
      emotion: {
        market_degree: 6.7,
        market_degree_source: "ths-market-summary",
        market_degree_label: "同花顺大盘评级",
        up_limit: 130,
        open_limit: 25
      },
      up_pool: [],
      diagnostics: []
    });

    const { unmount } = render(<App />);

    await screen.findByText("日线行情");
    await screen.findAllByText("6.7");
    const highScoreCard = screen.getByLabelText("工作台概览").querySelector(".market-degree-card");
    expect(highScoreCard).toHaveClass("market-degree-card-high");
    expect(within(highScoreCard as HTMLElement).getByText("6.7")).toHaveClass("up-text");

    unmount();
    vi.clearAllMocks();
    apiMocks.ensureDataService.mockResolvedValue({
      running: true,
      port: 9010,
      base_url: "http://127.0.0.1:9010",
      cache_dir: ".astock-cache",
      message: "browser preview uses mock local service"
    });
    apiMocks.loadDataServiceHealth.mockResolvedValue({
      ok: true,
      cache_path: "C:\\cache",
      port: 9010,
      coverage: [
        { dataset: "daily_bars", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-08", missing_rows: 0 }
      ]
    });
    apiMocks.loadDataServiceLogs.mockResolvedValue({ items: [] });
    apiMocks.loadDailyBarsCoverage.mockResolvedValue({ items: [] });
    apiMocks.loadRealtimeMarketSnapshot.mockResolvedValue({
      status: "live",
      source: "test",
      updated_at: "2026-05-27T10:30:00Z",
      indexes: [],
      breadth: { up: 1200, down: 3600, flat: 200, total: 5000, source: "test" },
      strong_sectors: [],
      yesterday_strong_sectors: [],
      message: "test"
    });
    apiMocks.loadRealtimeMarketSnapshotStream.mockImplementation(async (_baseUrl, handlers = {}) => {
      const snapshot = await apiMocks.loadRealtimeMarketSnapshot();
      handlers.onSnapshot?.(snapshot);
      return snapshot;
    });
    apiMocks.loadMarketNews.mockResolvedValue({ updated_at: "2026-05-27T10:30:00Z", source: "test", items: [] });
    apiMocks.loadMarketBriefing.mockImplementation((_baseUrl, kind) =>
      Promise.resolve({ kind, updated_at: "2026-05-27T10:30:00Z", source: "test", summary: "", sections: [], diagnostics: [] })
    );
    apiMocks.loadNewsSummary.mockResolvedValue({ updated_at: "2026-05-27T10:30:00Z", source: "test", item_count: 0, themes: [], highlights: [], risks: [], diagnostics: [] });
    apiMocks.loadRiskAlerts.mockResolvedValue({ updated_at: "2026-05-27T10:30:00Z", source: "test", diagnostics: [], items: [] });
    apiMocks.loadRecommendedStrategies.mockResolvedValue({ items: [] });
    apiMocks.loadClsFinance.mockResolvedValueOnce({
      updated_at: "2026-06-09T07:05:00Z",
      source: "cls-finance",
      source_url: "https://www.cls.cn/finance",
      tline: [],
      anchors: [],
      emotion: {
        market_degree: 3.2,
        market_degree_source: "ths-market-summary",
        market_degree_label: "同花顺大盘评级",
        up_limit: 40,
        open_limit: 5
      },
      up_pool: [],
      diagnostics: []
    });

    render(<App />);

    await waitFor(() => {
      const lowScoreCard = screen.getByLabelText("工作台概览").querySelector(".market-degree-card");
      expect(lowScoreCard).toHaveClass("market-degree-card-low");
      expect(within(lowScoreCard as HTMLElement).getByText("3.2")).toHaveClass("down-text");
    });
  });

  it("does not use CLS market heat as the Tonghuashun overview score fallback", async () => {
    apiMocks.loadClsFinance.mockResolvedValueOnce({
      updated_at: "2026-06-09T07:05:00Z",
      source: "cls-finance",
      source_url: "https://www.cls.cn/finance",
      tline: [],
      anchors: [],
      emotion: {
        market_degree: 56,
        market_degree_source: "cls-finance-emotion",
        market_degree_label: "财联社市场热度",
        up_limit: 130,
        open_limit: 25
      },
      up_pool: [],
      diagnostics: []
    });

    render(<App />);

    await screen.findByText("日线行情");
    await waitFor(() => expect(apiMocks.loadClsFinance).toHaveBeenCalled());
    const overview = screen.getByLabelText("工作台概览");
    const scoreCard = overview.querySelector(".market-degree-card");
    expect(scoreCard).not.toHaveTextContent("56.0");
    expect(within(scoreCard as HTMLElement).getByText("--")).toBeInTheDocument();
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("exposes common A-share strategy conditions in Chinese", async () => {
    const user = userEvent.setup();
    render(<App />);

    await openAdvancedMode(user);

    expect(screen.getAllByText("流通市值区间").length).toBeGreaterThan(0);
    expect(screen.getAllByText("近N日主力净流入").length).toBeGreaterThan(0);
    expect(screen.getAllByText("MACD柱线下限").length).toBeGreaterThan(0);
    expect(screen.getAllByText("量比区间").length).toBeGreaterThan(0);
    expect(screen.getAllByText("换手率区间").length).toBeGreaterThan(0);
    expect(screen.getAllByText("前期涨幅区间").length).toBeGreaterThan(0);
    expect(screen.getAllByText("突破前高形态").length).toBeGreaterThan(0);
    expect(await screen.findByText("日线行情")).toBeInTheDocument();
  });

  it("lets the user write and validate Chinese strategy conditions before running a backtest", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    await user.clear(screen.getByLabelText("新增条件表达式"));
    await user.type(screen.getByLabelText("新增条件表达式"), "流通市值50亿到300亿");
    await user.click(screen.getByRole("button", { name: "校验条件" }));
    await screen.findByText(/可识别：流通市值区间/);
    await user.click(screen.getByRole("button", { name: "添加已校验条件" }));
    await user.clear(screen.getByLabelText("新增条件表达式"));
    await user.type(screen.getByLabelText("新增条件表达式"), "量比2日介于1.2到2.5");
    await user.click(screen.getByRole("button", { name: "校验条件" }));
    await screen.findByText(/可识别：量比区间/);
    await user.click(screen.getByRole("button", { name: "添加已校验条件" }));
    await user.clear(screen.getByLabelText("初始资金"));
    await user.type(screen.getByLabelText("初始资金"), "250000");
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(apiMocks.runBacktestStreamWithDataService).toHaveBeenCalledWith(
      "http://127.0.0.1:9010",
      expect.objectContaining({
        name: "市场热度 + 市值量价筛选",
        entry_groups: [
          expect.objectContaining({
            conditions: expect.arrayContaining([
              expect.objectContaining({
                condition_id: "market_cap_between",
                params: expect.objectContaining({ min: 5000000000, max: 30000000000 })
              }),
              expect.objectContaining({
                condition_id: "volume_ratio_between",
                params: expect.objectContaining({ window: 2, min: 1.2, max: 2.5 })
              })
            ])
          })
        ]
      }),
      expect.objectContaining({ initial_cash: 250000 }),
      expect.objectContaining({
        onPhase: expect.any(Function),
        onProgress: expect.any(Function),
        onTrade: expect.any(Function),
        onResult: expect.any(Function)
      })
    );
    expect(apiMocks.runConfiguredBacktest).not.toHaveBeenCalled();
    expect(await screen.findByText("交易次数 1")).toBeInTheDocument();
    expect(screen.getAllByText(/流通市值/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/主力净流入/).length).toBeGreaterThan(0);
    expect(screen.getByText(/2d 量比 1\.20 位于区间/)).toBeInTheDocument();
    expect(screen.getByText(/换手率 4\.00% 位于区间/)).toBeInTheDocument();
    expect(screen.getByText(/MACD柱线 0\.0310 >= 0\.0000/)).toBeInTheDocument();
    expect(screen.getByText(/5d 前期涨幅 8\.00% 位于区间/)).toBeInTheDocument();
    expect(screen.getByText(/收盘价 12\.00 突破前 20d 高点 11\.80/)).toBeInTheDocument();
    expect(screen.queryByText(/volume ratio|turnover|MACD histogram|return|prior high/)).not.toBeInTheDocument();
  },
  // 这条用例走的是整个工作台：两次条件校验 + 一次流式回测 + userEvent 逐字输入。
  // 串行与不带 coverage 时都在 5 秒内，只有 33 个文件并行 + V8 coverage 插桩会被拉到
  // 5 秒以上（本机实测），因此给它单独的时间上限，断言本身不放宽。
  20_000);

  it("does not add duplicate entry conditions with the same parsed parameters", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    await user.clear(screen.getByLabelText("新增条件表达式"));
    await user.type(screen.getByLabelText("新增条件表达式"), "流通市值50亿到300亿");
    await user.click(screen.getByRole("button", { name: "校验条件" }));
    await screen.findByText(/可识别：流通市值区间/);
    await user.click(screen.getByRole("button", { name: "添加已校验条件" }));
    await user.click(screen.getByRole("button", { name: "添加已校验条件" }));

    expect(screen.getByText("该入场条件已存在，不会重复加入。")).toBeInTheDocument();
    const marketCapRules = screen.getAllByText(/条件：流通市值50亿到300亿/);
    expect(marketCapRules).toHaveLength(1);
  });

  it("places clickable entry templates under entry rules without the less-than-12 template", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    const entryRules = screen.getByRole("heading", { name: "入场规则" }).closest(".entry-rules-panel");
    expect(entryRules).not.toBeNull();
    expect(entryRules).toHaveTextContent("入场条件模板");
    expect(entryRules).toHaveTextContent("点击可套用");
    expect(entryRules).toHaveTextContent("收盘价站上20日均线");
    expect(entryRules).toHaveTextContent("近5日涨幅0%到12%");
    expect(entryRules).not.toHaveTextContent("近5日涨幅小于12%");

    await user.click(within(entryRules as HTMLElement).getByRole("button", { name: "套用入场条件：收盘价站上20日均线" }));
    expect(screen.getByLabelText("新增条件表达式")).toHaveValue("收盘价站上20日均线");
  });

  it("keeps exit templates beside the exit rule editor and applies them to the exit input", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    const exitRules = screen.getByRole("heading", { name: "离场规则" }).closest(".exit-rules-panel");
    expect(exitRules).not.toBeNull();
    expect(exitRules).toHaveTextContent("离场条件模板");
    expect(exitRules).toHaveTextContent("点击可套用");
    expect(exitRules).toHaveTextContent("收盘价跌破3日均线");
    expect(exitRules).toHaveTextContent("跌破20日低点");
    expect(exitRules).toHaveTextContent("近5日涨幅小于3%");
    expect(exitRules).toHaveTextContent("MACD死叉");
    expect(exitRules).toHaveTextContent("近3日主力净流出");
    expect(exitRules).not.toHaveTextContent("近5日涨幅0%到12%");

    await user.click(within(exitRules as HTMLElement).getByRole("button", { name: "套用离场条件：MACD死叉" }));
    expect(screen.getByLabelText("新增离场条件表达式")).toHaveValue("MACD死叉");
  });

  it("labels position size as a single-stock cap", async () => {
    render(<App />);

    await screen.findByText("日线行情");

    expect(screen.getByLabelText("个股仓位上限（%）")).toBeInTheDocument();
    expect(screen.queryByLabelText("总仓位上限（%）")).not.toBeInTheDocument();
  });

  it("uses controlled selectors to build strategy and backtest settings", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    await user.click(screen.getByRole("button", { name: "套用数据中心日期" }));
    await user.selectOptions(screen.getByLabelText("股票池"), "custom");
    await user.type(screen.getByLabelText("自选代码"), "600519,000001");
    await user.clear(screen.getByLabelText("新增条件表达式"));
    await user.type(screen.getByLabelText("新增条件表达式"), "收盘价站上20日均线");
    await user.click(screen.getByRole("button", { name: "校验条件" }));
    await screen.findByText(/可识别：收盘价站上均线/);
    await user.click(screen.getByRole("button", { name: "添加已校验条件" }));
    await user.selectOptions(screen.getByLabelText("组合方式"), "or");
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(apiMocks.runBacktestStreamWithDataService).toHaveBeenCalledWith(
      "http://127.0.0.1:9010",
      expect.objectContaining({
        entry_groups: [
          expect.objectContaining({
            operator: "or",
            conditions: expect.arrayContaining([
              expect.objectContaining({
                condition_id: "close_above_ma",
                params: expect.objectContaining({ window: 20 })
              })
            ])
          })
        ]
      }),
      expect.objectContaining({
        start_date: "2024-01-02",
        end_date: "2024-01-08",
        stock_pool: "custom",
        custom_symbols: ["600519", "000001"]
      }),
      expect.objectContaining({
        onPhase: expect.any(Function),
        onProgress: expect.any(Function),
        onTrade: expect.any(Function),
        onResult: expect.any(Function)
      })
    );
  });

  it("blocks unrecognized custom condition text before adding it to the strategy", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    await user.clear(screen.getByLabelText("新增条件表达式"));
    await user.type(screen.getByLabelText("新增条件表达式"), "随便乱写条件");
    await user.click(screen.getByRole("button", { name: "校验条件" }));

    expect(await screen.findByText(/无法识别条件/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "添加已校验条件" })).toBeDisabled();
  });

  it("uses expression-only strategy conditions without parameter selectors", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);

    expect(screen.getByLabelText("新增条件表达式")).toBeInTheDocument();
    expect(screen.queryByLabelText("新增条件")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("窗口")).not.toBeInTheDocument();
    expect(screen.getAllByText("收盘价站上20日均线").length).toBeGreaterThan(0);
    expect(screen.getAllByText("量比2日介于1.2到2.5").length).toBeGreaterThan(0);
  });

  it("opens risk alerts from the risk prompt and shows concrete risky stocks", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await user.click(screen.getByRole("button", { name: "查看风险清单" }));

    expect(await screen.findByRole("dialog", { name: "风险股票清单" })).toBeInTheDocument();
    expect(screen.getByText("*ST示例")).toBeInTheDocument();
    expect(screen.getByText("股票名称包含 *ST，存在退市风险警示。")).toBeInTheDocument();
    expect(screen.getByLabelText("风险股票滚动列表")).toBeInTheDocument();
  });

  it("opens risk alerts by clicking the top risk summary card", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await user.click(screen.getByRole("button", { name: /查看全市场风险提示/ }));

    expect(await screen.findByRole("dialog", { name: "风险股票清单" })).toBeInTheDocument();
    expect(screen.getByText("*ST示例")).toBeInTheDocument();
  });

  it("shows risk diagnostics when no current risky stocks are returned", async () => {
    const user = userEvent.setup();
    apiMocks.loadRiskAlerts.mockResolvedValue({
      updated_at: "2026-05-27T10:30:00Z",
      source: "local",
      diagnostics: ["东方财富风险源不可用，已使用本地 ST 字段兜底。"],
      items: []
    });
    render(<App />);

    await screen.findByText("日线行情");
    await user.click(screen.getByRole("button", { name: "查看风险清单" }));

    expect(await screen.findByText("暂无明确风险股票")).toBeInTheDocument();
    expect(screen.getByText("东方财富风险源不可用，已使用本地 ST 字段兜底。")).toBeInTheDocument();
  });

  it("applies a recommended strategy before running the backtest", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await user.click(screen.getByRole("tab", { name: /AI 推荐/ }));
    await user.click(await screen.findByRole("button", { name: "套用放量突破" }));
    // 套用后自动切回基础配置，直接从那里运行回测
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(apiMocks.runBacktestStreamWithDataService).toHaveBeenCalledWith(
      "http://127.0.0.1:9010",
      expect.objectContaining({ name: "放量突破" }),
      expect.any(Object),
      expect.objectContaining({
        onPhase: expect.any(Function),
        onProgress: expect.any(Function),
        onTrade: expect.any(Function),
        onResult: expect.any(Function)
      })
    );
  });

  it("shows run progress and updates the top summary cards from the result", async () => {
    const user = userEvent.setup();
    let resolveBacktest: (value: typeof demoResult) => void = () => {};
    apiMocks.runBacktestStreamWithDataService.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveBacktest = (value) => resolve(value);
        })
    );
    render(<App />);

    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(screen.getByRole("button", { name: "回测运行中" })).toBeDisabled();
    expect(screen.getByText(/校验参数/)).toBeInTheDocument();
    expect(screen.getByText(/读取本地数据/)).toBeInTheDocument();
    expect(screen.queryByText("尚未运行回测")).not.toBeInTheDocument();
    expect(screen.getByLabelText("股票池")).toBeDisabled();
    resolveBacktest(demoResult);
    expect(await screen.findByText("交易次数 1")).toBeInTheDocument();
    expect(screen.getByText("3.20%")).toBeInTheDocument();
    expect(screen.getByText(/1 笔交易/)).toBeInTheDocument();
  });

  it("appends streamed trades before the final backtest result", async () => {
    const user = userEvent.setup();
    let emitTrade: (() => void) | null = null;
    let finish: (() => void) | null = null;
    apiMocks.runBacktestStreamWithDataService.mockImplementation(
      (_baseUrl, _strategy, _settings, handlers) =>
        new Promise((resolve) => {
          handlers.onPhase("校验参数");
          emitTrade = () => handlers.onTrade(demoResult.trades[0]);
          finish = () => {
            handlers.onResult(demoResult);
            resolve(demoResult);
          };
        })
    );

    render(<App />);
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(screen.getByText("暂无交易记录")).toBeInTheDocument();
    act(() => {
      emitTrade?.();
    });
    expect(await screen.findByText("AAA")).toBeInTheDocument();
    act(() => {
      finish?.();
    });
    expect(await screen.findByText("交易次数 1")).toBeInTheDocument();
  });

  it("shows scan progress and opened trades from the stream before final result", async () => {
    const user = userEvent.setup();
    let emitProgress: (() => void) | null = null;
    let emitOpened: (() => void) | null = null;
    let finish: (() => void) | null = null;
    const openedTrade = { ...demoResult.trades[0], sell_date: null, sell_price: null, pnl: null, pnl_pct: null };
    apiMocks.runBacktestStreamWithDataService.mockImplementation(
      (_baseUrl, _strategy, _settings, handlers) =>
        new Promise((resolve) => {
          handlers.onPhase("校验参数");
          emitProgress = () => handlers.onProgress({ message: "扫描 2024-01-05：候选 2 只，持仓 1 只" });
          emitOpened = () => handlers.onTrade(openedTrade);
          finish = () => {
            handlers.onResult(demoResult);
            resolve(demoResult);
          };
        })
    );

    render(<App />);
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    act(() => {
      emitProgress?.();
      emitOpened?.();
    });
    expect(await screen.findByText(/扫描 2024-01-05/)).toBeInTheDocument();
    // 流式 trade 现在按 120ms 合帧批量进表（避免每条一次全树渲染），推进定时器
    // 触发一次 flush，而不是假设 setState 同步生效。
    await act(async () => {
      vi.advanceTimersByTime(200);
    });
    expect(screen.getAllByText("持仓中").length).toBeGreaterThan(0);
    expect(screen.getByText("AAA")).toBeInTheDocument();
    act(() => {
      finish?.();
    });
    expect(await screen.findByText("交易次数 1")).toBeInTheDocument();
  });

  it("keeps blocked trade reasons from the stream after the final backtest result", async () => {
    const user = userEvent.setup();
    let emitBlocked: (() => void) | null = null;
    let finish: (() => void) | null = null;
    const blockedTrade = {
      ...demoResult.trades[0],
      shares: 0,
      buy_amount: 0,
      actual_position_pct: 0,
      sell_date: null,
      sell_price: null,
      sell_amount: null,
      pnl: null,
      pnl_pct: null,
      blocked_reason: "次日开盘接近涨停，未买入：AAA"
    };
    apiMocks.runBacktestStreamWithDataService.mockImplementation(
      (_baseUrl, _strategy, _settings, handlers) =>
        new Promise((resolve) => {
          handlers.onPhase("校验参数");
          emitBlocked = () => handlers.onTrade(blockedTrade);
          finish = () => {
            handlers.onResult({ ...demoResult, trades: [] });
            resolve({ ...demoResult, trades: [] });
          };
        })
    );

    render(<App />);
    await screen.findByText("日线行情");
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));
    await waitFor(() => {
      expect(apiMocks.runBacktestStreamWithDataService).toHaveBeenCalled();
      expect(emitBlocked).not.toBeNull();
    });

    act(() => {
      emitBlocked?.();
    });
    expect(await screen.findByText("次日开盘接近涨停，未买入：AAA")).toBeInTheDocument();

    act(() => {
      finish?.();
    });
    expect(await screen.findByText("回测完成，已生成收益曲线和交易明细。")).toBeInTheDocument();
    expect(screen.getByText("次日开盘接近涨停，未买入：AAA")).toBeInTheDocument();
  });

  it("uses editable funding and matching inputs with examples instead of fixed selectors", async () => {
    render(<App />);

    await screen.findByText("日线行情");

    expect(screen.getByText("样例：100000")).toBeInTheDocument();
    expect(screen.getByText("样例：3")).toBeInTheDocument();
    expect(screen.getByLabelText("初始资金").tagName).toBe("INPUT");
    expect(screen.getByLabelText("固定持仓天数").tagName).toBe("INPUT");
  });

  it("lets the user set the single-stock position cap and shows position usage in the trades table", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByRole("heading", { name: "A股策略回测工作台" });
    await user.selectOptions(screen.getByLabelText("仓位模式"), "fixed_ratio");
    await user.clear(screen.getByLabelText("个股仓位上限（%）"));
    await user.type(screen.getByLabelText("个股仓位上限（%）"), "20");
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(apiMocks.runBacktestStreamWithDataService).toHaveBeenCalledWith(
      "http://127.0.0.1:9010",
      expect.any(Object),
      expect.objectContaining({
        position_sizing_mode: "fixed_ratio",
        position_size_pct: 0.2
      }),
      expect.objectContaining({
        onPhase: expect.any(Function),
        onProgress: expect.any(Function),
        onTrade: expect.any(Function),
        onResult: expect.any(Function)
      })
    );
    expect(await screen.findByText("仓位")).toBeInTheDocument();
    expect(screen.getAllByText(/48\.00%/).length).toBeGreaterThanOrEqual(3);
    expect(screen.getByText(/4\.80万/)).toBeInTheDocument();
    expect(screen.getByText(/平均仓位 48\.00%/)).toBeInTheDocument();
    expect(screen.getByText(/最大仓位 48\.00%/)).toBeInTheDocument();
    expect(screen.getByText(/4000 股/)).toBeInTheDocument();
  });

  it("uses full-market risk alerts and realtime breadth in the top summary", async () => {
    apiMocks.loadRiskAlerts.mockResolvedValue({
      updated_at: "2026-05-27T10:30:00Z",
      source: "adata",
      diagnostics: [],
      items: Array.from({ length: 12 }, (_, index) => ({
        symbol: `${index + 1}`.padStart(6, "0"),
        name: `*ST风险${index + 1}`,
        risk_type: "ST风险",
        reason: "股票名称包含 ST，存在风险警示。",
        severity: "high",
        source: "adata",
        detected_at: "2026-05-27T10:30:00Z"
      }))
    });

    render(<App />);

    await screen.findByText("日线行情");

    expect(await screen.findByText("62.75%")).toBeInTheDocument();
    expect(screen.getAllByText(/3200/).length).toBeGreaterThan(0);
    expect(screen.getByText("12项")).toBeInTheDocument();
  });

  it("labels stale local breadth as non-realtime in the top summary", async () => {
    apiMocks.loadRealtimeMarketSnapshot.mockResolvedValue({
      status: "stale",
      source: "local-latest",
      updated_at: "2026-05-26T07:30:00Z",
      indexes: [
        {
          symbol: "local-market",
          name: "本地全市场",
          last: 12.5,
          previous_close: 12.3,
          change: 0.2,
          change_pct: 0.0162601,
          source: "local-latest",
          updated_at: "2026-05-26T07:30:00Z"
        }
      ],
      breadth: { up: 3200, down: 1700, flat: 200, total: 5100, source: "local-latest" },
      strong_sectors: [{ name: "半导体", change_pct: 0.036, leading_symbol: "688001", source: "local-market-group" }],
      yesterday_strong_sectors: [],
      message: "实时行情源暂不可用，已使用本地最近交易日 2026-05-26 数据。",
      diagnostics: ["已使用本地最近交易日 2026-05-26 作为兜底快照。"]
    });

    render(<App />);

    expect(await screen.findByText("本地最近交易日/非实时 红盘 3200 / 样本 5100")).toBeInTheDocument();
    expect(screen.queryByText(/今日实时红盘 3200/)).not.toBeInTheDocument();
  });

  it("labels live breadth as realtime even when the breadth source is local-latest", async () => {
    apiMocks.loadRealtimeMarketSnapshot.mockResolvedValue({
      status: "live",
      source: "ashare-sina+local",
      updated_at: "2026-05-27T07:30:00Z",
      indexes: [
        {
          symbol: "sh000001",
          name: "上证指数",
          last: 3120.5,
          previous_close: 3100,
          change: 20.5,
          change_pct: 0.0066129,
          source: "ashare-sina",
          updated_at: "2026-05-27T07:30:00Z"
        }
      ],
      breadth: { up: 1545, down: 3600, flat: 62, total: 5207, source: "local-latest" },
      strong_sectors: [],
      yesterday_strong_sectors: [],
      message: "实时红绿家数已返回"
    });

    render(<App />);

    expect(await screen.findByText("今日实时红盘 1545 / 全市场 5207")).toBeInTheDocument();
    expect(screen.queryByText(/本地最近交易日\/非实时 红盘 1545/)).not.toBeInTheDocument();
  });

  it("shows structured market data and independent commentary after market close", async () => {
    apiMocks.loadRealtimeMarketSnapshot.mockResolvedValue({
      status: "live",
      source: "ashare-sina+local+ths-concept-section",
      updated_at: "2026-05-27T07:30:00Z",
      indexes: [
        {
          symbol: "sh000001",
          name: "上证指数",
          last: 3120.5,
          previous_close: 3100,
          change: 20.5,
          change_pct: 0.0066129,
          source: "ashare-sina",
          updated_at: "2026-05-27T07:30:00Z"
        }
      ],
      breadth: { up: 3200, down: 1700, flat: 200, total: 5100, source: "local-latest" },
      strong_sectors: [
        { name: "半导体", change_pct: 0.036, leading_symbol: "688001", source: "ths-concept-section" },
        { name: "电力设备", change_pct: 0.024, leading_symbol: "300750", source: "ths-concept-section" }
      ],
      yesterday_strong_sectors: [],
      message: "实时行情已更新"
    });

    render(<App />);

    expect(await screen.findByText("财联社看盘")).toBeInTheDocument();
    expect((await screen.findAllByText("市场热度")).length).toBeGreaterThan(0);
    expect(await screen.findByText("涨停 130")).toBeInTheDocument();
    expect(screen.getByText("PCB")).toBeInTheDocument();
    expect(screen.queryByText(/收盘后板块解读/)).not.toBeInTheDocument();
  });

  it("shows an empty CLS finance state when the finance API fails", async () => {
    apiMocks.loadClsFinance.mockRejectedValue(new Error("finance upstream timeout"));

    render(<App />);

    const panel = await screen.findByRole("region", { name: "财联社看盘" });
    await waitFor(() => expect(panel).toHaveTextContent("暂无财联社看盘"));
    expect(panel).not.toHaveTextContent("行情评价");
    expect(panel).not.toHaveTextContent("明日观察");
  });

  it("tracks yesterday's strong sectors in the market panel and commentary", async () => {
    apiMocks.loadRealtimeMarketSnapshot.mockResolvedValue({
      status: "live",
      source: "ashare-sina+local+ths-concept-section+local-yesterday-group",
      updated_at: "2026-05-27T02:30:00Z",
      indexes: [
        {
          symbol: "sh000001",
          name: "上证指数",
          last: 3120.5,
          previous_close: 3100,
          change: 20.5,
          change_pct: 0.0066129,
          source: "ashare-sina",
          updated_at: "2026-05-27T02:30:00Z"
        }
      ],
      breadth: { up: 3200, down: 1700, flat: 200, total: 5100, source: "local-latest" },
      strong_sectors: [
        { name: "半导体", change_pct: 0.036, leading_symbol: "688001", source: "ths-concept-section" },
        { name: "电力设备", change_pct: 0.024, leading_symbol: "300750", source: "ths-concept-section" }
      ],
      yesterday_strong_sectors: [
        { name: "机器人", change_pct: 0.041, leading_symbol: "300024", source: "local-yesterday-group" },
        { name: "半导体", change_pct: 0.031, leading_symbol: "688001", source: "local-yesterday-group" }
      ],
      message: "实时行情已更新；昨日强势板块追踪来自本地历史。"
    });

    render(<App />);

    expect(await screen.findByText("昨日强势追踪")).toBeInTheDocument();
    expect((await screen.findAllByText(/机器人/)).length).toBeGreaterThan(0);
    expect(await screen.findByText("财联社看盘")).toBeInTheDocument();
    expect(screen.getAllByText(/半导体/).length).toBeGreaterThan(0);
  });

  it("uses red for rising indexes and green for falling indexes", async () => {
    apiMocks.loadRealtimeMarketSnapshot.mockResolvedValue({
      status: "live",
      source: "test",
      updated_at: new Date("2026-05-27T10:30:00+08:00").toISOString(),
      indexes: [
        {
          symbol: "sh000001",
          name: "涓婅瘉鎸囨暟",
          last: 3120.5,
          previous_close: 3100,
          change: 20.5,
          change_pct: 0.0066129,
          source: "test",
          updated_at: new Date("2026-05-27T10:30:00+08:00").toISOString()
        },
        {
          symbol: "sz399001",
          name: "娣辫瘉鎴愭寚",
          last: 9600,
          previous_close: 9700,
          change: -100,
          change_pct: -0.010309,
          source: "test",
          updated_at: new Date("2026-05-27T10:30:00+08:00").toISOString()
        }
      ],
      breadth: { up: 1200, down: 3600, flat: 200, total: 5000, source: "test" },
      strong_sectors: [],
      yesterday_strong_sectors: [],
      message: "test"
    });

    render(<App />);

    expect(await screen.findByText("+0.66% / 20.5")).toHaveClass("up-text");
    expect(await screen.findByText("-1.03% / -100")).toHaveClass("down-text");
  });

  it("lets the user validate and add exit rules so sell logic is not hidden", async () => {
    const user = userEvent.setup();
    apiMocks.validateConditionExpression.mockImplementation(async (_baseUrl, text) => {
      if (text === "收盘价跌破3日均线") {
        return {
          ok: true,
          normalized_text: text,
          errors: [],
          examples: ["收盘价跌破3日均线"],
          condition: {
            id: "custom-close-below-ma",
            condition_id: "close_below_ma",
            enabled: true,
            params: { window: 3 },
            data_lag_days: 0,
            expression: text
          }
        };
      }
      return {
        ok: false,
        normalized_text: text,
        condition: null,
        errors: [{ code: "unrecognized_condition", message: "无法识别条件，请参考样例改写。" }],
        examples: ["收盘价跌破3日均线"]
      };
    });
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    expect(screen.getByRole("heading", { name: "离场规则" })).toBeInTheDocument();
    expect(screen.getByText(/固定持仓 3 天/)).toBeInTheDocument();
    await user.clear(screen.getByLabelText("新增离场条件表达式"));
    await user.type(screen.getByLabelText("新增离场条件表达式"), "收盘价跌破3日均线");
    await user.click(screen.getByRole("button", { name: "校验离场条件" }));
    await screen.findByText(/离场可识别/);
    await user.click(screen.getByRole("button", { name: "添加离场条件" }));
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(apiMocks.runBacktestStreamWithDataService).toHaveBeenCalledWith(
      "http://127.0.0.1:9010",
      expect.objectContaining({
        exit_rules: expect.arrayContaining([
          expect.objectContaining({
            condition_id: "close_below_ma",
            params: expect.objectContaining({ window: 3 }),
            expression: "收盘价跌破3日均线"
          })
        ])
      }),
      expect.any(Object),
      expect.objectContaining({
        onPhase: expect.any(Function),
        onProgress: expect.any(Function),
        onTrade: expect.any(Function),
        onResult: expect.any(Function)
      })
    );
  });

  it("offers an inline save action after entry and exit rules run successfully, then stores it in 策略配置", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm");
    const savedName = "市场热度 + 市值量价筛选";
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    await user.clear(screen.getByLabelText("新增条件表达式"));
    await user.type(screen.getByLabelText("新增条件表达式"), "收盘价站上20日均线");
    await user.click(screen.getByRole("button", { name: "校验条件" }));
    await screen.findByText(/可识别：收盘价站上均线/);
    await user.click(screen.getByRole("button", { name: "添加已校验条件" }));

    await user.clear(screen.getByLabelText("新增离场条件表达式"));
    await user.type(screen.getByLabelText("新增离场条件表达式"), "突破20日最低");
    await user.click(screen.getByRole("button", { name: "校验离场条件" }));
    await screen.findByText(/离场可识别：跌破前低离场/);
    await user.click(screen.getByRole("button", { name: "添加离场条件" }));

    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(await screen.findByText("回测完成，可将当前入场与离场规则保存到策略配置。")).toBeInTheDocument();
    expect(screen.getByText(`建议名称：${savedName}`)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存策略" }));
    expect(await screen.findByText(`已保存策略：${savedName}`)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /AI 推荐/ }));
    expect(screen.getByRole("heading", { name: "已保存策略" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: `套用已保存策略${savedName}` })).toBeInTheDocument();
    expect(screen.getByText(savedName)).toBeInTheDocument();

    const savedRaw = window.localStorage.getItem("astock-saved-strategies");
    expect(savedRaw).not.toBeNull();
    const savedStrategies = JSON.parse(savedRaw ?? "[]") as Array<{ name: string }>;
    expect(savedStrategies).toHaveLength(1);
    expect(savedStrategies[0]?.name).toBe(savedName);

    await user.click(screen.getByRole("button", { name: "套用放量突破" }));
    await user.click(screen.getByRole("button", { name: `套用已保存策略${savedName}` }));
    expect(await screen.findByText(`已套用已保存策略：${savedName}`)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /AI 推荐/ }));

    await user.click(screen.getByRole("button", { name: `删除已保存策略${savedName}` }));
    expect(await screen.findByText(`已删除已保存策略：${savedName}`)).toBeInTheDocument();
  }, 15_000);

  it("does not store the strategy when the user dismisses the inline save prompt", async () => {
    const user = userEvent.setup();
    const confirmSpy = vi.spyOn(window, "confirm");
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    await user.clear(screen.getByLabelText("新增条件表达式"));
    await user.type(screen.getByLabelText("新增条件表达式"), "收盘价站上20日均线");
    await user.click(screen.getByRole("button", { name: "校验条件" }));
    await screen.findByText(/可识别：收盘价站上均线/);
    await user.click(screen.getByRole("button", { name: "添加已校验条件" }));

    await user.clear(screen.getByLabelText("新增离场条件表达式"));
    await user.type(screen.getByLabelText("新增离场条件表达式"), "突破20日最低");
    await user.click(screen.getByRole("button", { name: "校验离场条件" }));
    await screen.findByText(/离场可识别：跌破前低离场/);
    await user.click(screen.getByRole("button", { name: "添加离场条件" }));

    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(await screen.findByText("回测完成，可将当前入场与离场规则保存到策略配置。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "暂不保存" }));
    expect(await screen.findByText("本次未保存策略，你可以继续调整后再次运行。")).toBeInTheDocument();
    expect(window.localStorage.getItem("astock-saved-strategies")).toBeNull();
    await user.click(screen.getByRole("tab", { name: /AI 推荐/ }));
    expect(screen.getByRole("heading", { name: "已保存策略" })).toBeInTheDocument();
  });

  it("caps strategy backtest date inputs at today when coverage ends in the future", async () => {
    vi.setSystemTime(new Date("2026-05-30T10:00:00+08:00"));
    apiMocks.loadDataServiceHealth.mockResolvedValue({
      ok: true,
      cache_path: "C:\\cache",
      port: 9010,
      coverage: [
        { dataset: "daily_bars", symbols: 2, start_date: "2024-01-02", end_date: "2026-12-31", missing_rows: 0 }
      ]
    });

    render(<App />);

    await screen.findByText("日线行情");
    const strategyStartDateInput = screen.getAllByLabelText("开始日期")[0] as HTMLInputElement;
    const strategyEndDateInput = screen.getAllByLabelText("结束日期")[0] as HTMLInputElement;

    expect(strategyStartDateInput.max).toBe("2026-05-30");
    expect(strategyEndDateInput.max).toBe("2026-05-30");
    expect(screen.getByText(/可用范围 2024-01-02 至 2026-05-30/)).toBeInTheDocument();
  });

  it("checks custom stock symbols before running and blocks unreal A-share codes", async () => {
    const user = userEvent.setup();
    apiMocks.runBacktestStreamWithDataService.mockClear();
    apiMocks.validateStockSymbols.mockResolvedValueOnce({
      ok: false,
      valid_symbols: ["600519"],
      invalid_symbols: ["999999"],
      normalized_symbols: ["600519", "999999"],
      source: "local-warehouse"
    });
    render(<App />);

    await screen.findByText("日线行情");
    await user.selectOptions(screen.getByLabelText("股票池"), "custom");
    await user.type(screen.getByLabelText("自选代码"), "600519,999999");
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(apiMocks.validateStockSymbols).toHaveBeenCalledWith("http://127.0.0.1:9010", ["600519", "999999"]);
    expect(await screen.findByRole("alert")).toHaveTextContent("自选代码包含无效股票代码：999999");
    expect(apiMocks.runBacktestStreamWithDataService).not.toHaveBeenCalled();
  });

  it("runs backtests through the latest local daily coverage date until dates are edited", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    vi.setSystemTime(new Date("2026-06-20T10:00:00+08:00"));
    apiMocks.loadDataServiceHealth.mockResolvedValue({
      ok: true,
      cache_path: "C:\\cache",
      port: 9010,
      coverage: [
        { dataset: "daily_bars", symbols: 5469, start_date: "2015-01-05", end_date: "2026-06-18", missing_rows: 2930 },
        { dataset: "market_cap", symbols: 5469, start_date: "2015-01-05", end_date: "2026-06-18", missing_rows: 48959 }
      ]
    });

    render(<App />);

    await screen.findByText("日线行情");
    await waitFor(() => expect(screen.getAllByLabelText("结束日期")[0]).toHaveValue("2026-06-18"));
    expect(screen.getAllByLabelText("开始日期")[0]).toHaveValue("2026-06-12");
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(apiMocks.runBacktestStreamWithDataService).toHaveBeenCalledWith(
      "http://127.0.0.1:9010",
      expect.any(Object),
      expect.objectContaining({
        start_date: "2026-06-12",
        end_date: "2026-06-18"
      }),
      expect.objectContaining({
        onPhase: expect.any(Function),
        onProgress: expect.any(Function),
        onTrade: expect.any(Function),
        onResult: expect.any(Function)
      })
    );
  });

  it("validates exit rules with exit-specific low-break conditions before running", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    await user.clear(screen.getByLabelText("新增离场条件表达式"));
    await user.type(screen.getByLabelText("新增离场条件表达式"), "突破20日最低");
    await user.click(screen.getByRole("button", { name: "校验离场条件" }));
    await screen.findByText(/离场可识别：跌破前低离场/);
    await user.click(screen.getByRole("button", { name: "添加离场条件" }));
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(apiMocks.validateConditionExpression).toHaveBeenCalledWith(
      "http://127.0.0.1:9010",
      "突破20日最低",
      "exit"
    );
    expect(apiMocks.runBacktestStreamWithDataService).toHaveBeenCalledWith(
      "http://127.0.0.1:9010",
      expect.objectContaining({
        exit_rules: expect.arrayContaining([
          expect.objectContaining({
            condition_id: "breakdown_below_n_day_low",
            params: expect.objectContaining({ window: 20 }),
            expression: "突破20日最低"
          })
        ])
      }),
      expect.any(Object),
      expect.objectContaining({
        onPhase: expect.any(Function),
        onProgress: expect.any(Function),
        onTrade: expect.any(Function),
        onResult: expect.any(Function)
      })
    );
  });

  it("blocks invalid backtest parameters before calling the backend", async () => {
    const user = userEvent.setup();
    apiMocks.runBacktestStreamWithDataService.mockClear();
    apiMocks.runConfiguredBacktest.mockClear();
    render(<App />);

    await screen.findByText("日线行情");
    await user.clear(screen.getByLabelText("初始资金"));
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("初始资金不能为空");
    expect(apiMocks.runBacktestStreamWithDataService).not.toHaveBeenCalled();
    expect(apiMocks.runConfiguredBacktest).not.toHaveBeenCalled();
  });

  it("explains invalid stop loss and date ranges before running", async () => {
    const user = userEvent.setup();
    apiMocks.runBacktestStreamWithDataService.mockClear();
    apiMocks.runConfiguredBacktest.mockClear();
    render(<App />);

    await screen.findByText("日线行情");
    await user.clear(screen.getByLabelText("止损比例"));
    await user.type(screen.getByLabelText("止损比例"), "5");
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("止损比例必须为负数");
    expect(apiMocks.runBacktestStreamWithDataService).not.toHaveBeenCalled();

    await user.clear(screen.getByLabelText("止损比例"));
    await user.type(screen.getByLabelText("止损比例"), "-5");
    const strategyStartDateInput = screen.getAllByLabelText("开始日期")[0];
    await user.clear(strategyStartDateInput);
    await user.type(strategyStartDateInput, "2024-01-09");
    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("开始日期不能晚于结束日期");
    expect(apiMocks.runBacktestStreamWithDataService).not.toHaveBeenCalled();
  });

  it("does not add duplicate exit rules with the same parsed parameters", async () => {
    const user = userEvent.setup();
    apiMocks.validateConditionExpression.mockImplementation(async (_baseUrl, text) => {
      if (text === "收盘价跌破3日均线") {
        return {
          ok: true,
          normalized_text: text,
          errors: [],
          examples: ["收盘价跌破3日均线"],
          condition: {
            id: "custom-close-below-ma",
            condition_id: "close_below_ma",
            enabled: true,
            params: { window: 3 },
            data_lag_days: 0,
            expression: text
          }
        };
      }
      return {
        ok: false,
        normalized_text: text,
        condition: null,
        errors: [{ code: "unrecognized_condition", message: "无法识别条件，请参考样例改写。" }],
        examples: ["收盘价跌破3日均线"]
      };
    });
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    await user.clear(screen.getByLabelText("新增离场条件表达式"));
    await user.type(screen.getByLabelText("新增离场条件表达式"), "收盘价跌破3日均线");
    await user.click(screen.getByRole("button", { name: "校验离场条件" }));
    await screen.findByText(/离场可识别/);
    await user.click(screen.getByRole("button", { name: "添加离场条件" }));

    expect(screen.getByText("该离场条件已存在，不会重复加入。")).toBeInTheDocument();
    const exitRules = screen.getByRole("heading", { name: "离场规则" }).closest(".exit-rules-panel");
    expect(exitRules).not.toBeNull();
    expect(exitRules?.textContent?.match(/卖出触发：收盘价跌破3日均线/g) ?? []).toHaveLength(1);
  });

  it("clears stale exit success messages when the next exit validation fails", async () => {
    const user = userEvent.setup();
    apiMocks.validateConditionExpression.mockImplementation(async (_baseUrl, text, mode) => {
      if (text === "MACD死叉" && mode === "exit") {
        return {
          ok: true,
          normalized_text: text,
          errors: [],
          examples: ["MACD死叉"],
          condition: {
            id: "custom-macd-dead-cross",
            condition_id: "macd_dead_cross",
            enabled: true,
            params: {},
            data_lag_days: 0,
            expression: text
          }
        };
      }
      return {
        ok: false,
        normalized_text: text,
        condition: null,
        errors: [{ code: "unrecognized_exit_condition", message: "无法识别离场条件，请参考样例改写。" }],
        examples: ["MACD死叉"]
      };
    });
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);
    await user.clear(screen.getByLabelText("新增离场条件表达式"));
    await user.type(screen.getByLabelText("新增离场条件表达式"), "MACD死叉");
    await user.click(screen.getByRole("button", { name: "校验离场条件" }));
    await screen.findByText(/离场可识别/);
    await user.click(screen.getByRole("button", { name: "添加离场条件" }));
    expect(screen.getByText("已加入离场条件。")).toBeInTheDocument();

    await user.clear(screen.getByLabelText("新增离场条件表达式"));
    await user.type(screen.getByLabelText("新增离场条件表达式"), "近五日涨幅大于100%");
    await user.click(screen.getByRole("button", { name: "校验离场条件" }));

    expect(await screen.findByText("无法识别离场条件，请参考样例改写。")).toBeInTheDocument();
    expect(screen.queryByText("已加入离场条件。")).not.toBeInTheDocument();
  });

  it("shows exit rules as readable sell rules without backend parser fields", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await openAdvancedMode(user);

    const exitRules = screen.getByRole("heading", { name: "离场规则" }).closest(".exit-rules-panel");
    expect(exitRules).not.toBeNull();
    expect(exitRules).toHaveTextContent("卖出触发：收盘价跌破3日均线");
    expect(exitRules).toHaveTextContent("均线周期: 3日");
    expect(exitRules).not.toHaveTextContent("close_below_ma");
    expect(exitRules).not.toHaveTextContent("解析参数");
    expect(exitRules).not.toHaveTextContent("window");
  });

  it("renders the entry rule editor inside the same panel layout as exit rules", async () => {
    const user = userEvent.setup();
    render(<App />);

    await openAdvancedMode(user);

    const entryHeading = await screen.findByRole("heading", { name: "入场规则" });

    const entryEditor = screen.getByLabelText("新增条件表达式").closest(".entry-rules-panel");
    expect(entryEditor).not.toBeNull();
    expect(entryEditor).toContainElement(entryHeading);
    expect(entryEditor).toHaveTextContent("入场规则");
    expect(entryEditor).toHaveTextContent("新增入场条件表达式");
    expect(entryEditor).toHaveTextContent("校验入场条件");
    expect(entryEditor).toHaveTextContent("添加入场条件");
  });

  it("keeps built-in local strategies visible without delete actions", async () => {
    const user = userEvent.setup();
    render(<App />);

    await screen.findByText("日线行情");
    await user.click(screen.getByRole("tab", { name: /AI 推荐/ }));
    expect(screen.getByRole("heading", { name: "已保存策略" })).toBeInTheDocument();

    expect(screen.getByText("基础均衡策略")).toBeInTheDocument();
    expect(screen.getByText("放量突破策略")).toBeInTheDocument();
    expect(screen.getByText("回踩均线策略")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除已保存策略基础均衡策略" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除已保存策略放量突破策略" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除已保存策略回踩均线策略" })).not.toBeInTheDocument();
  });

  it("shows backend errors without dropping the page", async () => {
    const user = userEvent.setup();
    apiMocks.runBacktestStreamWithDataService.mockRejectedValue(new Error("No cached daily bars found."));
    render(<App />);

    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("未找到已缓存的日线行情");
    await openAdvancedMode(user);
    expect(screen.getByRole("heading", { name: "写入条件" })).toBeInTheDocument();
  });

  it("uses a Chinese fallback for unrecognized backend errors", async () => {
    const user = userEvent.setup();
    apiMocks.runBacktestStreamWithDataService.mockRejectedValue(new Error("initial_cash must be > 0"));
    render(<App />);

    await user.click(screen.getByRole("button", { name: "运行历史回测" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("回测参数不合法");
    expect(screen.queryByText(/initial_cash must be > 0/)).not.toBeInTheDocument();
  });
});
