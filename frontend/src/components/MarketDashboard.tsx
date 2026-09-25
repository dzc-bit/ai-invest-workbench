import { useRef } from "react";
import { Activity, Radio, TrendingDown, TrendingUp } from "lucide-react";
import { marketPhaseLabel } from "../marketRefresh";
import type { MarketBreadth, MarketCommentaryResponse, MarketRefreshMeta, RealtimeMarketSnapshot } from "../types";

type Props = {
  snapshot: RealtimeMarketSnapshot | null;
  commentary?: MarketCommentaryResponse | null;
  isLoading?: boolean;
  refreshMeta?: MarketRefreshMeta;
};

function formatPercent(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) {
    return "--";
  }
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(2)}%`;
}

function formatNumber(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) {
    return "--";
  }
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value);
}

function formatTime(value: string | null | undefined): string {
  if (!value) {
    return "--";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(new Date(value));
}

function movementClass(value: number | null | undefined): "up-text" | "down-text" | "flat-text" {
  if (value == null || Number.isNaN(value) || value === 0) {
    return "flat-text";
  }
  return value > 0 ? "up-text" : "down-text";
}

function sourceLabel(source: string | null | undefined): string | null {
  if (!source) {
    return null;
  }
  return (
    {
      "cls-quote-index": "财联社指数",
      "ashare-sina": "Ashare/Sina",
      "cls-quote-breadth": "财联社涨跌分布",
      "ths-indexflash-breadth": "同花顺涨跌分布",
      "ths-market-summary": "同花顺市场总览",
      "sina-a-share-live": "新浪实时个股",
      "tencent-a-share-live": "腾讯实时个股",
      "akshare-a-share-live": "AKShare 实时个股",
      "heavy-market-crawler": "重型公开行情爬虫",
      "browser-market-provider": "浏览器公开行情爬虫",
      "eastmoney-a-share-spot": "东方财富轻量 spot",
      "cls-hot-plate": "财联社热门板块",
      "ths-hot-reason": "同花顺热点归因",
      "ths-concept-section": "同花顺概念题材",
      "ths-industry-html": "同花顺行业板块",
      "sina-sector": "新浪行业板块",
      "akshare-sector": "AKShare 概念板块",
      "akshare-industry-sector": "AKShare 行业板块",
      "eastmoney-sector": "东方财富概念板块",
      "eastmoney-industry-sector": "东方财富行业板块",
      "local-latest": "本地最近交易日",
      "local-market-group": "本地板块聚合",
      "local-yesterday-group": "本地昨日板块",
      "eastmoney-yesterday-limit-up": "东方财富昨日涨停池"
    }[source] ?? source
  );
}

function uniqueLabels(values: Array<string | null | undefined>): string[] {
  const labels: string[] = [];
  for (const value of values) {
    const label = sourceLabel(value);
    if (label && !labels.includes(label)) {
      labels.push(label);
    }
  }
  return labels;
}

function successfulSourceSummary(snapshot: RealtimeMarketSnapshot | null): string {
  if (!snapshot) {
    return "--";
  }
  const parts: string[] = [];
  const indexSources = uniqueLabels(snapshot.indexes.map((quote) => quote.source));
  if (indexSources.length > 0) {
    parts.push(`指数 ${indexSources.join("/")}`);
  }
  const breadthSource = sourceLabel(snapshot.breadth?.source);
  if (breadthSource) {
    parts.push(`红绿 ${breadthSource}`);
  }
  const sectorSources = uniqueLabels(snapshot.strong_sectors.map((sector) => sector.source));
  if (sectorSources.length > 0) {
    parts.push(`板块 ${sectorSources.join("/")}`);
  }
  const yesterdaySources = uniqueLabels((snapshot.yesterday_strong_sectors ?? []).map((sector) => sector.source));
  if (yesterdaySources.length > 0) {
    parts.push(`昨日 ${yesterdaySources.join("/")}`);
  }
  return parts.length > 0 ? parts.join("；") : sourceLabel(snapshot.source) ?? "--";
}

function firstFailedAttempt(snapshot: RealtimeMarketSnapshot | null): string | null {
  const diagnostics = snapshot?.diagnostics ?? [];
  return (
    diagnostics.find((message) =>
      /失败|不可用|无效|不完整|未取得|返回空|超时|failed|timeout|no valid rows|unavailable|invalid/i.test(message)
    ) ?? null
  );
}

function refreshStatusLabel(meta: MarketRefreshMeta | undefined, isLoading: boolean): string {
  if (isLoading || meta?.status === "refreshing") {
    return "刷新中";
  }
  if (meta?.status === "using_last_success") {
    return "使用最近数据";
  }
  if (meta?.status === "unavailable") {
    return "实时接口暂不可用";
  }
  return "";
}

function isYesterdaySectorTracking(snapshot: RealtimeMarketSnapshot | null): boolean {
  return (snapshot?.diagnostics ?? []).some((item) =>
    /eastmoney-yesterday-limit-up tracking (?:refresh is still running|refresh scheduled in background)\./i.test(item)
  );
}

/** 行情评价的状态语义（§6）：mode 决定"这是谁说的话"。
 * 非实时来源必须显式标注，绝不把回退包装成实时盘面。 */
function commentaryModeMeta(mode: MarketCommentaryResponse["mode"]): { label: string; live: boolean } {
  switch (mode) {
    case "intraday":
    case "post_close":
      return { label: mode === "intraday" ? "实时快照评价" : "收盘评价", live: true };
    case "lunch_break_review":
      return { label: "午间小结", live: false };
    case "non_trading_review":
      return { label: "休市回顾", live: false };
    case "news_fallback":
      return { label: "公开行情兜底 · 非实时", live: false };
    case "local_brief_review":
      return { label: "本地简短判断 · 非实时", live: false };
    default:
      return { label: "评价", live: false };
  }
}

export function MarketDashboard({ snapshot, commentary, isLoading = false, refreshMeta }: Props) {
  // 本轮快照缺红绿家数时沿用最近一次有数据的宽度并明确标注“沿用”，
  // 避免部分成功场景下长时间显示 "--"（AGENTS.md §5：缓存只能以 stale 标注使用）。
  const lastBreadthRef = useRef<{ breadth: MarketBreadth; at: string } | null>(null);
  if (snapshot?.breadth) {
    lastBreadthRef.current = { breadth: snapshot.breadth, at: snapshot.updated_at };
  }
  const carriedBreadth = snapshot && !snapshot.breadth ? lastBreadthRef.current : null;
  const breadth = snapshot?.breadth ?? carriedBreadth?.breadth ?? null;
  const breadthCarriedAt = !snapshot?.breadth && carriedBreadth ? carriedBreadth.at : null;
  const statusLabel = snapshot?.status === "live" ? "实时" : snapshot?.status === "stale" ? "本地兜底" : "待连接";
  const refreshLabel = refreshStatusLabel(refreshMeta, isLoading);
  const phase = refreshMeta?.phase ?? snapshot?.market_phase;
  const sourceSummary = successfulSourceSummary(snapshot);
  const failedAttempt = firstFailedAttempt(snapshot);
  const yesterdaySectorTracking = isYesterdaySectorTracking(snapshot);

  return (
    <section className="surface market-dashboard" aria-label="今日实时行情">
      <div className="section-title">
        <div>
          <span className="section-kicker">本地数据 + 实时接口</span>
          <h2>今日实时行情</h2>
        </div>
        <span className={`status-pill compact market-status ${snapshot?.status ?? "loading"}`}>
          <Radio size={15} aria-hidden="true" />
          {refreshLabel || statusLabel}
        </span>
      </div>
      {refreshMeta || phase ? (
        <div className="market-refresh-strip" role="status">
          <span>{phase ? marketPhaseLabel(phase) : "行情时段待确认"}</span>
          <strong>{refreshMeta?.message ?? snapshot?.message ?? "等待行情刷新"}</strong>
          {refreshMeta?.last_success_at ? <small>最近成功 {formatTime(refreshMeta.last_success_at)}</small> : null}
        </div>
      ) : null}

      {commentary ? (
        <div className="market-commentary" aria-label="行情评价">
          <div className="market-commentary-head">
            <span className="section-kicker">行情评价</span>
            <span
              className={`status-pill compact ${commentary.mode === "intraday" || commentary.mode === "post_close" ? "market-status live" : "market-status stale"}`}
            >
              {commentaryModeMeta(commentary.mode).label}
            </span>
          </div>
          <p>{commentary.summary}</p>
          {commentary.drivers.length > 0 ? (
            <ul>
              {commentary.drivers.slice(0, 3).map((driver) => (
                <li key={driver.title}>
                  <strong>{driver.title}</strong> {driver.detail}
                </li>
              ))}
            </ul>
          ) : null}
          {commentary.risks.length > 0 ? (
            <p className="market-commentary-risks">主要风险：{commentary.risks.slice(0, 3).join("；")}</p>
          ) : null}
          <small>
            生成于 {formatTime(commentary.updated_at)}（{commentary.trade_date}）
            {commentaryModeMeta(commentary.mode).live ? "" : " · 非实时结论，仅供参考"}
          </small>
        </div>
      ) : null}

      <div className="market-grid">
        <div className="index-strip">
          {(snapshot?.indexes ?? []).slice(0, 3).map((quote) => (
            <article className="index-quote" key={quote.symbol}>
              <span>{quote.name}</span>
              <strong>{formatNumber(quote.last)}</strong>
              <small className={movementClass(quote.change_pct)}>
                {formatPercent(quote.change_pct)} / {formatNumber(quote.change)}
              </small>
            </article>
          ))}
          {!snapshot && (
            <article className="index-quote">
              <span>行情连接</span>
              <strong>--</strong>
              <small>等待本地服务返回实时接口</small>
            </article>
          )}
        </div>

        <div className="breadth-panel">
          <div>
            <span>红绿家数{breadthCarriedAt ? "（沿用）" : ""}</span>
            <strong>
              <TrendingUp size={18} aria-hidden="true" /> 红 {breadth?.up ?? "--"}
            </strong>
            <strong className="down-text">
              <TrendingDown size={18} aria-hidden="true" /> 绿 {breadth?.down ?? "--"}
            </strong>
          </div>
          <small>
            平盘 {breadth?.flat ?? "--"} / 合计 {breadth?.total ?? "--"}
            {breadthCarriedAt ? ` · 沿用 ${formatTime(breadthCarriedAt)} 数据` : ""}
          </small>
        </div>

        <div className="sector-panel">
          <div className="sector-head">
            <span>强势板块</span>
            <Activity size={18} aria-hidden="true" />
          </div>
          <div className="sector-list">
            {(snapshot?.strong_sectors ?? []).slice(0, 5).map((sector) => (
              <span key={`${sector.name}-${sector.leading_symbol ?? ""}`}>
                {sector.name}
                <strong className={movementClass(sector.change_pct)}>{formatPercent(sector.change_pct)}</strong>
              </span>
            ))}
            {snapshot && snapshot.strong_sectors.length === 0 ? <span>暂无板块数据</span> : null}
            {!snapshot ? <span>等待行情快照</span> : null}
          </div>
          <div className="yesterday-sector-track">
            <span>昨日强势追踪</span>
            <div className="sector-list compact">
              {(snapshot?.yesterday_strong_sectors ?? []).slice(0, 4).map((sector) => (
                <span key={`yesterday-${sector.name}-${sector.leading_symbol ?? ""}`}>
                  {sector.name}
                  <strong className={movementClass(sector.change_pct)}>{formatPercent(sector.change_pct)}</strong>
                </span>
              ))}
              {snapshot && (snapshot.yesterday_strong_sectors ?? []).length === 0 ? (
                <span>{yesterdaySectorTracking ? "正在加载昨日板块数据" : "暂无昨日强势板块"}</span>
              ) : null}
              {!snapshot ? <span>等待行情快照</span> : null}
            </div>
          </div>
        </div>
      </div>

      <p className="market-footnote">
        成功来源 {sourceSummary} / 更新 {formatTime(snapshot?.updated_at)} / {snapshot?.message ?? "正在等待实时行情"}
        {failedAttempt ? ` / 尝试未成功 ${failedAttempt}` : ""}
      </p>
    </section>
  );
}
