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


def _warehouse_as_of(backend: AiBackend) -> str:
    """Latest date present in the warehouse, as ``YYYY-MM-DD`` ("" when empty).

    Local warehouse numbers are history: without this stamp the model presents
    a stale close as if it were the current quote (realtime-first discipline).
    """
    try:
        coverage = backend.coverage_snapshot()
    except Exception:  # noqa: BLE001
        return ""
    for item in coverage:
        if getattr(item, "dataset", "") == "daily_bars" and getattr(item, "end_date", None):
            return str(item.end_date)
    return ""


def _staleness_suffix(as_of: str) -> str:
    if not as_of:
        return ""
    return f"（本地数据仓截止 {as_of}，非实时）"


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
        as_of = _warehouse_as_of(backend)
        return {
            "ok": True,
            "rows": data,
            "row_count": len(data),
            "truncated": truncated,
            "as_of_date": as_of or None,
            "is_realtime": False,
            "note": "本地数据仓为历史数据，不反映当前盘中行情" if as_of else "",
        }

    def summarize_sql(payload: dict[str, Any]) -> str:
        if not payload.get("ok"):
            return f"查询失败：{payload.get('error')}"
        rows = payload.get("rows", [])
        as_of_note = _staleness_suffix(str(payload.get("as_of_date") or ""))
        if not rows:
            return f"查询成功：0 行{as_of_note}"
        retained = retain_rows(rows, max_rows=25)
        # 回填保留元数据：agent 据此告诉模型"还有 M 行，用 read_tool_result 从 offset=K 续读"。
        payload["shown_rows"] = retained.kept
        payload["more_rows"] = retained.omitted
        payload["resume_offset"] = retained.resume_offset
        note = (
            "\n（已达单次查询 500 行上限，库中可能还有更多数据——请在 SQL 里收紧条件或分页，不要当作全量结论。）"
            if payload.get("truncated")
            else ""
        )
        return f"查询成功 {payload.get('row_count')} 行{as_of_note}：\n{retained.text}{note}"

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
        return {"ok": True, "stats": stats, "is_realtime": False, "as_of_date": stats["end"]}

    def summarize_stats(payload: dict[str, Any]) -> str:
        stats = payload.get("stats", {})
        as_of = _staleness_suffix(str(stats.get("end") or ""))
        return (
            f"{stats.get('symbol')} {stats.get('name')} 近 {stats.get('window_days')} 个交易日"
            f"{as_of}：区间收益 {stats.get('total_return_pct', 0):+.2%}，"
            f"年化波动 {stats.get('annualized_volatility_pct') or '--'}，"
            f"最大回撤 {stats.get('max_drawdown_pct', 0):.2%}，主力净流入合计 {stats.get('main_net_inflow_sum')}"
        )

    def update_stock_data(args: dict[str, Any]) -> dict[str, Any]:
        from astock_backtester.data.operations import fetch_capital_flow_into_cache, fetch_daily_bars_into_cache

        mode = str(args.get("mode") or "daily_bars").strip().lower()
        if mode not in ("daily_bars", "capital_flow"):
            return {"ok": False, "error": "mode 只能是 daily_bars（日线+随行资金流合并）或 capital_flow（资金流缺口补齐）"}
        symbols = [normalize_symbol(str(s)) for s in args.get("symbols", []) if str(s).strip()]
        symbols = list(dict.fromkeys(symbols))
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

        # 全市场资金流补齐：同步链路逐只循环在步数预算内不可能完成，
        # 走数据中心同款后台任务（分批、可取消、失败归因），模型轮询进度。
        if not symbols:
            if mode != "capital_flow":
                return {
                    "ok": False,
                    "error": "日线补齐必须指定 symbols（最多 20 只）；全市场日线请在数据中心使用全市场同步。",
                }
            try:
                missing = backend.warehouse.read_capital_flow_missing_symbols(start_date, end_date)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"读取资金流缺口名单失败：{exc}"}
            if not missing:
                return {
                    "ok": True,
                    "mode": mode,
                    "status": "ok",
                    "imported_rows": 0,
                    "summary_detail": "窗口内没有缺资金流的股票，无需补齐。",
                }
            job = backend.sync_manager.start_capital_flow_backfill(sorted(missing), start_date, end_date)
            return {
                "ok": True,
                "mode": mode,
                "background_job": {
                    "job_id": job.job_id,
                    "mode": job.mode,
                    "status": job.status,
                    "total_symbols": job.total_symbols,
                    "start_date": job.start_date.isoformat(),
                    "end_date": job.end_date.isoformat(),
                },
                "summary_detail": f"已启动全市场资金流补齐后台任务（{job.total_symbols} 只），用 sync_job_status 轮询进度。",
            }
        if len(symbols) > BACKFILL_MAX_SYMBOLS:
            return {"ok": False, "error": f"单次最多补齐 {BACKFILL_MAX_SYMBOLS} 只股票"}

        def fetcher(symbols_list: list[str], fetch_start: str, fetch_end: str) -> pd.DataFrame:
            frames = []
            for symbol in symbols_list:
                frame = backend.provider.fetch_daily_bars(symbol, fetch_start, fetch_end)
                if not frame.empty:
                    frames.append(frame)
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

        def flow_fetcher(symbols_list: list[str], fetch_start: str, fetch_end: str) -> dict[str, Any]:
            # 爬虫内部已按首只股票的失败模式自适应 skip_eastmoney，无需调用方预判。
            return backend.capital_flow_crawler.fetch_many_fund_flows(symbols_list, fetch_start, fetch_end, timeout=15)

        if mode == "capital_flow":
            # 资金流缺口补齐：允许为“暂无日K”的股票先写资金流独立行，并跳过
            # 已完整的股票——与 /fetch/capital-flow 同一条链路。AI 侧不同步
            # 重扫 coverage（60 秒级），改由后台刷新接管。
            result = fetch_capital_flow_into_cache(
                cache=backend.cache,
                capital_flow_fetcher=flow_fetcher,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                warehouse=backend.warehouse,
                refresh_coverage=False,
            )
        else:
            result = fetch_daily_bars_into_cache(
                cache=backend.cache,
                warehouse=backend.warehouse,
                fetcher=fetcher,
                capital_flow_fetcher=flow_fetcher,
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                refresh_coverage=False,
            )
            try:
                backend.start_coverage_refresh(force=True)
            except Exception:  # noqa: BLE001 - 后台刷新失败不阻塞补齐结果
                pass
        for entry in result.logs:
            backend.log(entry.level, entry.message)
        payload: dict[str, Any] = {
            "ok": True,
            "mode": mode,
            "status": result.status,
            "imported_rows": result.imported_rows,
            "fetched_symbols": result.fetched_symbols,
            "skipped_symbols": result.skipped_symbols,
            "missing_symbols": result.missing_symbols,
            "failure_count": len(result.failures),
        }
        # 失败/缺失明细行集化：模型才知道“缺谁、为什么”，能改参数重试而不是盲目重来。
        rows: list[dict[str, Any]] = [
            {
                "symbol": str(item.get("symbol", "")),
                "reason": str(item.get("error") or item.get("message") or "抓取失败"),
            }
            for item in result.failures
        ]
        rows.extend({"symbol": symbol, "reason": "未取到该区间的目标数据"} for symbol in result.missing_symbols)
        if rows:
            payload["rows"] = rows
        return payload

    def summarize_update(payload: dict[str, Any]) -> str:
        if payload.get("background_job"):
            job = payload["background_job"]
            return f"资金流补齐已转后台任务 {job.get('job_id')}（{job.get('total_symbols')} 只），请轮询 sync_job_status。"
        detail = payload.get("summary_detail")
        if detail:
            return str(detail)
        heads = "、".join(str(item.get("symbol")) for item in (payload.get("rows") or [])[:3])
        rows_note = f"；示例：{heads}" if heads else ""
        return (
            f"数据补齐（{payload.get('mode')}）{payload.get('status')}：写入 {payload.get('imported_rows')} 行，"
            f"成功 {len(payload.get('fetched_symbols', []))} 只，"
            f"跳过 {len(payload.get('skipped_symbols', []))} 只，"
            f"缺失 {len(payload.get('missing_symbols', []))} 只，失败 {payload.get('failure_count', 0)} 项"
            f"{rows_note}。明细在结果的 rows 里，可用 read_tool_result 续读。"
        )

    def sync_job_status(args: dict[str, Any]) -> dict[str, Any]:
        job_id = str(args.get("job_id", "")).strip()
        if not job_id:
            return {"ok": False, "error": "job_id 不能为空"}
        job = backend.sync_manager.get_job(job_id)
        if job is None:
            return {"ok": False, "error": f"没有找到任务 {job_id}（任务只保留在内存中，服务重启后失效）"}
        return {"ok": True, "job": job.model_dump(mode="json")}

    def summarize_job(payload: dict[str, Any]) -> str:
        job = payload.get("job", {})
        failures = job.get("recent_failures") or []
        head = (
            f"任务 {job.get('mode')}（{job.get('job_id')}）状态 {job.get('status')}："
            f"已处理 {job.get('processed_symbols', 0)}/{job.get('total_symbols', 0)} 只"
            f"（完成 {job.get('completed_symbols', 0)}、跳过 {job.get('skipped_symbols', 0)}、失败 {job.get('failed_symbols', 0)}），"
            f"写入 {job.get('imported_rows', 0)} 行。"
        )
        if job.get("status") == "running":
            head += f" 当前：{job.get('current_symbol') or '…'}。"
        if failures:
            sample = "；".join(
                f"{item.get('symbol', '?')}（{str(item.get('error') or item.get('reason') or item.get('message') or '失败')[:40]}）"
                for item in failures[:3]
            )
            head += f" 近期失败示例：{sample}"
        return head

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
                "唯一的写操作，通过数据中心同款补齐链路写回本地数据仓。两种 mode："
                "daily_bars（默认）补指定股票区间的日线/市值并把资金流合并进新拉的日线行；"
                "capital_flow 补资金流缺口——允许为暂无日 K 的股票先写资金流独立行，"
                "省略 symbols 时自动找出窗口内缺资金流的股票并启动全市场后台任务（用 sync_job_status 轮询）。"
                "资金流独立行不能让股票变成可回测的日线数据。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["daily_bars", "capital_flow"],
                        "description": "补齐类型，默认 daily_bars",
                    },
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "最多 20 个 6 位代码；capital_flow 模式下省略表示全市场后台任务",
                    },
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                },
                "required": ["start_date", "end_date"],
            },
            executor=update_stock_data,
            summarizer=summarize_update,
            read_only=False,
        ),
        AiTool(
            name="sync_job_status",
            description="查询补齐后台任务（update_stock_data 的 capital_flow 全市场模式启动）的进度：已处理/成功/失败/写入行数。",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "update_stock_data 返回的 background_job.job_id"}
                },
                "required": ["job_id"],
            },
            executor=sync_job_status,
            summarizer=summarize_job,
        ),
    ]
