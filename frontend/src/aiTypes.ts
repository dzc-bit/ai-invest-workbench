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
// 事件流静默保活心跳：前端 dispatchChatEvent 对未知 type 静默忽略，
// 类型上仍要显式声明，避免 union 撒谎。
export type AiHeartbeatEvent = { type: "heartbeat" };

export type AiChatEvent =
  | AiSessionEvent
  | AiPhaseEvent
  | AiTokenEvent
  | AiToolCallEvent
  | AiToolResultEvent
  | AiResultEvent
  | AiErrorEvent
  | AiHeartbeatEvent;

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

export type AiResearchStyleMeta = {
  value: AiResearchStyle;
  label: string;
  description: string;
  /** 一句示例口吻：用户切换前就能预览"这个人怎么说话"。 */
  sample: string;
};

export const AI_RESEARCH_STYLES: AiResearchStyleMeta[] = [
  {
    value: "conservative",
    label: "保守 · 防御型",
    description:
      "审计出身的老派防御投资人：本金安全优先，先给否决理由再给条件放行；必查回撤、估值分位与流动性，禁用打板/卡位/满仓等进攻词汇。",
    sample: "结论：回避。下行风险三条：跌破 20 日线放量、解禁盘压力、流动性塌缩；在收盘站稳 5 日线之前，我不动。"
  },
  {
    value: "balanced",
    label: "均衡 · 默认",
    description:
      "对照式研究员：每条多头证据紧跟一条空头反驳，结论从对照里长出来；常用设问与“对价”句式，最后必落观望/偏多/偏空三选一。",
    sample: "多头看资金连续 3 日净流入，但空头会指出量价背离；这个位置的赔率够不够？观望，跌破 10 日线倒向偏空。"
  },
  {
    value: "aggressive",
    label: "激进 · 进攻型",
    description:
      "龙头选手视角：短句快节奏，只判断情绪周期位置、梯队与辨识度，给出打板/低吸/半路三选一与断板预案；不作持有型建议（高风险）。",
    sample: "看梯队：最高板 6，晋级率塌到 35%，退潮期——高标不接力，只看卡位低吸；断板即走，仓位减半。"
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

export type AiMemoryCategory = "risk_preference" | "watchlist" | "holding" | "style" | "strategy" | "fact";

export const AI_MEMORY_CATEGORY_LABELS: Record<string, string> = {
  risk_preference: "风险偏好",
  watchlist: "关注标的",
  holding: "持仓",
  style: "交易风格",
  strategy: "策略参数",
  fact: "其他事实"
};

export type AiMemoryRecord = {
  id: string;
  category: AiMemoryCategory | string;
  content: string;
  weight: number;
  hits: number;
  created_at: string;
  updated_at: string;
};

export type AiMemoriesResponse = {
  items: AiMemoryRecord[];
  rejected_market_facts_total?: number;
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
      return `模型服务调用失败，请检查网络、API Key 与服务商状态后重试。${
        tail ? `服务商返回：${tail}。` : ""
      }若是回答过长或上下文超限，请新建对话后拆小问题再问。`;
    }
    return error.message;
  }
  return "AI 请求失败，请稍后重试。";
}
