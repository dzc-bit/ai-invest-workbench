"""AI 投研原语工具：全市场筛选 / 单股事件时间轴 / 持仓感知。

三个工具只复用既有读取路径——条件语义来自 conditions.py 的双注册表
（EVALUATORS + MASK_BUILDERS，AGENTS.md §15-5），数据来自本地仓公共接口，
跨源爬取复用 astock_data_tools 的既有函数；不新增写工具、不新增爬虫。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pandas as pd

from astock_backtester.ai.context import retain_rows
from astock_backtester.ai.tools.astock_data_tools import (
    fetch_dragon_tiger_records,
    fetch_limit_up_rows,
    fetch_research_reports,
    fetch_tencent_quotes,
)
from astock_backtester.ai.tools.local_tools import validated_condition_nodes
from astock_backtester.ai.tools.registry import CODE_BAD_ARGUMENTS, CODE_NO_DATA, AiTool
from astock_backtester.backtest_runner import enrich_for_strategy
from astock_backtester.conditions import MASK_BUILDERS
from astock_backtester.data.symbols import normalize_symbol
from astock_backtester.data.text_cleaning import html_to_plaintext
from astock_backtester.models import ConditionGroup, ConditionOperator, StrategyConfig

if TYPE_CHECKING:
    from astock_backtester.ai.memory import MemoryStore
    from astock_backtester.ai.tools.local_tools import AiBackend

SCREEN_MAX_EXPRESSIONS = 6
SCREEN_MAX_ROWS = 500
SCREEN_READ_CALENDAR_DAYS = 120
TIMELINE_MAX_DAYS = 60
TIMELINE_MAX_ZT_POOL_DAYS = 5
TIMELINE_MAX_EVENTS = 80
POSITION_MEMORY_CATEGORIES = ("holding", "watchlist")


def build_research_tools(backend: AiBackend) -> list[AiTool]:
    def screen_stocks(args: dict[str, Any]) -> dict[str, Any]:
        """全市场横向筛选：复用回测同款条件语义，不手写 SQL。"""
        expressions = [str(text) for text in args.get("entry_expressions", [])][:SCREEN_MAX_EXPRESSIONS]
        if not expressions:
            return {"ok": False, "error_code": CODE_BAD_ARGUMENTS, "error": "entry_expressions 不能为空"}
        nodes, failures = validated_condition_nodes(expressions, mode="entry")
        if failures:
            return {
                "ok": False,
                "error_code": CODE_BAD_ARGUMENTS,
                "error": "部分条件无法识别，请按返回的模板示例改写",
                "failures": failures,
            }
        lagged = [node for node in nodes if int(node.data_lag_days or 0) > 0]
        if lagged:
            return {
                "ok": False,
                "error_code": CODE_BAD_ARGUMENTS,
                "error": (
                    "条件带有 data_lag_days（历史取数偏移），横向筛选暂不支持；"
                    "请去掉 data_lag_days 后重试。"
                ),
            }
        end = date.today()
        raw_end = str(args.get("end_date") or "").strip()
        if raw_end:
            try:
                end = pd.Timestamp(raw_end).date()
            except ValueError:
                return {"ok": False, "error_code": CODE_BAD_ARGUMENTS, "error": f"无法解析 end_date：{raw_end}"}
        if end > date.today():
            end = date.today()
        start = end - timedelta(days=SCREEN_READ_CALENDAR_DAYS)
        frame = backend.warehouse.read_daily_bars(
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            require_ohlc=True,
        )
        if frame.empty:
            return {
                "ok": False,
                "error_code": CODE_NO_DATA,
                "error": "本地数据仓没有覆盖所选日期的日线数据，请先在数据中心补齐。",
            }
        strategy = StrategyConfig(
            name="AI 全市场筛选",
            entry_groups=[ConditionGroup(id="ai-screen-group", operator=ConditionOperator.AND, conditions=nodes)],
        )
        enriched = enrich_for_strategy(frame, strategy)
        target = pd.Timestamp(end)
        if not (enriched["trade_date"] == target).any():
            target = enriched["trade_date"].max()
        # 掩码必须在**整个增强帧**上计算、再切目标日：macd_dead_cross 这类
        # 跨行条件依赖 groupby(symbol).shift(1)，单日帧里每只股票只有一行，
        # shift 后全 NaN → mask 恒 False，表现为"永远没有匹配"的假阴性。
        mask = pd.Series(True, index=enriched.index)
        for node in nodes:
            builder = MASK_BUILDERS.get(node.condition_id)
            if builder is None:
                return {
                    "ok": False,
                    "error_code": CODE_BAD_ARGUMENTS,
                    "error": f"条件 {node.condition_id} 没有向量化实现，请改用其它条件",
                }
            mask &= builder(node, enriched)
        day_frame = enriched[enriched["trade_date"] == target]
        matched = day_frame[mask.reindex(day_frame.index).fillna(False)]
        if matched.empty:
            return {
                "ok": True,
                "trade_date": str(target.date()),
                "conditions": [node.expression or node.condition_id for node in nodes],
                "scanned_symbols": int(day_frame["symbol"].nunique()),
                "matched_count": 0,
                "rows": [],
            }
        rows: list[dict[str, Any]] = []
        for _, row in matched.sort_values("change_pct", ascending=False).head(SCREEN_MAX_ROWS).iterrows():
            rows.append(
                {
                    "symbol": str(row["symbol"]),
                    "stock_name": str(row.get("stock_name") or ""),
                    "close": round(float(row["close"]), 3),
                    "change_pct": round(float(row["change_pct"]), 4) if pd.notna(row.get("change_pct")) else None,
                    "turnover_rate": round(float(row["turnover_rate"]), 4)
                    if pd.notna(row.get("turnover_rate"))
                    else None,
                    "main_net_inflow": round(float(row["main_net_inflow"]), 2)
                    if pd.notna(row.get("main_net_inflow"))
                    else None,
                    "float_market_cap": round(float(row["float_market_cap"]), 2)
                    if pd.notna(row.get("float_market_cap"))
                    else None,
                }
            )
        return {
            "ok": True,
            "trade_date": str(target.date()),
            "conditions": [node.expression or node.condition_id for node in nodes],
            "scanned_symbols": int(day_frame["symbol"].nunique()),
            "matched_count": int(len(matched)),
            "rows": rows,
        }

    def summarize_screen(payload: dict[str, Any]) -> str:
        if not payload.get("ok"):
            return f"筛选失败：{payload.get('error')}"
        rows = payload.get("rows") or []
        if not rows:
            return (
                f"{payload.get('trade_date')} 扫描 {payload.get('scanned_symbols')} 只，"
                "没有同时满足全部条件的股票。"
            )
        retained = retain_rows(
            rows,
            columns=["symbol", "stock_name", "close", "change_pct", "turnover_rate", "main_net_inflow", "float_market_cap"],
            max_rows=20,
        )
        payload["shown_rows"] = retained.kept
        payload["more_rows"] = retained.omitted
        payload["resume_offset"] = retained.resume_offset
        conditions = " 且 ".join(payload.get("conditions") or [])
        return (
            f"{payload.get('trade_date')} 扫描 {payload.get('scanned_symbols')} 只，"
            f"满足「{conditions}」的共 {payload.get('matched_count')} 只（按涨幅降序，前 20）：\n{retained.text}"
        )

    def stock_timeline(args: dict[str, Any]) -> dict[str, Any]:
        """单股事件时间轴：本地日线 + 龙虎榜 + 涨停池 + 研报，按日期倒序合并。"""
        symbol = normalize_symbol(str(args.get("symbol", "")))
        if not symbol or not symbol.isdigit():
            return {"ok": False, "error_code": CODE_BAD_ARGUMENTS, "error": f"无法识别股票代码：{args.get('symbol')!r}"}
        days = max(5, min(int(args.get("days", 20)), TIMELINE_MAX_DAYS))
        end = date.today()
        start = end - timedelta(days=days)
        events: list[dict[str, Any]] = []
        diagnostics: list[str] = []
        sources_status: dict[str, str] = {}

        try:
            frame = backend.warehouse.read_daily_bars(
                symbols=[symbol],
                start_date=start.isoformat(),
                end_date=end.isoformat(),
                require_ohlc=True,
            )
        except Exception as exc:  # noqa: BLE001 - 单源失败不拖垮时间轴
            frame = pd.DataFrame()
            sources_status["daily_bars"] = "error"
            diagnostics.append(f"本地日线读取失败：{exc}")
        if frame is not None and not frame.empty:
            sources_status["daily_bars"] = "ok"
            frame = frame.sort_values("trade_date").tail(10)
            for _, row in frame.iterrows():
                change = row.get("change_pct")
                change_text = f"，涨跌 {(float(change) * 100):+.2f}%" if pd.notna(change) else ""
                events.append(
                    {
                        "date": str(row["trade_date"])[:10],
                        "kind": "行情",
                        "source": "本地日线仓",
                        "detail": (
                            f"收盘 {float(row['close']):.2f}{change_text}，"
                            f"成交额 {float(row.get('amount') or 0) / 1e8:.2f} 亿"
                        ),
                    }
                )
        elif frame is not None:
            sources_status["daily_bars"] = "empty"
            diagnostics.append("本地数据仓没有该股票区间的日线数据（可用 update_stock_data 补齐）。")

        try:
            records, _dragon_diagnostics = fetch_dragon_tiger_records(symbol, end.isoformat(), days)
        except Exception as exc:  # noqa: BLE001 - 龙虎榜失败只记单源
            sources_status["dragon_tiger"] = "error"
            diagnostics.append(f"龙虎榜读取失败：{exc}")
        else:
            sources_status["dragon_tiger"] = "ok" if records else "empty"
            for record in records:
                net = record.get("net_buy_wan")
                net_text = f"{net:+,.0f} 万" if net is not None else "未知"
                events.append(
                    {
                        "date": record.get("date"),
                        "kind": "龙虎榜",
                        "source": "东财数据中心",
                        "detail": f"{record.get('reason', '')}，龙虎榜净买 {net_text}",
                    }
                )

        pool_days = max(1, min(int(args.get("limit_up_pool_days", 3)), TIMELINE_MAX_ZT_POOL_DAYS))
        zt_events: list[dict[str, Any]] = []
        for offset in range(pool_days):
            day = end - timedelta(days=offset)
            try:
                pool = fetch_limit_up_rows("zt", day.strftime("%Y%m%d"))
            except Exception as exc:  # noqa: BLE001 - 涨停池失败只记单源
                sources_status.setdefault("limit_up_pool", "error")
                if "涨停池读取失败" not in "".join(diagnostics):
                    diagnostics.append(f"涨停池读取失败：{exc}")
                continue
            for item in pool:
                if str(item.get("symbol")) == symbol:
                    # 事件日期必须是该涨停发生的 day，而不是窗口末日——
                    # 否则 T-1/T-2 的涨停都被标成今天，时间轴失序。
                    zt_events.append(
                        {
                            "date": str(day),
                            "kind": "涨停",
                            "source": "东财涨停池",
                            "detail": (
                                f"{item.get('zt_stat')}，连板 {item.get('limit_days')}，"
                                f"炸板 {item.get('break_times')} 次，首封 {item.get('first_seal')}"
                            ),
                        }
                    )
        sources_status.setdefault("limit_up_pool", "ok")
        events.extend(zt_events)

        try:
            reports = fetch_research_reports(symbol, 5)
        except Exception as exc:  # noqa: BLE001 - 研报失败只记单源
            reports = []
            sources_status["research_reports"] = "error"
            diagnostics.append(f"研报读取失败：{exc}")
        else:
            sources_status["research_reports"] = "ok" if reports else "empty"
            for report in reports[:5]:
                events.append(
                    {
                        "date": report.get("date"),
                        "kind": "研报",
                        "source": f"{report.get('org', '')}（东财）",
                        "detail": f"【{report.get('rating', '')}】{html_to_plaintext(str(report.get('title', '')))}",
                    }
                )

        if not events:
            return {
                "ok": False,
                "error_code": CODE_NO_DATA,
                "error": f"所有来源都没有 {symbol} 在 {start}~{end} 窗口内的事件。",
                "diagnostics": diagnostics,
            }
        events.sort(key=lambda item: str(item.get("date")), reverse=True)
        name = ""
        try:
            quotes, _ = fetch_tencent_quotes([symbol])
            if quotes:
                name = str(quotes[0].get("name") or "")
        except Exception:  # noqa: BLE001 - 名称取不到不影响时间轴
            name = ""
        return {
            "ok": True,
            "symbol": symbol,
            "name": name,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "sources_status": sources_status,
            "events": events[:TIMELINE_MAX_EVENTS],
            "diagnostics": diagnostics,
        }

    def summarize_timeline(payload: dict[str, Any]) -> str:
        if not payload.get("ok"):
            return f"时间轴构建失败：{payload.get('error')}"
        events = payload.get("events") or []
        retained = retain_rows(
            [
                {"date": item.get("date"), "kind": item.get("kind"), "detail": str(item.get("detail"))[:60]}
                for item in events
            ],
            columns=["date", "kind", "detail"],
            max_rows=20,
            cell_chars=64,
        )
        payload["shown_rows"] = retained.kept
        payload["more_rows"] = retained.omitted
        payload["resume_offset"] = retained.resume_offset
        status = payload.get("sources_status") or {}
        failed = [f"{key}:{value}" for key, value in status.items() if value not in ("ok", "empty")]
        failed_text = f"；失败源：{'、'.join(failed)}" if failed else ""
        return (
            f"{payload.get('symbol')} {payload.get('name')} 最近事件（按日期倒序，共 {len(events)} 条）：\n"
            f"{retained.text}{failed_text}"
        )

    return [
        AiTool(
            name="screen_stocks",
            description=(
                "全市场横向筛选：返回某交易日同时满足全部入场条件的股票名单（复用回测同款条件语义，"
                "条件 DSL 先经 validate_strategy_conditions 校验）。适合\"今天有哪些票同时满足 A/B/C\""
                "这类跨市场问题，不要用手写 SQL 逐条件拼。数据缺列的条件按回测同款降级语义视为通过；"
                "整表读入内存，本工具串行执行。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entry_expressions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "条件 DSL 列表（与 run_strategy_backtest 的入场条件同一写法，2-6 条）",
                    },
                    "end_date": {"type": "string", "description": "筛选交易日 YYYY-MM-DD，默认最新数据日"},
                },
                "required": ["entry_expressions"],
            },
            executor=screen_stocks,
            summarizer=summarize_screen,
            digest_chars=3_000,
        ),
        AiTool(
            name="stock_timeline",
            description=(
                "单股事件时间轴：合并本地日线行情、龙虎榜上榜、涨停池与机构研报，按日期倒序一次返回"
                "“这只票最近发生了什么”，替代连调 4 个工具自己拼。跨源失败逐源标注，不会因一源失败整体失败。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位股票代码"},
                    "days": {"type": "integer", "description": "回看自然日窗口，默认 20，最大 60"},
                    "limit_up_pool_days": {"type": "integer", "description": "涨停池回看交易日数，默认 3，最大 5"},
                },
                "required": ["symbol"],
            },
            executor=stock_timeline,
            summarizer=summarize_timeline,
            digest_chars=2_600,
            untrusted_body=True,
        ),
    ]


def build_memory_tools(memory: MemoryStore) -> list[AiTool]:
    def my_positions(_: dict[str, Any]) -> dict[str, Any]:
        """持仓/自选感知：只读长期记忆，不写、不当行情事实。"""
        try:
            records = memory.load()
        except Exception as exc:  # noqa: BLE001 - 记忆读取失败按无数据返回
            return {"ok": False, "error_code": CODE_NO_DATA, "error": f"长期记忆读取失败：{exc}"}
        rows = [
            {
                "id": record.id,
                "category": record.category,
                "content": record.content[:80],
                "weight": record.weight,
                "updated_at": str(record.updated_at)[:10],
            }
            for record in records
            if record.category in POSITION_MEMORY_CATEGORIES
        ]
        if not rows:
            return {
                "ok": False,
                "error_code": CODE_NO_DATA,
                "error": "长期记忆里还没有持仓/自选记录（用户聊到持仓并确认后会自动沉淀）。",
            }
        rows.sort(key=lambda item: item["updated_at"], reverse=True)
        return {
            "ok": True,
            "rows": rows,
            "note": "记忆是用户自述，不是行情事实；涉及价格/仓位数字一律用行情工具确认。",
        }

    def summarize_positions(payload: dict[str, Any]) -> str:
        if not payload.get("ok"):
            return f"持仓记忆读取失败：{payload.get('error')}"
        rows = payload.get("rows") or []
        retained = retain_rows(rows, columns=["category", "content", "updated_at"], max_rows=10)
        payload["shown_rows"] = retained.kept
        payload["more_rows"] = retained.omitted
        payload["resume_offset"] = retained.resume_offset
        return f"记忆中的持仓/自选（共 {len(rows)} 条，均为用户自述、非行情事实）：\n{retained.text}"

    return [
        AiTool(
            name="my_positions",
            description=(
                "读取长期记忆中的用户持仓与自选股记录（含备注与更新时间），回答\"我的持仓该不该走\""
                "类问题前先调用。记忆是用户自述而非行情事实；本工具只读，不写入。"
            ),
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
            executor=my_positions,
            summarizer=summarize_positions,
        ),
    ]
