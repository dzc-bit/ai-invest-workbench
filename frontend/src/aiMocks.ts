import type {
  AiChatEvent,
  AiChatRequest,
  AiConditionParseResult,
  AiConfigUpdatePayload,
  AiConfigView,
  AiDisplayTurn,
  AiEventStreamEvent,
  AiInsightScene,
  AiNewsDigest,
  AiReportsResponse,
  AiSessionDetail,
  AiSessionsResponse,
  AiStatus
} from "./aiTypes";
import type { BacktestSettingsConfig, OptimizeCombination, StrategyConfig } from "./types";

export function mockAiStatus(): AiStatus {
  return {
    configured: true,
    base_url: "https://mock.local/v1",
    model: "demo-model",
    insights_enabled: true,
    tool_names: ["realtime_market_snapshot", "market_news", "recent_daily_bars", "run_strategy_backtest"],
    knowledge_documents: 3,
    knowledge_chunks: 24,
    knowledge_ready: true
  };
}

export function mockAiConfig(): AiConfigView {
  return {
    base_url: "https://mock.local/v1",
    model: "demo-model",
    embedding_model: "demo-embedding",
    embedding_base_url: "",
    embedding_api_key_masked: "",
    api_style: "chat-completions",
    research_style: "balanced",
    max_tokens: 4096,
    api_key_masked: "sk-****demo",
    temperature: 0.3,
    max_steps: 8,
    insights_enabled: true,
    insight_max_per_hour: 6,
    report_enabled: false,
    report_time: "15:30",
    evolution_enabled: false,
    evolution_time: "16:00",
    configured: true
  };
}

export function mockAiSaveConfig(payload: AiConfigUpdatePayload): AiConfigView {
  return {
    ...mockAiConfig(),
    base_url: payload.base_url,
    model: payload.model,
    embedding_model: payload.embedding_model,
    embedding_base_url: payload.embedding_base_url,
    api_style: payload.api_style,
    research_style: payload.research_style,
    configured: Boolean(payload.base_url && payload.model)
  };
}

export function mockAiReports(): AiReportsResponse {
  return {
    items: [
      {
        name: "复盘报告-演示.md",
        size: 2048,
        created_at: new Date().toISOString()
      }
    ]
  };
}

export function mockAiNewsDigest(): AiNewsDigest {
  const created = new Date().toISOString();
  return {
    count: 3,
    updated_at: created,
    items: [
      {
        id: "digest-1",
        title: "政策利好落地，科技方向盘中活跃",
        summary: "半导体与 AI 应用获政策催化，北向资金净买入超 80 亿元（来源：财联社电报/东方财富）。",
        tags: ["政策", "资金"],
        symbols: [],
        source: "ai-agent",
        created_at: created
      },
      {
        id: "digest-2",
        title: "涨停家数回升，连板梯队高度抬升",
        summary: "今日涨停池数量明显增加，昨日涨停平均表现转正，短线情绪回暖（来源：涨停池/实时行情）。",
        tags: ["情绪"],
        symbols: ["601869"],
        source: "ai-agent",
        created_at: created
      },
      {
        id: "digest-3",
        title: "存储芯片厂上调合约报价",
        summary: "涨价周期带动产业链业绩预期上修，光量子计算亦取得突破（来源：东方财富7×24）。",
        tags: ["行业"],
        symbols: [],
        source: "ai-agent",
        created_at: created
      }
    ]
  };
}

const DEMO_STRATEGY: StrategyConfig = {
  name: "AI 生成策略",
  market_filters: [],
  entry_groups: [
    {
      id: "ai-entry-group",
      operator: "and",
      conditions: [
        {
          id: "ai-entry-0",
          condition_id: "close_above_ma",
          enabled: true,
          params: { window: 20 },
          data_lag_days: 0,
          expression: "收盘价站上20日均线"
        }
      ]
    }
  ],
  exit_rules: [
    {
      id: "ai-exit-0",
      condition_id: "macd_dead_cross",
      enabled: true,
      params: {},
      data_lag_days: 0,
      expression: "MACD死叉"
    }
  ],
  score_threshold: null
};

export function mockAiChatEvents(request: AiChatRequest): AiChatEvent[] {
  const sessionKey = request.session_id ?? MOCK_CURRENT_SESSION;
  const reply = request.context?.kind === "backtest_result"
    ? "本次回测总收益 12.4%，最大回撤 5.2%，胜率 58.3%，共 24 笔交易。收益主要由 3 月上旬的量能放大阶段贡献；回撤集中在 4 月中旬的连续止损。建议关注止盈参数的敏感性。以上为 AI 生成内容，仅供辅助观察，不构成投资建议。"
    : "市场当前红盘 3200 / 全市场 5120，上证指数 3100 点（+0.65%）。半导体板块领涨 3.8%。整体情绪偏暖，但宽度尚未过热。以上为 AI 生成内容，仅供辅助观察，不构成投资建议。";
  const demoCurve = Array.from({ length: 20 }, (_, index) => ({
    trade_date: `2026-04-${String(index + 1).padStart(2, "0")}`,
    equity: 1_000_000 * (1 + index * 0.006 + (index % 3) * 0.002),
    cash: 400_000,
    market_value: 600_000 * (1 + index * 0.008),
    drawdown_pct: -0.01 - (index % 4) * 0.004
  }));
  const ts = new Date().toISOString();
  const nextTurns: AiDisplayTurn[] = [
    { role: "user", content: request.message, ts },
    {
      role: "assistant",
      content: reply,
      tool_steps: [
        { id: "t1", name: "realtime_market_snapshot", ok: true, summary: "状态 live / 来源 mock", duration_ms: 320 },
        { id: "t2", name: "recent_daily_bars", ok: true, summary: "600519 最近 30 日区间 +5.20%", duration_ms: 210 }
      ],
      ts
    }
  ];
  const prior = mockSessionDisplays.get(sessionKey) ?? (sessionKey === MOCK_CURRENT_SESSION ? [] : demoDisplayFor(sessionKey));
  const display = [...prior, ...nextTurns];
  mockSessionDisplays.set(sessionKey, display);
  return [
    { type: "session", session_id: sessionKey, title: "演示会话" },
    { type: "phase", phase: "思考中（第 1/8 步）" },
    { type: "tool_call", id: "t1", name: "realtime_market_snapshot", args: {} },
    { type: "tool_result", id: "t1", name: "realtime_market_snapshot", ok: true, summary: "状态 live / 来源 mock；上证指数 3100 (+0.65%)", duration_ms: 320 },
    { type: "tool_call", id: "t2", name: "recent_daily_bars", args: { symbol: "600519" } },
    { type: "tool_result", id: "t2", name: "recent_daily_bars", ok: true, summary: "600519 贵州茅台 最近 30 个交易日：区间 +5.20%", duration_ms: 210 },
    { type: "phase", phase: "思考中（第 2/8 步）" },
    { type: "token", text: reply.slice(0, 20) },
    { type: "token", text: reply.slice(20) },
    {
      type: "result",
      session_id: sessionKey,
      display,
      strategy: request.context?.kind === "none" || !request.context ? DEMO_STRATEGY : null,
      chart: { type: "equity_curve", title: "回测权益曲线（演示）", points: demoCurve }
    }
  ];
}

let mockEventStreamEmitted = false;

const MOCK_CURRENT_SESSION = "mock-session";

// 生产端 result.display 是"整条会话累积的转录"，预览 mock 按同样的语义累计，
// 否则一次问答就会把刚回读出来的历史顶掉，预览看不到历史续用的真实效果。
const mockSessionDisplays = new Map<string, AiDisplayTurn[]>();

function demoDisplayFor(sessionId: string): AiDisplayTurn[] {
  const ts = new Date().toISOString();
  return sessionId === MOCK_CURRENT_SESSION
    ? [
        { role: "user", content: "帮我看看 600519，结合技术面、资金面和估值给个诊断。", ts },
        {
          role: "assistant",
          content:
            "600519 贵州茅台近 30 日区间 +5.20%，主力净流入连续 3 日为正但估值分位仍处近三年中位以上（来源：recent_daily_bars / tencent_valuation）。以上为 AI 生成内容，仅供辅助观察，不构成投资建议。",
          tool_steps: [
            { id: "h1", name: "recent_daily_bars", ok: true, summary: "600519 最近 30 日区间 +5.20%", duration_ms: 210 },
            { id: "h2", name: "tencent_valuation", ok: true, summary: "PE-TTM 32.4，近三年分位 61%", duration_ms: 140 }
          ],
          ts
        }
      ]
    : [
        { role: "user", content: "结合当前实时行情和最新新闻，做一次大盘快评。", ts },
        {
          role: "assistant",
          content:
            "红盘 3200 / 全市场 5120，上证指数 3100 点（+0.65%），半导体板块领涨 3.8%；宽度偏暖但未过热（来源：realtime_market_snapshot / market_news）。以上为 AI 生成内容，仅供辅助观察，不构成投资建议。",
          tool_steps: [
            { id: "h3", name: "realtime_market_snapshot", ok: true, summary: "状态 live / 上证指数 3100 (+0.65%)", duration_ms: 320 }
          ],
          ts
        }
      ];
}

export function mockAiSessions(): AiSessionsResponse {
  const now = Date.now();
  return {
    items: [
      {
        session_id: MOCK_CURRENT_SESSION,
        title: "600519 个股诊断（演示）",
        updated_at: new Date(now - 60_000).toISOString(),
        message_count: 2
      },
      {
        session_id: "mock-session-older",
        title: "大盘快评（演示）",
        updated_at: new Date(now - 86_400_000).toISOString(),
        message_count: 2
      }
    ]
  };
}

export function mockAiSessionDetail(sessionId: string): AiSessionDetail {
  const isStock = sessionId === MOCK_CURRENT_SESSION;
  if (!mockSessionDisplays.has(sessionId)) {
    mockSessionDisplays.set(sessionId, demoDisplayFor(sessionId));
  }
  const display = mockSessionDisplays.get(sessionId) ?? [];
  const ts = new Date().toISOString();
  return {
    session_id: sessionId,
    title: isStock ? "600519 个股诊断（演示）" : "大盘快评（演示）",
    created_at: ts,
    updated_at: ts,
    display
  };
}

export function mockAiSessionDelete(sessionId: string): boolean {
  return sessionId.length > 0;
}

export function mockAiConditionParse(text: string): AiConditionParseResult {
  const trimmed = text.trim() || "近5天放量上涨，破20日线卖";
  return {
    entry: [
      {
        id: `ai-parse-entry-0-${trimmed.length}`,
        condition_id: "volume_ratio_between",
        enabled: true,
        params: { window: 2, min: 1.2, max: 2.5 },
        data_lag_days: 0,
        expression: "量比2日介于1.2到2.5"
      },
      {
        id: "ai-parse-entry-1",
        condition_id: "capital_flow_n_day_sum_at_least",
        enabled: true,
        params: { window: 5, min: 3_000_000 },
        data_lag_days: 0,
        expression: "近5日主力净流入大于300万"
      }
    ],
    exit: [
      {
        id: "ai-parse-exit-0",
        condition_id: "close_below_ma",
        enabled: true,
        params: { window: 20 },
        data_lag_days: 0,
        expression: "收盘价跌破20日均线"
      }
    ],
    approximations: [`『${trimmed.slice(0, 8)}…』→量比2日介于1.2到2.5`],
    dropped: []
  };
}

const ONESHOT_MOCK_TEXTS: Record<AiInsightScene, string> = {
  results_overview: "本次回测收益平稳但交易次数偏少，胜率的优势不足以支撑加仓，建议先扩大日期范围验证稳定性。（演示数据）",
  data_coverage: "缺失集中在资金流字段，日线覆盖完整；新上市股票的上市前区间不再计为缺失，点击“补齐资金流”即可修复。（演示数据）",
  risk_alerts: "风险名单以 ST 类为主，集中在小市值方向；持仓若命中名单应以减仓优先，避免退市整理期流动性风险。（演示数据）"
};

export function mockAiInsightOneshot(scene: AiInsightScene): string {
  return ONESHOT_MOCK_TEXTS[scene];
}

export function mockAiOptimizeEvents(request: {
  strategy: StrategyConfig;
  settings: BacktestSettingsConfig;
  grid: Record<string, number[]>;
}): Array<Record<string, unknown> & { type: string }> {
  const keys = Object.keys(request.grid);
  const valuesList = keys.map((key) => request.grid[key] ?? []);
  const combinations: OptimizeCombination[] = [];
  let index = 0;
  const events: Array<Record<string, unknown> & { type: string }> = [
    { type: "phase", phase: "读取本地数据" }
  ];
  const product = valuesList.reduce((acc, values) => {
    const next: number[][] = [];
    for (const prefix of (acc.length ? acc : [[]]) as number[][]) {
      for (const value of values) {
        next.push([...prefix, value]);
      }
    }
    return next;
  }, [] as number[][]);
  for (const values of product) {
    index += 1;
    const params: Record<string, number> = {};
    keys.forEach((key, position) => {
      params[key] = values[position];
    });
    const metrics = {
      total_return_pct: 0.05 + index * 0.012,
      annualized_return_pct: 0.12 + index * 0.01,
      max_drawdown_pct: -0.03 - (index % 3) * 0.008,
      win_rate_pct: 0.48 + index * 0.02,
      trade_count: 6 + (index % 4),
      average_trade_return_pct: 0.004 + index * 0.001,
      average_position_pct: 0.35,
      max_position_pct: 0.5
    };
    const combination: OptimizeCombination = { index, params, metrics };
    combinations.push(combination);
    events.push({ type: "combination", ...combination });
    events.push({ type: "progress", completed: index, total: product.length });
  }
  const best = [...combinations].sort(
    (left, right) => right.metrics.total_return_pct - left.metrics.total_return_pct
  )[0];
  events.push({
    type: "result",
    result: {
      combinations,
      best: best ?? null,
      failures: [],
      total: product.length,
      evaluated: combinations.length,
      insight: "收益对持仓天数最敏感：持有 3 天的组合整体占优，但组合数量少，注意过拟合。（演示数据）",
      insight_error: null
    }
  });
  return events;
}

export function mockAiEventStream(): AiEventStreamEvent[] {
  // 仅含 insight 且只发一次：data_fresh 会触发页面模块即时刷新，破坏预览与测试
  // 的确定性；重复 insight 会虚涨未读徽标。
  if (mockEventStreamEmitted) {
    return [];
  }
  mockEventStreamEmitted = true;
  return [
    {
      type: "insight",
      insight: {
        id: "mock-insight-1",
        created_at: new Date().toISOString(),
        level: "info",
        title: "市场宽度快速回暖",
        digest: "红盘占比从 42% 回升至 62%，半导体板块领涨。AI 快讯，不构成投资建议。",
        source: "ai-insight",
        disclaimer: "AI 生成内容，仅供辅助观察，不构成投资建议"
      }
    }
  ];
}
