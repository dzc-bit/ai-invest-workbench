from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import UTC, datetime

from astock_backtester.data.realtime import is_valid_full_market_breadth
from astock_backtester.data.text_cleaning import is_noisy_market_line
from astock_backtester.models import (
    MarketBriefingResponse,
    MarketCommentaryPoint,
    MarketCommentaryResponse,
    MarketNewsResponse,
    RealtimeMarketSnapshot,
)


def _format_pct(value: float | None) -> str:
    if value is None:
        return "--"
    sign = "+" if value > 0 else ""
    return f"{sign}{value * 100:.2f}%"


def _today_from_snapshot(snapshot: RealtimeMarketSnapshot | None) -> datetime:
    return snapshot.updated_at if snapshot is not None else datetime.now(UTC)


def _news_theme_text(news: MarketNewsResponse | None) -> str | None:
    if news is None or not news.items:
        return None
    tags: dict[str, int] = {}
    for item in news.items[:12]:
        for tag in item.tags:
            tags[tag] = tags.get(tag, 0) + 1
    if tags:
        top_tags = sorted(tags.items(), key=lambda pair: pair[1], reverse=True)[:3]
        return "、".join(tag for tag, _ in top_tags)
    return news.items[0].title


def _briefing_basis_label(source: str) -> tuple[str, str, str]:
    if "local-brief" in source:
        return (
            "本地简短复盘",
            "本地简短复盘只提供防守口径，不含同花顺正文和实时红绿家数校验，盘中使用前需要重新刷新实时行情。",
            "已用本地简短复盘生成行情评价。",
        )
    if "market-fallback" in source:
        return (
            "公开行情复盘兜底",
            "该评价来自公开行情复盘兜底，不含同花顺正文和当前实时红绿家数校验，盘中使用前需要重新刷新实时行情。",
            "已用公开行情复盘兜底生成行情评价。",
        )
    return (
        "同花顺复盘",
        "该评价来自同花顺复盘公开页面，不含当前实时红绿家数校验，盘中使用前需要重新刷新实时行情。",
        "实时盘面读取失败，已用同花顺复盘总评生成行情评价。",
    )


def build_local_brief_commentary(
    now: datetime | None = None,
    diagnostics: list[str] | None = None,
    snapshot: RealtimeMarketSnapshot | None = None,
    news: MarketNewsResponse | None = None,
) -> MarketCommentaryResponse:
    timestamp = now or datetime.now(UTC)
    trade_date = _today_from_snapshot(snapshot).date() if snapshot is not None else timestamp.date()
    news_theme = _news_theme_text(news)
    theme_text = f" 新闻线索集中在 {news_theme}，只作为待验证方向。" if news_theme else ""
    summary = (
        "后端简短判断：实时盘面和同花顺复盘暂不可用，当前只给防守口径。"
        "不要把新闻或局部数据包装成确定结论，先等待红绿家数、指数和强势板块恢复后再确认。"
        f"{theme_text}"
    )
    details = "实时盘面、完整红绿家数和复盘正文均未形成可用依据，当前结论限制为风险控制。"
    if news_theme:
        details = f"{details} 新闻线索：{news_theme}。"
    fallback_diagnostics = list(diagnostics or [])
    fallback_diagnostics.append("后端已生成简短防守判断，避免前端 fallback 或局部数据被包装成确定行情结论。")
    return MarketCommentaryResponse(
        updated_at=timestamp,
        trade_date=trade_date,
        source="local-brief-commentary",
        mode="local_brief_review",
        stance="defensive",
        summary=summary,
        drivers=[
            MarketCommentaryPoint(
                title="后端防守判断",
                detail=details,
                weight="low",
            )
        ],
        risks=[
            "不能把新闻或局部数据包装成确定结论；缺少完整红绿家数时，盘面强弱只能等待实时接口或复盘正文恢复后复核。",
            "若继续刷新仍失败，先保留最近成功内容，避免把空态当作行情变化。",
        ],
        next_watch=[
            "优先恢复 /realtime/market-snapshot 的完整红绿家数与强势板块，再生成盘中评价。",
            "同花顺复盘恢复后，用复盘正文交叉验证新闻线索和题材榜。",
        ],
        diagnostics=fallback_diagnostics,
    )


def _snapshot_missing_reasons(snapshot: RealtimeMarketSnapshot) -> list[str]:
    reasons: list[str] = []
    can_review_stale = snapshot.status == "stale" and snapshot.market_phase in {"post_close", "non_trading", "lunch_break"}
    if snapshot.status != "live" and not can_review_stale:
        reasons.append(f"快照状态为 {snapshot.status}")
    if not snapshot.indexes:
        reasons.append("缺少指数")
    if snapshot.breadth is None or snapshot.breadth.total <= 0:
        reasons.append("缺少红绿家数")
    elif not is_valid_full_market_breadth(snapshot.breadth):
        reasons.append(f"红绿家数不完整({snapshot.breadth.source} total={snapshot.breadth.total})")
    if not snapshot.strong_sectors:
        reasons.append("缺少强势题材")
    elif any(str(sector.source).startswith("local-") for sector in snapshot.strong_sectors):
        reasons.append("strong sectors are local fallback, not live provider")
    return reasons


@dataclass
class MarketCommentaryProvider:
    realtime_provider: object
    news_provider: object | None = None
    briefing_provider: object | None = None
    snapshot_timeout: float | None = 30.0

    def current_commentary(self) -> MarketCommentaryResponse:
        now = datetime.now(UTC)
        diagnostics: list[str] = []
        snapshot: RealtimeMarketSnapshot | None = None
        news: MarketNewsResponse | None = None
        snapshot = self._read_snapshot(diagnostics)
        if self.news_provider is not None:
            try:
                news = self.news_provider.latest_news(limit=12)
            except Exception as exc:
                diagnostics.append(f"行情评价读取新闻失败：{exc}")

        if snapshot is None:
            briefing_commentary = self._briefing_fallback(now, news, diagnostics)
            if briefing_commentary is not None:
                return briefing_commentary
            return build_local_brief_commentary(now, diagnostics, news=news)

        diagnostics.extend(snapshot.diagnostics)

        if snapshot.status == "unavailable":
            if snapshot is not None and snapshot.message:
                diagnostics.append(f"行情评价实时快照不可用：{snapshot.message}")

        missing_reasons = _snapshot_missing_reasons(snapshot)
        if missing_reasons:
            diagnostics.append(f"实时盘面不完整：{'、'.join(missing_reasons)}，未生成确定盘面评价。")
            briefing_commentary = self._briefing_fallback(now, news, diagnostics, snapshot=snapshot)
            if briefing_commentary is not None:
                return briefing_commentary
            return build_local_brief_commentary(now, diagnostics, snapshot=snapshot, news=news)

        return self._commentary_from_snapshot(now, snapshot, news, diagnostics)

    def _read_snapshot(self, diagnostics: list[str]) -> RealtimeMarketSnapshot | None:
        if self.snapshot_timeout is None:
            try:
                return self.realtime_provider.market_snapshot()
            except Exception as exc:
                diagnostics.append(f"行情评价读取实时快照失败：{exc}")
                return None

        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self.realtime_provider.market_snapshot)
        try:
            return future.result(timeout=self.snapshot_timeout)
        except FutureTimeoutError as exc:
            if future.done():
                diagnostics.append(f"行情评价读取实时快照失败：{exc}")
                return None
            future.cancel()
            diagnostics.append(f"行情评价读取实时快照超时：{self.snapshot_timeout:g}秒")
            return None
        except Exception as exc:
            diagnostics.append(f"行情评价读取实时快照失败：{exc}")
            return None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _commentary_from_snapshot(
        self,
        now: datetime,
        snapshot: RealtimeMarketSnapshot,
        news: MarketNewsResponse | None,
        diagnostics: list[str],
    ) -> MarketCommentaryResponse:
        breadth = snapshot.breadth
        up_ratio = breadth.up / breadth.total if breadth and breadth.total > 0 else None
        main_index = snapshot.indexes[0] if snapshot.indexes else None
        index_pct = main_index.change_pct if main_index else None
        strong = snapshot.strong_sectors[:3]
        yesterday_names = {sector.name for sector in snapshot.yesterday_strong_sectors}
        continued = [sector.name for sector in strong if sector.name in yesterday_names]
        topic_text = "、".join(sector.name for sector in strong) or "暂未形成清晰题材"
        news_theme = _news_theme_text(news)
        diagnostics.append("实时盘面数据完整：已使用指数、红绿家数、强势题材和昨日强势追踪生成评价。")

        if up_ratio is not None and up_ratio >= 0.58 and (index_pct or 0) >= 0:
            stance = "positive"
            tone = "红盘家数占优，指数同步配合"
        elif up_ratio is not None and up_ratio <= 0.42:
            stance = "defensive"
            tone = "绿盘压力偏大，先防守再选择"
        elif index_pct is not None and index_pct > 0 and up_ratio is not None and up_ratio < 0.5:
            stance = "neutral"
            tone = "指数偏红但个股扩散不足"
        else:
            stance = "neutral"
            tone = "盘面分化，适合等条件确认"
        if snapshot.status != "live" and stance == "positive":
            stance = "neutral"
            tone = f"{tone}，但行情源为降级快照"

        breadth_text = (
            f"红盘 {breadth.up}、绿盘 {breadth.down}、平盘 {breadth.flat}"
            if breadth
            else "红绿家数暂缺"
        )
        index_text = (
            f"{main_index.name}{_format_pct(index_pct)}"
            if main_index
            else "指数暂缺"
        )
        summary_parts = [
            f"{tone}；{index_text}，{breadth_text}。",
            f"今日强势题材集中在 {topic_text}。",
        ]
        if continued:
            summary_parts.append(f"昨日强势中 {('、'.join(continued))} 仍在榜，说明资金有一定延续。")
        elif snapshot.yesterday_strong_sectors:
            summary_parts.append("昨日强势题材未进入今日强势榜，题材延续性需要重新确认。")
        else:
            summary_parts.append("昨日强势追踪暂缺，延续性只能用后续榜单复核。")
        if news_theme:
            summary_parts.append(f"新闻侧重点落在 {news_theme}，仅作为新闻线索候选，需与题材榜交叉验证。")

        drivers: list[MarketCommentaryPoint] = []
        if strong:
            drivers.append(
                MarketCommentaryPoint(
                    title="强势题材",
                    detail="、".join(
                        f"{sector.name}{_format_pct(sector.change_pct)}"
                        for sector in strong
                    ),
                    weight="high",
                )
            )
        if breadth:
            drivers.append(
                MarketCommentaryPoint(
                    title="市场宽度",
                    detail=f"{breadth_text}，上涨占比 {up_ratio * 100:.1f}%。" if up_ratio is not None else breadth_text,
                    weight="high" if up_ratio is not None and up_ratio >= 0.58 else "medium",
                )
            )
        if news_theme:
            drivers.append(MarketCommentaryPoint(title="新闻催化", detail=f"候选线索：{news_theme}", weight="medium"))

        risks: list[str] = []
        if snapshot.status != "live":
            risks.append("当前行情评价包含本地或降级快照，实时性不足，盘中结论需要用最新红绿家数和成交扩散复核。")
        if up_ratio is not None and index_pct is not None and index_pct > 0 and up_ratio < 0.5:
            risks.append("指数上涨但个股弱，可能是权重护盘，策略需要更挑剔。")
        if not strong:
            risks.append("强势题材未返回，不能把指数波动误判成主线。")
        if not continued and snapshot.yesterday_strong_sectors:
            risks.append("昨日强势题材延续不足，追旧热点容易被切换节奏影响。")
        if not risks:
            risks.append("题材强时仍要看成交量延续，避免单日情绪过热后追高。")

        next_watch = [
            f"继续观察 {strong[0].name} 是否保持榜首和成交扩散。" if strong else "等待强势题材重新返回后再确认主线。",
            "运行策略后优先看命中股票是否集中在当日强势题材里。",
        ]
        if continued:
            next_watch.append(f"昨日强势延续方向：{('、'.join(continued))}，次日先看承接。")

        return MarketCommentaryResponse(
            updated_at=now,
            trade_date=_today_from_snapshot(snapshot).date(),
            source=f"{snapshot.source}+commentary",
            mode=self._commentary_mode(snapshot),
            stance=stance,
            summary=f"{self._summary_prefix(snapshot)}{' '.join(summary_parts)}",
            drivers=drivers,
            risks=risks,
            next_watch=next_watch,
            diagnostics=diagnostics,
        )

    def _commentary_mode(self, snapshot: RealtimeMarketSnapshot) -> str:
        if snapshot.market_phase == "non_trading":
            return "non_trading_review"
        if snapshot.market_phase == "lunch_break":
            return "lunch_break_review"
        if snapshot.market_phase == "post_close":
            return "post_close"
        return "intraday"

    def _summary_prefix(self, snapshot: RealtimeMarketSnapshot) -> str:
        if snapshot.market_phase == "non_trading":
            return "非交易日最近交易日回顾："
        if snapshot.market_phase == "post_close":
            return "收盘后复盘："
        if snapshot.market_phase == "lunch_break":
            return "午间盘面回顾："
        return ""

    def _read_fupan(self, diagnostics: list[str]) -> MarketBriefingResponse | None:
        if self.briefing_provider is None:
            return None
        try:
            briefing = self.briefing_provider.latest_fupan()
        except Exception as exc:
            diagnostics.append(f"行情评价读取同花顺复盘失败：{exc}")
            return None
        if not isinstance(briefing, MarketBriefingResponse):
            diagnostics.append("行情评价读取同花顺复盘失败：返回结构无效。")
            return None
        if briefing.source == "fallback" and not briefing.sections:
            diagnostics.append("行情评价同花顺复盘仅返回空入口，未作为评价依据。")
            return None
        if briefing.diagnostics:
            diagnostics.extend(f"同花顺复盘诊断：{message}" for message in briefing.diagnostics[:3])
        if not briefing.summary and not briefing.sections:
            diagnostics.append("行情评价同花顺复盘没有摘要或章节，未作为评价依据。")
            return None
        return briefing

    def _briefing_fallback(
        self,
        now: datetime,
        news: MarketNewsResponse | None,
        diagnostics: list[str],
        snapshot: RealtimeMarketSnapshot | None = None,
    ) -> MarketCommentaryResponse | None:
        briefing = self._read_fupan(diagnostics)
        if briefing is None:
            return None

        basis_parts = [briefing.summary.strip()] if briefing.summary.strip() and not is_noisy_market_line(briefing.summary) else []
        for section in briefing.sections[:2]:
            if section.content and not is_noisy_market_line(section.content):
                basis_parts.append(section.content.strip())
        basis = " ".join(part for part in basis_parts if part)
        if not basis:
            diagnostics.append("行情评价同花顺复盘没有可读正文，未作为评价依据。")
            return None

        news_theme = _news_theme_text(news)
        lead_text = basis[:180].rstrip()
        if len(basis) > len(lead_text):
            lead_text = f"{lead_text}..."
        basis_label, basis_risk, diagnostic_message = _briefing_basis_label(briefing.source)
        if lead_text.startswith("收盘后复盘"):
            summary = lead_text
        else:
            summary = f"收盘后复盘：实时盘面读取失败，已改用{basis_label}作为复盘依据。{lead_text}"
        if news_theme:
            summary = f"{summary} 新闻侧重点为 {news_theme}，仅作为辅助线索，不替代复盘正文。"

        drivers = [
            MarketCommentaryPoint(
                title=basis_label,
                detail=lead_text,
                weight="high",
            )
        ]
        if news_theme:
            drivers.append(MarketCommentaryPoint(title="新闻催化", detail=f"辅助线索：{news_theme}", weight="medium"))

        diagnostics.append(diagnostic_message)
        return MarketCommentaryResponse(
            updated_at=now,
            trade_date=(snapshot.updated_at.date() if snapshot is not None else briefing.updated_at.date()),
            source=f"{briefing.source}+briefing-commentary",
            mode="post_close",
            stance="neutral",
            summary=summary,
            drivers=drivers,
            risks=[
                basis_risk,
                "新闻只作为辅助线索，不能把消息热度包装成确定行情结论。",
            ],
            next_watch=[
                "优先检查复盘提到的主线是否在下一交易日继续出现在强势题材榜。",
                "恢复实时行情后复核指数方向、红绿家数和策略命中股票是否同向集中。",
            ],
            diagnostics=diagnostics,
        )

    def _news_fallback(
        self,
        now: datetime,
        news: MarketNewsResponse | None,
        diagnostics: list[str],
        snapshot: RealtimeMarketSnapshot | None = None,
    ) -> MarketCommentaryResponse:
        news_theme = _news_theme_text(news)
        trade_date = _today_from_snapshot(snapshot).date() if snapshot is not None else now.date()
        if news_theme:
            summary = (
                f"实时盘面暂不可用，以下仅为新闻线索候选。新闻线索集中在 {news_theme}，"
                "不能包装成确定行情结论，等待红绿家数、指数和题材榜恢复后再验证。"
            )
            drivers = [MarketCommentaryPoint(title="新闻线索", detail=f"候选线索：{news_theme}", weight="medium")]
            risks = [
                "实时数据缺失，缺少实时红绿家数和指数配合，新闻热度尚未完成盘面确认，避免只凭消息追高。",
                "若次日题材榜和成交量不能同步扩散，相关新闻线索可能只是短线催化。",
            ]
            next_watch = [
                f"实时盘面暂不可用，明日优先观察 {news_theme} 是否进入强势题材榜并获得红盘家数配合。",
                "恢复实时快照后复核指数方向、上涨占比和策略命中股票是否同向集中。",
            ]
            source = "news-fallback+commentary"
        else:
            summary = (
                "实时盘面暂不可用，以下仅为新闻线索候选。当前新闻线索也暂不可用，"
                "今日评价降级为防守模式；等待红绿家数、指数和题材榜恢复后再确认方向。"
            )
            drivers = []
            risks = ["实时数据缺失，当前无法确认真实盘面强弱，避免只凭本地历史数据追高。"]
            next_watch = ["实时盘面暂不可用，优先恢复实时指数、红绿家数、题材榜和新闻源，再运行策略筛选。"]
            source = "fallback"

        return MarketCommentaryResponse(
            updated_at=now,
            trade_date=trade_date,
            source=source,
            mode="news_fallback",
            stance="defensive",
            summary=summary,
            drivers=drivers,
            risks=risks,
            next_watch=next_watch,
            diagnostics=diagnostics,
        )
