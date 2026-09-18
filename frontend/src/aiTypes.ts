import type { StrategyConfig, ConditionNode } from "./types";

export type AiToolStep = {
  id: string;
  name: string;
  ok?: boolean;
  summary?: string;
  duration_ms?: number;
  /** 失败类别（bad_arguments / no_data / tool_error / interrupted …），随会话落盘。 */
  code?: string;
};

export type AiDisplayTurn = {
  role: "user" | "assistant";
  content: string;
  tool_steps?: AiToolStep[];
  ts?: string;
};

export type AiChatContextKind = "none" | "backtest_result" | "strategy" | "market_snapshot";

export type AiChatContext = {
  kind: AiChatContextKind;
  payload?: Record<string, unknown>;
};

export type AiChatRequest = {
  message: string;
  session_id?: string | null;
  context?: AiChatContext | null;
};

export type AiSessionEvent = { type: "session"; session_id: string; title?: string };
export type AiPhaseEvent = { type: "phase"; phase: string };
export type AiTokenEvent = { type: "token"; text: string };
export type AiToolCallEvent = { type: "tool_call"; id: string; name: string; args: Record<string, unknown> };
export type AiToolResultEvent = {
  type: "tool_result";
  id: string;
  name: string;
  ok: boolean;
  summary: string;
  duration_ms: number;
  diagnostics?: string[];
};
export type AiEquityPoint = {
  trade_date: string;
  equity: number;
  cash: number;
  market_value: number;
  drawdown_pct: number;
};

export type AiChartArtifact = {
  type: "equity_curve";
  title: string;
  points: AiEquityPoint[];
};

export type AiResultEvent = {
  type: "result";
  session_id: string;
  display: AiDisplayTurn[];
  strategy?: StrategyConfig | null;
  chart?: AiChartArtifact | null;
  updated_at?: string;
};
export type AiErrorEvent = { type: "error"; code?: string; message?: string };

export type AiChatEvent =
  | AiSessionEvent
  | AiPhaseEvent
  | AiTokenEvent
  | AiToolCallEvent
  | AiToolResultEvent
  | AiResultEvent
  | AiErrorEvent;

export type AiChatHandlers = {
  onSession?: (event: AiSessionEvent) => void;
  onPhase?: (phase: string) => void;
  onToken?: (text: string) => void;
  onToolCall?: (event: AiToolCallEvent) => void;
  onToolResult?: (event: AiToolResultEvent) => void;
  onResult?: (event: AiResultEvent) => void;
};

export type AiApiStyle = "chat-completions" | "responses" | "anthropic";
export type AiResearchStyle = "conservative" | "balanced" | "aggressive";

export const AI_API_STYLE_LABELS: Record<AiApiStyle, string> = {
  "chat-completions": "Chat Completions（OpenAI 兼容 · 默认）",
  responses: "Responses（OpenAI 新接口）",
  anthropic: "Anthropic Messages"
};

export const AI_RESEARCH_STYLES: Array<{ value: AiResearchStyle; label: string; description: string }> = [
  {
    value: "conservative",
    label: "保守 · 防御型",
    description: "低波动、高股息、低估值为先，强调回撤控制与流动性，警惕题材连板。"
  },
  {
    value: "balanced",
    label: "均衡 · 默认",
    description: "基本面/资金面/技术面三线均衡，右侧交易为主，守正出奇。"
  },
  {
    value: "aggressive",
    label: "激进 · 进攻型",
    description: "情绪周期与龙头战法视角，聚焦主线题材与连板梯队（高风险，附纪律提示）。"
  }
];

export type AiStatus = {
  configured: boolean;
  base_url: string;
  model: string;
  insights_enabled: boolean;
  tool_names: string[];
  knowledge_documents: number;
  knowledge_chunks: number;
  knowledge_ready: boolean;
  memory_count?: number;
};

export type AiConfigView = {
  base_url: string;
  model: string;
  embedding_model: string;
  embedding_base_url: string;
  embedding_api_key_masked: string;
  api_style: AiApiStyle;
  research_style: AiResearchStyle;
  api_key_masked: string;
  temperature: number;
  max_tokens: number;
  max_steps: number;
  insights_enabled: boolean;
  insight_max_per_hour: number;
  report_enabled: boolean;
  report_time: string;
  evolution_enabled: boolean;
  evolution_time: string;
  configured: boolean;
};

export type AiConfigUpdatePayload = {
  base_url: string;
  model: string;
  embedding_model: string;
  embedding_base_url: string;
  embedding_api_key: string;
  api_key: string;
  api_style: AiApiStyle;
  research_style: AiResearchStyle;
  temperature: number;
  max_tokens: number;
  max_steps: number;
  insights_enabled: boolean;
  insight_max_per_hour: number;
  report_enabled: boolean;
  report_time: string;
  evolution_enabled: boolean;
  evolution_time: string;
};

export type AiReportMeta = {
  name: string;
  size: number;
  created_at: string;
};

export type AiReportsResponse = {
  items: AiReportMeta[];
};

export type AiOverfitFinding = {
  level: "critical" | "warning" | "info";
  code: string;
  message: string;
};

export type AiOverfitResult = {
  level: "critical" | "warning" | "info" | "none";
  findings: AiOverfitFinding[];
};

export type AiDigestItem = {
  id: string;
  title: string;
  summary: string;
  tags: string[];
  symbols: string[];
  source: string;
  created_at: string;
};

export type AiNewsDigest = {
  items: AiDigestItem[];
  count: number;
  updated_at: string | null;
};

export type AiInsight = {
  id: string;
  created_at: string;
  level: "info" | "warning" | "high";
  title: string;
  digest: string;
  related_symbols?: string[];
  source: string;
  disclaimer: string;
};

export type AiEventStreamEvent = {
  type: "insight" | "data_fresh" | "heartbeat";
  insight?: AiInsight;
  module?: string | null;
  timestamp?: string | null;
};

export type AiTask = {
  message: string;
  context?: AiChatContext | null;
};

export type AiSessionMeta = {
  session_id: string;
  title: string;
  updated_at?: string | null;
  message_count: number;
};

export type AiSessionsResponse = {
  items: AiSessionMeta[];
};

export type AiSessionDetail = {
  session_id: string;
  title: string;
  created_at?: string | null;
  updated_at?: string | null;
  display: AiDisplayTurn[];
};

export type AiConditionParseResult = {
  entry: ConditionNode[];
  exit: ConditionNode[];
  approximations: string[];
  dropped: Array<{ kind: string; expression: string; error: string; examples?: string }>;
};

export type AiInsightScene = "results_overview" | "data_coverage" | "risk_alerts";

export type AiInsightOneshotResult = {
  ok: boolean;
  scene: AiInsightScene;
  text: string;
  generated_at: string;
};

export function translateAiError(error: unknown): string {
  if (error instanceof Error) {
    if (error.message.includes("ai_not_configured") || error.message.includes("尚未配置")) {
      return "AI 服务尚未配置，请点击右上角设置填写 base_url、API Key 和模型名。";
    }
    if (error.message.includes("ai_session_busy") || error.message.includes("仍在生成中")) {
      return "上一轮回答还在生成中，请等它结束（或点击停止）后再发送。";
    }
    if (error.message.includes("ai_upstream_error") || error.message.includes("模型服务调用失败")) {
      // 后端 detail 里带着真正的病因（上下文超长 / 401 / 429 / 超时），
      // 整句换成固定文案等于让用户照着“检查网络”盲猜。
      const detail = error.message
        .replace(/^模型服务调用失败/u, "")
        .replace(/^[\s（）:：-]+/u, "")
        .trim();
      const tail = detail.length > 160 ? `${detail.slice(0, 160)}…` : detail;
      return `模型服务调用失败，请检查网络、API Key 与服务商状态后重试${
        tail ? `。服务商返回：${tail}` : ""
      }若是回答过长或上下文超限，请新建对话后拆小问题再问。`;
    }
    return error.message;
  }
  return "AI 请求失败，请稍后重试。";
}
