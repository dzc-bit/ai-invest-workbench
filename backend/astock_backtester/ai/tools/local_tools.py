"""Read-only tools wrapping the existing local service capabilities.

Executors only touch public provider/warehouse APIs plus the backtest runner;
nothing here mutates the warehouse, and the backtest tool enforces explicit
resource limits (pool size, date range, capital bounds) before running.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Protocol

import pandas as pd
from pydantic import ValidationError

from astock_backtester.ai.context import retain_rows
from astock_backtester.ai.tools.registry import AiTool
from astock_backtester.backtest_runner import run_configured_backtest
from astock_backtester.condition_parser import validate_condition_text, validate_exit_condition_text
from astock_backtester.data.symbols import normalize_symbol
from astock_backtester.indicators import add_moving_average
from astock_backtester.models import (
    BacktestSettings,
    ConditionGroup,
    ConditionNode,
    ConditionOperator,
    StrategyConfig,
)

BACKTEST_WARMUP_CALENDAR_DAYS = 120
MAX_BACKTEST_SYMBOLS = 50
MAX_BACKTEST_RANGE_DAYS = 366
MAX_CONDITION_EXPRESSIONS = 6


class AiBackend(Protocol):
    """Narrow view of DataServiceState the tools are allowed to touch.

    只允许公共接口（AGENTS.md §15-3）：coverage 快照与后台同步任务都走
    DataServiceState/SyncJobManager 的公开方法，不触碰私有属性。
    """

    cache: Any
    warehouse: Any
    provider: Any
    realtime_provider: Any
    news_provider: Any
    briefing_provider: Any
    risk_provider: Any
    capital_flow_crawler: Any
    sync_manager: Any

    def log(self, level: str, message: str) -> None: ...

    def coverage_snapshot(self) -> list[Any]: ...

    def start_coverage_refresh(self, *, force: bool = False) -> Any | None: ...


def _validated_nodes(expressions: list[str], *, mode: str) -> tuple[list[ConditionNode], list[dict[str, Any]]]:
    nodes: list[ConditionNode] = []
    failures: list[dict[str, Any]] = []
    for index, text in enumerate(expressions):
        result = validate_exit_condition_text(text) if mode == "exit" else validate_condition_text(text)
        if result.ok and result.condition is not None:
            source = result.condition
            nodes.append(
                ConditionNode(
                    id=f"ai-{mode}-{index}",
                    condition_id=source.condition_id,
                    params=source.params,
                    data_lag_days=source.data_lag_days,
                    expression=source.expression or text,
                )
            )
        else:
            failures.append(
                {
                    "expression": text,
                    "errors": [error.message for error in result.errors],
                    "examples": result.examples[:4],
                }
            )
    return nodes, failures


def build_local_tools(backend: AiBackend) -> list[AiTool]:
    def realtime_snapshot(_: dict[str, Any]) -> dict[str, Any]:
        snapshot = backend.realtime_provider.market_snapshot()
        return {"ok": True, "snapshot": snapshot.model_dump(mode="json")}

    def summarize_realtime(payload: dict[str, Any]) -> str:
        snapshot = payload.get("snapshot", {})
        parts = [f"状态 {snapshot.get('status')} / 来源 {snapshot.get('source')}"]
        for index in snapshot.get("indexes", [])[:4]:
            change = index.get("change_pct")
            change_text = f" {change:+.2f}%" if isinstance(change, (int, float)) else ""
            parts.append(f"{index.get('name')} {index.get('last')}{change_text}")
        breadth = snapshot.get("breadth")
        if breadth:
            parts.append(f"红盘 {breadth.get('up')} / 全市场 {breadth.get('total')}")
        sectors = snapshot.get("strong_sectors", [])[:3]
        if sectors:
            names = "、".join(f"{s.get('name')} {s.get('change_pct'):+.2f}%" for s in sectors)
            parts.append(f"强势板块：{names}")
        return "；".join(parts)

    def market_news(args: dict[str, Any]) -> dict[str, Any]:
        limit = max(1, min(int(args.get("limit", 10)), 30))
        response = backend.news_provider.latest_news()
        items = response.items[:limit]
        return {"ok": True, "items": [item.model_dump(mode="json") for item in items], "total": len(response.items)}

    def summarize_news(payload: dict[str, Any]) -> str:
        lines = []
        for item in payload.get("items", [])[:10]:
            published = str(item.get("published_at") or "")[11:16]
            prefix = f"{published} " if published else ""
            lines.append(f"- {prefix}{item.get('title')}（{item.get('source')}）")
        return f"共 {payload.get('total')} 条，最新 {len(payload.get('items', []))} 条：\n" + "\n".join(lines)

    def market_briefing(args: dict[str, Any]) -> dict[str, Any]:
        kind = str(args.get("kind", "fupan")).strip().lower()
        if kind not in ("fupan", "zaopan"):
            return {"ok": False, "error": "kind 只能是 fupan 或 zaopan"}
        briefing = backend.briefing_provider.latest_fupan() if kind == "fupan" else backend.briefing_provider.latest_zaopan()
        return {"ok": True, "briefing": briefing.model_dump(mode="json")}

    def summarize_briefing(payload: dict[str, Any]) -> str:
        briefing = payload.get("briefing", {})
        sections = [section.get("title") for section in briefing.get("sections", [])[:4] if section.get("title")]
        body = f"来源 {briefing.get('source')}：{briefing.get('summary', '')}"
        if sections:
            body += "；章节：" + "、".join(sections)
        return body

    def risk_alerts(_: dict[str, Any]) -> dict[str, Any]:
        response = backend.risk_provider.current_alerts()
        return {"ok": True, "items": [item.model_dump(mode="json") for item in response.items]}

    def summarize_risk(payload: dict[str, Any]) -> str:
        items = payload.get("items", [])
        by_severity: dict[str, int] = {"high": 0, "medium": 0, "low": 0}
        for item in items:
            severity = str(item.get("severity", "low"))
            by_severity[severity] = by_severity.get(severity, 0) + 1
        heads = "；".join(f"{item.get('symbol')} {item.get('name')}（{item.get('risk_type')}）" for item in items[:3])
        label = f"共 {len(items)} 项（高 {by_severity['high']} / 中 {by_severity['medium']} / 低 {by_severity['low']}）"
        return f"{label}。示例：{heads or '无'}"

    def data_health_report(_: dict[str, Any]) -> dict[str, Any]:
        # 损坏 ≠ 缺失：分区损坏会让画像读取抛异常，此时明确让模型“先修损坏”，
        # 而不是收到一条裸 tool_error 后去补数据。
        try:
            profile = backend.warehouse.data_gap_profile()
        except Exception as exc:  # noqa: BLE001
            # 注意：Warehouse.corrupt_partitions 是 @property（返回 dict），不是方法。
            try:
                corrupt = sorted(backend.warehouse.corrupt_partitions)
            except Exception:  # noqa: BLE001
                corrupt = []
            payload: dict[str, Any] = {
                "ok": False,
                "error_code": "warehouse_corrupt" if corrupt else "tool_error",
                "error": f"缺口画像读取失败：{exc}",
            }
            if corrupt:
                payload["corrupt_partitions"] = corrupt
                payload["hint"] = "存在损坏分区：请先在数据中心处理损坏分区，损坏会被伪装成“缺失数据”，补齐无法修复。"
            return payload
        if not profile.get("available"):
            return {"ok": False, "error": str(profile.get("reason", "数据仓缺口画像不可用"))}
        result: dict[str, Any] = {"ok": True, "profile": profile}
        # 停更分布行集化：摘要只给 top，逐条明细进 rows 供 read_tool_result 续读。
        rows: list[dict[str, Any]] = []
        for dataset, label in (("daily_bars", "日线"), ("market_cap", "市值"), ("capital_flow", "资金流")):
            section = profile.get(dataset, {})
            if section.get("delisted_symbols"):
                rows.append({"dataset": label, "note": f"{section['delisted_symbols']} 只已退市（终态，无需补齐）"})
            for entry in section.get("stale_distribution", []):
                rows.append({"dataset": label, "last_date": entry.get("last_date"), "symbols": entry.get("symbols")})
        if rows:
            result["rows"] = rows
        # coverage 汇总：模型回答“还缺多少行”不必再自己 SQL 数——SQL 行数统计
        # 只得内部缺口，不含日历/生命周期/停更尾部，会和覆盖卡打架。
        try:
            coverage = backend.coverage_snapshot()
        except Exception:  # noqa: BLE001
            coverage = []
        if any(item.symbols > 0 for item in coverage):
            result["coverage"] = [
                {
                    "dataset": item.dataset,
                    "symbols": item.symbols,
                    "missing_rows": item.missing_rows,
                    "end_date": str(item.end_date or ""),
                }
                for item in coverage
            ]
        return result

    def summarize_data_health(payload: dict[str, Any]) -> str:
        profile = payload.get("profile", {})
        window = profile.get("window", {})
        daily = profile.get("daily_bars", {})
        lines = [
            f"数据窗口 {window.get('start_date')}~{window.get('end_date')}"
            f"（画像只覆盖最近 {len(window.get('partitions', []))} 个年分区，更早停更的股票不在分布里）："
            f"日线 {daily.get('symbols')} 只，其中 {daily.get('symbols_current')} 只更新到最新，"
            f"{daily.get('symbols_stale')} 只已停更。"
        ]
        if daily.get("delisted_symbols"):
            lines.append(f"另有 {daily['delisted_symbols']} 只已退市（终态，不算缺口，不要建议补齐）。")
        stale = daily.get("stale_distribution", [])[:5]
        if stale:
            parts = "；".join(f"{entry['symbols']} 只停在 {entry['last_date']}" for entry in stale)
            lines.append(f"停更分布（top）：{parts}。")
        thin = daily.get("thin_days", [])[:5]
        if thin:
            parts = "、".join(f"{entry['trade_date']}（仅 {entry['rows']} 行）" for entry in thin)
            lines.append(f"疑似写入失败日：{parts}。")
        for key, label in (("market_cap", "市值"), ("capital_flow", "资金流")):
            section = profile.get(key, {})
            entries = (section.get("stale_distribution") or [])[:3]
            if entries:
                parts = "；".join(f"{entry['symbols']} 只停在 {entry['last_date']}" for entry in entries)
                lines.append(f"{label}停更：{parts}。")
                if section.get("delisted_symbols"):
                    lines.append(f"（{label}另有 {section['delisted_symbols']} 只退市股已从停更统计剔除。）")
        coverage = payload.get("coverage") or []
        if coverage:
            parts = "；".join(
                f"{item['dataset']} {item['symbols']} 只缺 {item['missing_rows']} 行" for item in coverage
            )
            lines.append(f"覆盖缺口汇总（累计真实缺口口径，含日历/生命周期/停更尾部）：{parts}。")
            lines.append("注意：直接用 SQL 数 NULL 行只能得到内部缺口，不等于覆盖缺口口径。长期停牌的股票会计为缺口且无法补齐。")
        return "\n".join(lines)

    def recent_daily_bars(args: dict[str, Any]) -> dict[str, Any]:
        symbol = normalize_symbol(str(args.get("symbol", "")))
        if not symbol or not symbol.isdigit():
            return {"ok": False, "error": f"无法识别股票代码：{args.get('symbol')!r}"}
        days = max(5, min(int(args.get("days", 30)), 120))
        end = date.today()
        start = end - timedelta(days=days * 3)
        frame = backend.warehouse.read_daily_bars(
            symbols=[symbol], start_date=start.isoformat(), end_date=end.isoformat(), require_ohlc=True
        )
        if frame.empty:
            return {
                "ok": False,
                "error": f"本地数据仓没有 {symbol} 的日线数据，请先在数据中心补齐该股票。",
            }
        frame = add_moving_average(frame, [5, 10, 20]).tail(days)
        rows = []
        for _, row in frame.iterrows():
            rows.append(
                {
                    "trade_date": str(row.get("trade_date"))[:10],
                    "open": round(float(row["open"]), 3),
                    "high": round(float(row["high"]), 3),
                    "low": round(float(row["low"]), 3),
                    "close": round(float(row["close"]), 3),
                    "volume": float(row.get("volume", 0)),
                    "change_pct": round(float(row["change_pct"]), 4) if pd.notna(row.get("change_pct")) else None,
                    "turnover_rate": round(float(row["turnover_rate"]), 4) if pd.notna(row.get("turnover_rate")) else None,
                    "main_net_inflow": round(float(row["main_net_inflow"]), 2) if pd.notna(row.get("main_net_inflow")) else None,
                    "ma5": round(float(row["ma_5"]), 3) if pd.notna(row.get("ma_5")) else None,
                    "ma10": round(float(row["ma_10"]), 3) if pd.notna(row.get("ma_10")) else None,
                    "ma20": round(float(row["ma_20"]), 3) if pd.notna(row.get("ma_20")) else None,
                }
            )
        name = str(frame.iloc[0].get("stock_name") or symbol)
        return {"ok": True, "symbol": symbol, "name": name, "rows": rows}

    def summarize_bars(payload: dict[str, Any]) -> str:
        rows = payload.get("rows", [])
        if not rows:
            return "无数据"
        last = rows[-1]
        first_close = rows[0]["close"] or 0
        last_close = last["close"] or 0
        range_pct = (last_close - first_close) / first_close * 100 if first_close else 0
        # 摘要行之外还要给真实逐日数据：只报首末两点的话，"近 5 日走势""哪天放量"
        # 这类问题模型根本无从回答（原来是把最多 120 天塌成一行）。
        retained = retain_rows(
            rows,
            columns=["trade_date", "close", "change_pct", "volume", "turnover_rate", "main_net_inflow", "ma5", "ma10", "ma20"],
            max_rows=16,
            keep="tail",
        )
        payload["shown_rows"] = retained.kept
        payload["more_rows"] = retained.omitted
        payload["resume_offset"] = retained.resume_offset
        return (
            f"{payload.get('symbol')} {payload.get('name')} 最近 {len(rows)} 个交易日："
            f"最新收盘 {last_close}，MA5 {last.get('ma5')} / MA10 {last.get('ma10')} / MA20 {last.get('ma20')}，"
            f"区间 {range_pct:+.2f}%\n{retained.text}"
        )

    def validate_strategy_conditions(args: dict[str, Any]) -> dict[str, Any]:
        entry = [str(text) for text in args.get("entry_expressions", [])][:MAX_CONDITION_EXPRESSIONS]
        exit_rules = [str(text) for text in args.get("exit_expressions", [])][:MAX_CONDITION_EXPRESSIONS]
        if not entry:
            return {"ok": False, "error": "至少需要一条入场条件"}
        entry_nodes, entry_failures = _validated_nodes(entry, mode="entry")
        exit_nodes, exit_failures = _validated_nodes(exit_rules, mode="exit")
        return {
            "ok": not entry_failures and not exit_failures,
            "entry_valid": [node.expression for node in entry_nodes],
            "exit_valid": [node.expression for node in exit_nodes],
            "failures": entry_failures + exit_failures,
        }

    def summarize_validation(payload: dict[str, Any]) -> str:
        if payload.get("ok"):
            return "全部条件校验通过：" + "；".join(payload.get("entry_valid", []))
        failures = payload.get("failures", [])
        first = failures[0] if failures else {}
        return f"{len(failures)} 条条件未通过，示例：{first.get('expression')} → {first.get('errors', [])}"

    def run_strategy_backtest(args: dict[str, Any]) -> dict[str, Any]:
        entry = [str(text) for text in args.get("entry_expressions", [])][:MAX_CONDITION_EXPRESSIONS]
        exit_rules = [str(text) for text in args.get("exit_expressions", [])][:MAX_CONDITION_EXPRESSIONS]
        if not entry:
            return {"ok": False, "error": "至少需要一条入场条件"}
        entry_nodes, entry_failures = _validated_nodes(entry, mode="entry")
        exit_nodes, exit_failures = _validated_nodes(exit_rules, mode="exit")
        if entry_failures or exit_failures:
            return {
                "ok": False,
                "error": "部分条件无法识别，请按模板改写后重试",
                "failures": entry_failures + exit_failures,
            }
        custom_symbols = [normalize_symbol(str(s)) for s in args.get("custom_symbols", []) if str(s).strip()]
        stock_pool = str(args.get("stock_pool", "all"))
        if stock_pool == "custom":
            if not custom_symbols:
                return {"ok": False, "error": "自定义股票池为空"}
            if len(custom_symbols) > MAX_BACKTEST_SYMBOLS:
                return {"ok": False, "error": f"自定义股票池最多 {MAX_BACKTEST_SYMBOLS} 只"}
        try:
            settings = BacktestSettings(
                start_date=str(args.get("start_date", "")),
                end_date=str(args.get("end_date", "")),
                initial_cash=float(args.get("initial_cash", 1_000_000)),
                stock_pool=stock_pool,  # type: ignore[arg-type]
                custom_symbols=custom_symbols,
                max_positions=max(1, min(int(args.get("max_positions", 10)), 30)),
                max_daily_buys=max(1, min(int(args.get("max_daily_buys", 3)), 10)),
                position_size_pct=float(args.get("position_size_pct", 0.2)),
                fixed_holding_days=max(1, min(int(args.get("fixed_holding_days", 5)), 60)),
                take_profit_pct=args.get("take_profit_pct"),
                stop_loss_pct=args.get("stop_loss_pct"),
            )
        except ValidationError as exc:
            return {"ok": False, "error": f"回测参数不合法：{exc.errors()[0].get('msg', exc)}"}
        if (settings.end_date - settings.start_date).days > MAX_BACKTEST_RANGE_DAYS:
            return {"ok": False, "error": f"回测区间最长 {MAX_BACKTEST_RANGE_DAYS} 天，请缩小日期范围"}
        if settings.initial_cash < 10_000 or settings.initial_cash > 100_000_000:
            return {"ok": False, "error": "初始资金必须在 1 万到 1 亿之间"}

        warmup_start = settings.start_date - timedelta(days=BACKTEST_WARMUP_CALENDAR_DAYS)
        symbols = settings.custom_symbols if settings.stock_pool == "custom" else None
        frame = backend.warehouse.read_daily_bars(
            symbols=symbols,
            start_date=warmup_start.isoformat(),
            end_date=settings.end_date.isoformat(),
            require_ohlc=True,
        )
        if frame.empty:
            return {
                "ok": False,
                "error": "本地数据仓没有覆盖所选日期与股票池的日线数据，请先在数据中心补齐。",
            }
        strategy = StrategyConfig(
            name="AI 生成策略",
            entry_groups=[
                ConditionGroup(id="ai-entry-group", operator=ConditionOperator.AND, conditions=entry_nodes)
            ],
            exit_rules=exit_nodes,
        )
        result = run_configured_backtest(frame, strategy, settings)
        curve = result.equity_curve
        step = max(1, len(curve) // 40)
        matches = result.latest_strategy_matches.matches[:5] if result.latest_strategy_matches else []
        return {
            "ok": True,
            "data_rows": int(len(frame)),
            "metrics": result.metrics.model_dump(mode="json"),
            "preflight_issues": [issue.model_dump(mode="json") for issue in result.preflight_issues],
            "equity_curve_downsampled": [point.model_dump(mode="json") for point in curve[::step]],
            "recent_trades": [trade.model_dump(mode="json") for trade in result.trades[-5:]],
            "latest_matches": [match.model_dump(mode="json") for match in matches],
            "strategy": strategy.model_dump(mode="json"),
            "settings": settings.model_dump(mode="json"),
        }

    def summarize_backtest(payload: dict[str, Any]) -> str:
        metrics = payload.get("metrics", {})
        return (
            f"回测完成（{payload.get('data_rows')} 行数据）：总收益 {metrics.get('total_return_pct', 0):+.2%}，"
            f"最大回撤 {metrics.get('max_drawdown_pct', 0):.2%}，胜率 {metrics.get('win_rate_pct', 0):.1f}%，"
            f"{metrics.get('trade_count', 0)} 笔交易；候选 {len(payload.get('latest_matches', []))} 只。"
            "完整策略 JSON 在结果数据中，可直接应用到策略工作台。"
        )

    return [
        AiTool(
            name="realtime_market_snapshot",
            description="获取实时行情快照：指数、红绿家数（市场宽度）、强势板块。回答任何'现在行情如何'类问题前必须调用。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            executor=realtime_snapshot,
            summarizer=summarize_realtime,
        ),
        AiTool(
            name="market_news",
            description="获取最新市场新闻/电报列表，返回标题、来源与时间。",
            parameters={
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "返回条数，默认 10，最大 30"}},
            },
            executor=market_news,
            summarizer=summarize_news,
        ),
        AiTool(
            name="market_briefing",
            description="获取同花顺复盘（fupan）或早盘（zaopan）总评正文摘要。",
            parameters={
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": ["fupan", "zaopan"]}},
                "required": ["kind"],
            },
            executor=market_briefing,
            summarizer=summarize_briefing,
        ),
        AiTool(
            name="risk_alerts",
            description="获取全市场 ST/退市等风险提示清单（本地规则计算）。",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            executor=risk_alerts,
            summarizer=summarize_risk,
        ),
        AiTool(
            name="data_health_report",
            description=(
                "检查本地数据仓具体缺哪些数据：各数据集停更股票分布（多少只停在哪个日期）、"
                "疑似写入失败日、市值/资金流尾部缺口、覆盖缺口汇总（累计真实缺口口径）与退市股计数。"
                "停更分布的逐条明细在结果的 rows 里，可用 read_tool_result 续读。"
                "回答“数据为什么缺/哪些股票没更新/能不能回测某个区间/数据健康”类问题前先调用本工具。"
            ),
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            executor=data_health_report,
            summarizer=summarize_data_health,
            digest_chars=2_600,
        ),
        AiTool(
            name="recent_daily_bars",
            description="读取本地数据仓中单只股票最近 N 个交易日的日线（含 MA5/10/20），用于个股技术面分析。",
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位股票代码，如 600519"},
                    "days": {"type": "integer", "description": "交易日数量，默认 30，最大 120"},
                },
                "required": ["symbol"],
            },
            executor=recent_daily_bars,
            summarizer=summarize_bars,
            digest_chars=2_600,
        ),
        AiTool(
            name="validate_strategy_conditions",
            description="校验自然语言改写后的条件 DSL 是否可执行。生成策略时必须先调用本工具，失败时按返回的模板示例改写重试。",
            parameters={
                "type": "object",
                "properties": {
                    "entry_expressions": {"type": "array", "items": {"type": "string"}, "description": "入场条件 DSL 列表"},
                    "exit_expressions": {"type": "array", "items": {"type": "string"}, "description": "离场条件 DSL 列表"},
                },
                "required": ["entry_expressions"],
            },
            executor=validate_strategy_conditions,
            summarizer=summarize_validation,
        ),
        AiTool(
            name="run_strategy_backtest",
            description="用已通过校验的条件 DSL 组装策略并运行本地历史回测，返回收益指标、交易明细与可应用的策略 JSON。",
            parameters={
                "type": "object",
                "properties": {
                    "entry_expressions": {"type": "array", "items": {"type": "string"}},
                    "exit_expressions": {"type": "array", "items": {"type": "string"}},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "stock_pool": {"type": "string", "enum": ["all", "main_board", "gem", "star", "beijing", "custom"]},
                    "custom_symbols": {"type": "array", "items": {"type": "string"}},
                    "initial_cash": {"type": "number"},
                    "max_positions": {"type": "integer"},
                    "max_daily_buys": {"type": "integer"},
                    "position_size_pct": {"type": "number", "description": "0-1 之间的小数"},
                    "fixed_holding_days": {"type": "integer"},
                    "take_profit_pct": {"type": "number", "description": "正小数，如 0.15"},
                    "stop_loss_pct": {"type": "number", "description": "负小数，如 -0.08"},
                },
                "required": ["entry_expressions", "start_date", "end_date"],
            },
            executor=run_strategy_backtest,
            summarizer=summarize_backtest,
            read_only=False,
        ),
    ]


def local_tool_digest(result_payload: dict[str, Any], *, max_chars: int = 4000) -> str:
    """Dense JSON preview used when a tool payload must be shown verbatim."""
    try:
        return json.dumps(result_payload, ensure_ascii=False, default=str)[:max_chars]
    except (TypeError, ValueError):
        return str(result_payload)[:max_chars]
