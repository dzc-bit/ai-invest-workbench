import { Suspense, lazy, useMemo } from "react";
import { Download, Play, Sparkles } from "lucide-react";
import type { AiTask } from "../aiTypes";
import { downloadBacktestReport } from "../reportHtml";
import type { BacktestResult, BacktestSettingsConfig, DailyStrategyMatches, StrategyConfig } from "../types";
import { AiOneShotLine } from "./AiOneShotLine";
import { OverfitStrip, useOverfitAssessment } from "./OverfitAssessment";

// recharts 整栈约 366 KB（raw），而本组件首屏即挂载 —— 静态 import 会让每个用户
// 在跑第一次回测前就把图表库下载完。拆成懒加载块后，只有真的要画曲线才拉。
const EquityCurve = lazy(() => import("./EquityCurve"));

type Props = {
  result: BacktestResult | null;
  isRunning?: boolean;
  phases?: string[];
  progressMessage?: string | null;
  onRun: () => void;
  riskAlertCount?: number;
  onOpenRiskAlerts: () => void;
  onAskAi?: (task: AiTask) => void;
  aiBaseUrl?: string | null;
  strategy?: StrategyConfig;
  settings?: BacktestSettingsConfig;
};

function buildBacktestContext(result: BacktestResult): Record<string, unknown> {
  return {
    metrics: result.metrics,
    preflight_issues: result.preflight_issues,
    trade_count: result.trades.length,
    recent_trades: result.trades.slice(-5),
    latest_strategy_matches: result.latest_strategy_matches?.matches.slice(0, 5) ?? [],
    equity_curve_tail: result.equity_curve.slice(-10)
  };
}

function translateMatchReason(reason: string): string {
  return reason
    .replace(/close ([\d.]+) broke prior (\d+)d high ([\d.]+)/, "收盘价 $1 突破前$2日高点 $3")
    .replace(/close ([\d.]+) broke prior (\d+)d low ([\d.]+)/, "收盘价 $1 跌破前$2日低点 $3")
    .replace(/(\d+)d volume ratio ([\d.]+) in \[([\d.]+), ([\d.]+)\]/, "$1日量比 $2 位于 [$3, $4]")
    .replace(/float market cap ([\d.]+) in \[([\d.]+), ([\d.]+)\]/, "流通市值 $1 位于 [$2, $3]")
    .replace(/turnover ([\d.]+) in \[([\d.]+), ([\d.]+)\]/, "换手率 $1 位于 [$2, $3]");
}

function matchReasonText(reasons: string[]): string {
  const translated = reasons.slice(0, 4).map(translateMatchReason);
  return translated.length > 0 ? translated.join("；") : "命中策略条件";
}

function isToday(value: string | null | undefined): boolean {
  if (!value) {
    return false;
  }
  const today = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).format(new Date());
  return value === today;
}

function normalizedEquityCurve(result: BacktestResult | null): BacktestResult["equity_curve"] {
  if (!result) {
    return [];
  }
  const byDate = new Map<string, BacktestResult["equity_curve"][number]>();
  for (const point of result.equity_curve) {
    if (!point.trade_date || !Number.isFinite(point.equity)) {
      continue;
    }
    byDate.set(point.trade_date, point);
  }
  return Array.from(byDate.values()).sort((left, right) => left.trade_date.localeCompare(right.trade_date));
}

function MatchedStocksPanel({ dailyMatches }: { dailyMatches?: DailyStrategyMatches | null }) {
  const hasPayload = Boolean(dailyMatches);
  const rawItems = dailyMatches?.matches ?? [];
  const items = [...rawItems].sort((left, right) => (right.rank_score ?? 0) - (left.rank_score ?? 0));
  const matchesAreToday = isToday(dailyMatches?.trade_date);
  const title = matchesAreToday ? "今日 user 模式候选" : "本地最近交易日候选";
  const kicker = matchesAreToday ? "当日符合用户策略的个股" : "本地最近交易日/非实时";
  const dateLabel = dailyMatches
    ? `信号日 ${dailyMatches.signal_date} / 展示日 ${dailyMatches.trade_date} / 本地回测快照`
    : "等待 latest_strategy_matches";
  return (
    <section className="matched-stocks-panel" aria-label="策略命中">
      <div className="matched-stocks-head">
        <div>
          <span className="section-kicker">{kicker}</span>
          <h3>{title}</h3>
          <small>{dateLabel}</small>
        </div>
        <strong>{hasPayload ? `${items.length} 只` : "待对接"}</strong>
      </div>
      {items.length > 0 ? (
        <div className="matched-stocks-list">
          {items.slice(0, 12).map((stock) => (
            <article className="matched-stock-card" key={`${stock.symbol}-${stock.trade_date ?? ""}`}>
              <div className="matched-stock-id">
                <strong>{stock.name ? `${stock.symbol} ${stock.name}` : stock.symbol}</strong>
              </div>
              <div className="matched-stock-quote">
                {stock.rank_score != null ? <small>评分 {stock.rank_score.toFixed(2)}</small> : null}
              </div>
              <p className="matched-stock-reason-text">理由：{matchReasonText(stock.reasons)}</p>
            </article>
          ))}
        </div>
      ) : (
        <div className="matched-stocks-empty">
          <strong>{hasPayload ? `${matchesAreToday ? "今日" : "本地最近交易日"}没有股票命中当前策略` : "等待回测结果返回策略候选"}</strong>
          <span>
            {hasPayload
              ? "可以放宽入场条件、扩大股票池，或查看数据中心是否缺少行情/资金字段。"
              : "回测完成后，这里会展示代码、名称、评分和匹配理由。"}
          </span>
        </div>
      )}
    </section>
  );
}

export function ResultsOverview({
  result,
  isRunning = false,
  phases = [],
  progressMessage = null,
  onRun,
  riskAlertCount = 0,
  onOpenRiskAlerts,
  onAskAi,
  aiBaseUrl = null,
  strategy,
  settings
}: Props) {
  const issueCount = result?.preflight_issues.length ?? 0;
  const overfitRequest = useMemo(() => (result ? { metrics: result.metrics } : null), [result]);
  const overfit = useOverfitAssessment(aiBaseUrl, overfitRequest);
  const chartData = normalizedEquityCurve(result);
  const chartStart = chartData[0]?.trade_date;
  const chartEnd = chartData.at(-1)?.trade_date;
  const zeroTradeHint = result && result.metrics.trade_count === 0
    ? "本次没有产生交易。常见原因是日期范围过短、股票池过窄、条件过严或本地字段缺失。"
    : null;

  return (
    <section className="surface results-surface">
      <div className="section-title">
        <div>
          <span className="section-kicker">策略收益与风险</span>
          <h2>收益概览</h2>
        </div>
        <div className="section-title-actions">
          {result && !isRunning && onAskAi ? (
            <button
              className="secondary-button"
              type="button"
              aria-label="让 AI 解读本次回测结果"
              onClick={() =>
                onAskAi({
                  message: "请解读上面这次回测结果：收益与回撤的主要来源、交易行为特征、参数上值得尝试的调整方向，并指出数据预检提示的含义。",
                  context: { kind: "backtest_result", payload: buildBacktestContext(result) }
                })
              }
            >
              <Sparkles size={15} aria-hidden="true" />
              AI 解读
            </button>
          ) : null}
          {result && !isRunning && strategy && settings ? (
            <button
              className="secondary-button"
              type="button"
              aria-label="导出回测报告"
              onClick={() => {
                downloadBacktestReport({ result, strategy, settings });
              }}
            >
              <Download size={15} aria-hidden="true" />
              导出报告
            </button>
          ) : null}
          <button className="primary-button" type="button" onClick={onRun} disabled={isRunning}>
            <Play size={16} aria-hidden="true" />
            {isRunning ? "回测运行中" : "运行历史回测"}
          </button>
        </div>
      </div>
      {isRunning || phases.length > 0 ? (
        <div className="run-progress" role="status" aria-label="回测运行进度">
          {phases.map((phase, index) => (
            <span key={phase} className={index === phases.length - 1 && isRunning ? "active" : "done"}>
              {phase}
            </span>
          ))}
        </div>
      ) : null}
      {progressMessage && (!result || !isRunning) ? (
        <div className="run-live-state">
          <strong>{isRunning ? "回测正在撮合" : "回测完成"}</strong>
          <span>{progressMessage}</span>
        </div>
      ) : null}
      {isRunning && !result && !progressMessage ? (
        <div className="run-live-state">
          <strong>回测正在撮合</strong>
          <span>正在扫描历史交易日，首笔开仓或平仓会立即写入右侧交易明细。</span>
        </div>
      ) : null}
      {result ? (
        <>
          <div className="metrics">
            <span>总收益 {(result.metrics.total_return_pct * 100).toFixed(2)}%</span>
            <span>年化收益 {(result.metrics.annualized_return_pct * 100).toFixed(2)}%</span>
            <span>最大回撤 {(result.metrics.max_drawdown_pct * 100).toFixed(2)}%</span>
            <span>胜率 {(result.metrics.win_rate_pct * 100).toFixed(2)}%</span>
            <span>交易次数 {result.metrics.trade_count}</span>
            <span>平均仓位 {(result.metrics.average_position_pct * 100).toFixed(2)}%</span>
            <span>最大仓位 {(result.metrics.max_position_pct * 100).toFixed(2)}%</span>
          </div>
          <OverfitStrip assessment={overfit} />
          {aiBaseUrl ? (
            <AiOneShotLine
              key={`${result.metrics.total_return_pct}-${result.metrics.trade_count}-${result.equity_curve.at(-1)?.trade_date ?? ""}`}
              baseUrl={aiBaseUrl}
              scene="results_overview"
              context={buildBacktestContext(result)}
              label="AI 回测短评"
            />
          ) : null}
          <div className="risk-strip">
            <strong>风险提示</strong>
            <span>
              {riskAlertCount > 0
                ? `市场风险清单发现 ${riskAlertCount} 只 ST 或退市风险股票`
                : issueCount === 0
                  ? "数据预检未发现阻断项"
                  : `发现 ${issueCount} 条数据或策略预检提示`}
            </span>
            <span>资金校准：{result.trades.some((trade) => trade.buy_reason.some((reason) => reason.includes("inflow"))) ? "已使用资金面条件" : "未使用资金面校准"}</span>
            <button className="secondary-button" type="button" onClick={onOpenRiskAlerts}>
              查看风险清单
            </button>
          </div>
          {zeroTradeHint ? (
            <div className="risk-strip no-trade-hint">
              <strong>无交易说明</strong>
              <span>{zeroTradeHint}</span>
            </div>
          ) : null}
          <MatchedStocksPanel dailyMatches={result.latest_strategy_matches} />
          <div className="chart">
            <div className="chart-head">
              <strong>历史权益曲线</strong>
              {chartStart && chartEnd ? <span>回测区间 {chartStart} 至 {chartEnd}</span> : null}
            </div>
            <div className="chart-body">
              <Suspense fallback={<div className="chart-placeholder">正在加载图表…</div>}>
                <EquityCurve data={chartData} />
              </Suspense>
            </div>
          </div>
        </>
      ) : !isRunning ? (
        <div className="empty-state">
          <strong>尚未运行回测</strong>
          <span>调整策略和参数后，点击“运行历史回测”查看收益曲线、回撤和交易原因。</span>
          <button className="secondary-button" type="button" onClick={onOpenRiskAlerts}>
            查看风险清单
          </button>
        </div>
      ) : null}
    </section>
  );
}
