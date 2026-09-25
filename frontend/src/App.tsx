import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { Activity, Database, Flame, Gauge, ShieldAlert, Sparkles } from "lucide-react";
import { AiAssistantPanel } from "./components/AiAssistantPanel";
import { aiParseConditions, loadAiNewsDigest, loadAiStatus, revealAiKey } from "./aiApi";
import { isTauriRuntime as isTauri } from "./tauriRuntime";
import { useAiEventStream } from "./hooks/useAiEventStream";
import { useMarketModules } from "./hooks/useMarketModules";
import type { AiConditionParseResult, AiDigestItem, AiInsight, AiStatus as AiStatusView, AiTask } from "./aiTypes";
import "./ai-panel.css";
import { useMarketPolling } from "./hooks/useMarketPolling";
import {
  BackendError,
  runBacktestStreamWithDataService,
  runConfiguredBacktest,
  validateConditionExpression,
  validateStockSymbols
} from "./api";
import { ClsFinancePanel } from "./components/ClsFinancePanel";
import { DataCenter } from "./components/DataCenter";
import { MarketDashboard } from "./components/MarketDashboard";
import { NewsPanel } from "./components/NewsPanel";
import { NewsSummaryPanel } from "./components/NewsSummaryPanel";
import { RiskAlertsModal } from "./components/RiskAlertsModal";
import {
  cloneStrategyConfig,
  createSavedStrategyPreset,
  hasSavableRules,
  strategySignature
} from "./savedStrategies";
import { useSavedStrategyStore } from "./useSavedStrategyStore";
import { StrategyWorkbench } from "./components/StrategyWorkbench";
import { TradesTable } from "./components/TradesTable";
import { TonghuashunBriefingPanel } from "./components/TonghuashunBriefingPanel";
import { UpdatePanel } from "./components/UpdatePanel";
import { initialMarketRefreshMeta } from "./marketRefresh";
import { defaultSettings, defaultStrategy } from "./strategyDefaults";
import { formatLocalDate, recentAShareTradingDateRangeEnding } from "./tradingCalendar";
import type {
  BacktestResult,
  BacktestSettingsConfig,
  DataServiceStatus,
  DatasetCoverage,
  ConditionValidationResult,
  MarketRefreshMeta,
  RealtimeMarketSnapshot,
  SavedStrategyPreset,
  StockSymbolValidationResult,
  StrategyConfig
} from "./types";

type PendingStrategySave = {
  strategy: StrategyConfig;
  name: string;
} | null;

const ResultsOverview = lazy(() => import("./components/ResultsOverview").then((module) => ({
  default: module.ResultsOverview
})));

function latestDailyCoverage(coverage: DatasetCoverage[]): DatasetCoverage | undefined {
  return coverage.find((item) => item.dataset === "daily_bars");
}

function formatPercent(value: number | null | undefined): string {
  return value == null ? "--" : `${(value * 100).toFixed(2)}%`;
}

function formatCompact(value: number): string {
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

function formatMarketDegree(value: number | null | undefined): string {
  return value == null ? "--" : value.toFixed(1);
}

function marketDegreeCardClass(value: number | null | undefined): string {
  if (value == null) {
    return "";
  }
  if (value >= 5) {
    return "market-degree-card-high";
  }
  if (value < 4) {
    return "market-degree-card-low";
  }
  return "market-degree-card-neutral";
}

function marketDegreeTextClass(value: number | null | undefined): "up-text" | "down-text" | "flat-text" | undefined {
  if (value == null) {
    return undefined;
  }
  if (value >= 5) {
    return "up-text";
  }
  if (value < 4) {
    return "down-text";
  }
  return "flat-text";
}

function translateError(error: unknown): string {
  if (error instanceof BackendError && error.code === "no_local_data") {
    return "未找到已缓存的日线行情，请先确认 a-stock-data 数据包已导入到本地缓存。";
  }
  const message = error instanceof Error ? error.message : String(error);
  if (message.includes("No cached daily bars found")) {
    return "未找到已缓存的日线行情，请先确认 a-stock-data 数据包已导入到本地缓存。";
  }
  if (message.includes("Selected strategy requires capital-flow data")) {
    return "当前策略需要资金流向数据，请检查数据中心的资金流向覆盖情况。";
  }
  if (message.includes("Required column is missing")) {
    return "历史数据字段不完整，请在数据中心补齐所选策略需要的行情、资金或市值字段。";
  }
  if (message.includes("unknown condition_id")) {
    return "策略条件暂不支持，请从条件库中选择已注册的 A 股条件。";
  }
  if (
    message.includes("must be") ||
    message.includes("condition group") ||
    message.includes("strategy must") ||
    message.includes("end_date")
  ) {
    return "回测参数不合法，请检查日期、资金、持仓、费用和止盈止损设置。";
  }
  return "回测运行失败，请检查数据中心覆盖范围和策略参数。";
}

function validateBacktestSettings(settings: BacktestSettingsConfig, draftErrors: string[]): string[] {
  const errors = [...draftErrors];
  if (!settings.start_date) {
    errors.push("开始日期不能为空。");
  }
  if (!settings.end_date) {
    errors.push("结束日期不能为空。");
  }
  if (settings.start_date && settings.end_date && settings.start_date > settings.end_date) {
    errors.push("开始日期不能晚于结束日期。");
  }
  if (!Number.isFinite(settings.initial_cash) || settings.initial_cash <= 0) {
    errors.push("初始资金必须大于0。");
  }
  if (!Number.isFinite(settings.position_size_pct) || settings.position_size_pct <= 0 || settings.position_size_pct > 1) {
    errors.push("个股仓位上限必须大于0且不能超过100%。");
  }
  if (!Number.isInteger(settings.fixed_holding_days) || settings.fixed_holding_days < 1) {
    errors.push("固定持仓天数必须至少为1天。");
  }
  if (!Number.isInteger(settings.max_positions) || settings.max_positions < 1) {
    errors.push("最大持仓数必须至少为1。");
  }
  if (!Number.isInteger(settings.max_daily_buys) || settings.max_daily_buys < 1) {
    errors.push("每日最多买入必须至少为1。");
  }
  if (settings.take_profit_pct != null && (!Number.isFinite(settings.take_profit_pct) || settings.take_profit_pct <= 0)) {
    errors.push("止盈比例必须为正数。");
  }
  if (settings.stop_loss_pct != null && (!Number.isFinite(settings.stop_loss_pct) || settings.stop_loss_pct >= 0)) {
    errors.push("止损比例必须为负数。");
  }
  if (!Number.isFinite(settings.slippage_rate) || settings.slippage_rate < 0) {
    errors.push("滑点比例不能小于0。");
  }
  if (!Number.isFinite(settings.fee_rate) || settings.fee_rate < 0) {
    errors.push("手续费率不能小于0。");
  }
  if (!Number.isFinite(settings.stamp_tax_rate) || settings.stamp_tax_rate < 0) {
    errors.push("印花税率不能小于0。");
  }
  if (!Number.isInteger(settings.min_listing_days) || settings.min_listing_days < 0) {
    errors.push("最少上市天数不能小于0。");
  }
  if (settings.stock_pool === "custom" && settings.custom_symbols.length === 0) {
    errors.push("股票池为自选代码时，至少需要填写一个股票代码。");
  }
  return [...new Set(errors)];
}

type BacktestTrade = BacktestResult["trades"][number];

function tradeIdentity(trade: BacktestTrade): string {
  return `${trade.symbol}-${trade.buy_signal_date}-${trade.buy_date}`;
}

function mergeBacktestTrades(current: BacktestTrade[], incoming: BacktestTrade[]): BacktestTrade[] {
  const incomingKeys = new Set(incoming.map(tradeIdentity));
  return [
    ...incoming,
    ...current.filter((trade) => !incomingKeys.has(tradeIdentity(trade)))
  ];
}

export function App() {
  const [coverage, setCoverage] = useState<DatasetCoverage[]>([]);
  const [result, setResult] = useState<BacktestResult | null>(null);
  const [streamedTrades, setStreamedTrades] = useState<BacktestResult["trades"]>([]);
  const [strategy, setStrategy] = useState<StrategyConfig>(defaultStrategy);
  const [settings, setSettings] = useState<BacktestSettingsConfig>(defaultSettings);
  const [error, setError] = useState<string | null>(null);
  const [dataService, setDataService] = useState<DataServiceStatus | null>(null);
  const [isRunningBacktest, setIsRunningBacktest] = useState(false);
  const [runPhases, setRunPhases] = useState<string[]>([]);
  const [runProgressMessage, setRunProgressMessage] = useState<string | null>(null);
  const [marketSnapshot, setMarketSnapshot] = useState<RealtimeMarketSnapshot | null>(null);
  const [marketRefreshMeta, setMarketRefreshMeta] = useState<MarketRefreshMeta>(() => initialMarketRefreshMeta());
  const [isLoadingMarket, setIsLoadingMarket] = useState(false);
  const [riskModalOpen, setRiskModalOpen] = useState(false);
  // Persistence lifecycle (loading/ready/failed), single initial load, serialized
  // mutations and out-of-order-load protection are all owned by the store.
  const {
    store: savedStrategyStore,
    strategies: savedStrategies,
    status: strategyLoadStatus,
    isMutating: isMutatingStrategies,
    error: strategyLoadError
  } = useSavedStrategyStore();
  const [conditionValidation, setConditionValidation] = useState<ConditionValidationResult | null>(null);
  const [isValidatingCondition, setIsValidatingCondition] = useState(false);
  const [stockSymbolValidation, setStockSymbolValidation] = useState<StockSymbolValidationResult | null>(null);
  const [isValidatingStockSymbols, setIsValidatingStockSymbols] = useState(false);
  const [settingsDraftErrors, setSettingsDraftErrors] = useState<string[]>([]);
  const [strategySaveMessage, setStrategySaveMessage] = useState<string | null>(null);
  const [pendingStrategySave, setPendingStrategySave] = useState<PendingStrategySave>(null);
  const [settingsDateTouched, setSettingsDateTouched] = useState(false);
  const [aiOpen, setAiOpen] = useState(false);
  const [aiTask, setAiTask] = useState<AiTask | null>(null);
  const [aiInsights, setAiInsights] = useState<AiInsight[]>([]);
  const [aiUnseenInsights, setAiUnseenInsights] = useState(0);
  const [aiDigest, setAiDigest] = useState<AiDigestItem[]>([]);
  const [aiStatus, setAiStatus] = useState<AiStatusView | null>(null);

  useEffect(() => {
    if (!dataService) {
      setAiStatus(null);
      return;
    }
    let cancelled = false;
    loadAiStatus(dataService.base_url)
      .then((status) => {
        if (!cancelled) {
          setAiStatus(status);
        }
      })
      .catch(() => {
        // AI 状态读取失败时按未配置处理，只影响 AI 点评入口的可用性。
        if (!cancelled) {
          setAiStatus(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [dataService]);

  const handleParseConditions = useCallback(
    async (text: string): Promise<AiConditionParseResult> => {
      if (!dataService) {
        throw new Error("本地数据服务未连接，暂时无法使用 AI 条件解析。");
      }
      return aiParseConditions(dataService.base_url, text);
    },
    [dataService]
  );

  useEffect(() => {
    if (strategyLoadStatus === "failed" && strategyLoadError) {
      setStrategySaveMessage(`加载已保存策略失败，仅显示内置策略：${strategyLoadError}`);
    }
  }, [strategyLoadStatus, strategyLoadError]);

  const queueStrategySavePrompt = (currentStrategy: StrategyConfig) => {
    const hasCustomEntryRule = currentStrategy.entry_groups.some((group) =>
      group.conditions.some((condition) => Boolean(condition.expression?.trim()))
    );
    const hasCustomExitRule = currentStrategy.exit_rules.some((condition) => Boolean(condition.expression?.trim()));
    if (
      !hasSavableRules(currentStrategy) ||
      !hasCustomEntryRule ||
      !hasCustomExitRule ||
      strategySignature(currentStrategy) === strategySignature(defaultStrategy)
    ) {
      setPendingStrategySave(null);
      return;
    }
    const existing = savedStrategies.find((item) => strategySignature(item.strategy) === strategySignature(currentStrategy));
    if (existing) {
      setPendingStrategySave(null);
      setStrategySaveMessage(`当前策略已保存在“${existing.name}”中。`);
      return;
    }
    const nextPreset = createSavedStrategyPreset(currentStrategy, savedStrategies);
    setPendingStrategySave({
      strategy: cloneStrategyConfig(currentStrategy),
      name: nextPreset.name
    });
    setStrategySaveMessage("回测完成，可将当前入场与离场规则保存到策略配置。");
  };

  const confirmPendingStrategySave = async () => {
    if (!pendingStrategySave || isMutatingStrategies) {
      return;
    }
    // The store waits for the single initial load, refuses to persist unless the
    // load succeeded, and serializes this against any other save/delete.
    const result = await savedStrategyStore.save(pendingStrategySave.strategy);
    if (result.ok) {
      setStrategySaveMessage(`已保存策略：${result.savedName ?? pendingStrategySave.name}`);
      setPendingStrategySave(null);
    } else {
      setStrategySaveMessage(result.error ? `策略保存失败：${result.error}` : "策略保存失败。");
    }
  };

  const dismissPendingStrategySave = () => {
    setPendingStrategySave(null);
    setStrategySaveMessage("本次未保存策略，你可以继续调整后再次运行。");
  };

  const handleCoverageChange = (nextCoverage: DatasetCoverage[]) => {
    setCoverage(nextCoverage);
  };

  const handleSettingsChange = (nextSettings: BacktestSettingsConfig) => {
    setSettings((current) => {
      if (nextSettings.start_date !== current.start_date || nextSettings.end_date !== current.end_date) {
        setSettingsDateTouched(true);
      }
      if (
        nextSettings.stock_pool !== current.stock_pool ||
        nextSettings.custom_symbols.join(",") !== current.custom_symbols.join(",")
      ) {
        setStockSymbolValidation(null);
      }
      return nextSettings;
    });
  };

  useEffect(() => {
    if (settingsDateTouched) {
      return;
    }
    const daily = latestDailyCoverage(coverage);
    if (!daily?.end_date) {
      return;
    }
    const today = formatLocalDate(new Date());
    const effectiveEndDate = daily.end_date < today ? daily.end_date : today;
    const range = recentAShareTradingDateRangeEnding(effectiveEndDate);
    setSettings((current) => {
      if (current.start_date === range.startDate && current.end_date === range.endDate) {
        return current;
      }
      return {
        ...current,
        start_date: range.startDate,
        end_date: range.endDate
      };
    });
  }, [coverage, settingsDateTouched]);

  const runBacktest = async () => {
    const validationErrors = validateBacktestSettings(settings, settingsDraftErrors);
    if (validationErrors.length > 0) {
      setError(validationErrors.join(" "));
      setRunProgressMessage(null);
      setRunPhases([]);
      return;
    }
    if (settings.stock_pool === "custom") {
      const symbolValidation = await validateCustomStockSymbols(settings.custom_symbols);
      if (!symbolValidation?.ok) {
        const invalidSymbols = symbolValidation?.invalid_symbols.length
          ? symbolValidation.invalid_symbols.join("、")
          : settings.custom_symbols.join("、");
        setError(`自选代码包含无效股票代码：${invalidSymbols}`);
        setRunProgressMessage(null);
        setRunPhases([]);
        return;
      }
    }
    try {
      setError(null);
      setResult(null);
      setStreamedTrades([]);
      setIsRunningBacktest(true);
      setRunProgressMessage("正在准备历史数据与策略条件。");
      setRunPhases(["校验参数", "读取本地数据"]);
      window.setTimeout(() => setRunPhases((current) => (current.length < 3 ? [...current, "计算指标"] : current)), 120);
      window.setTimeout(() => setRunPhases((current) => (current.length < 4 ? [...current, "撮合交易"] : current)), 260);
      const nextResult = dataService
        ? await runBacktestStreamWithDataService(dataService.base_url, strategy, settings, {
            onPhase: (phase) =>
              setRunPhases((current) => (current.includes(phase) ? current : [...current, phase])),
            onProgress: (event) => setRunProgressMessage(event.message),
            onTrade: (trade) =>
              setStreamedTrades((current) => mergeBacktestTrades(current, [trade])),
            onResult: (completed) => {
              setResult(completed);
              setStreamedTrades((current) => mergeBacktestTrades(current, completed.trades));
              setRunProgressMessage("回测完成，已生成收益曲线和交易明细。");
            }
          })
        : await runConfiguredBacktest(strategy, settings);
      setResult(nextResult);
      setStreamedTrades((current) => mergeBacktestTrades(current, nextResult.trades));
      queueStrategySavePrompt(strategy);
      setRunPhases(["校验参数", "读取本地数据", "计算指标", "撮合交易", "生成结果"]);
    } catch (caught) {
      setError(translateError(caught));
      setRunProgressMessage(null);
    } finally {
      setIsRunningBacktest(false);
    }
  };

  useMarketPolling({
    dataService,
    marketSnapshot,
    setIsLoadingMarket,
    setMarketSnapshot,
    setMarketRefreshMeta
  });

  const {
    marketCommentary,
    marketNews,
    isLoadingNews,
    clsFinance,
    isLoadingClsFinance,
    newsSummary,
    isLoadingNewsSummary,
    fupanBriefing,
    zaopanBriefing,
    riskAlerts,
    isLoadingRiskAlerts,
    recommendedStrategies,
    refreshNews,
    refreshRiskAlerts
  } = useMarketModules(dataService, coverage);

  const loadAiDigest = useCallback(
    async (isCancelled: () => boolean): Promise<boolean> => {
      if (!dataService) {
        return false;
      }
      try {
        const digest = await loadAiNewsDigest(dataService.base_url);
        if (!isCancelled()) {
          setAiDigest(digest.items ?? []);
        }
        return (digest.items ?? []).length > 0;
      } catch {
        // AI 简报不可用（未配置模型等）时保持空列表，不影响原始资讯模块。
        return false;
      }
    },
    [dataService]
  );

  useEffect(() => {
    let cancelled = false;
    void loadAiDigest(() => cancelled);
    return () => {
      cancelled = true;
    };
  }, [loadAiDigest]);

  useAiEventStream({
    baseUrl: dataService?.base_url ?? null,
    enabled: Boolean(dataService),
    onInsight: (insight) => {
      setAiInsights((current) => [insight, ...current.filter((item) => item.id !== insight.id)].slice(0, 20));
      setAiUnseenInsights((count) => count + 1);
    },
    onDataFresh: (module) => {
      // 推拉结合：后端提示有新数据时立即刷新对应模块，而不是死等轮询周期。
      if (module === "news") {
        refreshNews();
      } else if (module === "risk") {
        refreshRiskAlerts();
      } else if (module === "ai_news") {
        void loadAiDigest(() => false);
      }
    }
  });

  const validateConditionText = async (text: string, mode: "entry" | "exit" = "entry"): Promise<ConditionValidationResult> => {
    if (!dataService) {
      return {
        ok: false,
        normalized_text: text.trim(),
        condition: null,
        errors: [{ code: "service_unavailable", message: "本地数据服务未连接，暂时无法校验条件。" }],
        examples: mode === "exit" ? ["收盘价跌破3日均线", "跌破20日低点"] : ["收盘价站上20日均线", "量比2日介于1.2到2.5"]
      };
    }
    return validateConditionExpression(dataService.base_url, text, mode);
  };

  const validateCustomStockSymbols = async (symbols: string[]): Promise<StockSymbolValidationResult | null> => {
    const requested = symbols.map((symbol) => symbol.trim()).filter(Boolean);
    if (requested.length === 0) {
      const emptyResult: StockSymbolValidationResult = {
        ok: false,
        valid_symbols: [],
        invalid_symbols: [],
        normalized_symbols: [],
        source: "empty"
      };
      setStockSymbolValidation(emptyResult);
      return emptyResult;
    }
    if (!dataService) {
      const serviceUnavailable: StockSymbolValidationResult = {
        ok: false,
        valid_symbols: [],
        invalid_symbols: requested,
        normalized_symbols: requested,
        source: "service-unavailable"
      };
      setStockSymbolValidation(serviceUnavailable);
      return serviceUnavailable;
    }
    setIsValidatingStockSymbols(true);
    try {
      const result = await validateStockSymbols(dataService.base_url, requested);
      setStockSymbolValidation(result);
      return result;
    } catch {
      const failed: StockSymbolValidationResult = {
        ok: false,
        valid_symbols: [],
        invalid_symbols: requested,
        normalized_symbols: requested,
        source: "request-failed"
      };
      setStockSymbolValidation(failed);
      return failed;
    } finally {
      setIsValidatingStockSymbols(false);
    }
  };

  const handleValidateCondition = async (text: string) => {
    setIsValidatingCondition(true);
    try {
      setConditionValidation(await validateConditionText(text));
    } catch (caught) {
      setConditionValidation({
        ok: false,
        normalized_text: text.trim(),
        condition: null,
        errors: [{ code: "request_failed", message: caught instanceof Error ? caught.message : "条件校验失败。" }],
        examples: ["收盘价站上20日均线", "量比2日介于1.2到2.5"]
      });
    } finally {
      setIsValidatingCondition(false);
    }
  };

  const coverageSymbols = coverage.reduce((sum, item) => sum + item.symbols, 0);
  const liveHeatRatio = marketSnapshot?.breadth && marketSnapshot.breadth.total > 0
    ? marketSnapshot.breadth.up / marketSnapshot.breadth.total
    : null;
  const marketDegreeSource = clsFinance?.emotion?.market_degree_source;
  const hasTonghuashunMarketDegree = marketDegreeSource === "ths-market-summary";
  const marketDegree = hasTonghuashunMarketDegree ? clsFinance?.emotion?.market_degree : null;
  const marketDegreeLabel = hasTonghuashunMarketDegree
    ? clsFinance?.emotion?.market_degree_label ?? "同花顺大盘评级"
    : "同花顺大盘评级";
  const marketDegreeNote = marketDegree == null
    ? isLoadingClsFinance ? "正在读取同花顺大盘评分" : "同花顺评分暂不可用"
    : marketDegreeLabel;
  const issueCount = result?.preflight_issues.length ?? 0;
  const riskAlertCount = riskAlerts?.items.length ?? 0;
  const closedTrades = result?.metrics.trade_count ?? 0;
  const visibleTrades = result ? mergeBacktestTrades(streamedTrades, result.trades) : streamedTrades;
  const poolLabel = {
    all: "全A",
    main_board: "沪深主板",
    gem: "创业板",
    star: "科创板",
    beijing: "北交所",
    custom: settings.custom_symbols.length > 0 ? `自选 ${settings.custom_symbols.length} 只` : "自选代码"
  }[settings.stock_pool];
  const marketBreadthLabel = marketSnapshot?.breadth
    ? marketSnapshot.status === "live"
      ? `今日实时红盘 ${marketSnapshot.breadth.up} / 全市场 ${marketSnapshot.breadth.total}`
      : `本地最近交易日/非实时 红盘 ${marketSnapshot.breadth.up} / 样本 ${marketSnapshot.breadth.total}`
    : `${poolLabel} / 等待实时行情`;

  const applySavedStrategy = (preset: SavedStrategyPreset) => {
    setStrategy(cloneStrategyConfig(preset.strategy));
    setStrategySaveMessage(`已套用已保存策略：${preset.name}`);
  };

  const deleteSavedStrategy = async (presetId: string) => {
    if (isMutatingStrategies) {
      return;
    }
    // The store waits for the single initial load, refuses to persist unless the
    // load succeeded, and serializes this against any other save/delete.
    const result = await savedStrategyStore.remove(presetId);
    if (result.ok) {
      setStrategySaveMessage(result.removedName ? `已删除已保存策略：${result.removedName}` : "已删除已保存策略。");
    } else {
      setStrategySaveMessage(result.error ?? "删除策略失败。");
    }
  };

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="topbar-copy">
          <span className="eyebrow">A股历史回测</span>
          <h1>A股策略回测工作台</h1>
          <p>基于本地 a-stock-data 历史数据，调参、回滚、查看策略预期收益。</p>
        </div>
        <div className="topbar-actions" aria-label="运行状态">
          <UpdatePanel />
          <span className="status-pill"><Activity size={16} aria-hidden="true" /> 保守日线撮合</span>
          <span className="status-pill"><Database size={16} aria-hidden="true" /> 本地缓存</span>
        </div>
      </header>
      {!isTauri ? (
        <div className="preview-banner" role="status">
          浏览器预览：页面展示的是演示数据，回测/行情/补数据等操作仅在桌面端（Tauri）生效。
        </div>
      ) : null}
      <div className="market-news-layout">
        <MarketDashboard snapshot={marketSnapshot} commentary={marketCommentary} isLoading={isLoadingMarket} refreshMeta={marketRefreshMeta} />
        <NewsPanel news={marketNews} aiDigest={aiDigest} isLoading={isLoadingNews} onRefresh={refreshNews} />
      </div>
      <div className="market-insight-layout">
        <ClsFinancePanel finance={clsFinance} isLoading={isLoadingClsFinance} />
        <NewsSummaryPanel summary={newsSummary} isLoading={isLoadingNewsSummary} />
      </div>
      <TonghuashunBriefingPanel fupan={fupanBriefing} zaopan={zaopanBriefing} />
      <section className="summary-band" aria-label="工作台概览">
        <article className="summary-card heat-card">
          <div>
            <span>市场热度</span>
            <strong>{formatPercent(liveHeatRatio)}</strong>
          </div>
          <Flame size={24} aria-hidden="true" />
          <small>{marketBreadthLabel}</small>
        </article>
        <article className={`summary-card market-degree-card ${marketDegreeCardClass(marketDegree)}`.trim()}>
          <div>
            <span>大盘评分</span>
            <strong className={marketDegreeTextClass(marketDegree)}>{formatMarketDegree(marketDegree)}</strong>
          </div>
          <Gauge size={24} aria-hidden="true" />
          <small>{marketDegreeNote}</small>
        </article>
        <article className="summary-card">
          <div>
            <span>收益表现</span>
            <strong>{formatPercent(result?.metrics.total_return_pct)}</strong>
          </div>
          <Activity size={24} aria-hidden="true" />
          <small>最大回撤 {formatPercent(result?.metrics.max_drawdown_pct)} / {closedTrades} 笔交易</small>
        </article>
        <button
          className="summary-card summary-card-button"
          type="button"
          aria-label={`查看全市场风险提示，当前 ${riskAlertCount > 0 ? riskAlertCount : issueCount} 项`}
          onClick={() => setRiskModalOpen(true)}
        >
          <div>
            <span>风险提示</span>
            <strong>{riskAlertCount > 0 ? `${riskAlertCount}项` : issueCount === 0 ? "0项" : `${issueCount}项`}</strong>
          </div>
          <ShieldAlert size={24} aria-hidden="true" />
          <small>{riskAlertCount > 0 ? "全市场 ST / 退市风险清单" : `当前覆盖股票数 ${formatCompact(coverageSymbols)}`}</small>
        </button>
      </section>
      <div className="workspace">
        <StrategyWorkbench
          coverage={coverage}
          settings={settings}
          strategy={strategy}
          onSettingsChange={handleSettingsChange}
          onStrategyChange={setStrategy}
          disabled={isRunningBacktest}
          conditionValidation={conditionValidation}
          isValidatingCondition={isValidatingCondition}
          validationExamples={conditionValidation?.examples ?? []}
          recommendedStrategies={recommendedStrategies}
          savedStrategies={savedStrategies}
          isMutatingStrategies={isMutatingStrategies}
          strategySaveMessage={strategySaveMessage}
          pendingStrategySaveName={pendingStrategySave?.name ?? null}
          onValidateCondition={handleValidateCondition}
          validateConditionText={validateConditionText}
          onApplySavedStrategy={applySavedStrategy}
          onDeleteSavedStrategy={deleteSavedStrategy}
          onConfirmPendingStrategySave={confirmPendingStrategySave}
          onDismissPendingStrategySave={dismissPendingStrategySave}
          onSettingsDraftErrorsChange={setSettingsDraftErrors}
          stockSymbolValidation={stockSymbolValidation}
          isValidatingStockSymbols={isValidatingStockSymbols}
          onValidateStockSymbols={validateCustomStockSymbols}
          aiReady={Boolean(aiStatus?.configured)}
          onParseConditions={handleParseConditions}
          optimizeBaseUrl={dataService?.base_url ?? null}
        />
        {error ? <div className="error-banner" role="alert">{error}</div> : null}
        <div className="results-trades-grid">
          <Suspense fallback={(
            <section className="surface results-surface" aria-busy="true">
              <h2>收益概览</h2>
            </section>
          )}>
            <ResultsOverview
              result={result}
              isRunning={isRunningBacktest}
              phases={runPhases}
              progressMessage={runProgressMessage}
              onRun={runBacktest}
              riskAlertCount={riskAlertCount}
              onOpenRiskAlerts={() => setRiskModalOpen(true)}
              aiBaseUrl={aiStatus?.configured ? dataService?.base_url ?? null : null}
              strategy={strategy}
              settings={settings}
              onAskAi={(task) => {
                setAiTask(task);
                setAiOpen(true);
                setAiUnseenInsights(0);
              }}
            />
          </Suspense>
          <TradesTable trades={isRunningBacktest ? streamedTrades : visibleTrades} />
        </div>
        <DataCenter
          cacheDir=".astock-cache"
          coverage={coverage}
          onCoverageChange={handleCoverageChange}
          onServiceReady={setDataService}
        />
      </div>
      <RiskAlertsModal
        open={riskModalOpen}
        alerts={riskAlerts}
        isLoading={isLoadingRiskAlerts}
        onClose={() => setRiskModalOpen(false)}
        onRefresh={refreshRiskAlerts}
        aiBaseUrl={aiStatus?.configured ? dataService?.base_url ?? null : null}
      />
      <AiAssistantPanel
        open={aiOpen}
        baseUrl={dataService?.base_url ?? null}
        insights={aiInsights}
        task={aiTask}
        onTaskConsumed={() => setAiTask(null)}
        onClose={() => setAiOpen(false)}
        onInsightsShown={() => setAiUnseenInsights(0)}
        onApplyStrategy={(strategy) => {
          setStrategy(cloneStrategyConfig(strategy));
          setStrategySaveMessage("已套用 AI 生成的策略，可在策略工作台继续调整。");
        }}
      />
      {/* 抽屉打开时悬浮球隐藏：它的位置正好压住抽屉输入区的发送按钮，
          且抽屉头部已有关闭按钮，双重关闭入口反而互相遮挡。 */}
      {!aiOpen && (
        <button
          className="ai-fab"
          type="button"
          aria-label={`打开 AI 投研助手${aiUnseenInsights > 0 ? `，${aiUnseenInsights} 条未读快讯` : ""}`}
          onClick={() => {
            setAiOpen(true);
            setAiUnseenInsights(0);
          }}
        >
          <Sparkles size={22} aria-hidden="true" />
          {aiUnseenInsights > 0 ? <span className="ai-fab-badge">{aiUnseenInsights}</span> : null}
        </button>
      )}
    </main>
  );
}
