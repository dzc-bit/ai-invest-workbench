import { BackendError, backendErrorFromStatus, consumeNdjsonStream, streamIncompleteError } from "./api";
import { isTauriRuntime } from "./tauriRuntime";
import { previewAiMocks } from "./previewMocks";
import type {
  AiChatEvent,
  AiChatHandlers,
  AiChatRequest,
  AiConditionParseResult,
  AiConfigUpdatePayload,
  AiConfigView,
  AiEventStreamEvent,
  AiInsightOneshotResult,
  AiInsightScene,
  AiMemoriesResponse,
  AiNewsDigest,
  AiOverfitResult,
  AiReportsResponse,
  AiSessionDetail,
  AiSessionsResponse,
  AiStatus
} from "./aiTypes";
import type {
  BacktestSettingsConfig,
  OptimizeCombination,
  OptimizeStreamHandlers,
  OptimizeSummary,
  StrategyConfig
} from "./types";

const AI_CHAT_STREAM_IDLE_TIMEOUT_MS = 180_000;
const AI_EVENTS_IDLE_TIMEOUT_MS = 40_000;
const AI_OPTIMIZE_IDLE_TIMEOUT_MS = 120_000;

/** 按场景配置的 JSON 超时。模型类请求的上界必须高于后端自身的 120 秒非流式超时
 * （见 `ai/llm_client.py`），否则前端先于后端放弃，用户只能看到"超时"而不是
 * 服务商返回的原因。 */
const AI_QUICK_JSON_TIMEOUT_MS = 20_000;
const AI_REPORT_JSON_TIMEOUT_MS = 60_000;
const AI_MODEL_JSON_TIMEOUT_MS = 150_000;

export type AiJsonOptions = {
  timeoutMs?: number;
  signal?: AbortSignal;
};

/** 全部 AI JSON 请求共用这一条传输：超时、取消与错误码解析只有一份实现。 */
async function aiRequestJson<T>(
  baseUrl: string,
  path: string,
  init: RequestInit,
  fallbackMessage: string,
  options: AiJsonOptions = {}
): Promise<T> {
  const timeoutMs = options.timeoutMs ?? AI_QUICK_JSON_TIMEOUT_MS;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  const forwardAbort = () => controller.abort();
  options.signal?.addEventListener("abort", forwardAbort, { once: true });
  try {
    const response = await fetch(`${baseUrl}${path}`, { ...init, signal: controller.signal });
    const text = await response.text();
    if (!response.ok) {
      // 保留后端稳定业务码（validation_error / ai_session_busy / no_local_data…），
      // 只有响应体不可解析时才退回 http_error。
      throw backendErrorFromStatus(response.status, text);
    }
    try {
      return JSON.parse(text) as T;
    } catch {
      throw new BackendError("payload_error", `${fallbackMessage}：响应不是合法 JSON。`);
    }
  } catch (error) {
    if (error instanceof BackendError) {
      throw error;
    }
    if (options.signal?.aborted) {
      throw new BackendError("cancelled", `${fallbackMessage}：请求已取消。`);
    }
    if (controller.signal.aborted) {
      throw new BackendError("timeout", `${fallbackMessage}（本地服务 ${Math.round(timeoutMs / 1000)} 秒未响应）。`);
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener("abort", forwardAbort);
  }
}

function aiGetJson<T>(
  baseUrl: string,
  path: string,
  fallbackMessage: string,
  options: AiJsonOptions = {}
): Promise<T> {
  return aiRequestJson<T>(baseUrl, path, { headers: { Accept: "application/json" } }, fallbackMessage, options);
}

function aiPostJson<T>(
  baseUrl: string,
  path: string,
  body: Record<string, unknown>,
  fallbackMessage: string,
  options: AiJsonOptions = {}
): Promise<T> {
  return aiRequestJson<T>(
    baseUrl,
    path,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body)
    },
    fallbackMessage,
    options
  );
}

export async function loadAiStatus(baseUrl: string): Promise<AiStatus> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) return mocks.mockAiStatus();
  }
  return aiGetJson<AiStatus>(baseUrl, "/ai/status", "AI 状态查询失败");
}

export async function loadAiConfig(baseUrl: string): Promise<AiConfigView> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) return mocks.mockAiConfig();
  }
  return aiGetJson<AiConfigView>(baseUrl, "/ai/config", "AI 配置读取失败");
}

export async function saveAiConfig(baseUrl: string, payload: AiConfigUpdatePayload): Promise<AiConfigView> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) return mocks.mockAiSaveConfig(payload);
  }
  return aiPostJson<AiConfigView>(
    baseUrl,
    "/ai/config",
    payload as unknown as Record<string, unknown>,
    "AI 配置保存失败"
  );
}

export async function revealAiKey(baseUrl: string): Promise<string> {
  if (!isTauriRuntime()) {
    // 演示 Key 只在 DEV 预览存在；生产构建不含该字面量。
    if (import.meta.env.DEV) {
      return "sk-demo-key-123456";
    }
    throw new BackendError("forbidden", "仅桌面端可读取本机 API Key。");
  }
  const json = await aiGetJson<{ api_key?: string }>(baseUrl, "/ai/config/reveal", "读取 API Key 失败");
  return String(json.api_key ?? "");
}

export async function loadAiNewsDigest(baseUrl: string): Promise<AiNewsDigest> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) return mocks.mockAiNewsDigest();
  }
  return aiGetJson<AiNewsDigest>(baseUrl, "/ai/news", "AI 资讯聚合读取失败");
}

export async function loadAiReports(baseUrl: string): Promise<AiReportsResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) return mocks.mockAiReports();
  }
  return aiGetJson<AiReportsResponse>(baseUrl, "/ai/reports", "AI 报告列表读取失败", {
    timeoutMs: AI_REPORT_JSON_TIMEOUT_MS
  });
}

export async function loadAiReportFile(baseUrl: string, name: string): Promise<string> {
  if (!isTauriRuntime()) {
    if (import.meta.env.DEV) {
      return `# ${name}\n\n（预览模式：示例报告内容。）`;
    }
    throw new BackendError("no_local_data", "本地数据服务未连接（当前不是桌面端运行环境）。");
  }
  const json = await aiGetJson<{ content?: string }>(
    baseUrl,
    `/ai/report/file?name=${encodeURIComponent(name)}`,
    "AI 报告读取失败",
    { timeoutMs: AI_REPORT_JSON_TIMEOUT_MS }
  );
  return String(json.content ?? "");
}

export async function loadAiSessions(baseUrl: string): Promise<AiSessionsResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) return mocks.mockAiSessions();
  }
  return aiGetJson<AiSessionsResponse>(baseUrl, "/ai/sessions", "历史会话列表读取失败");
}

export async function loadAiSession(baseUrl: string, sessionId: string): Promise<AiSessionDetail> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) return mocks.mockAiSessionDetail(sessionId);
  }
  return aiGetJson<AiSessionDetail>(
    baseUrl,
    `/ai/session?session_id=${encodeURIComponent(sessionId)}`,
    "历史对话回读失败"
  );
}

export async function loadAiMemories(baseUrl: string): Promise<AiMemoriesResponse> {
  if (!isTauriRuntime()) {
    return { items: [], rejected_market_facts_total: 0 };
  }
  return aiGetJson<AiMemoriesResponse>(baseUrl, "/ai/memories", "长期记忆列表读取失败");
}

export async function updateAiMemory(
  baseUrl: string,
  payload: { id: string; content: string; category?: string; weight?: number }
): Promise<void> {
  if (!isTauriRuntime()) {
    return;
  }
  await aiPostJson(baseUrl, "/ai/memory/update", payload as Record<string, unknown>, "记忆修改失败");
}

export async function deleteAiMemory(baseUrl: string, id: string): Promise<void> {
  if (!isTauriRuntime()) {
    return;
  }
  await aiPostJson(baseUrl, "/ai/memory/delete", { id }, "记忆删除失败");
}

export async function aiOverfitCheck(
  baseUrl: string,
  payload: {
    metrics: Record<string, unknown>;
    combos?: Array<Record<string, unknown>>;
    rejected_combinations?: number;
  },
  options: AiJsonOptions = {}
): Promise<AiOverfitResult> {
  if (!isTauriRuntime()) {
    return { level: "none", findings: [] };
  }
  // 纯确定性检测：它变慢说明服务有问题，不该让结果区一直等。
  return aiPostJson<AiOverfitResult>(baseUrl, "/ai/overfit/check", payload, "过拟合检测失败", options);
}

export async function aiParseConditions(
  baseUrl: string,
  text: string,
  options: AiJsonOptions = {}
): Promise<AiConditionParseResult> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) return mocks.mockAiConditionParse(text);
  }
  return aiPostJson<AiConditionParseResult>(baseUrl, "/ai/conditions/parse", { text }, "AI 条件解析失败", {
    ...options,
    timeoutMs: options.timeoutMs ?? AI_MODEL_JSON_TIMEOUT_MS
  });
}

export async function aiInsightOneshot(
  baseUrl: string,
  scene: AiInsightScene,
  context: Record<string, unknown>,
  options: AiJsonOptions = {}
): Promise<string> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) return mocks.mockAiInsightOneshot(scene);
  }
  const result = await aiPostJson<AiInsightOneshotResult>(
    baseUrl,
    "/ai/insight/oneshot",
    { scene, context },
    "AI 点评生成失败",
    { ...options, timeoutMs: options.timeoutMs ?? AI_MODEL_JSON_TIMEOUT_MS }
  );
  return result.text;
}

/** NDJSON 事件的线格式：字段按 type 分支，只有分发处认识它们。 */
type StreamEvent = Record<string, unknown> & { type: string };

/** 寻优流的唯一分发实现：预览与真实路径共用；返回值表示是否拿到终态。 */
function dispatchOptimizeEvent(event: StreamEvent, handlers: OptimizeStreamHandlers): boolean {
  if (event.type === "combination") {
    const { type: _eventType, ...combination } = event;
    handlers.onCombination?.(combination as unknown as OptimizeCombination);
  } else if (event.type === "progress") {
    handlers.onProgress?.({
      completed: Number(event.completed ?? 0),
      total: Number(event.total ?? 0)
    });
  } else if (event.type === "phase") {
    handlers.onPhase?.(String(event.phase ?? ""));
  } else if (event.type === "result") {
    handlers.onResult?.((event.result ?? {}) as unknown as OptimizeSummary);
    return true;
  } else if (event.type === "error") {
    throw new BackendError(
      typeof event.code === "string" ? event.code : "request_failed",
      typeof event.message === "string" ? event.message : "AI 参数寻优失败"
    );
  }
  return false;
}

/** 对话流的唯一分发实现：预览与真实路径共用；返回值表示是否拿到终态。 */
function dispatchChatEvent(event: AiChatEvent, handlers: AiChatHandlers): boolean {
  if (event.type === "session") {
    handlers.onSession?.(event);
  } else if (event.type === "phase") {
    handlers.onPhase?.(event.phase);
  } else if (event.type === "token") {
    handlers.onToken?.(event.text);
  } else if (event.type === "tool_call") {
    handlers.onToolCall?.(event);
  } else if (event.type === "tool_result") {
    handlers.onToolResult?.(event);
  } else if (event.type === "result") {
    handlers.onResult?.(event);
    return true;
  } else if (event.type === "error") {
    throw new BackendError(event.code ?? "request_failed", event.message ?? "AI 请求失败");
  }
  return false;
}

/**
 * 有限任务流的完成契约：只有拿到 result 事件（或后端 error 抛出）才算跑完。
 * 只收到 token/progress 就 EOF 必须报中断，否则残缺会被当成成功回答。
 */
async function consumeTerminalStream(
  url: string,
  init: RequestInit,
  options: { signal?: AbortSignal },
  idleTimeoutMs: number,
  label: string,
  onEvent: (event: StreamEvent) => boolean
): Promise<void> {
  let completed = false;
  await consumeNdjsonStream(url, init, options, idleTimeoutMs, (line) => {
    if (!line.trim()) {
      return;
    }
    completed = onEvent(JSON.parse(line) as StreamEvent) || completed;
  });
  if (!completed) {
    throw streamIncompleteError(label);
  }
}

export async function runAiOptimizeStream(
  baseUrl: string,
  request: { strategy: StrategyConfig; settings: BacktestSettingsConfig; grid: Record<string, number[]> },
  handlers: OptimizeStreamHandlers = {},
  options: { signal?: AbortSignal } = {}
): Promise<void> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) {
      let completed = false;
      for (const event of mocks.mockAiOptimizeEvents(request)) {
        completed = dispatchOptimizeEvent(event, handlers) || completed;
      }
      if (!completed) {
        throw streamIncompleteError("AI 参数寻优");
      }
      return;
    }
    // 不用 ai_not_configured：那会让用户被引去填 API Key，而这里是环境问题。
    throw new BackendError("request_failed", "本地数据服务未连接（当前不是桌面端运行环境）。");
  }
  await consumeTerminalStream(
    `${baseUrl}/ai/optimize`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify(request)
    },
    options,
    AI_OPTIMIZE_IDLE_TIMEOUT_MS,
    "AI 参数寻优",
    (event) => dispatchOptimizeEvent(event, handlers)
  );
}

/** 让后台轮次在安全边界收尾（不是杀线程，也不只是断开接收）。
 * ``cancelling`` 为假说明这一轮其实已经在跑了，前端据此如实显示状态。 */
export async function cancelAiChat(baseUrl: string, sessionId: string): Promise<{ ok: boolean; cancelling: boolean }> {
  return aiPostJson<{ ok: boolean; cancelling: boolean }>(
    baseUrl,
    "/ai/chat/cancel",
    { session_id: sessionId },
    "停止生成失败"
  );
}

export async function runAiChatStream(
  baseUrl: string,
  request: AiChatRequest,
  handlers: AiChatHandlers = {},
  options: { signal?: AbortSignal } = {}
): Promise<void> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) {
      let completed = false;
      for (const event of mocks.mockAiChatEvents(request)) {
        completed = dispatchChatEvent(event, handlers) || completed;
      }
      if (!completed) {
        throw streamIncompleteError("AI 回答");
      }
      return;
    }
    // 不用 ai_not_configured：那会让用户被引去填 API Key，而这里是环境问题。
    throw new BackendError("request_failed", "本地数据服务未连接（当前不是桌面端运行环境）。");
  }
  await consumeTerminalStream(
    `${baseUrl}/ai/chat/stream`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify(request)
    },
    options,
    AI_CHAT_STREAM_IDLE_TIMEOUT_MS,
    "AI 回答",
    (event) => dispatchChatEvent(event as unknown as AiChatEvent, handlers)
  );
}

/**
 * Long-lived AI events stream (insight / data_fresh / heartbeat). Resolves when
 * the stream ends or is aborted; callers typically reconnect with backoff.
 * 常驻订阅没有"最终结果"可言：结束即结束，重连由 useAiEventStream 负责。
 */
export async function openAiEventStream(
  baseUrl: string,
  onEvent: (event: AiEventStreamEvent) => void,
  options: { signal?: AbortSignal } = {}
): Promise<void> {
  if (!isTauriRuntime()) {
    const mocks = await previewAiMocks();
    if (mocks) {
      for (const event of mocks.mockAiEventStream()) {
        if (options.signal?.aborted) {
          return;
        }
        onEvent(event);
      }
      return;
    }
    // 生产构建非 Tauri：没有任何事件源，直接等待调用方放弃。
    await new Promise<void>((resolve) => {
      options.signal?.addEventListener("abort", () => resolve(), { once: true });
    });
    return;
  }
  await consumeNdjsonStream(
    `${baseUrl}/ai/events/stream`,
    { headers: { Accept: "application/x-ndjson" } },
    options,
    AI_EVENTS_IDLE_TIMEOUT_MS,
    (line) => {
      if (!line.trim()) {
        return;
      }
      onEvent(JSON.parse(line) as AiEventStreamEvent);
    }
  );
}
