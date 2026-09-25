/**
 * Browser preview mock data for api.ts.
 *
 * Extracted from the inline `isTauriRuntime()` branches to reduce api.ts
 * cognitive complexity.  These objects are ONLY returned when the app runs
 * outside of the Tauri desktop runtime (e.g. `vite dev` in a browser).
 *
 * Keeping them in a dedicated file makes it easier to:
 *  - Keep mock shapes in sync with backend Pydantic models
 *  - Spot drift between mock data and real API responses
 *  - Reduce the visual noise in the request functions themselves
 */

import type {
  BacktestResult,
  ClsFinanceResponse,
  ConditionValidationResult,
  DailyBarsCoverageResponse,
  DataServiceHealth,
  DataServiceStatus,
  DiagnosticsDataGapsResponse,
  DiagnosticsSourcesResponse,
  FetchResult,
  ImportResult,
  MarketBriefingResponse,
  MarketCommentaryResponse,
  MarketNewsResponse,
  NewsSummaryResponse,
  RealtimeMarketSnapshot,
  RecommendedStrategiesResponse,
  RiskAlertsResponse,
  StockSymbolValidationResult,
  SyncJobStatus
} from "./types";

export const demoBacktestResult: BacktestResult = {
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
    { trade_date: "2024-01-05", equity: 102300, cash: 51000, market_value: 51300, drawdown_pct: 0 },
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
      buy_reason: ["float market cap 8800000000 in [1000000000, 30000000000]", "3d main net inflow 6000000 >= 3000000"],
      sell_reason: ["fixed holding days reached"],
      blocked_reason: null,
      pnl: -7200,
      pnl_pct: -0.15
    }
  ],
  latest_strategy_matches: {
    signal_date: "2024-01-04",
    trade_date: "2024-01-04",
    matches: [
      {
        symbol: "AAA",
        name: "示例股份",
        close: 12,
        change_pct: 0.021,
        reasons: ["float market cap 8800000000 in [1000000000, 30000000000]", "3d main net inflow 6000000 >= 3000000"],
        signal_date: "2024-01-04",
        trade_date: "2024-01-04",
        rank_score: 1.4
      }
    ]
  },
  preflight_issues: []
};

export function mockDataServiceStatus(cacheDir: string): DataServiceStatus {
  return {
    running: true,
    port: 9010,
    base_url: "http://127.0.0.1:9010",
    cache_dir: cacheDir,
    message: "browser preview uses mock local service"
  };
}

export function mockDataServiceHealth(): DataServiceHealth {
  return {
    ok: true,
    cache_path: ".astock-cache",
    port: 9010,
    coverage: [
      { dataset: "daily_bars", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-08", missing_rows: 0, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-08", missing_rows: 0, suspension_rows: 0 },
      { dataset: "market_cap", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-08", missing_rows: 0, suspension_rows: 0 }
    ]
  };
}

export function mockDataServiceLogs() {
  return { items: [{ level: "info" as const, message: "browser preview uses mock local service" }] };
}

export function mockRealtimeMarketSnapshot(): RealtimeMarketSnapshot {
  return {
    status: "live",
    source: "browser-preview",
    updated_at: new Date("2026-05-27T10:30:00+08:00").toISOString(),
    indexes: [
      {
        symbol: "sh000001",
        name: "上证指数",
        last: 3120.5,
        previous_close: 3100,
        change: 20.5,
        change_pct: 0.0066,
        source: "browser-preview",
        updated_at: new Date("2026-05-27T10:30:00+08:00").toISOString()
      },
      {
        symbol: "sz399001",
        name: "深证成指",
        last: 9800.2,
        previous_close: 9700,
        change: 100.2,
        change_pct: 0.0103,
        source: "browser-preview",
        updated_at: new Date("2026-05-27T10:30:00+08:00").toISOString()
      }
    ],
    breadth: { up: 3200, down: 1700, flat: 200, total: 5100, source: "browser-preview" },
    strong_sectors: [
      { name: "半导体", change_pct: 0.036, leading_symbol: "688001", source: "browser-preview" },
      { name: "电力设备", change_pct: 0.024, leading_symbol: "300750", source: "browser-preview" }
    ],
    yesterday_strong_sectors: [
      { name: "半导体", change_pct: 0.031, leading_symbol: "688001", source: "browser-preview" },
      { name: "机器人", change_pct: 0.022, leading_symbol: "300024", source: "browser-preview" }
    ],
    message: "浏览器预览实时行情"
  };
}

export function mockMarketNews(): MarketNewsResponse {
  const base = new Date("2026-05-27T10:30:00+08:00").toISOString();
  const at = (offsetMinutes: number) => new Date(new Date("2026-05-27T10:30:00+08:00").getTime() - offsetMinutes * 60_000).toISOString();
  return {
    updated_at: base,
    source: "eastmoney-columns+eastmoney-rolling+eastmoney-fast-news+cls-telegraph+sina-rolling",
    diagnostics: [],
    items: [
      { title: "政策利好推动科技板块走强", summary: "半导体、AI 应用方向盘中活跃。", source: "东方财富", published_at: at(10), url: "https://example.test/news/1", tags: ["科技", "政策"], sentiment: "positive" },
      { title: "央行开展 4200 亿元逆回购操作", summary: "公开市场净投放 1800 亿元，资金面平稳偏松。", source: "财联社电报", published_at: at(25), url: "https://example.test/news/2", tags: ["宏观"], sentiment: "neutral" },
      { title: "北向资金今日净买入超 80 亿元", summary: "白酒、电池板块获集中加仓。", source: "新浪财经", published_at: at(41), url: "https://example.test/news/3", tags: ["资金"], sentiment: "positive" },
      { title: "工信部：加快推进制造业数字化转型", summary: "工业软件、智能制造装备方向受关注。", source: "东方财富", published_at: at(58), url: "https://example.test/news/4", tags: ["政策", "行业"], sentiment: "positive" },
      { title: "国际油价小幅回落 布伦特原油跌破 78 美元", summary: "市场权衡供给与需求前景。", source: "环球市场播报", published_at: at(73), url: "https://example.test/news/5", tags: ["海外"], sentiment: "negative" },
      { title: "多家存储芯片厂上调合约报价", summary: "涨价周期带动产业链业绩预期上修。", source: "东方财富7×24", published_at: at(89), url: "https://example.test/news/6", tags: ["行业"], sentiment: "positive" },
      { title: "两市成交额连续第 5 个交易日突破万亿", summary: "量能维持高位，题材轮动加快。", source: "财联社电报", published_at: at(104), url: "https://example.test/news/7", tags: ["情绪"], sentiment: "neutral" },
      { title: "光伏硅料价格企稳 组件排产环比回升", summary: "产业链库存去化接近尾声。", source: "新浪财经", published_at: at(120), url: "https://example.test/news/8", tags: ["行业"], sentiment: "positive" },
      { title: "美股三大指数期货窄幅震荡", summary: "市场等待本周 CPI 数据落地。", source: "环球市场播报", published_at: at(136), url: "https://example.test/news/9", tags: ["海外"], sentiment: "neutral" },
      { title: "某白酒龙头公告中期分红方案", summary: "分红比例高于市场预期，高股息方向获支撑。", source: "东方财富", published_at: at(150), url: "https://example.test/news/10", tags: ["个股"], sentiment: "positive" }
    ]
  };
}

export function mockMarketCommentary(): MarketCommentaryResponse {
  // 预览模式刻意用 news_fallback 档：让"非实时"标注在预览里也是显式的。
  return {
    updated_at: new Date("2026-06-01T15:30:00+08:00").toISOString(),
    trade_date: "2026-06-01",
    source: "browser-preview",
    mode: "news_fallback",
    stance: "neutral",
    summary: "预览数据：这是行情评价模块的回退示例，不来自实时盘面。",
    drivers: [{ title: "示例驱动", detail: "仅供预览版式确认", weight: "low" }],
    risks: ["示例风险条目"],
    next_watch: ["示例观察点"],
    diagnostics: ["browser-preview"]
  };
}

export function mockMarketBriefing(kind: "fupan" | "zaopan"): MarketBriefingResponse {
  const now = new Date("2026-06-01T15:30:00+08:00").toISOString();
  return {
    kind,
    updated_at: now,
    source: "browser-preview",
    source_url: kind === "fupan" ? "https://stock.10jqka.com.cn/fupan/" : "https://stock.10jqka.com.cn/zaopan/",
    summary:
      kind === "fupan"
        ? "A股三大指数集体调整，煤炭、养鸡、AI应用等方向活跃，科技成长方向分化加剧。"
        : "早盘关注昨日行情回顾、公司事项、机构观点与停复牌信息，先看主线承接再决定仓位。",
    sections: [
      {
        title: kind === "fupan" ? "指数/概念分析" : "早盘要点",
        content:
          kind === "fupan"
            ? "强势题材集中在煤炭、养鸡和 AI 应用，若次日量能无法延续，应降低追高权重。"
            : "公司事项和机构观点提供盘前线索，但仍需用开盘后的红绿家数与板块强度确认。",
        links: [],
        tables: [
          {
            title: "示例表格",
            columns: ["方向", "观察点"],
            rows: [{ "方向": "AI应用", "观察点": "看成交额与龙头承接" }]
          }
        ]
      }
    ],
    diagnostics: []
  };
}

export function mockClsFinance(): ClsFinanceResponse {
  return {
    updated_at: new Date("2026-06-09T15:05:00+08:00").toISOString(),
    source: "browser-preview",
    source_url: "https://www.cls.cn/finance",
    preclose_px: 3959.337,
    tline: [
      { date: 20260609, minute: 930, last_px: 3977.539, change: 0.0047 },
      { date: 20260609, minute: 1030, last_px: 3966.391, change: -0.0028 },
      { date: 20260609, minute: 1330, last_px: 3998.12, change: 0.0098 },
      { date: 20260609, minute: 1500, last_px: 4015.5, change: 0.0142 }
    ],
    anchors: [
      {
        code: "cls80025",
        name: "PCB",
        article_id: 2394344,
        c_time: "2026-06-09 09:31:30",
        direction: "up",
        url: "https://www.cls.cn/plate?code=cls80025"
      },
      {
        code: "cls80081",
        name: "油气设服",
        article_id: 2394352,
        c_time: "2026-06-09 09:39:24",
        direction: "down",
        url: "https://www.cls.cn/plate?code=cls80081"
      }
    ],
    emotion: {
      market_degree: 4.9,
      market_degree_source: "ths-market-summary",
      market_degree_label: "同花顺大盘评级",
      shsz_balance: "2.64万亿",
      shsz_balance_change: "-1524亿",
      up_limit: 130,
      open_limit: 25,
      performance: "1.74%",
      breadth: {
        up: 3322,
        down: 2049,
        flat: 156,
        total: 5527,
        source: "browser-preview",
        distribution: { suspend: 12 }
      }
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
  };
}

export function mockNewsSummary(): NewsSummaryResponse {
  return {
    updated_at: new Date("2026-06-01T15:00:00+08:00").toISOString(),
    source: "browser-preview",
    item_count: 18,
    themes: [
      {
        title: "AI 应用与算力",
        summary: "政策、订单和产品发布共同催化，盘中热度延续。",
        sentiment: "positive",
        source_count: 8,
        headlines: ["应用端公司成交活跃", "算力链仍有资金关注"]
      },
      {
        title: "新能源设备",
        summary: "部分细分方向有修复，但持续性仍要看订单和价格信号。",
        sentiment: "neutral",
        source_count: 5,
        headlines: ["设备端反弹更明显", "龙头估值修复带动板块"]
      }
    ],
    highlights: ["AI 应用与算力消息热度靠前", "新能源设备出现局部修复"],
    risks: ["高位股波动加大", "行业价格压力仍在"],
    diagnostics: []
  };
}

export function mockDiagnosticsSources(): DiagnosticsSourcesResponse {
  return {
    ok: true,
    generated_at: new Date().toISOString(),
    sources: [
      {
        source: "realtime",
        ok: true,
        status: "live",
        snapshot_source: "browser-preview",
        updated_at: new Date().toISOString(),
        seconds_since_success: 12,
        diagnostics: []
      },
      {
        source: "news",
        ok: true,
        seconds_since_success: 45,
        item_count: 18,
        diagnostics: []
      },
      {
        source: "finance",
        ok: false,
        seconds_since_success: null,
        diagnostics: ["同花顺大盘评分读取失败：缺少浏览器脚本生成的访问凭证（演示数据）。"]
      }
    ]
  };
}

export function mockDiagnosticsDataGaps(): DiagnosticsDataGapsResponse {
  return {
    ok: true,
    generated_at: new Date().toISOString(),
    profile: {
      available: true,
      window: { start_date: "2025-01-02", end_date: "2026-09-11", partitions: ["year=2025", "year=2026"] },
      daily_bars: {
        symbols: 5463,
        symbols_current: 74,
        symbols_stale: 5389,
        stale_distribution: [
          { last_date: "2026-07-14", symbols: 3084 },
          { last_date: "2025-12-31", symbols: 1080 },
          { last_date: "2026-09-04", symbols: 1222 }
        ],
        thin_days: [{ trade_date: "2026-09-07", rows: 74 }]
      },
      market_cap: { symbols: 5463, stale_distribution: [{ last_date: "2026-09-04", symbols: 1222 }] },
      capital_flow: { symbols: 5463, stale_distribution: [{ last_date: "2026-08-28", symbols: 906 }] }
    }
  };
}

export function mockRiskAlerts(): RiskAlertsResponse {
  return {
    updated_at: new Date("2026-05-27T10:30:00+08:00").toISOString(),
    source: "browser-preview",
    diagnostics: [],
    items: [
      {
        symbol: "000001",
        name: "*ST示例",
        risk_type: "ST风险",
        reason: "股票名称包含 *ST，存在退市风险警示。",
        severity: "high",
        source: "browser-preview",
        detected_at: new Date("2026-05-27T10:30:00+08:00").toISOString()
      }
    ]
  };
}

export function mockConditionValidation(
  text: string,
  mode: "entry" | "exit"
): ConditionValidationResult {
  const trimmed = text.trim();

  // "收盘价站上20日均线" is valid for BOTH entry and exit mode
  // (matches the original inline behavior in api.ts before extraction).
  if (trimmed === "收盘价站上20日均线") {
    return {
      ok: true,
      normalized_text: trimmed,
      condition: {
        id: "expr-close-above-ma",
        condition_id: "close_above_ma",
        enabled: true,
        params: { window: 20 },
        data_lag_days: 0,
        expression: trimmed
      },
      errors: [],
      examples: ["收盘价站上20日均线", "量比2日介于1.2到2.5"]
    };
  }

  if (mode === "entry") {
    return {
      ok: false,
      normalized_text: trimmed,
      condition: null,
      errors: [{ code: "unrecognized_condition", message: "无法识别条件，请参考样例改写。" }],
      examples: ["收盘价站上20日均线", "量比2日介于1.2到2.5"]
    };
  }

  // --- exit-mode specific conditions below ---

  if (["突破20日最低", "跌破20日低点"].includes(trimmed)) {
    return {
      ok: true,
      normalized_text: trimmed,
      condition: {
        id: "expr-breakdown-below-low",
        condition_id: "breakdown_below_n_day_low",
        enabled: true,
        params: { window: 20 },
        data_lag_days: 0,
        expression: trimmed
      },
      errors: [],
      examples: ["收盘价跌破3日均线", "跌破20日低点", "突破20日最低"]
    };
  }
  if (trimmed === "近5日涨幅小于3%") {
    return {
      ok: true,
      normalized_text: trimmed,
      condition: {
        id: "expr-past-return-at-most",
        condition_id: "past_return_at_most",
        enabled: true,
        params: { window: 5, max: 0.03 },
        data_lag_days: 0,
        expression: trimmed
      },
      errors: [],
      examples: ["近5日涨幅小于3%", "MACD死叉", "近3日主力净流出"]
    };
  }
  if (trimmed === "MACD死叉") {
    return {
      ok: true,
      normalized_text: trimmed,
      condition: {
        id: "expr-macd-dead-cross",
        condition_id: "macd_dead_cross",
        enabled: true,
        params: {},
        data_lag_days: 0,
        expression: trimmed
      },
      errors: [],
      examples: ["近5日涨幅小于3%", "MACD死叉", "近3日主力净流出"]
    };
  }
  if (["资金流出", "近3日主力净流出"].includes(trimmed)) {
    const rolling = trimmed === "近3日主力净流出";
    return {
      ok: true,
      normalized_text: trimmed,
      condition: {
        id: rolling ? "expr-capital-flow-out-3d" : "expr-capital-flow-out-today",
        condition_id: rolling ? "capital_flow_n_day_sum_at_most" : "capital_flow_today_at_most",
        enabled: true,
        params: rolling ? { window: 3, max: 0 } : { max: 0 },
        data_lag_days: 0,
        expression: trimmed
      },
      errors: [],
      examples: ["近5日涨幅小于3%", "MACD死叉", "近3日主力净流出"]
    };
  }
  return {
    ok: false,
    normalized_text: trimmed,
    condition: null,
    errors: [{ code: "unrecognized_condition", message: "无法识别离场条件，请参考样例改写。" }],
    examples: ["收盘价跌破3日均线", "近5日涨幅小于3%", "MACD死叉", "近3日主力净流出"]
  };
}

export function mockStockSymbolValidation(symbols: string[]): StockSymbolValidationResult {
  const normalizedSymbols = symbols.map((s) => s.trim()).filter(Boolean);
  const known = new Set(["600519", "000001"]);
  const valid = normalizedSymbols.filter((symbol) => known.has(symbol));
  return {
    ok: valid.length === normalizedSymbols.length,
    valid_symbols: valid,
    invalid_symbols: normalizedSymbols.filter((symbol) => !known.has(symbol)),
    normalized_symbols: normalizedSymbols,
    source: "browser-preview"
  };
}

export function mockRecommendedStrategies(): RecommendedStrategiesResponse {
  return {
    items: [
      {
        id: "volume-breakout",
        name: "放量突破",
        description: "价格突破前高并伴随量能放大。",
        suitable_market: "指数温和上行、题材活跃时使用。",
        risk_note: "避免连续大涨后追高。",
        example_conditions: ["突破20日新高", "量比2日介于1.2到2.5"],
        scenario: "温和上行",
        featured: true,
        required_datasets: ["daily_bars", "market_cap"],
        capability_note: "浏览器预览使用模拟可用数据。",
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
      },
      {
        id: "steady-cap-volume",
        name: "市值量价均衡",
        description: "过滤超大市值和极端成交，寻找流动性适中的趋势机会。",
        suitable_market: "震荡偏强或结构性行情中使用。",
        risk_note: "更适合在市场有承接、但不是全面高潮时使用。",
        example_conditions: ["流通市值10亿到300亿", "换手率2%到8%", "量比2日介于1.2到2.5"],
        scenario: "震荡轮动",
        featured: true,
        required_datasets: ["daily_bars", "market_cap"],
        capability_note: "浏览器预览使用模拟可用数据。",
        strategy: {
          name: "市值量价均衡",
          market_filters: [],
          entry_groups: [
            {
              id: "entry",
              operator: "and",
              conditions: [
                {
                  id: "preview-cap",
                  condition_id: "market_cap_between",
                  enabled: true,
                  params: { min: 1000000000, max: 30000000000 },
                  data_lag_days: 0,
                  expression: "流通市值10亿到300亿"
                },
                {
                  id: "preview-turnover",
                  condition_id: "turnover_between",
                  enabled: true,
                  params: { min: 0.02, max: 0.08 },
                  data_lag_days: 0,
                  expression: "换手率2%到8%"
                },
                {
                  id: "preview-volume",
                  condition_id: "volume_ratio_between",
                  enabled: true,
                  params: { window: 2, min: 1.2, max: 2.5 },
                  data_lag_days: 0,
                  expression: "量比2日介于1.2到2.5"
                }
              ]
            }
          ],
          exit_rules: [
            {
              id: "preview-exit-ma",
              condition_id: "close_below_ma",
              enabled: true,
              params: { window: 3 },
              data_lag_days: 0,
              expression: "收盘价跌破3日均线"
            }
          ],
          score_threshold: null
        }
      }
    ]
  };
}

export function mockDailyBarsCoverage(
  symbols: string[],
  startDate: string,
  endDate: string
): DailyBarsCoverageResponse {
  return {
    items: symbols.map((symbol) => ({
      symbol,
      start_date: startDate,
      end_date: endDate,
      rows: 5,
      missing_trade_dates: [],
      missing_capital_flow_dates: [],
      missing_market_cap_dates: []
    }))
  };
}

export function mockFetchDailyBarsResult(
  symbols: string[],
  startDate: string,
  endDate: string
): FetchResult {
  return {
    status: "ok",
    imported_rows: symbols.length * 5,
    requested_symbols: symbols,
    fetched_symbols: symbols,
    missing_symbols: [],
    coverage: [
      { dataset: "daily_bars", symbols: symbols.length, start_date: startDate, end_date: endDate, missing_rows: 0, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: symbols.length, start_date: startDate, end_date: endDate, missing_rows: 0, suspension_rows: 0 },
      { dataset: "market_cap", symbols: symbols.length, start_date: startDate, end_date: endDate, missing_rows: 0, suspension_rows: 0 }
    ],
    logs: [{ level: "info", message: `Fetched ${symbols.length * 5} daily bar rows` }]
  };
}

export function mockFetchCapitalFlowResult(
  symbols: string[],
  startDate: string,
  endDate: string
): FetchResult {
  if (symbols.length === 0) {
    return {
      status: "ok",
      imported_rows: 0,
      requested_symbols: [],
      fetched_symbols: [],
      missing_symbols: [],
      coverage: [
        { dataset: "daily_bars", symbols: 2, start_date: startDate, end_date: endDate, missing_rows: 0, suspension_rows: 0 },
        { dataset: "capital_flow", symbols: 2, start_date: startDate, end_date: endDate, missing_rows: 0, suspension_rows: 0 },
        { dataset: "market_cap", symbols: 2, start_date: startDate, end_date: endDate, missing_rows: 0, suspension_rows: 0 }
      ],
      logs: [{ level: "info", message: "Capital-flow backfill started for all preview symbols" }],
      diagnostics: [{ code: "capital_flow_backfill_job_started", requested_symbols: 2, source: "capital_flow_crawler" }],
      failures: [],
      job: {
        job_id: "preview-capital-flow",
        mode: "capital_flow_backfill",
        status: "completed",
        total_symbols: 2,
        completed_symbols: 2,
        failed_symbols: 0,
        imported_rows: 2,
        current_symbol: null,
        start_date: startDate,
        end_date: endDate,
        errors: []
      }
    };
  }
  return {
    status: "ok",
    imported_rows: symbols.length,
    requested_symbols: symbols,
    fetched_symbols: symbols,
    missing_symbols: [],
    coverage: [
      { dataset: "daily_bars", symbols: symbols.length, start_date: startDate, end_date: endDate, missing_rows: 0, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: symbols.length, start_date: startDate, end_date: endDate, missing_rows: 0, suspension_rows: 0 },
      { dataset: "market_cap", symbols: symbols.length, start_date: startDate, end_date: endDate, missing_rows: 0, suspension_rows: 0 }
    ],
    logs: [{ level: "info", message: `Capital-flow crawler merged ${symbols.length} rows as primary main_net_inflow source` }],
    diagnostics: [{ code: "capital_flow_crawler_merge", merged_rows: symbols.length, source: "capital_flow_crawler" }],
    failures: []
  };
}

export function mockImportDailyBarsResult(source: "sample" | "file", path?: string): ImportResult {
  return {
    status: "ok",
    imported_rows: 10,
    coverage: [
      { dataset: "daily_bars", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-08", missing_rows: 0, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-08", missing_rows: 0, suspension_rows: 0 },
      { dataset: "market_cap", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-08", missing_rows: 0, suspension_rows: 0 }
    ],
    logs: [{ level: "info", message: `Imported daily bars from ${source}${path ? `: ${path}` : ""}` }]
  };
}

export function mockStartFullMarketSync(
  startDate: string,
  endDate: string,
  symbols?: string[]
): { job: SyncJobStatus } {
  const totalSymbols = symbols?.length ?? 2;
  return {
    job: {
      job_id: "preview",
      mode: "full_market_bootstrap",
      status: "completed",
      total_symbols: totalSymbols,
      completed_symbols: totalSymbols,
      failed_symbols: 0,
      imported_rows: totalSymbols * 10,
      current_symbol: null,
      start_date: startDate,
      end_date: endDate,
      errors: []
    }
  };
}

export function mockLoadSyncJob(jobId: string): { job: SyncJobStatus } {
  return {
    job: {
      job_id: jobId,
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
  };
}

export function mockCancelSyncJob(jobId: string): { job: SyncJobStatus } {
  return {
    job: {
      job_id: jobId,
      mode: "capital_flow_backfill",
      status: "cancelled",
      total_symbols: 2,
      completed_symbols: 1,
      failed_symbols: 0,
      processed_symbols: 1,
      skipped_symbols: 0,
      imported_rows: 1,
      returned_rows: 1,
      current_symbol: null,
      start_date: "2026-06-01",
      end_date: "2026-06-05",
      errors: []
    }
  };
}

export function mockCallBackendCoverage() {
  return {
    coverage: [
      { dataset: "daily_bars", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-08", missing_rows: 0, suspension_rows: 0 },
      { dataset: "capital_flow", symbols: 2, start_date: "2024-01-02", end_date: "2024-01-08", missing_rows: 0, suspension_rows: 0 }
    ]
  };
}
