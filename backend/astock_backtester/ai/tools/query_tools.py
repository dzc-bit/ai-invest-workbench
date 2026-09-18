"""Execution tools: SQL over the local warehouse, stats computation, data backfill.

- ``query_warehouse_sql``: NL→SQL via DuckDB over the daily-bars parquet
  partitions.  The connection is in-memory and the guarded statement filter
  only allows SELECT/WITH, so the warehouse files stay untouched.
- ``compute_stock_stats``: pandas-based per-symbol statistics (returns,
  volatility, drawdown, flow sums).
- ``update_stock_data``: the ONLY write path — it routes through the same
  sanctioned ``operations`` layer the data center uses (never raw SQL).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

import pandas as pd

from astock_backtester.ai.context import retain_rows
from astock_backtester.ai.tools.local_tools import AiBackend
from astock_backtester.ai.tools.registry import AiTool
from astock_backtester.data.symbols import normalize_symbol

SQL_MAX_ROWS = 500
SQL_ROW_LIMIT = 501
BACKFILL_MAX_SYMBOLS = 20
BACKFILL_MAX_RANGE_DAYS = 400

_DAILY_BARS_COLUMNS = (
    "symbol(6位代码), stock_name, trade_date(时间戳), open, high, low, close, volume, amount, "
    "change_pct(小数), turnover_rate(小数), volume_ratio, float_market_cap, total_market_cap, "
    "main_net_inflow(元), is_st, is_suspended"
)

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|import|pragma|call|set|reset|"
    r"vacuum|checkpoint|load|install|execute|prepare|begin|commit|rollback|force)\b"
    r"|\bread_\w+\b|\bglob\s*\(|\bsniff_csv\b|\bparquet_scan\b|\biceberg_scan\b|\bdelta_scan\b",
    re.IGNORECASE,
)
# 任何含路径特征的字符串字面量（盘符、分隔符、相对跳转）都不放行：用户 SQL 只应引用
# 工具注入的 daily_bars 视图和纯值字面量（'600519'、'2026-01-01'）。
_PATH_LIKE_LITERAL = re.compile(r"'[^']*(?:\\|[A-Za-z]:|\.\./|\.\.\\)[^']*'")


def _statement_allowed(sql: str) -> bool:
    if _FORBIDDEN_SQL.search(sql):
        return False
    return sql.lstrip().lower().startswith(("select", "with"))


def build_query_tools(backend: AiBackend) -> list[AiTool]:
    def query_warehouse_sql(args: dict[str, Any]) -> dict[str, Any]:
        sql = str(args.get("sql", "")).strip().rstrip(";")
        if not sql:
            return {"ok": False, "error": "sql 不能为空"}
        if not _statement_allowed(sql) or _PATH_LIKE_LITERAL.search(sql):
            return {
                "ok": False,
                "error": "只允许只读 SELECT/WITH 查询（禁止写语句与文件读取函数）。要补数据请用 update_stock_data 工具。",
            }
        parquet_paths = backend.warehouse.daily_bars_parquet_paths()
        if not parquet_paths:
            return {"ok": False, "error": "本地数据仓还没有日线 parquet 分区，请先在数据中心同步数据。"}
        if not re.search(r"\blimit\b", sql, re.IGNORECASE):
            sql = f"SELECT * FROM ({sql}) _guarded LIMIT {SQL_ROW_LIMIT}"
        import duckdb

        path_list = "[" + ", ".join(f"'{path}'" for path in parquet_paths) + "]"
        try:
            connection = duckdb.connect()
            try:
                connection.execute(f"CREATE OR REPLACE VIEW daily_bars AS SELECT * FROM read_parquet({path_list})")
                relation = connection.execute(sql)
                columns = [item[0] for item in (relation.description or [])]
                rows = relation.fetchmany(SQL_ROW_LIMIT)
            finally:
                connection.close()
        except Exception as exc:  # noqa: BLE001 - SQL errors are tool results, not crashes
            return {"ok": False, "error": f"SQL 执行失败：{exc}"}
        truncated = len(rows) > SQL_MAX_ROWS
        data = [dict(zip(columns, row, strict=False)) for row in rows[:SQL_MAX_ROWS]]
        for row in data:
            for key, value in row.items():
                if isinstance(value, (pd.Timestamp, date)):
                    row[key] = str(value)[:10]
        return {"ok": True, "rows": data, "row_count": len(data), "truncated": truncated}

    def summarize_sql(payload: dict[str, Any]) -> str:
        if not payload.get("ok"):
            return f"查询失败：{payload.get('error')}"
        rows = payload.get("rows", [])
        if not rows:
            return "查询成功：0 行"
        retained = retain_rows(rows, max_rows=25)
        # 回填保留元数据：agent 据此告诉模型"还有 M 行，用 read_tool_result 从 offset=K 续读"。
        payload["shown_rows"] = retained.kept
        payload["more_rows"] = retained.omitted
        return f"查询成功 {payload.get('row_count')} 行：\n{retained.text}"

    def compute_stock_stats(args: dict[str, Any]) -> dict[str, Any]:
        symbol = normalize_symbol(str(args.get("symbol", "")))
        if not symbol or not symbol.isdigit():
            return {"ok": False, "error": f"无法识别股票代码：{args.get('symbol')!r}"}
        window = max(20, min(int(args.get("window", 60)), 250))
        end = date.today()
        start = end - timedelta(days=window * 3)
        frame = backend.warehouse.read_daily_bars(
            symbols=[symbol], start_date=start.isoformat(), end_date=end.isoformat(), require_ohlc=True
        )
        if frame.empty:
            return {"ok": False, "error": f"本地数据仓没有 {symbol} 的日线数据。"}
        frame = frame.sort_values("trade_date").tail(window)
        closes = frame["close"].astype(float)
        returns = closes.pct_change().dropna()
        running_max = closes.cummax()
        drawdown = (closes / running_max - 1.0).min()
        last_row = frame.iloc[-1]
        stats = {
            "symbol": symbol,
            "name": str(last_row.get("stock_name") or symbol),
            "window_days": int(len(frame)),
            "start": str(frame.iloc[0]["trade_date"])[:10],
            "end": str(last_row["trade_date"])[:10],
            "first_close": round(float(closes.iloc[0]), 3),
            "last_close": round(float(closes.iloc[-1]), 3),
            "total_return_pct": round(float(closes.iloc[-1] / closes.iloc[0] - 1.0), 4),
            "annualized_volatility_pct": round(float(returns.std() * (242**0.5)), 4) if len(returns) > 2 else None,
            "max_drawdown_pct": round(float(drawdown), 4),
            "avg_turnover_rate": round(float(frame["turnover_rate"].mean()), 4) if "turnover_rate" in frame else None,
            "main_net_inflow_sum": round(float(frame["main_net_inflow"].sum()), 2) if "main_net_inflow" in frame else None,
        }
        return {"ok": True, "stats": stats}

    def summarize_stats(payload: dict[str, Any]) -> str:
        stats = payload.get("stats", {})
        return (
            f"{stats.get('symbol')} {stats.get('name')} 近 {stats.get('window_days')} 个交易日："
            f"区间收益 {stats.get('total_return_pct', 0):+.2%}，年化波动 {stats.get('annualized_volatility_pct') or '--'}，"
            f"最大回撤 {stats.get('max_drawdown_pct', 0):.2%}，主力净流入合计 {stats.get('main_net_inflow_sum')}"
        )

    def update_stock_data(args: dict[str, Any]) -> dict[str, Any]:
        from astock_backtester.data.operations import fetch_daily_bars_into_cache

        symbols = [normalize_symbol(str(s)) for s in args.get("symbols", []) if str(s).strip()]
        symbols = list(dict.fromkeys(symbols))
        if not symbols:
            return {"ok": False, "error": "symbols 不能为空"}
        if len(symbols) > BACKFILL_MAX_SYMBOLS:
            return {"ok": False, "error": f"单次最多补齐 {BACKFILL_MAX_SYMBOLS} 只股票"}
        try:
            start_date = str(args["start_date"])
            end_date = str(args["end_date"])
        except KeyError as exc:
            return {"ok": False, "error": f"缺少参数：{exc}"}
        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        if end < start:
            return {"ok": False, "error": "end_date 不能早于 start_date"}
        if (end - start).days > BACKFILL_MAX_RANGE_DAYS:
            return {"ok": False, "error": f"补齐区间最长 {BACKFILL_MAX_RANGE_DAYS} 天"}
        if pd.Timestamp(end) > pd.Timestamp(date.today()):
            end_date = date.today().isoformat()

        def fetcher(symbols_list: list[str], fetch_start: str, fetch_end: str) -> pd.DataFrame:
            frames = []
            for symbol in symbols_list:
                frame = backend.provider.fetch_daily_bars(symbol, fetch_start, fetch_end)
                if not frame.empty:
                    frames.append(frame)
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

        def flow_fetcher(symbols_list: list[str], fetch_start: str, fetch_end: str) -> dict[str, Any]:
            return backend.capital_flow_crawler.fetch_many_fund_flows(symbols_list, fetch_start, fetch_end, timeout=15)

        result = fetch_daily_bars_into_cache(
            cache=backend.cache,
            warehouse=backend.warehouse,
            fetcher=fetcher,
            capital_flow_fetcher=flow_fetcher,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
        )
        for entry in result.logs:
            backend.log(entry.level, entry.message)
        return {
            "ok": True,
            "status": result.status,
            "imported_rows": result.imported_rows,
            "fetched_symbols": result.fetched_symbols,
            "missing_symbols": result.missing_symbols,
            "failure_count": len(result.failures),
        }

    def summarize_update(payload: dict[str, Any]) -> str:
        return (
            f"数据补齐{payload.get('status')}：写入 {payload.get('imported_rows')} 行，"
            f"成功 {len(payload.get('fetched_symbols', []))} 只，缺失 {len(payload.get('missing_symbols', []))} 只，"
            f"失败 {payload.get('failure_count', 0)} 项。"
        )

    return [
        AiTool(
            name="query_warehouse_sql",
            description=(
                "对本地日线数据仓执行只读 SQL（DuckDB 方言）。表 daily_bars，列："
                f"{_DAILY_BARS_COLUMNS}。trade_date 是 TIMESTAMP（比较用 TIMESTAMP '2026-01-01'），"
                "symbol 是字符串（'600519'）。适合筛选、聚合、排序、分组统计等任意查询；写数据请用 update_stock_data。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "只读 SELECT/WITH 语句，默认自动加 LIMIT 500"}
                },
                "required": ["sql"],
            },
            executor=query_warehouse_sql,
            summarizer=summarize_sql,
            digest_chars=3_600,
        ),
        AiTool(
            name="compute_stock_stats",
            description="计算单只股票的区间统计：区间收益、年化波动率、最大回撤、平均换手、主力净流入合计。",
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位股票代码"},
                    "window": {"type": "integer", "description": "交易日窗口，默认 60，最大 250"},
                },
                "required": ["symbol"],
            },
            executor=compute_stock_stats,
            summarizer=summarize_stats,
        ),
        AiTool(
            name="update_stock_data",
            description=(
                "唯一的写操作：通过数据中心同款补齐链路，把指定股票在区间的日线/市值/资金流写回本地数据仓。"
                "用户要求'补数据/更新数据/拉取某股票行情入库'时使用；执行后会写入仓库并刷新覆盖信息。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"}, "description": "最多 20 个 6 位代码"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["symbols", "start_date", "end_date"],
            },
            executor=update_stock_data,
            summarizer=summarize_update,
        ),
    ]
