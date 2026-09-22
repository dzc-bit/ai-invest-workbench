import { BackendError, consumeNdjsonStream } from "./api";
import { isTauriRuntime } from "./tauriRuntime";
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
  AiNewsDigest,
  AiOverfitResult,
  AiReportsResponse,
  AiSessionDetail,
  AiSessionsResponse,
  AiStatus
} from "./aiTypes";
import type { BacktestSettingsConfig, OptimizeStreamHandlers, StrategyConfig } from "./types";
import {
  mockAiChatEvents,
  mockAiConfig,
  mockAiConditionParse,
  mockAiEventStream,
  mockAiInsightOneshot,
  mockAiNewsDigest,
  mockAiOptimizeEvents,
  mockAiReports,
  mockAiSaveConfig,
  mockAiSessionDelete,
  mockAiSessionDetail,
  mockAiSessions,
  mockAiStatus
} from "./aiMocks";

const AI_CHAT_STREAM_IDLE_TIMEOUT_MS = 180_000;
const AI_EVENTS_IDLE_TIMEOUT_MS = 40_000;
const AI_OPTIMIZE_IDLE_TIMEOUT_MS = 120_000;

async function aiPostJson<T>(baseUrl: string, path: string, body: Record<string, unknown>, fallbackMessage: string): Promise<T> {
  const response = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  const json = await response.json();
  if (!response.ok) {
    throw new BackendError(
      typeof json.code === "string" ? json.code : "request_failed",
      typeof json.message === "string" ? json.message : fallbackMessage
    );
  }
  return json as T;
}

export async function loadAiStatus(baseUrl: string): Promise<AiStatus> {
  if (!isTauriRuntime()) {
    return mockAiStatus();
  }
  const response = await fetch(`${baseUrl}/ai/status`);
  const json = await response.json();
  if (!response.ok) {
    throw new BackendError(typeof json.code === "string" ? json.code : "request_failed", "AI 状态查询失败");
  }
  return json as AiStatus;
}

export async function loadAiConfig(baseUrl: string): Promise<AiConfigView> {
  if (!isTauriRuntime()) {
    return mockAiConfig();
  }
  const response = await fetch(`${baseUrl}/ai/config`);
  const json = await response.json();
  if (!response.ok) {
    throw new BackendError(typeof json.code === "string" ? json.code : "request_failed", "AI 配置读取失败");
  }
  return json as AiConfigView;
}

export async function saveAiConfig(baseUrl: string, payload: AiConfigUpdatePayload): Promise<AiConfigView> {
  if (!isTauriRuntime()) {
    return mockAiSaveConfig(payload);
  }
  const response = await fetch(`${baseUrl}/ai/config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  const json = await response.json();
  if (!response.ok) {
    throw new BackendError(typeof json.code === "string" ? json.code : "request_failed", "AI 配置保存失败");
  }
  return json as AiConfigView;
}

export async function revealAiKey(baseUrl: string): Promise<string> {
  if (!isTauriRuntime()) {
    return "sk-demo-key-123456";
  }
  const response = await fetch(`${baseUrl}/ai/config/reveal`);
  const json = await response.json();
  if (!response.ok) {
    throw new BackendError(typeof json.code === "string" ? json.code : "request_failed", "读取 API Key 失败");
  }
  return String(json.api_key ?? "");
}

export async function loadAiNewsDigest(baseUrl: string): Promise<AiNewsDigest> {
  if (!isTauriRuntime()) {
    return mockAiNewsDigest();
  }
  const response = await fetch(`${baseUrl}/ai/news`);
  const json = await response.json();
  if (!response.ok) {
    throw new BackendError(typeof json.code === "string" ? json.code : "request_failed", "AI 资讯聚合读取失败");
  }
  return json as AiNewsDigest;
}

export async function loadAiReports(baseUrl: string): Promise<AiReportsResponse> {
  if (!isTauriRuntime()) {
    return mockAiReports();
  }
  const response = await fetch(`${baseUrl}/ai/reports`);
  const json = await response.json();
  if (!response.ok) {
    throw new BackendError(typeof json.code === "string" ? json.code : "request_failed", "AI 报告列表读取失败");
  }
  return json as AiReportsResponse;
}

export async function loadAiReportFile(baseUrl: string, name: string): Promise<string> {
  if (!isTauriRuntime()) {
    return `# ${name}\n\n（预览模式：示例报告内容。）`;
  }
  const response = await fetch(`${baseUrl}/ai/report/file?name=${encodeURIComponent(name)}`);
  const json = await response.json();
  if (!response.ok) {
    throw new BackendError(typeof json.code === "string" ? json.code : "request_failed", "AI 报告读取失败");
  }
  return String(json.content ?? "");
}

export async function loadAiSessions(baseUrl: string): Promise<AiSessionsResponse> {
  if (!isTauriRuntime()) {
    return mockAiSessions();
  }
  const response = await fetch(`${baseUrl}/ai/sessions`);
  const json = await response.json();
  if (!response.ok) {
    throw new BackendError(typeof json.code === "string" ? json.code : "request_failed", "历史会话列表读取失败");
  }
  return json as AiSessionsResponse;
}

export async function loadAiSession(baseUrl: string, sessionId: string): Promise<AiSessionDetail> {
  if (!isTauriRuntime()) {
    return mockAiSessionDetail(sessionId);
  }
  const response = await fetch(`${baseUrl}/ai/session?session_id=${encodeURIComponent(sessionId)}`);
  const json = await response.json();
  if (!response.ok) {
    throw new BackendError(typeof json.code === "string" ? json.code : "request_failed", "历史对话回读失败");
  }
  return json as AiSessionDetail;
}

export async function deleteAiSession(baseUrl: string, sessionId: string): Promise<boolean> {
  if (!isTauriRuntime()) {
    return mockAiSessionDelete(sessionId);
  }
  const result = await aiPostJson<{ deleted?: boolean }>(
    baseUrl,
    "/ai/session/delete",
    { session_id: sessionId },
    "历史会话删除失败"
  );
  return result.deleted === true;
}

export async function aiOverfitCheck(
  baseUrl: string,
  payload: { metrics: Record<string, unknown>; combos?: Array<Record<string, unknown>> }
): Promise<AiOverfitResult> {
  if (!isTauriRuntime()) {
    return { level: "none", findings: [] };
  }
  return aiPostJson<AiOverfitResult>(baseUrl, "/ai/overfit/check", payload, "过拟合检测失败");
}

export async function aiParseConditions(baseUrl: string, text: string): Promise<AiConditionParseResult> {
  if (!isTauriRuntime()) {
    return mockAiConditionParse(text);
  }
  return aiPostJson<AiConditionParseResult>(baseUrl, "/ai/conditions/parse", { text }, "AI 条件解析失败");
}

export async function aiInsightOneshot(
  baseUrl: string,
  scene: AiInsightScene,
  context: Record<string, unknown>
): Promise<string> {
  if (!isTauriRuntime()) {
    return mockAiInsightOneshot(scene);
  }
  const result = await aiPostJson<AiInsightOneshotResult>(baseUrl, "/ai/insight/oneshot", { scene, context }, "AI 点评生成失败");
  return result.text;
}

export async function runAiOptimizeStream(
  baseUrl: string,
  request: { strategy: StrategyConfig; settings: BacktestSettingsConfig; grid: Record<string, number[]> },
  handlers: OptimizeStreamHandlers = {},
  options: { signal?: AbortSignal } = {}
): Promise<void> {
  if (!isTauriRuntime()) {
    for (const event of mockAiOptimizeEvents(request)) {
      dispatchOptimizeEvent(event, handlers);
    }
    return;
  }
  await consumeNdjsonStream(
    `${baseUrl}/ai/optimize`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify(request)
    },
    options,
    AI_OPTIMIZE_IDLE_TIMEOUT_MS,
    (line) => {
      if (!line.trim()) {
        return;
      }
      const event = JSON.parse(line) as Record<string, unknown> & { type: string };
      if (event.type === "combination") {
        const { type: _eventType, ...combination } = event;
        handlers.onCombination?.(combination as never);
      } else if (event.type === "progress") {
        handlers.onProgress?.({
          completed: Number(event.completed ?? 0),
          total: Number(event.total ?? 0)
        });
      } else if (event.type === "phase") {
        handlers.onPhase?.(String(event.phase ?? ""));
      } else if (event.type === "result") {
        handlers.onResult?.((event.result ?? {}) as never);
      } else if (event.type === "error") {
        throw new BackendError(
          typeof event.code === "string" ? event.code : "request_failed",
          typeof event.message === "string" ? event.message : "AI 参数寻优失败"
        );
      }
    }
  );
}

function dispatchOptimizeEvent(
  event: Record<string, unknown> & { type: string },
  handlers: OptimizeStreamHandlers
): void {
  if (event.type === "combination") {
    const { type: _eventType, ...combination } = event;
    handlers.onCombination?.(combination as never);
  } else if (event.type === "progress") {
    handlers.onProgress?.({
      completed: Number(event.completed ?? 0),
      total: Number(event.total ?? 0)
    });
  } else if (event.type === "phase") {
    handlers.onPhase?.(String(event.phase ?? ""));
  } else if (event.type === "result") {
    handlers.onResult?.((event.result ?? {}) as never);
  } else if (event.type === "error") {
    throw new BackendError(
      typeof event.code === "string" ? event.code : "request_failed",
      typeof event.message === "string" ? event.message : "AI 参数寻优失败"
    );
  }
}

export async function runAiChatStream(
  baseUrl: string,
  request: AiChatRequest,
  handlers: AiChatHandlers = {},
  options: { signal?: AbortSignal } = {}
): Promise<void> {
  if (!isTauriRuntime()) {
    for (const event of mockAiChatEvents(request)) {
      dispatchChatEvent(event, handlers);
    }
    return;
  }
  await consumeNdjsonStream(
    `${baseUrl}/ai/chat/stream`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/x-ndjson" },
      body: JSON.stringify(request)
    },
    options,
    AI_CHAT_STREAM_IDLE_TIMEOUT_MS,
    (line) => {
      if (!line.trim()) {
        return;
      }
      dispatchChatEvent(JSON.parse(line) as AiChatEvent, handlers);
    }
  );
}

function dispatchChatEvent(event: AiChatEvent, handlers: AiChatHandlers): void {
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
  } else if (event.type === "error") {
    throw new BackendError(event.code ?? "request_failed", event.message ?? "AI 请求失败");
  }
}

/**
 * Long-lived AI events stream (insight / data_fresh / heartbeat). Resolves when
 * the stream ends or is aborted; callers typically reconnect with backoff.
 */
export async function openAiEventStream(
  baseUrl: string,
  onEvent: (event: AiEventStreamEvent) => void,
  options: { signal?: AbortSignal } = {}
): Promise<void> {
  if (!isTauriRuntime()) {
    for (const event of mockAiEventStream()) {
      if (options.signal?.aborted) {
        return;
      }
      onEvent(event);
    }
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
