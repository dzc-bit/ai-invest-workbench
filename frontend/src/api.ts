import { invoke } from "@tauri-apps/api/core";
import type {
  BacktestResult,
  BacktestStreamHandlers,
  BacktestSettingsConfig,
  DataServiceHealth,
  DataServiceStatus,
  DailyBarsCoverageResponse,
  DiagnosticsDataGapsResponse,
  DiagnosticsSourcesResponse,
  FetchResult,
  ImportResult,
  ConditionValidationResult,
  ClsFinanceResponse,
  MarketBriefingResponse,
  MarketCommentaryResponse,
  MarketNewsResponse,
  NewsSummaryResponse,
  RealtimeMarketSnapshot,
  RecommendedStrategiesResponse,
  RiskAlertsResponse,
  StockSymbolValidationResult,
  StrategyConfig,
  SyncJobStatus
} from "./types";
import { isTauriRuntime } from "./tauriRuntime";
import { previewApiMocks } from "./previewMocks";

type BackendResponse<T> = ({ ok: true } & T) | { ok: false; error: { code: string; message: string } };

export class BackendError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "BackendError";
    this.code = code;
  }
}

async function callBackend<T>(payload: Record<string, unknown>): Promise<T> {
  if (!isTauriRuntime()) {
    // 浏览器预览（非 Tauri）：演示数据只存在于 DEV 构建。生产包不含 mock，
    // 这里直接把异常抛给调用方，绝不静默伪造回测结果。
    const mocks = await previewApiMocks();
    if (mocks) {
      if (payload.command === "coverage") {
        return mocks.mockCallBackendCoverage() as T;
      }
      return { result: mocks.demoBacktestResult } as T;
    }
    throw new BackendError("no_local_data", "本地数据服务未连接（当前不是桌面端运行环境）。");
  }
  const response = await invoke<BackendResponse<T>>("backend_command", { payload });
  if (!response.ok) {
    throw new BackendError(response.error.code, response.error.message);
  }
  return response;
}

const DEFAULT_SERVICE_TIMEOUT_MS = 12_000;
const LONG_RUNNING_SERVICE_TIMEOUT_MS = 300_000;
const HEALTH_SERVICE_TIMEOUT_MS = 60_000;
const CLS_FINANCE_SERVICE_TIMEOUT_MS = 30_000;
const NEWS_SERVICE_TIMEOUT_MS = 20_000;
const REALTIME_STREAM_IDLE_TIMEOUT_MS = 15_000;
const BACKTEST_STREAM_IDLE_TIMEOUT_MS = 120_000;

type ServiceFetchOptions = {
  timeoutMs?: number;
};

export type StreamRequestOptions = {
  signal?: AbortSignal;
  idleTimeoutMs?: number;
};

export async function consumeNdjsonStream(
  url: string,
  init: RequestInit,
  options: StreamRequestOptions,
  defaultIdleTimeoutMs: number,
  onLine: (line: string) => void
): Promise<void> {
  const controller = new AbortController();
  const idleTimeoutMs = options.idleTimeoutMs ?? defaultIdleTimeoutMs;
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null;
  let readerCancelled = false;
  let completed = false;
  let idleTimer: number | undefined;
  let rejectIdle: (error: Error) => void = () => undefined;
  let rejectAbort: (error: Error) => void = () => undefined;
  const idleFailure = new Promise<never>((_resolve, reject) => {
    rejectIdle = reject;
  });
  const abortFailure = new Promise<never>((_resolve, reject) => {
    rejectAbort = reject;
  });

  const cancelReader = (reason: Error) => {
    if (!reader || readerCancelled) {
      return;
    }
    readerCancelled = true;
    void reader.cancel(reason).catch(() => undefined);
  };
  const failForAbort = () => {
    const error = new Error("stream request cancelled");
    rejectAbort(error);
    cancelReader(error);
    controller.abort(error);
  };
  const armIdleTimer = () => {
    if (idleTimer !== undefined) {
      window.clearTimeout(idleTimer);
    }
    idleTimer = window.setTimeout(() => {
      const error = new BackendError("timeout", `stream idle timeout after ${idleTimeoutMs}ms`);
      rejectIdle(error);
      cancelReader(error);
      controller.abort(error);
    }, idleTimeoutMs);
  };

  if (options.signal?.aborted) {
    failForAbort();
  } else {
    options.signal?.addEventListener("abort", failForAbort, { once: true });
  }

  try {
    armIdleTimer();
    const response = await Promise.race([
      fetch(url, { ...init, signal: controller.signal }),
      idleFailure,
      abortFailure
    ]);
    if (!response.ok) {
      armIdleTimer();
      const text = await Promise.race([response.text(), idleFailure, abortFailure]);
      throw new BackendError("http_error", `HTTP ${response.status}: ${text || "local data service request failed"}`);
    }
    if (!response.body) {
      throw new Error("NDJSON stream is not available in this browser.");
    }

    const decoder = new TextDecoder();
    reader = response.body.getReader();
    let buffer = "";
    while (true) {
      armIdleTimer();
      const { done, value } = await Promise.race([reader.read(), idleFailure, abortFailure]);
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        onLine(line);
      }
    }
    buffer += decoder.decode();
    onLine(buffer);
    completed = true;
  } finally {
    if (!completed && reader && !readerCancelled) {
      const error = new Error("stream consumption failed");
      cancelReader(error);
      controller.abort(error);
    }
    if (idleTimer !== undefined) {
      window.clearTimeout(idleTimer);
    }
    options.signal?.removeEventListener("abort", failForAbort);
  }
}

async function serviceFetch<T>(
  baseUrl: string,
  path: string,
  payload?: Record<string, unknown>,
  options: ServiceFetchOptions = {}
): Promise<T> {
  const controller = new AbortController();
  const timeoutMs = options.timeoutMs ?? DEFAULT_SERVICE_TIMEOUT_MS;
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`${baseUrl}${path}`, {
      method: payload ? "POST" : "GET",
      headers: { "Content-Type": "application/json" },
      body: payload ? JSON.stringify(payload) : undefined,
      signal: controller.signal
    });
    const json = await response.json();
    if (!response.ok) {
      throw new BackendError(
        typeof json.code === "string" ? json.code : "request_failed",
        typeof json.message === "string" ? json.message : "local data service request failed"
      );
    }
    return json as T;
  } catch (error) {
    if (controller.signal.aborted || (error instanceof DOMException && error.name === "AbortError")) {
      throw new BackendError("timeout", "本地数据服务请求超时，请稍后重试或重新连接本地服务。");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function ensureDataService(cacheDir: string): Promise<DataServiceStatus> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockDataServiceStatus(cacheDir);
  }
  return invoke<DataServiceStatus>("ensure_data_service", { cacheDir });
}

export async function loadDataServiceHealth(baseUrl: string): Promise<DataServiceHealth> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockDataServiceHealth();
  }
  return serviceFetch<DataServiceHealth>(baseUrl, "/health", undefined, { timeoutMs: HEALTH_SERVICE_TIMEOUT_MS });
}

export async function loadDataServiceLogs(baseUrl: string): Promise<{ items: Array<{ level: "info" | "warning" | "error"; message: string; timestamp?: string }> }> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockDataServiceLogs();
  }
  return serviceFetch(baseUrl, "/logs/recent");
}

export async function loadRealtimeMarketSnapshot(baseUrl: string): Promise<RealtimeMarketSnapshot> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockRealtimeMarketSnapshot();
  }
  return serviceFetch<RealtimeMarketSnapshot>(baseUrl, "/realtime/market-snapshot");
}

type RealtimeSnapshotStreamEvent =
  | Partial<RealtimeMarketSnapshot> & {
      type: "indexes";
      indexes: RealtimeMarketSnapshot["indexes"];
      updated_at?: string;
    }
  | Partial<RealtimeMarketSnapshot> & {
      type: "breadth";
      breadth?: RealtimeMarketSnapshot["breadth"];
      updated_at?: string;
    }
  | Partial<RealtimeMarketSnapshot> & {
      type: "sectors";
      strong_sectors?: RealtimeMarketSnapshot["strong_sectors"];
      yesterday_strong_sectors?: RealtimeMarketSnapshot["yesterday_strong_sectors"];
      updated_at?: string;
    }
  | { type: "result"; snapshot: RealtimeMarketSnapshot }
  | { type: "error"; message?: string; code?: string };

type RealtimeSnapshotStreamHandlers = {
  onSnapshot?: (snapshot: RealtimeMarketSnapshot) => void;
};

function partialRealtimeSnapshot(
  current: RealtimeMarketSnapshot | null,
  event: Exclude<RealtimeSnapshotStreamEvent, { type: "result" | "error" }>
): RealtimeMarketSnapshot {
  const now = event.updated_at ?? current?.updated_at ?? new Date().toISOString();
  const next: RealtimeMarketSnapshot = {
    status: current?.status ?? "stale",
    source: current?.source ?? "stream-partial",
    updated_at: now,
    market_phase: event.market_phase ?? current?.market_phase ?? "trading",
    indexes: current?.indexes ?? [],
    breadth: current?.breadth ?? null,
    strong_sectors: current?.strong_sectors ?? [],
    yesterday_strong_sectors: current?.yesterday_strong_sectors ?? [],
    message: current?.message ?? "实时行情分块加载中",
    diagnostics: event.diagnostics ?? current?.diagnostics ?? []
  };
  if (event.type === "indexes") {
    next.indexes = event.indexes;
    next.source = event.indexes[0]?.source ?? next.source;
    next.message = "实时指数已返回，继续加载红绿家数和板块";
  } else if (event.type === "breadth") {
    next.breadth = event.breadth ?? null;
    next.source = event.breadth?.source ?? next.source;
    // breadth 事件可能携带 null（provider 链整体超时）：如实说明“未返回”，
    // 不能伪装成“已返回”。
    next.message = event.breadth
      ? "实时红绿家数已返回，继续加载板块"
      : "实时红绿家数未返回，尝试备选数据源与本地统计";
  } else if (event.type === "sectors") {
    next.strong_sectors = event.strong_sectors ?? [];
    next.yesterday_strong_sectors = event.yesterday_strong_sectors ?? next.yesterday_strong_sectors;
    next.source = next.strong_sectors[0]?.source ?? next.source;
    next.message = "实时板块已返回，正在完成行情快照";
  }
  return next;
}

export async function loadRealtimeMarketSnapshotStream(
  baseUrl: string,
  handlers: RealtimeSnapshotStreamHandlers = {},
  options: StreamRequestOptions = {}
): Promise<RealtimeMarketSnapshot> {
  if (!isTauriRuntime()) {
    const snapshot = await loadRealtimeMarketSnapshot(baseUrl);
    handlers.onSnapshot?.(snapshot);
    return snapshot;
  }

  let current: RealtimeMarketSnapshot | null = null;
  let finalResult: RealtimeMarketSnapshot | null = null;
  let streamError: string | null = null;
  let streamErrorCode = "request_failed";

  const handleLine = (line: string) => {
    if (!line.trim()) {
      return;
    }
    const event = JSON.parse(line) as RealtimeSnapshotStreamEvent;
    if (event.type === "error") {
      streamError = event.message ?? "Realtime market stream failed.";
      if (event.code) {
        streamErrorCode = event.code;
      }
      return;
    }
    if (event.type === "result") {
      finalResult = event.snapshot;
      current = event.snapshot;
      handlers.onSnapshot?.(event.snapshot);
      return;
    }
    current = partialRealtimeSnapshot(current, event);
    handlers.onSnapshot?.(current);
  };

  await consumeNdjsonStream(
    `${baseUrl}/realtime/market-snapshot/stream`,
    { headers: { Accept: "application/x-ndjson" } },
    options,
    REALTIME_STREAM_IDLE_TIMEOUT_MS,
    handleLine
  );

  if (!finalResult) {
    throw new BackendError(
      streamErrorCode,
      streamError ?? "Realtime market stream ended before a final result was produced."
    );
  }
  return finalResult;
}

export async function loadMarketNews(baseUrl: string): Promise<MarketNewsResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockMarketNews();
  }
  return serviceFetch<MarketNewsResponse>(baseUrl, "/market/news", undefined, {
    timeoutMs: NEWS_SERVICE_TIMEOUT_MS
  });
}

export async function loadMarketBriefing(baseUrl: string, kind: "fupan" | "zaopan"): Promise<MarketBriefingResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockMarketBriefing(kind);
  }
  return serviceFetch<MarketBriefingResponse>(baseUrl, `/market/${kind}`);
}

/** 行情评价（怎么读盘面）。后端是四段状态机：非 intraday/post_close 的 mode
 * 都不是实时盘面结论，调用方必须把状态语义显式展示（AGENTS.md §6）。 */
export async function loadMarketCommentary(baseUrl: string): Promise<MarketCommentaryResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockMarketCommentary();
  }
  return serviceFetch<MarketCommentaryResponse>(baseUrl, "/market/commentary");
}

export async function loadClsFinance(baseUrl: string): Promise<ClsFinanceResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockClsFinance();
  }
  return serviceFetch<ClsFinanceResponse>(baseUrl, "/market/finance", undefined, {
    timeoutMs: CLS_FINANCE_SERVICE_TIMEOUT_MS
  });
}

export async function loadNewsSummary(baseUrl: string): Promise<NewsSummaryResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockNewsSummary();
  }
  return serviceFetch<NewsSummaryResponse>(baseUrl, "/market/news-summary", undefined, {
    timeoutMs: NEWS_SERVICE_TIMEOUT_MS
  });
}

export async function loadRiskAlerts(baseUrl: string): Promise<RiskAlertsResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockRiskAlerts();
  }
  return serviceFetch<RiskAlertsResponse>(baseUrl, "/risk/alerts");
}

export async function loadDiagnosticsSources(baseUrl: string): Promise<DiagnosticsSourcesResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockDiagnosticsSources();
  }
  return serviceFetch<DiagnosticsSourcesResponse>(baseUrl, "/diagnostics/sources");
}

export async function loadDiagnosticsDataGaps(baseUrl: string): Promise<DiagnosticsDataGapsResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockDiagnosticsDataGaps();
  }
  return serviceFetch<DiagnosticsDataGapsResponse>(baseUrl, "/diagnostics/data-gaps");
}

export async function validateConditionExpression(
  baseUrl: string,
  text: string,
  mode: "entry" | "exit" = "entry"
): Promise<ConditionValidationResult> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockConditionValidation(text, mode);
  }
  return serviceFetch<ConditionValidationResult>(baseUrl, "/strategy/conditions/validate", { text, mode });
}

export async function validateStockSymbols(baseUrl: string, symbols: string[]): Promise<StockSymbolValidationResult> {
  const normalizedSymbols = symbols.map((symbol) => symbol.trim()).filter(Boolean);
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockStockSymbolValidation(normalizedSymbols);
  }
  return serviceFetch<StockSymbolValidationResult>(baseUrl, "/symbols/validate", { symbols: normalizedSymbols });
}

export async function loadRecommendedStrategies(baseUrl: string): Promise<RecommendedStrategiesResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockRecommendedStrategies();
  }
  return serviceFetch<RecommendedStrategiesResponse>(baseUrl, "/strategy/recommended");
}

export async function loadDailyBarsCoverage(
  baseUrl: string,
  symbols: string[],
  startDate: string,
  endDate: string
): Promise<DailyBarsCoverageResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockDailyBarsCoverage(symbols, startDate, endDate);
  }
  return serviceFetch<DailyBarsCoverageResponse>(baseUrl, "/coverage/daily-bars", {
    symbols,
    start_date: startDate,
    end_date: endDate
  });
}

export async function fetchDailyBars(
  baseUrl: string,
  symbols: string[],
  startDate: string,
  endDate: string
): Promise<FetchResult> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockFetchDailyBarsResult(symbols, startDate, endDate);
  }
  return serviceFetch<FetchResult>(
    baseUrl,
    "/fetch/daily-bars",
    {
      symbols,
      start_date: startDate,
      end_date: endDate
    },
    { timeoutMs: LONG_RUNNING_SERVICE_TIMEOUT_MS }
  );
}

export async function fetchCapitalFlow(
  baseUrl: string,
  symbols: string[],
  startDate: string,
  endDate: string
): Promise<FetchResult> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockFetchCapitalFlowResult(symbols, startDate, endDate);
  }
  return serviceFetch<FetchResult>(
    baseUrl,
    "/fetch/capital-flow",
    {
      symbols,
      start_date: startDate,
      end_date: endDate
    },
    { timeoutMs: LONG_RUNNING_SERVICE_TIMEOUT_MS }
  );
}

export async function importDailyBars(baseUrl: string, source: "sample" | "file", path?: string): Promise<ImportResult> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockImportDailyBarsResult(source, path);
  }
  return serviceFetch<ImportResult>(baseUrl, "/import/daily-bars", { source, path }, { timeoutMs: LONG_RUNNING_SERVICE_TIMEOUT_MS });
}

export async function startFullMarketSync(
  baseUrl: string,
  startDate: string,
  endDate: string,
  symbols?: string[]
): Promise<{ job: SyncJobStatus }> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockStartFullMarketSync(startDate, endDate, symbols);
  }
  return serviceFetch<{ job: SyncJobStatus }>(
    baseUrl,
    "/sync/full-market",
    {
      symbols,
      start_date: startDate,
      end_date: endDate
    },
    { timeoutMs: LONG_RUNNING_SERVICE_TIMEOUT_MS }
  );
}

/** 「只补缺口」：以缺口为基准的补齐入口（§9）。后端用 incomplete_symbols
 * 计算窗口内不完整的股票名单，只对名单发起抓取，不做全量扫描。 */
export type MissingOnlySyncResponse = {
  started: boolean;
  reason?: string;
  missing_symbols: number;
  start_date: string;
  end_date: string;
  job?: SyncJobStatus;
};

export async function startMissingOnlySync(
  baseUrl: string,
  startDate: string,
  endDate: string
): Promise<MissingOnlySyncResponse> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) {
      const mock = await mocks.mockStartFullMarketSync(startDate, endDate);
      return { started: true, missing_symbols: mock.job.total_symbols, start_date: startDate, end_date: endDate, job: mock.job };
    }
    throw new BackendError("no_local_data", "本地数据服务未连接（当前不是桌面端运行环境）。");
  }
  return serviceFetch<MissingOnlySyncResponse>(
    baseUrl,
    "/sync/missing-only",
    {
      start_date: startDate,
      end_date: endDate
    },
    { timeoutMs: LONG_RUNNING_SERVICE_TIMEOUT_MS }
  );
}

export async function loadSyncJob(baseUrl: string, jobId: string): Promise<{ job: SyncJobStatus }> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockLoadSyncJob(jobId);
  }
  return serviceFetch<{ job: SyncJobStatus }>(baseUrl, `/sync/jobs/${jobId}`);
}

export async function cancelSyncJob(baseUrl: string, jobId: string): Promise<{ job: SyncJobStatus }> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) return mocks.mockCancelSyncJob(jobId);
  }
  return serviceFetch<{ job: SyncJobStatus }>(baseUrl, `/sync/jobs/${jobId}/cancel`, {});
}

export async function runBacktestStreamWithDataService(
  baseUrl: string,
  strategy: StrategyConfig,
  settings: BacktestSettingsConfig,
  handlers: BacktestStreamHandlers = {},
  options: StreamRequestOptions = {}
): Promise<BacktestResult> {
  if (!isTauriRuntime()) {
    const mocks = await previewApiMocks();
    if (mocks) {
      handlers.onPhase?.("校验参数");
      handlers.onPhase?.("读取本地数据");
      handlers.onProgress?.({ message: "扫描 2024-01-05：候选 1 只，持仓 0 只" });
      handlers.onTrade?.(mocks.demoBacktestResult.trades[0]);
      handlers.onResult?.(mocks.demoBacktestResult);
      return mocks.demoBacktestResult;
    }
    throw new BackendError("no_local_data", "本地数据服务未连接（当前不是桌面端运行环境）。");
  }

  let finalResult: BacktestResult | null = null;

  const handleLine = (line: string) => {
    if (!line.trim()) {
      return;
    }
    const event = JSON.parse(line) as
      | { type: "phase"; phase: string }
      | { type: "progress"; message: string; trade_date?: string; scanned_days?: number; total_days?: number; open_positions?: number; closed_trades?: number; candidates?: number }
      | { type: "trade_opened"; trade: BacktestResult["trades"][number] }
      | { type: "trade_closed"; trade: BacktestResult["trades"][number] }
      | { type: "trade_blocked"; trade: BacktestResult["trades"][number] }
      | { type: "result"; result: BacktestResult }
      | { type: "error"; message?: string; code?: string };
    if (event.type === "phase") {
      handlers.onPhase?.(event.phase);
    } else if (event.type === "progress") {
      handlers.onProgress?.(event);
    } else if (event.type === "trade_opened" || event.type === "trade_closed" || event.type === "trade_blocked") {
      handlers.onTrade?.(event.trade);
    } else if (event.type === "result") {
      finalResult = event.result;
      handlers.onResult?.(event.result);
    } else if (event.type === "error") {
      throw new BackendError(event.code ?? "request_failed", event.message ?? "Backtest stream failed.");
    }
  };

  await consumeNdjsonStream(
    `${baseUrl}/run/backtest/stream`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy, settings })
    },
    options,
    BACKTEST_STREAM_IDLE_TIMEOUT_MS,
    handleLine
  );

  if (!finalResult) {
    throw new Error("Backtest stream ended before a final result was produced.");
  }
  return finalResult;
}

export async function runConfiguredBacktest(strategy: StrategyConfig, settings: BacktestSettingsConfig): Promise<BacktestResult> {
  const response = await callBackend<{ result: BacktestResult }>({
    command: "run_backtest",
    strategy,
    settings,
    cache_dir: ".astock-cache"
  });
  return response.result;
}
