import { useEffect, useMemo, useState } from "react";
import {
  cancelSyncJob,
  ensureDataService,
  fetchCapitalFlow,
  fetchDailyBars,
  importDailyBars,
  loadDailyBarsCoverage,
  loadDataServiceHealth,
  loadDataServiceLogs,
  loadDiagnosticsDataGaps,
  loadDiagnosticsSources,
  loadSyncJob,
  startFullMarketSync,
  startMissingOnlySync
} from "../api";
import { aiInsightOneshot } from "../aiApi";
import type { DataSourceHealth, DiagnosticsDataGapsResponse, DiagnosticsSourcesResponse } from "../types";
import { recentAShareTradingDateRange } from "../tradingCalendar";
import type { DailyBarsCoverageItem, DataServiceStatus, DatasetCoverage, ServiceLogEntry, SyncJobStatus } from "../types";

type Props = {
  cacheDir: string;
  coverage: DatasetCoverage[];
  onCoverageChange: (coverage: DatasetCoverage[]) => void;
  onServiceReady?: (service: DataServiceStatus) => void;
};

type BusyAction = "refresh" | "fetch" | "capital-flow" | "sample" | "file" | "sync" | "missing-only" | null;

const COVERAGE_REFRESH_RETRY_MS = 1200;
const COVERAGE_REFRESH_MAX_ATTEMPTS = 150;
function dailyBarsCoverage(coverage: DatasetCoverage[]): DatasetCoverage | undefined {
  return coverage.find((item) => item.dataset === "daily_bars");
}

function coverageFillDateRange(coverage: DatasetCoverage[]): { startDate: string; endDate: string } {
  const fallback = recentAShareTradingDateRange();
  const daily = dailyBarsCoverage(coverage);
  if (daily?.end_date && daily.symbols >= 100 && daily.end_date < fallback.endDate) {
    return { startDate: daily.end_date, endDate: fallback.endDate };
  }
  return fallback;
}

const datasetLabels: Record<string, { label: string; source: string }> = {
  daily_bars: { label: "日线行情", source: "a-stock-data / 本地缓存" },
  capital_flow: { label: "资金流向", source: "东方财富/百度资金流 / 本地缓存" },
  market_cap: { label: "市值数据", source: "A股基础指标 / 本地缓存" }
};

function formatList(values: string[]): string {
  return values.length === 0 ? "无" : values.join(", ");
}

type OperationResultMessage = {
  logs: { message: string }[];
  failures?: Array<Record<string, unknown>>;
  diagnostics?: Array<Record<string, unknown>>;
};

function operationMessage(result: OperationResultMessage): string {
  const failureSymbols = (result.failures ?? [])
    .map((item) => item.symbol)
    .filter((value): value is string => typeof value === "string" && value.length > 0);
  if (failureSymbols.length > 0) {
    const diagnosticCodes = (result.diagnostics ?? [])
      .map((item) => item.code)
      .filter((value): value is string => typeof value === "string" && value.length > 0);
    const codeText = diagnosticCodes.length > 0 ? ` / ${Array.from(new Set(diagnosticCodes)).join(", ")}` : "";
    const logText = result.logs.at(-1)?.message;
    return `部分失败: ${Array.from(new Set(failureSymbols)).join(", ")}${codeText}${logText ? ` / ${logText}` : ""}`;
  }
  return result.logs.at(-1)?.message ?? "数据操作已完成";
}

function connectionErrorMessage(error: unknown): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  if (typeof error === "string" && error.trim()) {
    return error;
  }
  return "本地数据服务连接失败，请重试。";
}

function isSyncRunning(job: SyncJobStatus | null): boolean {
  return job?.status === "running" || job?.status === "cancelling";
}

function syncRunningMessage(job: SyncJobStatus): string {
  if (job.status === "cancelling") {
    return "正在停止任务，已导入的数据会保留";
  }
  return `${job.mode === "capital_flow_backfill" ? "正在补齐全市场资金流" : "正在下载全市场历史数据"}，已处理 ${job.processed_symbols ?? job.completed_symbols}/${job.total_symbols}`;
}

function syncFinishedMessage(job: SyncJobStatus): string {
  if (job.status === "cancelled") {
    return job.mode === "capital_flow_backfill" ? "资金流补齐已停止" : "全市场下载已停止";
  }
  const filledText = syncFilledText(job);
  return `${job.mode === "capital_flow_backfill" ? "资金流补齐完成" : "全市场下载完成"}，写入 ${job.imported_rows} 行${filledText}`;
}

function syncFilledText(job: SyncJobStatus): string {
  if (job.mode === "capital_flow_backfill") {
    return "";
  }
  const parts: string[] = [];
  const dailyRows = job.filled_daily_rows ?? 0;
  const marketCapRows = job.filled_market_cap_rows ?? 0;
  if (dailyRows > 0) {
    parts.push(`补齐日线 ${dailyRows} 行`);
  }
  if (marketCapRows > 0) {
    parts.push(`补齐市值 ${marketCapRows} 行`);
  }
  return parts.length > 0 ? `，${parts.join("，")}` : "";
}

function fieldText(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) {
    return value.trim();
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return `${value}`;
  }
  return null;
}

function syncFailureText(item: Record<string, unknown>): string {
  const symbol = fieldText(item.symbol) ?? fieldText(item.code) ?? "未知股票";
  const code = fieldText(item.code);
  const reason = fieldText(item.error) ?? fieldText(item.message) ?? fieldText(item.reason) ?? "未返回失败原因";
  return code && code !== symbol ? `${symbol} / ${code} / ${reason}` : `${symbol} / ${reason}`;
}

function recentSyncFailureTexts(job: SyncJobStatus): string[] {
  return (job.recent_failures ?? []).slice(0, 5).map(syncFailureText);
}

function latestCoverageEndDate(coverage: DatasetCoverage[]): string | null {
  const dailyEndDate = dailyBarsCoverage(coverage)?.end_date;
  if (dailyEndDate) {
    return dailyEndDate;
  }
  const dates = coverage
    .map((item) => item.end_date)
    .filter((value): value is string => Boolean(value))
    .sort();
  return dates.at(-1) ?? null;
}

function isEmptyCoverageSnapshot(coverage: DatasetCoverage[]): boolean {
  return coverage.length > 0 && coverage.every((item) => item.symbols === 0 && item.missing_rows === 0 && !item.start_date && !item.end_date);
}

function isPendingEmptyHealthCoverage(coverage: DatasetCoverage[], refreshing: boolean | undefined): boolean {
  return Boolean(refreshing) && isEmptyCoverageSnapshot(coverage);
}

function lifecycleBadge(item: DailyBarsCoverageItem, windowStart: string): { label: string; tone: "good" | "warn" } | null {
  if (item.delisted_date || item.lifecycle_status === "delisted") {
    return { label: `已退市${item.delisted_date ? `（${item.delisted_date}）` : ""}`, tone: "warn" };
  }
  if (item.listing_date && windowStart && windowStart < item.listing_date) {
    return { label: `未上市（${item.listing_date} 起）`, tone: "good" };
  }
  return null;
}

function formatSecondsSinceSuccess(seconds: number | null | undefined): string {
  if (seconds == null) {
    return "尚无成功记录";
  }
  if (seconds < 60) {
    return `${Math.round(seconds)} 秒前成功`;
  }
  if (seconds < 3600) {
    return `${Math.round(seconds / 60)} 分钟前成功`;
  }
  return `${Math.round(seconds / 3600)} 小时前成功`;
}

function sourceHealthLabel(source: DataSourceHealth): string {
  if (source.source === "realtime") {
    return source.status === "live" ? "实时行情源" : source.status === "stale" ? "实时行情源（沿用最近成功）" : "实时行情源不可用";
  }
  if (source.source === "news") {
    return "市场新闻源";
  }
  if (source.source === "finance") {
    return "财联社行情源";
  }
  return source.source;
}

export function DataCenter({ cacheDir, coverage, onCoverageChange, onServiceReady }: Props) {
  const [service, setService] = useState<DataServiceStatus | null>(null);
  const [symbolsInput, setSymbolsInput] = useState("");
  const [startDate, setStartDate] = useState(() => coverageFillDateRange(coverage).startDate);
  const [endDate, setEndDate] = useState(() => coverageFillDateRange(coverage).endDate);
  const [dateRangeTouched, setDateRangeTouched] = useState(false);
  const [importPath, setImportPath] = useState("");
  const [items, setItems] = useState<DailyBarsCoverageItem[]>([]);
  const [logs, setLogs] = useState<ServiceLogEntry[]>([]);
  const [syncJob, setSyncJob] = useState<SyncJobStatus | null>(null);
  const [message, setMessage] = useState("正在连接本地数据服务");
  const [busyAction, setBusyAction] = useState<BusyAction>(null);
  const [coverageRefreshToken, setCoverageRefreshToken] = useState(0);
  const [diagnostics, setDiagnostics] = useState<DiagnosticsSourcesResponse | null>(null);
  const [dataGaps, setDataGaps] = useState<DiagnosticsDataGapsResponse | null>(null);
  const [isLoadingDataGaps, setIsLoadingDataGaps] = useState(false);
  const [isDiagnosing, setIsDiagnosing] = useState(false);
  const [aiCoverageDiagnosis, setAiCoverageDiagnosis] = useState<string | null>(null);
  const [diagnosisError, setDiagnosisError] = useState<string | null>(null);
  const syncRunning = isSyncRunning(syncJob);
  const syncImportedRows = syncRunning && syncJob ? syncJob.imported_rows : 0;
  const busy = busyAction !== null || syncRunning;
  const recentSyncFailures = syncJob ? recentSyncFailureTexts(syncJob) : [];
  const displayCoverage = coverage;
  const syncProgressNote =
    syncImportedRows > 0
      ? `已写入 ${syncImportedRows} 行${syncJob ? syncFilledText(syncJob) : ""}，等待覆盖刷新确认`
      : null;

  const symbols = useMemo(
    () =>
      symbolsInput
        .split(/[,\s]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    [symbolsInput]
  );

  const reconnectService = async () => {
    const status = await ensureDataService(cacheDir);
    setService(status);
    onServiceReady?.(status);
    await refreshServiceState(status);
    return status;
  };

  const refreshDetails = async (
    activeService: DataServiceStatus,
    selectedSymbols = symbols,
    selectedStartDate = startDate,
    selectedEndDate = endDate
  ) => {
    if (selectedSymbols.length === 0) {
      setItems([]);
      return;
    }
    const response = await loadDailyBarsCoverage(
      activeService.base_url,
      selectedSymbols,
      selectedStartDate,
      selectedEndDate
    );
    setItems(response.items);
  };

  const applyCoverageDateRange = (nextCoverage: DatasetCoverage[]) => {
    const range = coverageFillDateRange(nextCoverage);
    if (!dateRangeTouched) {
      setStartDate(range.startDate);
      setEndDate(range.endDate);
      return range;
    }
    return { startDate, endDate };
  };

  const refreshServiceState = async (activeService: DataServiceStatus) => {
    const [health, recentLogs] = await Promise.all([
      loadDataServiceHealth(activeService.base_url),
      loadDataServiceLogs(activeService.base_url)
    ]);
    const hasPendingEmptyCoverage = isPendingEmptyHealthCoverage(health.coverage, health.coverage_refreshing);
    if (!hasPendingEmptyCoverage) {
      onCoverageChange(health.coverage);
    }
    setLogs(recentLogs.items);
    const range = hasPendingEmptyCoverage ? coverageFillDateRange(coverage) : applyCoverageDateRange(health.coverage);
    if (health.coverage_refreshing) {
      setCoverageRefreshToken((current) => current + 1);
    }
    return range;
  };

  const refreshAfterOperation = async (
    activeService: DataServiceStatus,
    selectedSymbols = symbols,
    selectedStartDate = startDate,
    selectedEndDate = endDate
  ) => {
    const range = await refreshServiceState(activeService);
    if (selectedSymbols.length > 0) {
      await refreshDetails(activeService, selectedSymbols, selectedStartDate, selectedEndDate);
    } else {
      setItems([]);
    }
    void refreshDataGaps(activeService);
    return range;
  };

  const refreshLogs = async (activeService: DataServiceStatus) => {
    const recentLogs = await loadDataServiceLogs(activeService.base_url);
    setLogs(recentLogs.items);
  };

  const refreshDiagnostics = async (activeService: DataServiceStatus) => {
    try {
      setDiagnostics(await loadDiagnosticsSources(activeService.base_url));
    } catch {
      // 健康监控失败不影响数据中心其他能力，保留上一次结果。
    }
  };

  const refreshDataGaps = async (activeService: DataServiceStatus) => {
    if (isLoadingDataGaps) {
      return;
    }
    setIsLoadingDataGaps(true);
    try {
      setDataGaps(await loadDiagnosticsDataGaps(activeService.base_url));
    } catch {
      // 缺口画像失败保留上一次结果，不打扰主流程。
    } finally {
      setIsLoadingDataGaps(false);
    }
  };

  const handleDiagnoseCoverage = async () => {
    if (!service || isDiagnosing) {
      return;
    }
    setIsDiagnosing(true);
    setDiagnosisError(null);
    setAiCoverageDiagnosis(null);
    try {
      // 逐股明细按缺口降序取样：默认前 8 只往往是恰好完整的股票，
      // 会让模型误判“逐股缺口为空”。
      const worstItems = [...items]
        .sort((left, right) => {
          const leftMissing =
            (left.missing_trade_dates?.length ?? 0) +
            (left.missing_market_cap_dates?.length ?? 0) +
            (left.missing_capital_flow_dates?.length ?? 0);
          const rightMissing =
            (right.missing_trade_dates?.length ?? 0) +
            (right.missing_market_cap_dates?.length ?? 0) +
            (right.missing_capital_flow_dates?.length ?? 0);
          return rightMissing - leftMissing;
        })
        .slice(0, 8);
      const text = await aiInsightOneshot(service.base_url, "data_coverage", {
        coverage,
        details: worstItems
      });
      setAiCoverageDiagnosis(text);
    } catch (caught) {
      setDiagnosisError(
        caught instanceof Error ? caught.message : "AI 诊断失败，请稍后重试。"
      );
    } finally {
      setIsDiagnosing(false);
    }
  };

  useEffect(() => {
    let cancelled = false;
    void ensureDataService(cacheDir)
      .then(async (status) => {
        if (cancelled) {
          return;
        }
        setService(status);
        onServiceReady?.(status);
        setMessage(status.message);
        const range = await refreshServiceState(status);
        await refreshDetails(status, [], range.startDate, range.endDate);
        void refreshDiagnostics(status);
        void refreshDataGaps(status);
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setMessage(connectionErrorMessage(error));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [cacheDir]);

  useEffect(() => {
    if (!service || coverageRefreshToken === 0) {
      return;
    }
    let cancelled = false;
    let attempts = 0;
    let timer: number | undefined;
    const pollCoverage = () => {
      timer = window.setTimeout(() => {
        attempts += 1;
        void loadDataServiceHealth(service.base_url)
          .then((health) => {
            if (cancelled) {
              return;
            }
            if (!isPendingEmptyHealthCoverage(health.coverage, health.coverage_refreshing)) {
              onCoverageChange(health.coverage);
              applyCoverageDateRange(health.coverage);
            }
            if (health.coverage_refreshing && attempts < COVERAGE_REFRESH_MAX_ATTEMPTS) {
              pollCoverage();
            }
          })
          .catch(() => {
            // Keep the connected service usable; the next manual refresh will retry coverage.
          });
      }, COVERAGE_REFRESH_RETRY_MS);
    };
    pollCoverage();
    return () => {
      cancelled = true;
      if (timer !== undefined) {
        window.clearTimeout(timer);
      }
    };
  }, [service, coverageRefreshToken, onCoverageChange, dateRangeTouched, startDate, endDate]);

  useEffect(() => {
    const activeSyncJob = syncJob;
    if (!service || !activeSyncJob || !isSyncRunning(activeSyncJob)) {
      return;
    }
    let cancelled = false;
    const timer = window.setInterval(() => {
      void loadSyncJob(service.base_url, activeSyncJob.job_id)
        .then(async (result) => {
          if (cancelled) {
            return;
          }
          setSyncJob(result.job);
          setMessage(
            isSyncRunning(result.job)
              ? syncRunningMessage(result.job)
              : syncFinishedMessage(result.job)
          );
          if (!isSyncRunning(result.job)) {
            await refreshAfterOperation(service);
          }
        })
        .catch((error: Error) => {
          if (!cancelled) {
            setMessage(error.message);
          }
        });
    }, 1000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [service, syncJob?.job_id, syncJob?.status]);

  const handleRefreshDetails = async () => {
    if (!service) {
      return;
    }
    setBusyAction("refresh");
    setMessage("正在刷新覆盖范围");
    try {
      await refreshDetails(service);
      await refreshServiceState(service);
      setMessage("覆盖范围已刷新");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "刷新覆盖范围失败");
    } finally {
      setBusyAction(null);
    }
  };

  const handleFetch = async () => {
    if (!service) {
      return;
    }
    if (symbols.length === 0) {
      await runFullMarketSync("fetch");
      return;
    }
    setBusyAction("fetch");
    setMessage("正在补全缺失数据");
    try {
      const result = await fetchDailyBars(service.base_url, symbols, startDate, endDate);
      setMessage(operationMessage(result));
      onCoverageChange(result.coverage);
      const nextRange = coverageFillDateRange(result.coverage);
      const fetchedEndDate = latestCoverageEndDate(result.coverage);
      const detailRange = dateRangeTouched
        ? { startDate, endDate: fetchedEndDate && fetchedEndDate > endDate ? fetchedEndDate : endDate }
        : nextRange;
      if (!dateRangeTouched) {
        setStartDate(nextRange.startDate);
        setEndDate(nextRange.endDate);
      } else if (fetchedEndDate && fetchedEndDate > endDate) {
        setEndDate(fetchedEndDate);
      }
      await refreshAfterOperation(service, symbols, detailRange.startDate, detailRange.endDate);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "补全缺失数据失败");
      let activeService = service;
      try {
        activeService = await reconnectService();
      } catch {
        // Keep the original operation error visible if reconnect also fails.
      }
      await refreshLogs(activeService);
    } finally {
      setBusyAction(null);
    }
  };

  const handleCapitalFlowBackfill = async () => {
    if (!service) {
      return;
    }
    setBusyAction("capital-flow");
    setMessage("\u6b63\u5728\u8865\u9f50\u8d44\u91d1\u6d41");
    try {
      const result = await fetchCapitalFlow(service.base_url, symbols, startDate, endDate);
      setMessage(operationMessage(result));
      onCoverageChange(result.coverage);
      if (result.job) {
        setSyncJob(result.job);
        setMessage(isSyncRunning(result.job) ? syncRunningMessage(result.job) : syncFinishedMessage(result.job));
        if (isSyncRunning(result.job)) {
          return;
        }
      }
      await refreshAfterOperation(service, symbols, startDate, endDate);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "\u8865\u9f50\u8d44\u91d1\u6d41\u5931\u8d25");
      let activeService = service;
      try {
        activeService = await reconnectService();
      } catch {
        // Keep the original operation error visible if reconnect also fails.
      }
      await refreshLogs(activeService);
    } finally {
      setBusyAction(null);
    }
  };

  const handleImportSample = async () => {
    if (!service) {
      return;
    }
    setBusyAction("sample");
    setMessage("正在导入示例数据");
    try {
      const result = await importDailyBars(service.base_url, "sample");
      setMessage(operationMessage(result));
      onCoverageChange(result.coverage);
      await refreshAfterOperation(service);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "导入示例数据失败");
      let activeService = service;
      try {
        activeService = await reconnectService();
      } catch {
        // Keep the original operation error visible if reconnect also fails.
      }
      await refreshLogs(activeService);
    } finally {
      setBusyAction(null);
    }
  };

  const handleImportFile = async () => {
    if (!service || !importPath.trim()) {
      return;
    }
    setBusyAction("file");
    setMessage("正在导入本地文件");
    try {
      const result = await importDailyBars(service.base_url, "file", importPath.trim());
      setMessage(operationMessage(result));
      onCoverageChange(result.coverage);
      await refreshAfterOperation(service);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "导入本地文件失败");
      let activeService = service;
      try {
        activeService = await reconnectService();
      } catch {
        // Keep the original operation error visible if reconnect also fails.
      }
      await refreshLogs(activeService);
    } finally {
      setBusyAction(null);
    }
  };

  const handleFullMarketSync = async () => {
    if (!service) {
      return;
    }
    await runFullMarketSync("sync");
  };

  const handleMissingOnlySync = async () => {
    if (!service) {
      return;
    }
    setBusyAction("missing-only");
    setMessage("正在按缺口计算补齐名单");
    try {
      const result = await startMissingOnlySync(service.base_url, startDate, endDate);
      if (!result.started) {
        setMessage(result.reason ?? "窗口内没有不完整的股票，无需补齐。");
        return;
      }
      if (result.job) {
        setSyncJob(result.job);
      }
      setMessage(`缺口补齐已启动：窗口内 ${result.missing_symbols} 只不完整（不扫描全市场）`);
      if (result.job && !isSyncRunning(result.job)) {
        setMessage(syncFinishedMessage(result.job));
        await refreshAfterOperation(service);
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "缺口补齐启动失败");
      await refreshLogs(service);
    } finally {
      setBusyAction(null);
    }
  };

  const runFullMarketSync = async (action: "fetch" | "sync") => {
    if (!service) {
      return;
    }
    setBusyAction(action);
    setMessage(action === "fetch" ? "正在补全全市场缺失数据" : "正在下载全市场历史数据");
    try {
      const result = await startFullMarketSync(service.base_url, startDate, endDate);
      setSyncJob(result.job);
      if (result.job.status === "running") {
        setMessage(syncRunningMessage(result.job));
      } else {
        setMessage(syncFinishedMessage(result.job));
        await refreshAfterOperation(service);
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "全市场下载失败");
      await refreshLogs(service);
    } finally {
      setBusyAction(null);
    }
  };

  const handleCancelSyncJob = async () => {
    if (!service || !syncJob || !isSyncRunning(syncJob)) {
      return;
    }
    setMessage("正在停止任务，已导入的数据会保留");
    try {
      const result = await cancelSyncJob(service.base_url, syncJob.job_id);
      setSyncJob(result.job);
      setMessage(isSyncRunning(result.job) ? syncRunningMessage(result.job) : syncFinishedMessage(result.job));
      if (!isSyncRunning(result.job)) {
        await refreshAfterOperation(service);
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "停止任务失败");
      await refreshLogs(service);
    }
  };

  return (
    <section className="surface data-center">
      <div className="section-title">
        <div>
          <span className="section-kicker">数据健康</span>
          <h2>数据中心</h2>
        </div>
        <button className="secondary-button" type="button" onClick={handleRefreshDetails} disabled={!service || busy}>
          {busyAction === "refresh" ? "正在刷新覆盖范围" : "刷新覆盖范围"}
        </button>
      </div>

      <div className="data-service-panel">
        <div className="service-summary">
          <strong>{service ? "本地服务已连接" : "本地服务未连接"}</strong>
          <span>{service ? `${service.base_url} / ${service.cache_dir}` : message}</span>
        </div>
        <div className="service-form">
          <label>
            股票代码
            <input
              value={symbolsInput}
              onChange={(event) => setSymbolsInput(event.target.value)}
              placeholder="留空默认补全市场"
            />
          </label>
          <label>
            开始日期
            <input
              type="date"
              value={startDate}
              onChange={(event) => {
                setDateRangeTouched(true);
                setStartDate(event.target.value);
              }}
            />
          </label>
          <label>
            结束日期
            <input
              type="date"
              value={endDate}
              onChange={(event) => {
                setDateRangeTouched(true);
                setEndDate(event.target.value);
              }}
            />
          </label>
          <label>
            导入文件路径
            <input
              value={importPath}
              onChange={(event) => setImportPath(event.target.value)}
              placeholder="C:\\data\\daily.csv"
            />
          </label>
        </div>
        <div className="service-actions">
          <button className="primary-button" type="button" onClick={handleFullMarketSync} disabled={!service || busy}>
            {busyAction === "sync" ? "正在下载全市场历史数据" : "下载全市场历史数据"}
          </button>
          <button className="primary-button" type="button" onClick={handleFetch} disabled={!service || busy}>
            {busyAction === "fetch" ? "正在补全缺失数据" : "补全缺失数据"}
          </button>
          <button className="secondary-button" type="button" onClick={handleMissingOnlySync} disabled={!service || busy}>
            {busyAction === "missing-only" ? "正在只补缺口" : "只补缺口"}
          </button>
          <button className="secondary-button" type="button" onClick={handleCapitalFlowBackfill} disabled={!service || busy}>
            {busyAction === "capital-flow" ? "\u6b63\u5728\u8865\u9f50\u8d44\u91d1\u6d41" : "\u8865\u9f50\u8d44\u91d1\u6d41"}
          </button>
          <button className="secondary-button" type="button" onClick={handleImportSample} disabled={!service || busy}>
            {busyAction === "sample" ? "正在导入示例数据" : "导入示例数据"}
          </button>
          <button className="secondary-button" type="button" onClick={handleImportFile} disabled={!service || busy || !importPath.trim()}>
            {busyAction === "file" ? "正在导入本地文件" : "导入本地文件"}
          </button>
          <button className="secondary-button" type="button" aria-label="AI 诊断缺失" onClick={handleDiagnoseCoverage} disabled={!service || isDiagnosing}>
            {isDiagnosing ? "AI 诊断中…" : "AI 诊断缺失"}
          </button>
        </div>
        {diagnosisError ? (
          <div className="condition-validation bad" role="alert">
            {diagnosisError}
          </div>
        ) : null}
        {aiCoverageDiagnosis ? (
          <p className="ai-oneshot-line" aria-label="AI 覆盖诊断">
            {aiCoverageDiagnosis}
          </p>
        ) : null}
        <p className={`operation-status ${busy ? "is-busy" : ""}`} role="status" aria-label="数据中心状态">
          {message}
        </p>
        {syncJob ? (
          <div className="sync-progress" role="status">
            <strong>已处理 {syncJob.processed_symbols ?? syncJob.completed_symbols}/{syncJob.total_symbols}</strong>
            <span>
              完成 {syncJob.completed_symbols}，跳过 {syncJob.skipped_symbols ?? 0}，失败 {syncJob.failed_symbols}
            </span>
            <span>
              接口返回 {syncJob.returned_rows ?? 0} 行，写入 {syncJob.imported_rows} 行
              {syncFilledText(syncJob)}
            </span>
            {syncJob.current_symbol ? <span>当前 {syncJob.current_symbol}</span> : null}
            {syncJob.last_error ? <span>最近失败: {syncJob.last_error}</span> : null}
            {recentSyncFailures.length > 0 ? (
              <div className="sync-failure-list" aria-label="最近失败原因">
                {recentSyncFailures.map((item) => (
                  <span key={item}>{item}</span>
                ))}
              </div>
            ) : null}
            <progress value={syncJob.processed_symbols ?? syncJob.completed_symbols + syncJob.failed_symbols} max={syncJob.total_symbols || 1} />
            {isSyncRunning(syncJob) ? (
              <button className="secondary-button" type="button" onClick={handleCancelSyncJob} disabled={syncJob.status === "cancelling"}>
                {syncJob.status === "cancelling" ? "正在停止" : "停止任务"}
              </button>
            ) : null}
          </div>
        ) : null}
        {logs.length > 0 ? (
          <div className="service-log-list" aria-label="本地服务日志">
            {logs.slice(0, 5).map((entry, index) => (
              <span key={`${entry.timestamp ?? index}-${entry.message}`} className={`service-log ${entry.level}`}>
                {entry.message}
              </span>
            ))}
          </div>
        ) : null}
      </div>

      {displayCoverage.length === 0 ? (
        <div className="empty-state">
          <strong>等待数据覆盖信息</strong>
          <span>如果本地缓存为空，运行回测前需要先导入或补全 a-stock-data 历史数据。</span>
        </div>
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>数据集</th>
                <th>股票数</th>
                <th>覆盖日期</th>
                <th>缺失行</th>
                <th>来源状态</th>
              </tr>
            </thead>
            <tbody>
              {displayCoverage.map((item) => {
                const meta = datasetLabels[item.dataset] ?? { label: "扩展数据", source: "本地缓存" };
                return (
                  <tr key={item.dataset}>
                    <td>
                      <strong>{meta.label}</strong>
                      <small className="muted-code">本地历史缓存</small>
                    </td>
                    <td>{item.symbols}</td>
                    <td>{item.start_date ?? "-"} 至 {item.end_date ?? "-"}</td>
                    <td>
                      <span>{item.missing_rows}</span>
                      {item.suspension_rows > 0 ? (
                        <small className="coverage-progress-note">停牌类缺行 {item.suspension_rows}（不可补）</small>
                      ) : null}
                      {syncJob && item.dataset === (syncJob.mode === "capital_flow_backfill" ? "capital_flow" : "daily_bars") && syncProgressNote ? (
                        <small className="coverage-progress-note">{syncProgressNote}</small>
                      ) : null}
                    </td>
                    <td>
                      <span className={item.missing_rows === 0 ? "health-pill good" : "health-pill warn"}>
                        {item.missing_rows === 0 ? "数据已就绪" : "建议补齐"}
                      </span>
                      <small>{meta.source}</small>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="coverage-details">
        {items.map((item) => {
          const badge = lifecycleBadge(item, startDate);
          return (
            <article key={item.symbol} className="coverage-item">
              <strong>
                {item.symbol}
                {badge ? <span className={`health-pill ${badge.tone}`}>{badge.label}</span> : null}
              </strong>
              <span>{item.start_date ?? "-"} 至 {item.end_date ?? "-"}，{item.rows} 行</span>
              <span>缺失交易日: {formatList(item.missing_trade_dates)}</span>
              <span>缺失资金流: {formatList(item.missing_capital_flow_dates)}</span>
              <span>缺失市值: {formatList(item.missing_market_cap_dates)}</span>
            </article>
          );
        })}
      </div>

      <details className="source-health-card">
        <summary>数据源健康监控</summary>
        {diagnostics ? (
          <div className="source-health-list" aria-label="数据源健康状态">
            {diagnostics.sources.map((source) => (
              <div className={`source-health-row ${source.ok ? "ok" : "bad"}`} key={source.source}>
                <strong>{sourceHealthLabel(source)}</strong>
                <span className={`health-pill ${source.ok ? "good" : "warn"}`}>
                  {source.ok ? "可用" : "不可用"}
                </span>
                <small>{formatSecondsSinceSuccess(source.seconds_since_success)}</small>
                {source.diagnostics.length > 0 ? (
                  <ul>
                    {source.diagnostics.slice(0, 3).map((message) => (
                      <li key={message}>{message}</li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ))}
          </div>
        ) : (
          <p className="muted-code">尚未获取数据源健康状态，连接本地服务后自动加载。</p>
        )}
        {service ? (
          <button className="secondary-button compact" type="button" onClick={() => void refreshDiagnostics(service)}>
            刷新数据源健康
          </button>
        ) : null}
      </details>

      <details className="source-health-card" aria-label="缺失数据监控">
        <summary>
          缺失数据监控
          {dataGaps?.profile?.daily_bars
            ? `（停更 ${dataGaps.profile.daily_bars.symbols_stale} 只）`
            : ""}
        </summary>
        {dataGaps?.warehouse_health && !dataGaps.warehouse_health.healthy ? (
          <div className="data-gap-section corrupt-partitions" role="alert">
            <strong>检测到损坏分区（先修损坏，再补数据——补齐无法修复损坏）</strong>
            <ul>
              {Object.entries(dataGaps.warehouse_health.corrupt_partitions).map(([partition, error]) => (
                <li key={partition}>
                  <span className="muted-code">{partition}</span>：{error}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {dataGaps?.profile?.available ? (
          <div className="data-gap-panel">
            <p className="muted-code">
              统计窗口 {dataGaps.profile.window?.start_date} ~ {dataGaps.profile.window?.end_date}
              （{dataGaps.profile.window?.partitions?.join("、")} 分区）
            </p>
            <div className="data-gap-section">
              <strong>
                日线停更：{dataGaps.profile.daily_bars?.symbols_current} 只最新 / {dataGaps.profile.daily_bars?.symbols_stale} 只停更
              </strong>
              <ul>
                {(dataGaps.profile.daily_bars?.stale_distribution ?? []).slice(0, 6).map((entry) => (
                  <li key={entry.last_date}>
                    {entry.symbols} 只股票的数据停在 {entry.last_date}
                  </li>
                ))}
              </ul>
            </div>
            {(dataGaps.profile.daily_bars?.thin_days?.length ?? 0) > 0 ? (
              <div className="data-gap-section">
                <strong>疑似写入失败日</strong>
                <ul>
                  {(dataGaps.profile.daily_bars?.thin_days ?? []).slice(0, 6).map((entry) => (
                    <li key={entry.trade_date}>
                      {entry.trade_date} 仅有 {entry.rows} 行
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            <div className="data-gap-section">
              <strong>市值/资金流停更</strong>
              <ul>
                {(dataGaps.profile.market_cap?.stale_distribution ?? []).slice(0, 3).map((entry) => (
                  <li key={`cap-${entry.last_date}`}>
                    市值 {entry.symbols} 只停在 {entry.last_date}
                  </li>
                ))}
                {(dataGaps.profile.capital_flow?.stale_distribution ?? []).slice(0, 3).map((entry) => (
                  <li key={`flow-${entry.last_date}`}>
                    资金流 {entry.symbols} 只停在 {entry.last_date}
                  </li>
                ))}
                {((dataGaps.profile.market_cap?.stale_distribution?.length ?? 0) +
                  (dataGaps.profile.capital_flow?.stale_distribution?.length ?? 0)) === 0 ? (
                  <li>市值与资金流均更新到最新</li>
                ) : null}
              </ul>
            </div>
            <small className="muted-code">该明细同时提供给 AI 助手（data_health_report 工具）。</small>
          </div>
        ) : (
          <p className="muted-code">
            {isLoadingDataGaps ? "正在扫描数据仓缺口…" : dataGaps?.profile?.reason ?? "尚未获取缺口画像，连接本地服务后自动加载。"}
          </p>
        )}
        {service ? (
          <button
            className="secondary-button compact"
            type="button"
            disabled={isLoadingDataGaps}
            onClick={() => void refreshDataGaps(service)}
          >
            {isLoadingDataGaps ? "扫描中…" : "刷新缺口画像"}
          </button>
        ) : null}
      </details>
    </section>
  );
}
