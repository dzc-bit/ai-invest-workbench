import type { Trade } from "../types";

type Props = {
  trades: Trade[];
};

type TradeWithBlockedReason = Trade & {
  blocked_reason?: string | null;
};

function hasBlockedReason(trade: Trade): trade is TradeWithBlockedReason {
  return "blocked_reason" in trade;
}

function blockedReasonOf(trade: Trade): string | null {
  if (!hasBlockedReason(trade)) {
    return null;
  }
  return trade.blocked_reason?.trim() || null;
}

/** 交易行的身份：与 App 侧的去重口径一致，必须含 buy_signal_date。

 ``symbol-buy_date`` 比去重用的身份粗一档：同一天同一只股票的两笔（例如一笔
 被阻断、一笔成交）会撞 key，React 复用错位行。
 */
function tradeIdentity(trade: Trade): string {
  return `${trade.symbol}-${trade.buy_signal_date}-${trade.buy_date}`;
}

function formatPrice(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) {
    return "--";
  }
  return value.toFixed(2);
}

function formatPercent(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}

function formatCompactMoney(value: number | null | undefined): string {
  if (value == null) {
    return "--";
  }
  if (Math.abs(value) >= 10000) {
    return `${(value / 10000).toFixed(2)}万`;
  }
  return value.toFixed(2);
}

function translateReason(reason: string): string {
  if (reason.includes("float market cap")) {
    return reason.replace("float market cap", "流通市值").replace("in", "位于区间");
  }
  if (reason.includes("main net inflow")) {
    return reason.replace("main net inflow", "主力净流入");
  }
  if (reason.includes("volume ratio")) {
    return reason.replace("volume ratio", "量比").replace("in", "位于区间");
  }
  if (reason.includes("turnover")) {
    return reason.replace("turnover", "换手率").replace("in", "位于区间");
  }
  if (reason.includes("MACD histogram")) {
    return reason.replace("MACD histogram", "MACD柱线");
  }
  if (reason.includes("return")) {
    return reason.replace("return", "前期涨幅").replace("in", "位于区间");
  }
  if (reason.includes("close") && reason.includes("above MA")) {
    return reason.replace("close", "收盘价").replace("above", "高于");
  }
  if (reason.includes("close") && reason.includes("below MA")) {
    return reason.replace("close", "收盘价").replace("below", "低于");
  }
  if (reason.includes("broke prior") && reason.includes("high")) {
    return reason.replace("close", "收盘价").replace("broke prior", "突破前").replace("high", "高点");
  }
  if (reason.includes("broke prior") && reason.includes("low")) {
    return reason.replace("close", "收盘价").replace("broke prior", "跌破前").replace("low", "低点");
  }
  if (reason.includes("fixed holding days reached")) {
    return reason.replace("fixed holding days reached", "达到固定持仓天数");
  }
  if (reason.includes("take profit touched")) {
    return reason.replace("take profit touched", "触及止盈");
  }
  if (reason.includes("stop loss touched")) {
    return reason.replace("stop loss touched", "触及止损");
  }
  if (reason.includes("market rising ratio")) {
    return reason.replace("market rising ratio", "市场上涨家数占比");
  }
  if (reason.includes("unavailable before enough history")) {
    return reason.replace("unavailable before enough history", "历史长度不足，暂不可用");
  }
  return "未识别的策略条件说明";
}

export function TradesTable({ trades }: Props) {
  return (
    <section className="surface trades-surface">
      <div className="section-title">
        <div>
          <span className="section-kicker">信号解释</span>
          <h2>交易明细</h2>
        </div>
        <span className="status-pill compact">{trades.length} 笔</span>
      </div>
      <div className="table-wrap trades-scroll">
        <table>
          <thead>
            <tr>
              <th>股票</th>
              <th>买入实际成交价</th>
              <th>卖出实际成交价</th>
              <th>仓位</th>
              <th>触发原因</th>
              <th>收益</th>
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <tr>
                <td colSpan={6}>暂无交易记录</td>
              </tr>
            ) : (
              trades.map((trade) => {
                const blockedReason = blockedReasonOf(trade);
                const isBlockedEvent = trade.shares === 0 && blockedReason != null;
                const buyReasonText = trade.buy_reason.map(translateReason).join("；");
                return (
                  <tr className={isBlockedEvent ? "trade-blocked-row" : undefined} key={tradeIdentity(trade)}>
                    <td>
                      <strong>{trade.symbol}</strong>
                      {blockedReason ? <div className="trade-status-badge">阻断/延迟</div> : null}
                    </td>
                    <td>
                      <strong>{trade.buy_date}</strong> @ {formatPrice(trade.buy_price)}
                      <div>{isBlockedEvent ? "未成交" : `${trade.shares} 股`}</div>
                    </td>
                    <td>
                      {isBlockedEvent ? "未成交" : (
                        <>
                          <strong>{trade.sell_date ?? "持仓中"}</strong>
                          {trade.sell_date || trade.sell_price != null ? <div>@ {formatPrice(trade.sell_price)}</div> : null}
                        </>
                      )}
                    </td>
                    <td>
                      <strong>{formatPercent(trade.actual_position_pct)}</strong>
                      <div>
                        成交 {formatCompactMoney(trade.buy_amount)} / 计划 {formatCompactMoney(trade.planned_amount)}
                      </div>
                    </td>
                    <td>
                      {buyReasonText ? <div className="trade-reason-text">{buyReasonText}</div> : "暂无触发原因"}
                      {blockedReason ? (
                        <div className="trade-blocked-reason">
                          <strong>阻断原因：</strong>
                          <span>{blockedReason}</span>
                        </div>
                      ) : null}
                    </td>
                    <td className={isBlockedEvent || trade.pnl_pct == null ? "" : trade.pnl_pct >= 0 ? "up-text" : "down-text"}>
                      {isBlockedEvent ? "未产生收益" : trade.pnl_pct == null ? "持仓中" : `${(trade.pnl_pct * 100).toFixed(2)}%`}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}
