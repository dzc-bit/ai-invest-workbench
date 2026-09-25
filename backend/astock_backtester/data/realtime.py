from __future__ import annotations

import json
import logging
import re
import time as monotonic_time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from threading import Event, Lock, Thread
from typing import Any

import pandas as pd
import requests
from bs4 import BeautifulSoup

from astock_backtester.data.cls import cls_request_json
from astock_backtester.data.cls_finance import (
    THS_MARKET_INDEXFLASH_URLS,
    THS_MARKET_SCORE_HEADERS,
    read_ths_browser_cookie,
)
from astock_backtester.data.http_transport import MINIMAL_USER_AGENT, resilient_get, scraping_get, should_allow_alternate_transport
from astock_backtester.data.realtime_parsers import (
    BEIJING_TZ,
    CLS_HOT_PLATE_URL,
    CLS_QUOTE_HOME_URL,
    EASTMONEY_A_SPOT_URL,
    EASTMONEY_SECTOR_URLS,
    EASTMONEY_YESTERDAY_LIMIT_UP_URL,
    INDEXES,
    MIN_CONTROLLED_BACKUP_SECTOR_ROWS,
    MIN_FULL_MARKET_BREADTH_TOTAL,
    SINA_BREADTH_BATCH_SIZE,
    SINA_HEADERS,
    TENCENT_QUOTE_URL,
    THS_BOARD_CODE_RE,
    THS_BREADTH_RE,
    THS_CONCEPT_SECTION_URL,
    THS_HEADERS,
    THS_HOT_TOPIC_HEADERS,
    THS_HOT_TOPIC_URL,
    THS_INDUSTRY_DETAIL_URL,
    THS_INDUSTRY_HTML_URL,
    THS_MARKET_SUMMARY_URL,
    THS_STOCK_CODE_RE,
    is_valid_full_market_breadth,
)
from astock_backtester.data.realtime_parsers import (
    aggregate_ths_hot_topic_rows as _aggregate_ths_hot_topic_rows,
)
from astock_backtester.data.realtime_parsers import (
    aggregate_yesterday_limit_up_sectors as _aggregate_yesterday_limit_up_sectors,
)
from astock_backtester.data.realtime_parsers import (
    append_yesterday_sector_note as _append_yesterday_sector_note,
)
from astock_backtester.data.realtime_parsers import (
    breadth_from_cls_home_data as _breadth_from_cls_home_data,
)
from astock_backtester.data.realtime_parsers import (
    decode_sina_response as _decode_sina_response,
)
from astock_backtester.data.realtime_parsers import (
    dedupe_sectors as _dedupe_sectors,
)
from astock_backtester.data.realtime_parsers import (
    extract_code_from_href as _extract_code_from_href,
)
from astock_backtester.data.realtime_parsers import (
    is_renderable_snapshot as _is_renderable_snapshot,
)
from astock_backtester.data.realtime_parsers import (
    market_phase as _market_phase,
)
from astock_backtester.data.realtime_parsers import (
    normalize_sector_change_pct as _normalize_sector_change_pct,
)
from astock_backtester.data.realtime_parsers import (
    parse_float as _parse_float,
)
from astock_backtester.data.realtime_parsers import (
    parse_int as _parse_int,
)
from astock_backtester.data.realtime_parsers import (
    phase_diagnostic as _phase_diagnostic,
)
from astock_backtester.data.realtime_parsers import (
    quote_from_cls_home as _quote_from_cls_home,
)
from astock_backtester.data.realtime_parsers import (
    quote_from_sina as _quote_from_sina,
)
from astock_backtester.data.realtime_parsers import (
    sector_rows_from_cls_hot_plate as _sector_rows_from_cls_hot_plate,
)
from astock_backtester.data.realtime_parsers import (
    unique_sources as _unique_sources,
)
from astock_backtester.data.symbols import a_share_market_symbol, normalize_symbol
from astock_backtester.data.trading_calendar import a_share_trade_dates
from astock_backtester.data.warehouse import Warehouse
from astock_backtester.models import (
    MarketBreadth,
    MarketIndexQuote,
    RealtimeMarketSnapshot,
    SectorMover,
)

logger = logging.getLogger(__name__)


def unavailable_market_snapshot(message: str, *, diagnostics: list[str] | None = None) -> RealtimeMarketSnapshot:
    now = datetime.now(UTC)
    return RealtimeMarketSnapshot(
        status="unavailable",
        source="service-fallback",
        updated_at=now,
        market_phase=_market_phase(now),
        message=message,
        diagnostics=diagnostics or [],
    )


@dataclass
class BrowserMarketProvider:
    timeout: float = 2.5

    def fetch_breadth_from_dom(self, url: str) -> MarketBreadth | None:
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            logger.warning("silent failure in fetch_breadth_from_dom", exc_info=True)
            return None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout * 1000))
                text = page.locator("body").inner_text(timeout=int(self.timeout * 1000))
                browser.close()
        except Exception:
            logger.warning("silent failure in fetch_breadth_from_dom", exc_info=True)
            return None
        match = THS_BREADTH_RE.search(text)
        if not match:
            return None
        up, down, flat = (int(item) for item in match.groups())
        return MarketBreadth(up=up, down=down, flat=flat, total=up + down + flat, source="browser-market-provider")


@dataclass
class HeavyMarketCrawlerProvider:
    requester: Callable[..., requests.Response] = scraping_get
    timeout: float = 2.5
    browser_provider: BrowserMarketProvider | None = None
    _last_successful_breadth: MarketBreadth | None = field(default=None, init=False, repr=False)

    def fetch_breadth(self) -> MarketBreadth | None:
        # The provider instance is cached on ``RealtimeMarketProvider`` and can
        # be invoked from a worker thread that may already have timed out.  It
        # therefore must NOT retain any cross-request breadth state; every call
        # returns a value scoped to the caller and publishes nothing shared.
        for url in [
            THS_MARKET_SUMMARY_URL,
            "https://q.10jqka.com.cn/",
        ]:
            breadth = self._fetch_public_html_breadth(url)
            if breadth is not None:
                return breadth
        if self.browser_provider is not None:
            breadth = self.browser_provider.fetch_breadth_from_dom(THS_MARKET_SUMMARY_URL)
            if breadth is not None:
                return breadth
        return None

    def _fetch_public_html_breadth(self, url: str) -> MarketBreadth | None:
        try:
            response = self.requester(url, timeout=self.timeout, headers=THS_HEADERS)
            response.raise_for_status()
            response.encoding = response.encoding or "gbk"
        except Exception:
            logger.warning("silent failure in _fetch_public_html_breadth", exc_info=True)
            return None
        text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
        match = THS_BREADTH_RE.search(text)
        if not match:
            return None
        up, down, flat = (int(item) for item in match.groups())
        return MarketBreadth(up=up, down=down, flat=flat, total=up + down + flat, source="heavy-market-crawler")


@dataclass
class _ClsHomeFlight:
    event: Event
    payload: dict[str, Any] | None = None


@dataclass
class RealtimeMarketProvider:
    warehouse: Warehouse
    timeout: float = 4.0
    requester: Callable[..., requests.Response] = scraping_get
    alternate_requester: Callable[..., Any] | None = None
    allow_alternate_transport: bool | None = None
    ths_cookie_getter: Callable[[float], str | None] | None = None
    # 红绿家数预算：单源 2.2 秒（主源财联社签名 XHR 冷连接需要 ~2s，不能压得更低）、
    # 总预算 8 秒——首源偶发失败时，同花顺/Sina/腾讯等备选源仍能在预算内依次
    # 跑起来，而不是旧版 3 秒总预算一到整体放弃、只留下空白宽度。
    # 流式接口会先返回指数与板块，等待增加对用户不可感。
    breadth_time_budget: float = 8.0
    breadth_source_timeout: float = 2.2
    sector_time_budget: float = 3.0
    sector_source_timeout: float = 0.8
    local_snapshot_time_budget: float = 2.0
    allow_eastmoney_breadth_fallback: bool = False
    cls_home_cache_ttl: float = 1.5
    yesterday_sector_time_budget: float = 1.0
    yesterday_sector_cache_ttl: float = 15 * 60.0
    _sector_member_cache: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False)
    _last_successful_snapshot: RealtimeMarketSnapshot | None = field(default=None, init=False, repr=False)
    _state_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _heavy_market_provider: HeavyMarketCrawlerProvider | None = field(default=None, init=False, repr=False)
    # P1-4: monotonic generation counters to determine publication rights
    # without relying on wall-clock timestamps.
    _request_generation: int = field(default=0, init=False, repr=False)
    _last_snapshot_generation: int = field(default=0, init=False, repr=False)
    # Round-4: fixed-capacity single-flight executors.  Each provider slot
    # has its own 1-worker executor reused across requests.  An in-flight
    # lock prevents stacking concurrent work; a new request that finds the
    # slot busy returns None / empty immediately instead of creating yet
    # another thread.
    _breadth_executor: ThreadPoolExecutor | None = field(default=None, init=False, repr=False)
    _sector_executor: ThreadPoolExecutor | None = field(default=None, init=False, repr=False)
    _local_executor: ThreadPoolExecutor | None = field(default=None, init=False, repr=False)
    _breadth_in_flight: Lock = field(default_factory=Lock, init=False, repr=False)
    _sector_in_flight: Lock = field(default_factory=Lock, init=False, repr=False)
    _local_in_flight: Lock = field(default_factory=Lock, init=False, repr=False)
    _cls_home_cache: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _cls_home_cached_at: float = field(default=0.0, init=False, repr=False)
    _cls_home_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _cls_home_in_flight: _ClsHomeFlight | None = field(default=None, init=False, repr=False)
    _yesterday_sector_cache_date: str | None = field(default=None, init=False, repr=False)
    _yesterday_sector_cache: list[SectorMover] = field(default_factory=list, init=False, repr=False)
    _yesterday_sector_cached_at: float = field(default=0.0, init=False, repr=False)
    _yesterday_sector_diagnostics_date: str | None = field(default=None, init=False, repr=False)
    _yesterday_sector_diagnostics: list[str] = field(default_factory=list, init=False, repr=False)
    _yesterday_sector_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _yesterday_sector_executor: ThreadPoolExecutor | None = field(default=None, init=False, repr=False)
    _yesterday_sector_in_flight: bool = field(default=False, init=False, repr=False)
    # 本地股票池计数的后台预热闸门：缓存未热时起一次性 daemon 线程现算
    # （warehouse.refresh_symbol_count，真实数据仓 ~10s），绝不在红绿家数
    # provider 循环里同步算——那会吃光 breadth_time_budget（8s），把后面
    # 本可用的 Sina/Tencent/AKShare 全部判超时。
    _symbol_count_warmup_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def _get_breadth_executor(self) -> ThreadPoolExecutor:
        if self._breadth_executor is None:
            self._breadth_executor = ThreadPoolExecutor(max_workers=1)
        return self._breadth_executor

    def _get_sector_executor(self) -> ThreadPoolExecutor:
        if self._sector_executor is None:
            self._sector_executor = ThreadPoolExecutor(max_workers=1)
        return self._sector_executor

    def _get_local_executor(self) -> ThreadPoolExecutor:
        if self._local_executor is None:
            self._local_executor = ThreadPoolExecutor(max_workers=1)
        return self._local_executor

    def _next_request_generation(self) -> int:
        """Atomically increment and return a new request generation number."""
        with self._state_lock:
            self._request_generation += 1
            return self._request_generation

    def _remember_successful_snapshot(
        self,
        snapshot: RealtimeMarketSnapshot,
        *,
        generation: int | None = None,
    ) -> None:
        with self._state_lock:
            current = self._last_successful_snapshot
            if current is None:
                self._last_successful_snapshot = snapshot.model_copy(deep=True)
                if generation is not None:
                    self._last_snapshot_generation = generation
                return
            # P1-4: production always passes generation.  For mixed-mode
            # safety, compare BOTH generation and updated_at so a legacy
            # call with a newer wall-clock time can never clobber a
            # generation-tracked snapshot.  Example: gen=5 (no-generation
            # legacy) with t=10:05 must not overwrite gen=6 with t=10:00.
            if generation is not None:
                should_update = generation > self._last_snapshot_generation
            elif self._last_snapshot_generation > 0:
                # A generation-tracked snapshot is present — a legacy
                # (no-generation) call must never overwrite it, even
                # when the legacy call has a newer wall-clock timestamp.
                should_update = False
            else:
                should_update = snapshot.updated_at > current.updated_at
            if should_update:
                self._last_successful_snapshot = snapshot.model_copy(deep=True)
                if generation is not None:
                    self._last_snapshot_generation = generation

    def retained_successful_snapshot(self) -> RealtimeMarketSnapshot | None:
        with self._state_lock:
            if self._last_successful_snapshot is None:
                return None
            return self._last_successful_snapshot.model_copy(deep=True)

    def market_snapshot(self) -> RealtimeMarketSnapshot:
        for event in self.market_snapshot_events():
            if event.get("type") == "result":
                snapshot = event.get("snapshot")
                if isinstance(snapshot, RealtimeMarketSnapshot):
                    return snapshot
        raise RuntimeError("Realtime market snapshot stream ended without a result.")

    def market_snapshot_events(self) -> Iterable[dict[str, Any]]:
        now = datetime.now(UTC)
        # P1-4: each request gets a monotonic generation so that late
        # arrivals cannot overwrite results from a newer request.
        request_generation = self._next_request_generation()
        self._clear_cls_home_completed_cache()
        phase = _market_phase(now)
        diagnostics: list[str] = []
        index_diagnostics: list[str] = []
        breadth_diagnostics: list[str] = []
        sector_diagnostics: list[str] = []
        yesterday_sector_diagnostics: list[str] = []
        phase_note = _phase_diagnostic(phase)
        if phase_note:
            diagnostics.append(phase_note)

        indexes: list[MarketIndexQuote] = []
        live_breadth: MarketBreadth | None = None
        live_sectors: list[SectorMover] = []
        self._yesterday_sector_snapshot_or_schedule(yesterday_sector_diagnostics)
        yesterday_sectors, has_yesterday_sector_result = self._yesterday_sector_cache_snapshot_for_current_pool()
        diagnostics.extend(yesterday_sector_diagnostics)
        live_sector_rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(self._call_indexes, index_diagnostics): "indexes",
                executor.submit(self._fetch_live_breadth_with_budget, breadth_diagnostics): "breadth",
                executor.submit(
                    self._fetch_live_sectors_with_budget,
                    sector_diagnostics,
                    live_sector_rows,
                ): "sectors",
            }
            for future in as_completed(futures):
                event_type = futures[future]
                try:
                    value = future.result()
                except Exception as exc:
                    diagnostics.append(f"实时行情{event_type}分块读取失败：{exc}")
                    value = [] if event_type != "breadth" else None
                if event_type == "indexes":
                    indexes = value
                    yield {
                        "type": "indexes",
                        "indexes": indexes,
                        "updated_at": now,
                        "market_phase": phase,
                        "diagnostics": [*diagnostics, *index_diagnostics],
                    }
                elif event_type == "breadth":
                    live_breadth = value
                    yield {
                        "type": "breadth",
                        "breadth": live_breadth,
                        "updated_at": now,
                        "market_phase": phase,
                        "diagnostics": [*diagnostics, *breadth_diagnostics],
                    }
                elif event_type == "sectors":
                    live_sectors = value
                    yield {
                        "type": "sectors",
                        "strong_sectors": live_sectors,
                        "updated_at": now,
                        "market_phase": phase,
                        "diagnostics": [*diagnostics, *sector_diagnostics],
                    }
        diagnostics.extend(index_diagnostics)
        diagnostics.extend(breadth_diagnostics)
        diagnostics.extend(sector_diagnostics)
        current_indexes = indexes
        has_current_live_context = bool(current_indexes and live_breadth is not None and live_sectors)
        skip_local_topic_fetch = any(
            item.startswith(("实时强势题材接口超时", "实时强势题材接口失败"))
            for item in sector_diagnostics
        )
        local_snapshot = (
            None
            if has_current_live_context
            else self._snapshot_from_local_with_budget(
                now,
                diagnostics,
                live_sector_rows,
                skip_topic_fetch=skip_local_topic_fetch,
            )
        )
        retained = self.retained_successful_snapshot()
        retained_fields: list[str] = []

        indexes = current_indexes
        if not indexes:
            if retained and retained.indexes:
                indexes = retained.indexes
                retained_fields.append("indexes")
            elif local_snapshot:
                indexes = local_snapshot.indexes

        breadth = live_breadth
        if breadth is None:
            if retained and retained.breadth is not None:
                breadth = retained.breadth
                retained_fields.append("breadth")
            elif local_snapshot:
                breadth = local_snapshot.breadth

        strong_sectors = live_sectors
        if not strong_sectors:
            if retained and retained.strong_sectors:
                strong_sectors = retained.strong_sectors
                retained_fields.append("strong_sectors")
            elif local_snapshot:
                strong_sectors = local_snapshot.strong_sectors

        if not has_yesterday_sector_result:
            if retained and retained.yesterday_strong_sectors:
                yesterday_sectors = retained.yesterday_strong_sectors
                retained_fields.append("yesterday_strong_sectors")
            else:
                yesterday_sectors = local_snapshot.yesterday_strong_sectors if local_snapshot else []
        if not live_breadth and breadth:
            yield {
                "type": "breadth",
                "breadth": breadth,
                "updated_at": now,
                "market_phase": phase,
                "diagnostics": list(diagnostics),
            }
        if not live_sectors and strong_sectors:
            yield {
                "type": "sectors",
                "strong_sectors": strong_sectors,
                "yesterday_strong_sectors": yesterday_sectors,
                "updated_at": now,
                "market_phase": phase,
                "diagnostics": list(diagnostics),
            }
        elif live_sectors:
            yield {
                "type": "sectors",
                "strong_sectors": strong_sectors,
                "yesterday_strong_sectors": yesterday_sectors,
                "updated_at": now,
                "market_phase": phase,
                "diagnostics": list(diagnostics),
            }
        has_partial_realtime_context = bool(current_indexes or live_breadth is not None or live_sectors)
        status = (
            "live"
            if has_current_live_context and not retained_fields
            else ("stale" if retained_fields or has_partial_realtime_context else local_snapshot.status)
        )
        if local_snapshot and not live_breadth and local_snapshot.diagnostics:
            diagnostics.extend(local_snapshot.diagnostics)
        if retained_fields and retained:
            diagnostics.extend(retained.diagnostics)
            diagnostics.append(
                "沿用最近成功行情快照："
                f"fields={','.join(retained_fields)}; snapshot_at={retained.updated_at.isoformat()}。"
            )
        if not current_indexes:
            diagnostics.append("实时指数接口暂不可用，尝试使用最近成功快照或本地最近交易日。")
        if local_snapshot and current_indexes and live_breadth is None and local_snapshot.breadth:
            diagnostics.append("实时红绿家数接口暂不可用，已回退到本地最近交易日统计。")
        if local_snapshot and current_indexes and live_breadth is None and local_snapshot.breadth is None:
            diagnostics.append("实时红绿家数接口暂不可用，本地最近交易日红绿宽度也不完整，已隐藏该宽度统计。")
        if local_snapshot and current_indexes and not live_sectors and local_snapshot.strong_sectors:
            diagnostics.append("实时强势题材接口暂不可用，已回退到本地最近交易日题材聚合。")
        source_parts: list[str] = []
        if indexes:
            source_parts.extend(_unique_sources(quote.source for quote in indexes))
        if breadth is not None:
            source_parts.append(breadth.source)
        if strong_sectors:
            source_parts.append(strong_sectors[0].source)
        if yesterday_sectors:
            source_parts.extend(_unique_sources(sector.source for sector in yesterday_sectors))
        source = "+".join(source_parts) if source_parts else (
            local_snapshot.source if local_snapshot else (retained.source if retained else "live")
        )
        if retained_fields and not source.endswith("+retained-last-success"):
            source = f"{source}+retained-last-success"
        if not current_indexes:
            message = retained.message if retained_fields and retained else local_snapshot.message
        else:
            message = self._build_live_message(
                live_breadth,
                live_sectors,
                index_source=indexes[0].source if indexes else None,
            )
        message = _append_yesterday_sector_note(message, yesterday_sectors)
        seen_diagnostics: set[str] = set()
        diagnostics = [
            item
            for item in diagnostics
            if not (item in seen_diagnostics or seen_diagnostics.add(item))
        ]
        snapshot = RealtimeMarketSnapshot(
            status=status,
            source=source,
            updated_at=now,
            market_phase=phase,
            indexes=indexes or (local_snapshot.indexes if local_snapshot else []),
            breadth=breadth,
            strong_sectors=strong_sectors,
            yesterday_strong_sectors=yesterday_sectors,
            message=message,
            diagnostics=diagnostics,
        )
        if snapshot.status == "live" and _is_renderable_snapshot(snapshot):
            self._remember_successful_snapshot(snapshot, generation=request_generation)
            yield {"type": "result", "snapshot": snapshot}
            return
        if retained is not None and not indexes and not retained_fields:
            retained_at = retained.updated_at
            retained.status = "stale"
            retained.updated_at = now
            retained.market_phase = phase
            retained.source = (
                retained.source
                if retained.source.endswith("+retained-last-success")
                else f"{retained.source}+retained-last-success"
            )
            retained.message = _append_yesterday_sector_note(
                f"{phase_note or '实时接口暂不可用'} 沿用最近成功行情快照。", retained.yesterday_strong_sectors
            )
            # P1-2: merge current request diagnostics + original snapshot
            # diagnostics + retained time hint.  Deduplicate ALL sources
            # (including current diagnostics against themselves) while
            # keeping insertion order stable.
            seen: set[str] = set()
            merged_diagnostics: list[str] = []
            for item in diagnostics:
                if item not in seen:
                    seen.add(item)
                    merged_diagnostics.append(item)
            for item in retained.diagnostics:
                if item not in seen:
                    seen.add(item)
                    merged_diagnostics.append(item)
            retained_hint = f"沿用最近成功行情快照：{retained_at.isoformat()}。"
            if retained_hint not in seen:
                merged_diagnostics.append(retained_hint)
            retained.diagnostics = merged_diagnostics
            yield {"type": "result", "snapshot": retained}
            return
        yield {"type": "result", "snapshot": snapshot}

    def _call_live_breadth(
        self,
        diagnostics: list[str],
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> MarketBreadth | None:
        try:
            return self._fetch_live_breadth(
                diagnostics,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except TypeError:
            try:
                return self._fetch_live_breadth(diagnostics)
            except TypeError:
                return self._fetch_live_breadth()

    def _fetch_live_breadth_with_budget(self, diagnostics: list[str]) -> MarketBreadth | None:
        if self.breadth_time_budget is None:
            worker_diagnostics: list[str] = []
            result = self._call_live_breadth(worker_diagnostics)
            diagnostics.extend(worker_diagnostics)
            return result
        # Single-flight: if the breadth slot is already busy, reject
        # immediately instead of stacking another thread.
        if not self._breadth_in_flight.acquire(blocking=False):
            diagnostics.append("实时红绿家数接口繁忙：上一轮请求尚未完成，已跳过本轮。")
            return None
        try:
            deadline = monotonic_time.monotonic() + self.breadth_time_budget
            cancel_event = Event()
            # The worker writes only to its private diagnostics list.  The caller
            # publishes those diagnostics ONLY when the future completes within the
            # budget; on timeout the private list is discarded so a late worker can
            # never pollute the shared diagnostics.
            worker_diagnostics: list[str] = []
            executor = self._get_breadth_executor()
            future = executor.submit(
                self._call_live_breadth, worker_diagnostics, deadline, cancel_event
            )
        except Exception:
            self._breadth_in_flight.release()
            raise
        future.add_done_callback(lambda _future: self._breadth_in_flight.release())
        try:
            remaining = max(0.0, deadline - monotonic_time.monotonic())
            result = future.result(timeout=remaining)
            # Double-check: even after future.result returns, the computation
            # itself may have exhausted the budget.  Discard the result if so.
            if monotonic_time.monotonic() > deadline:
                raise TimeoutError
            diagnostics.extend(worker_diagnostics)
            return result
        except TimeoutError:
            cancel_event.set()
            future.cancel()
            diagnostics.append(f"实时红绿家数接口超时：{self.breadth_time_budget:g}秒，已继续返回可用行情。")
            return None
        except Exception as exc:
            diagnostics.append(f"实时红绿家数接口失败：{exc}，已继续返回可用行情。")
            return None

    def _fetch_live_sectors_with_budget(
        self,
        diagnostics: list[str],
        sector_rows_out: list[dict] | None = None,
    ) -> list[SectorMover]:
        worker_rows: list[dict] = []
        worker_diagnostics: list[str] = []
        if self.sector_time_budget is None:
            sectors = self._call_live_sectors(worker_diagnostics, sector_rows_out=worker_rows)
            diagnostics.extend(worker_diagnostics)
            if sector_rows_out is not None:
                sector_rows_out[:] = worker_rows
            return sectors
        # Single-flight: if the sector slot is already busy, reject
        # immediately instead of stacking another thread.
        if not self._sector_in_flight.acquire(blocking=False):
            diagnostics.append("实时强势题材接口繁忙：上一轮请求尚未完成，已跳过本轮。")
            return []
        try:
            deadline = monotonic_time.monotonic() + self.sector_time_budget
            cancel_event = Event()
            # The worker writes only to its private diagnostics/rows.  Both are
            # published ONLY when the future completes within the budget; on timeout
            # they are discarded so a late worker cannot pollute shared state.
            executor = self._get_sector_executor()
            future = executor.submit(
                self._call_live_sectors,
                worker_diagnostics,
                deadline,
                cancel_event,
                worker_rows,
            )
        except Exception:
            self._sector_in_flight.release()
            raise
        future.add_done_callback(lambda _future: self._sector_in_flight.release())
        try:
            remaining = max(0.0, deadline - monotonic_time.monotonic())
            sectors = future.result(timeout=remaining)
            if monotonic_time.monotonic() > deadline:
                raise TimeoutError
            diagnostics.extend(worker_diagnostics)
            if sector_rows_out is not None:
                sector_rows_out[:] = worker_rows
            return sectors
        except TimeoutError:
            cancel_event.set()
            future.cancel()
            diagnostics.append(f"实时强势题材接口超时：{self.sector_time_budget:g}秒，已先返回红绿家数并回退本地题材。")
            return []
        except Exception as exc:
            diagnostics.append(f"实时强势题材接口失败：{exc}，已回退本地题材。")
            return []

    def _snapshot_from_local_with_budget(
        self,
        now: datetime,
        diagnostics: list[str],
        sector_rows: list[dict] | None = None,
        *,
        skip_topic_fetch: bool = False,
    ) -> RealtimeMarketSnapshot:
        if self.local_snapshot_time_budget is None:
            return self._call_snapshot_from_local(now, sector_rows, skip_topic_fetch)
        # Single-flight: if the local snapshot slot is already busy, reject
        # immediately instead of stacking another thread.
        if not self._local_in_flight.acquire(blocking=False):
            diagnostics.append("本地兜底行情快照繁忙：上一轮本地快照尚未完成，已跳过本轮。")
            return unavailable_market_snapshot(
                "本地兜底行情快照生成繁忙。",
                diagnostics=["本地兜底行情快照繁忙：上一轮尚未完成。"],
            )
        try:
            deadline = monotonic_time.monotonic() + self.local_snapshot_time_budget
            cancel_event = Event()
            executor = self._get_local_executor()
            future = executor.submit(
                self._call_snapshot_from_local,
                now,
                sector_rows,
                skip_topic_fetch,
                deadline,
                cancel_event,
            )
        except Exception:
            self._local_in_flight.release()
            raise
        future.add_done_callback(lambda _future: self._local_in_flight.release())
        try:
            remaining = max(0.0, deadline - monotonic_time.monotonic())
            result = future.result(timeout=remaining)
            if monotonic_time.monotonic() > deadline:
                raise TimeoutError
            return result
        except TimeoutError:
            # Signal the still-running worker so any late sector-member cache
            # write is blocked before it can commit to shared state.
            cancel_event.set()
            future.cancel()
            diagnostics.append(f"本地兜底行情快照超时：{self.local_snapshot_time_budget:g}秒，已继续返回可用行情。")
            return unavailable_market_snapshot(
                "本地兜底行情快照生成超时。",
                diagnostics=[f"本地兜底行情快照超时：{self.local_snapshot_time_budget:g}秒。"],
            )
        except Exception as exc:
            diagnostics.append(f"本地兜底行情快照失败：{exc}，已继续返回可用行情。")
            return unavailable_market_snapshot(
                f"本地兜底行情快照生成失败：{exc}",
                diagnostics=[f"本地兜底行情快照失败：{exc}"],
            )

    def _call_live_sectors(
        self,
        diagnostics: list[str],
        deadline: float | None = None,
        cancel_event: Event | None = None,
        sector_rows_out: list[dict] | None = None,
    ) -> list[SectorMover]:
        try:
            return self._fetch_live_sectors(
                diagnostics,
                deadline=deadline,
                cancel_event=cancel_event,
                sector_rows_out=sector_rows_out,
            )
        except TypeError:
            try:
                return self._fetch_live_sectors(diagnostics)
            except TypeError:
                return self._fetch_live_sectors()

    def _call_snapshot_from_local(
        self,
        now: datetime,
        sector_rows: list[dict] | None,
        skip_topic_fetch: bool = False,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> RealtimeMarketSnapshot:
        try:
            return self._snapshot_from_local(
                now,
                sector_rows=sector_rows,
                skip_topic_fetch=skip_topic_fetch,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except TypeError:
            try:
                return self._snapshot_from_local(
                    now,
                    sector_rows=sector_rows,
                    skip_topic_fetch=skip_topic_fetch,
                )
            except TypeError:
                try:
                    return self._snapshot_from_local(now, sector_rows=sector_rows)
                except TypeError:
                    return self._snapshot_from_local(now)

    def _call_indexes(self, diagnostics: list[str]) -> list[MarketIndexQuote]:
        try:
            return self._fetch_indexes(diagnostics)
        except TypeError:
            return self._fetch_indexes()

    def _fetch_indexes(self, diagnostics: list[str] | None = None) -> list[MarketIndexQuote]:
        diagnostics = diagnostics if diagnostics is not None else []
        cls_quotes = self._fetch_cls_indexes(diagnostics)
        if cls_quotes:
            return cls_quotes
        symbols = ",".join(symbol for symbol, _ in INDEXES)
        url = f"https://hq.sinajs.cn/list={symbols}"
        try:
            response = self.requester(
                url,
                timeout=self.timeout,
                headers={"Referer": "https://finance.sina.com.cn/"},
            )
            response.raise_for_status()
        except Exception:
            logger.warning("silent failure in _fetch_indexes", exc_info=True)
            return []
        response.encoding = response.encoding or "gbk"
        decoded = _decode_sina_response(response.text)
        quotes: list[MarketIndexQuote] = []
        for symbol, name in INDEXES:
            quote = _quote_from_sina(symbol, name, decoded.get(symbol, []))
            if quote:
                quotes.append(quote)
        return quotes

    def _fetch_cls_home_payload(
        self,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        request_timeout = min(self.timeout, 2.5) if timeout is None else min(self.timeout, timeout)
        if deadline is not None:
            remaining = deadline - monotonic_time.monotonic()
            if remaining <= 0:
                raise TimeoutError("CLS home request budget exhausted")
            request_timeout = min(request_timeout, remaining)
        request_timeout = max(0.05, request_timeout)
        now = monotonic_time.monotonic()
        with self._cls_home_lock:
            if (
                self._cls_home_cache is not None
                and self.cls_home_cache_ttl > 0
                and now - self._cls_home_cached_at <= self.cls_home_cache_ttl
            ):
                return deepcopy(self._cls_home_cache)
            flight = self._cls_home_in_flight
            if flight is None:
                flight = _ClsHomeFlight(event=Event())
                self._cls_home_in_flight = flight
                owner = True
            else:
                owner = False

        if not owner:
            max_wait_timeout = max(0.05, min(float(self.timeout), 3.0) + 0.25)
            if deadline is not None:
                remaining = deadline - monotonic_time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("CLS home single-flight request budget exhausted")
                wait_timeout = min(max_wait_timeout, remaining)
            else:
                wait_timeout = min(request_timeout + 0.1, max_wait_timeout)
            if not flight.event.wait(timeout=wait_timeout):
                raise TimeoutError("CLS home single-flight request timed out")
            with self._cls_home_lock:
                if flight.payload is not None:
                    return deepcopy(flight.payload)
            raise RuntimeError("CLS home single-flight request failed")

        try:
            payload = cls_request_json(
                self.requester,
                CLS_QUOTE_HOME_URL,
                timeout=request_timeout,
            )
            self._validate_cls_home_payload(payload)
            with self._cls_home_lock:
                self._cls_home_cache = deepcopy(payload)
                self._cls_home_cached_at = monotonic_time.monotonic()
                flight.payload = deepcopy(payload)
            return payload
        finally:
            with self._cls_home_lock:
                if self._cls_home_in_flight is flight:
                    self._cls_home_in_flight = None
                flight.event.set()

    def _clear_cls_home_completed_cache(self) -> None:
        with self._cls_home_lock:
            self._cls_home_cache = None
            self._cls_home_cached_at = 0.0

    def _validate_cls_home_payload(self, payload: dict[str, Any]) -> None:
        code = payload.get("code")
        if code not in (None, 0, "0", 200, "200"):
            detail = str(
                payload.get("message") or payload.get("msg") or payload.get("error") or ""
            ).strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(f"CLS home response rejected with code={code}{suffix}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("CLS home response has no mapping data")
        index_rows = data.get("index_quote")
        has_parseable_index = isinstance(index_rows, list) and any(
            _quote_from_cls_home(row) is not None for row in index_rows if isinstance(row, dict)
        )
        if not has_parseable_index and _breadth_from_cls_home_data(data) is None:
            raise RuntimeError(
                "CLS home response has no usable parseable index_quote or complete up_down_dis field"
            )

    def _trade_date_on_or_before(self, candidate: pd.Timestamp) -> str:
        candidate = pd.Timestamp(candidate).normalize()
        while not a_share_trade_dates(candidate, candidate):
            candidate -= pd.Timedelta(days=1)
        return candidate.date().isoformat()

    def _latest_trade_date(self) -> str:
        today = pd.Timestamp(datetime.now(UTC).astimezone(BEIJING_TZ).date()).normalize()
        return self._trade_date_on_or_before(today)

    def _previous_trade_date(self) -> str:
        today = pd.Timestamp(datetime.now(UTC).astimezone(BEIJING_TZ).date()).normalize()
        return self._trade_date_on_or_before(today - pd.Timedelta(days=1))

    def _yesterday_pool_dates(self) -> tuple[str, str]:
        as_of_date = self._latest_trade_date()
        pool_date = self._trade_date_on_or_before(pd.Timestamp(as_of_date) - pd.Timedelta(days=1))
        return as_of_date, pool_date

    def _yesterday_sector_cache_snapshot_for_current_pool(self) -> tuple[list[SectorMover], bool]:
        _, target_date = self._yesterday_pool_dates()
        with self._yesterday_sector_lock:
            if self._yesterday_sector_cache_date != target_date:
                return [], False
            return deepcopy(self._yesterday_sector_cache), True

    def _yesterday_sector_snapshot_or_schedule(
        self,
        diagnostics: list[str],
    ) -> list[SectorMover]:
        _, target_date = self._yesterday_pool_dates()
        now = monotonic_time.monotonic()
        with self._yesterday_sector_lock:
            has_cached_result = self._yesterday_sector_cache_date == target_date
            if (
                has_cached_result
                and now - self._yesterday_sector_cached_at <= self.yesterday_sector_cache_ttl
            ):
                diagnostics.extend(self._yesterday_sector_diagnostics)
                return deepcopy(self._yesterday_sector_cache)
            if self._yesterday_sector_diagnostics_date == target_date:
                diagnostics.extend(self._yesterday_sector_diagnostics)
            if self._yesterday_sector_in_flight:
                diagnostics.append("eastmoney-yesterday-limit-up tracking refresh is still running.")
                if has_cached_result:
                    diagnostics.append(
                        "eastmoney-yesterday-limit-up recent-success cache used as stale fallback while refresh is running."
                    )
                    return deepcopy(self._yesterday_sector_cache)
                return []
            self._yesterday_sector_in_flight = True
            if self._yesterday_sector_executor is None:
                self._yesterday_sector_executor = ThreadPoolExecutor(max_workers=1)
            executor = self._yesterday_sector_executor

        worker_diagnostics: list[str] = []
        try:
            future = executor.submit(self._fetch_yesterday_strong_sectors, worker_diagnostics)
        except Exception as exc:
            with self._yesterday_sector_lock:
                self._yesterday_sector_in_flight = False
            diagnostics.append(f"eastmoney-yesterday-limit-up background refresh failed: {exc}")
            if has_cached_result:
                diagnostics.append(
                    "eastmoney-yesterday-limit-up recent-success cache used as stale fallback after refresh scheduling failed."
                )
                return deepcopy(self._yesterday_sector_cache)
            return []

        def release(done) -> None:
            try:
                done.result()
            except Exception as exc:
                worker_diagnostics.append(f"eastmoney-yesterday-limit-up background refresh failed: {exc}")
            with self._yesterday_sector_lock:
                self._yesterday_sector_in_flight = False
                self._yesterday_sector_diagnostics_date = target_date
                self._yesterday_sector_diagnostics = list(worker_diagnostics)

        future.add_done_callback(release)
        diagnostics.append("eastmoney-yesterday-limit-up tracking refresh scheduled in background.")
        if has_cached_result:
            diagnostics.append(
                "eastmoney-yesterday-limit-up recent-success cache used as stale fallback while refresh is scheduled."
            )
            return deepcopy(self._yesterday_sector_cache)
        return []

    def _fetch_yesterday_strong_sectors(
        self,
        diagnostics: list[str],
        *,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> list[SectorMover]:
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "yesterday-sector"):
            return []
        as_of_date, target_date = self._yesterday_pool_dates()
        now = monotonic_time.monotonic()
        with self._yesterday_sector_lock:
            if (
                self._yesterday_sector_cache_date == target_date
                and now - self._yesterday_sector_cached_at <= self.yesterday_sector_cache_ttl
            ):
                return deepcopy(self._yesterday_sector_cache)

        request_budget = min(self.yesterday_sector_time_budget, self.timeout, 2.5)
        request_deadline = monotonic_time.monotonic() + request_budget
        if deadline is not None:
            request_deadline = min(request_deadline, deadline)

        page_size = 100
        max_pages = 3
        pool: list[dict] = []
        expected_total: int | None = None
        expected_pages: int | None = None
        more_pages_expected = False
        partial_result = False
        for page_index in range(max_pages):
            remaining = request_deadline - monotonic_time.monotonic()
            if remaining <= 0:
                if pool:
                    partial_result = True
                    diagnostics.append(
                        "eastmoney-yesterday-limit-up partial result: "
                        f"fetched {len(pool)} of {expected_total or 'an unknown number of'} pool rows before timeout."
                    )
                    break
                diagnostics.append("eastmoney-yesterday-limit-up request timed out before the first page.")
                return []
            try:
                response = self.requester(
                    EASTMONEY_YESTERDAY_LIMIT_UP_URL,
                    timeout=max(0.05, remaining),
                    headers={
                        "Referer": "https://quote.eastmoney.com/ztb/detail",
                        "User-Agent": MINIMAL_USER_AGENT,
                    },
                    params={
                        "ut": "7eea3edcaed734bea9cbfc24409ed989",
                        "dpt": "wz.ztzt",
                        "date": as_of_date.replace("-", ""),
                        "Pageindex": str(page_index),
                        "pagesize": str(page_size),
                        "sort": "zs:desc",
                        "ft": "1",
                        "l": "0",
                    },
                )
                response.raise_for_status()
                payload = response.json() or {}
            except Exception as exc:
                if pool:
                    partial_result = True
                    diagnostics.append(
                        "eastmoney-yesterday-limit-up partial result: "
                        f"page={page_index} failed after {len(pool)} pool rows: {exc}"
                    )
                    break
                diagnostics.append(f"eastmoney-yesterday-limit-up request failed: {exc}")
                return []

            if isinstance(payload, dict) and "rc" in payload:
                rc = _parse_int(payload.get("rc"))
                if rc != 0:
                    detail = str(
                        payload.get("message") or payload.get("msg") or payload.get("error") or ""
                    ).strip()
                    suffix = f": {detail}" if detail else ""
                    diagnostics.append(
                        f"eastmoney-yesterday-limit-up logical failure: rc={payload.get('rc')}{suffix}"
                    )
                    return []

            data = payload.get("data") if isinstance(payload, dict) else None
            page_pool = data.get("pool") if isinstance(data, dict) else None
            if not isinstance(page_pool, list):
                if pool:
                    partial_result = True
                    diagnostics.append(
                        "eastmoney-yesterday-limit-up partial result: "
                        f"page={page_index} returned no valid pool list after {len(pool)} pool rows."
                    )
                    break
                diagnostics.append("eastmoney-yesterday-limit-up returned no valid pool list.")
                return []
            pool.extend(item for item in page_pool if isinstance(item, dict))

            if expected_total is None:
                for key in ("tc", "count", "total", "total_count", "totalCount"):
                    value = _parse_int(data.get(key))
                    if value is not None:
                        expected_total = value
                        break
            if expected_pages is None:
                for key in ("page_count", "pageCount", "pages", "total_pages", "totalPages"):
                    value = _parse_int(data.get(key))
                    if value is not None:
                        expected_pages = value
                        break

            more_pages_expected = (
                (expected_total is not None and len(pool) < expected_total)
                or (expected_pages is not None and page_index + 1 < expected_pages)
                or (expected_total is None and expected_pages is None and len(page_pool) >= page_size)
            )
            if not more_pages_expected:
                break
            if not page_pool:
                partial_result = True
                diagnostics.append(
                    "eastmoney-yesterday-limit-up partial result: "
                    f"page={page_index} was empty after {len(pool)} pool rows."
                )
                break
        else:
            if more_pages_expected:
                partial_result = True
                diagnostics.append(
                    "eastmoney-yesterday-limit-up partial result: "
                    f"fetched {len(pool)} of {expected_total or 'an unknown number of'} pool rows "
                    f"within the {max_pages}-page request budget."
                )

        rows: list[dict] = []
        for item in pool:
            raw_change_key = "zdp"
            raw_change = item.get("zdp")
            if raw_change in (None, "", "--", "-"):
                raw_change_key = "pct"
                raw_change = item.get("pct")
            if raw_change in (None, "", "--", "-"):
                raw_change_key = "change_pct"
                raw_change = item.get("change_pct")
            row = {
                "code": item.get("c") or item.get("code"),
                "name": item.get("n") or item.get("name"),
                "industry": (
                    item.get("hybk_name")
                    or item.get("hybk")
                    or item.get("industry")
                    or item.get("sector")
                ),
            }
            row[raw_change_key] = raw_change
            rows.append(row)
        sectors = _aggregate_yesterday_limit_up_sectors(rows)
        is_complete_empty_pool = expected_total == 0 and not pool
        if not sectors and not is_complete_empty_pool:
            diagnostics.append(
                "eastmoney-yesterday-limit-up returned no valid pool rows "
                f"for pool_date={target_date}, as_of_date={as_of_date}."
            )
            return []
        if not partial_result:
            with self._yesterday_sector_lock:
                self._yesterday_sector_cache_date = target_date
                self._yesterday_sector_cache = deepcopy(sectors)
                self._yesterday_sector_cached_at = monotonic_time.monotonic()
        else:
            diagnostics.append("eastmoney-yesterday-limit-up partial result was not cached.")
        diagnostics.append(
            "eastmoney-yesterday-limit-up tracking loaded "
            f"pool_date={target_date}, as_of_date={as_of_date}, sectors={len(sectors)}."
        )
        return sectors

    def _fetch_cls_indexes(self, diagnostics: list[str] | None = None) -> list[MarketIndexQuote]:
        diagnostics = diagnostics if diagnostics is not None else []
        try:
            payload = self._fetch_cls_home_payload()
        except Exception as exc:
            diagnostics.append(f"CLS home index source failed: {exc}")
            return []
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("index_quote") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            diagnostics.append("CLS home index source has no valid index_quote field.")
            return []
        quotes = [quote for row in rows if isinstance(row, dict) and (quote := _quote_from_cls_home(row))]
        if not quotes:
            diagnostics.append("CLS home index source returned no parseable index quotes.")
            return []
        preferred = {symbol for symbol, _ in INDEXES}
        ordered = [quote for quote in quotes if quote.symbol in preferred]
        return ordered or quotes[: len(INDEXES)]

    def _sector_request_timeout(self, max_seconds: float | None = None) -> float:
        timeout = self.sector_source_timeout
        if self.sector_time_budget is not None:
            timeout = min(timeout, max(0.2, self.sector_time_budget / 3))
        if max_seconds is not None:
            timeout = min(timeout, max_seconds)
        return min(self.timeout, timeout)

    def _context_expired(
        self,
        cancel_event: Event | None,
        deadline: float | None,
    ) -> bool:
        """Return True when the request budget is exhausted.

        Unlike :meth:`_source_chain_cancelled`, this is a side-effect-free
        check used to gate writes to cross-request shared caches without
        emitting a diagnostic message.
        """
        if cancel_event is not None and cancel_event.is_set():
            return True
        if deadline is not None and monotonic_time.monotonic() >= deadline:
            return True
        return False

    def _source_chain_cancelled(
        self,
        cancel_event: Event | None,
        deadline: float | None,
        diagnostics: list[str],
        chain: str,
    ) -> bool:
        cancelled = cancel_event is not None and cancel_event.is_set()
        expired = deadline is not None and monotonic_time.monotonic() >= deadline
        if not cancelled and not expired:
            return False
        message = f"{chain} source chain stopped because the request budget was exhausted."
        if message not in diagnostics:
            diagnostics.append(message)
        return True

    def _publish_sector_rows(
        self,
        rows: list[dict],
        sector_rows_out: list[dict] | None,
        cancel_event: Event | None,
        deadline: float | None,
        diagnostics: list[str],
    ) -> bool:
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return False
        if sector_rows_out is not None:
            sector_rows_out[:] = rows
        return True

    def _allow_public_alternate_transport(self) -> bool:
        return should_allow_alternate_transport(self.requester, self.allow_alternate_transport)

    def _request_public_html(
        self,
        url: str,
        *,
        timeout: float,
        source: str,
        diagnostics: list[str],
        deadline: float | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return resilient_get(
            self.requester,
            url,
            timeout=timeout,
            source=source,
            diagnostics=diagnostics,
            retries=1,
            deadline=deadline,
            alternate_requester=self.alternate_requester,
            allow_alternate=self._allow_public_alternate_transport(),
            headers=headers,
        )

    def _call_ths_concept_section_rows(
        self,
        diagnostics: list[str],
        deadline: float | None,
        cancel_event: Event | None,
    ) -> list[dict]:
        try:
            return self._fetch_ths_concept_section_rows(
                diagnostics=diagnostics,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except TypeError:
            return self._fetch_ths_concept_section_rows()

    def _call_ths_industry_html_rows(
        self,
        diagnostics: list[str],
        deadline: float | None,
        cancel_event: Event | None,
    ) -> list[dict]:
        try:
            return self._fetch_ths_industry_html_rows(
                diagnostics=diagnostics,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except TypeError:
            return self._fetch_ths_industry_html_rows()

    def _fetch_live_sectors(
        self,
        diagnostics: list[str] | None = None,
        *,
        deadline: float | None = None,
        cancel_event: Event | None = None,
        sector_rows_out: list[dict] | None = None,
    ) -> list[SectorMover]:
        diagnostics = diagnostics if diagnostics is not None else []
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        cls_sectors = self._call_cls_hot_plate_sectors(
            diagnostics,
            sector_rows_out,
            deadline,
            cancel_event,
        )
        if cls_sectors:
            return _dedupe_sectors(cls_sectors, 10)
        diagnostics.append("cls-hot-plate strong-sector source returned no valid rows.")
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        ths_concept_rows = self._call_ths_concept_section_rows(diagnostics, deadline, cancel_event)
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        ths_concept_sectors = self._parse_sector_rows(ths_concept_rows, "ths-concept-section")
        if ths_concept_sectors:
            if not self._publish_sector_rows(
                ths_concept_rows, sector_rows_out, cancel_event, deadline, diagnostics
            ):
                return []
            return _dedupe_sectors(ths_concept_sectors, 10)
        diagnostics.append("ths-concept-section strong-sector source returned no valid rows.")
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        ths_industry_rows = self._call_ths_industry_html_rows(diagnostics, deadline, cancel_event)
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        ths_industry_sectors = self._parse_sector_rows(ths_industry_rows, "ths-industry-html")
        if ths_industry_sectors:
            if not self._publish_sector_rows(
                ths_industry_rows, sector_rows_out, cancel_event, deadline, diagnostics
            ):
                return []
            return _dedupe_sectors(ths_industry_sectors, 10)
        diagnostics.append("ths-industry-html strong-sector source returned no valid rows.")
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        sina_sectors = self._fetch_sina_sectors()
        if sina_sectors:
            if not self._publish_sector_rows(
                [], sector_rows_out, cancel_event, deadline, diagnostics
            ):
                return []
            return _dedupe_sectors(sina_sectors, 10)
        diagnostics.append("sina-sector strong-sector source returned no valid rows.")
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        akshare_concept_rows = self._fetch_akshare_sector_rows_with_timeout("concept", "akshare-sector", diagnostics)
        akshare_concept_sectors = self._parse_sector_rows(akshare_concept_rows, "akshare-sector")
        if akshare_concept_sectors:
            if not self._publish_sector_rows(
                akshare_concept_rows, sector_rows_out, cancel_event, deadline, diagnostics
            ):
                return []
            return _dedupe_sectors(akshare_concept_sectors, 10)
        diagnostics.append("akshare-sector strong-sector source returned no valid rows.")
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        akshare_industry_rows = self._fetch_akshare_sector_rows_with_timeout("industry", "akshare-industry-sector", diagnostics)
        akshare_industry_sectors = self._parse_sector_rows(akshare_industry_rows, "akshare-industry-sector")
        if akshare_industry_sectors:
            if not self._publish_sector_rows(
                akshare_industry_rows, sector_rows_out, cancel_event, deadline, diagnostics
            ):
                return []
            return _dedupe_sectors(akshare_industry_sectors, 10)
        diagnostics.append("akshare-industry-sector strong-sector source returned no valid rows.")
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        eastmoney_concept_rows = self._call_eastmoney_sector_rows(
            ["m:90+t:3+f:!50", "m:90+t:3"],
            diagnostics=diagnostics,
            source_label="eastmoney-sector",
        )
        eastmoney_concept_sectors = self._parse_sector_rows(eastmoney_concept_rows, "eastmoney-sector")
        if eastmoney_concept_sectors:
            if not self._publish_sector_rows(
                eastmoney_concept_rows, sector_rows_out, cancel_event, deadline, diagnostics
            ):
                return []
            return _dedupe_sectors(eastmoney_concept_sectors, 10)
        diagnostics.append("eastmoney-sector controlled backup returned no valid rows.")
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        eastmoney_industry_rows = self._call_eastmoney_sector_rows(
            ["m:90+t:2+f:!50", "m:90+t:2"],
            diagnostics=diagnostics,
            source_label="eastmoney-industry-sector",
        )
        eastmoney_industry_sectors = self._parse_sector_rows(eastmoney_industry_rows, "eastmoney-industry-sector")
        if eastmoney_industry_sectors:
            if not self._publish_sector_rows(
                eastmoney_industry_rows, sector_rows_out, cancel_event, deadline, diagnostics
            ):
                return []
            return _dedupe_sectors(eastmoney_industry_sectors, 10)
        diagnostics.append("eastmoney-industry-sector controlled backup returned no valid rows.")
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        ths_hot_topic_rows = self._fetch_ths_hot_topic_rows()
        if ths_hot_topic_rows:
            # Hot-reason rows are useful topic candidates, but their gains are
            # individual stock moves. Do not present them as board quote pct.
            self._publish_sector_rows(
                ths_hot_topic_rows,
                sector_rows_out,
                cancel_event,
                deadline,
                diagnostics,
            )
        else:
            diagnostics.append("ths-hot-reason strong-topic source returned no valid rows.")
        return []

    def _call_cls_hot_plate_sectors(
        self,
        diagnostics: list[str],
        sector_rows_out: list[dict] | None,
        deadline: float | None,
        cancel_event: Event | None,
    ) -> list[SectorMover]:
        try:
            return self._fetch_cls_hot_plate_sectors(
                diagnostics,
                sector_rows_out=sector_rows_out,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except TypeError:
            return self._fetch_cls_hot_plate_sectors(diagnostics)

    def _fetch_cls_hot_plate_sectors(
        self,
        diagnostics: list[str],
        *,
        sector_rows_out: list[dict] | None = None,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> list[SectorMover]:
        try:
            payload = cls_request_json(
                self.requester,
                CLS_HOT_PLATE_URL,
                params={"type": "industry,concept,area", "way": "change", "rever": 1},
                timeout=self._sector_request_timeout(max_seconds=2.0),
            )
        except Exception as exc:
            diagnostics.append(f"cls-hot-plate strong-sector source failed: {exc}")
            return []
        rows = _sector_rows_from_cls_hot_plate(payload)
        sectors = self._parse_sector_rows(rows, "cls-hot-plate")
        if sectors and not self._publish_sector_rows(
            rows, sector_rows_out, cancel_event, deadline, diagnostics
        ):
            return []
        return sectors

    def _parse_sector_rows(self, rows: list[dict], source: str) -> list[SectorMover]:
        sectors: list[SectorMover] = []
        for row in rows:
            name = str(row.get("f14") or row.get("name") or row.get("板块") or "").strip()
            if not name:
                continue
            change_pct = _normalize_sector_change_pct(row)
            if change_pct is None:
                continue
            leader = str(row.get("f140") or row.get("f128") or row.get("leading_symbol") or "").strip() or None
            sectors.append(
                SectorMover(
                    name=name,
                    change_pct=change_pct,
                    leading_symbol=normalize_symbol(leader) if leader else None,
                    source=source,
                )
            )
        return sectors

    def _fetch_live_breadth(
        self,
        diagnostics: list[str],
        *,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> MarketBreadth | None:
        fetchers: list[tuple[str, Callable[[], MarketBreadth | None]]] = [
            (
                "财联社涨跌分布",
                lambda: self._call_cls_breadth(diagnostics, deadline, cancel_event),
            ),
            (
                "同花顺市场总览",
                lambda: self._call_ths_market_summary_breadth(diagnostics, deadline, cancel_event),
            ),
            (
                "同花顺涨跌分布",
                lambda: self._fetch_ths_indexflash_breadth(diagnostics, deadline, cancel_event),
            ),
            ("Sina 批量实时个股", self._fetch_sina_breadth),
            ("Tencent 批量实时个股", lambda: self._fetch_tencent_breadth(diagnostics)),
            ("AKShare 实时个股", lambda: self._fetch_akshare_breadth_with_timeout(diagnostics)),
            ("重型公开行情爬虫", lambda: self._fetch_heavy_breadth(diagnostics)),
        ]
        if self.allow_eastmoney_breadth_fallback:
            fetchers.append(("东方财富轻量 spot 兜底", lambda: self._fetch_eastmoney_breadth(diagnostics)))
        local_symbol_count: int | None = None
        for label, fetcher in fetchers:
            if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "market-breadth"):
                return None
            try:
                breadth = fetcher()
            except Exception as exc:
                diagnostics.append(f"{label}红绿家数读取失败：{exc}")
                continue
            if breadth is None:
                continue
            if breadth.source == "cls-quote-breadth" and breadth.total > 0:
                return breadth
            if breadth.total >= MIN_FULL_MARKET_BREADTH_TOTAL:
                return breadth
            if local_symbol_count is None:
                coverage_count = self._coverage_symbol_count()
                if coverage_count < 0:
                    diagnostics.append(
                        "本地股票池计数缓存未热（已转后台预热，不在红绿家数预算内现算全仓扫描），本轮不参与完整性校验。"
                    )
                    coverage_count = 0
                local_symbol_count = max(self._latest_local_symbol_count(), coverage_count)
            if self._breadth_is_complete(breadth, local_symbol_count, diagnostics):
                return breadth
        return None

    def _call_cls_breadth(
        self,
        diagnostics: list[str],
        deadline: float | None,
        cancel_event: Event | None,
    ) -> MarketBreadth | None:
        try:
            return self._fetch_cls_breadth(diagnostics, deadline=deadline, cancel_event=cancel_event)
        except TypeError:
            return self._fetch_cls_breadth()

    def _fetch_cls_breadth(
        self,
        diagnostics: list[str] | None = None,
        *,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> MarketBreadth | None:
        diagnostics = diagnostics if diagnostics is not None else []
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "market-breadth"):
            return None
        payload = self._fetch_cls_home_payload(
            timeout=self._breadth_request_timeout(),
            deadline=deadline,
        )
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        breadth = _breadth_from_cls_home_data(data)
        if breadth is None:
            diagnostics.append("CLS home breadth source has no valid up_down_dis field.")
        return breadth

    def _fetch_ths_indexflash_breadth(
        self,
        diagnostics: list[str],
        deadline: float | None,
        cancel_event: Event | None,
    ) -> MarketBreadth | None:
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "market-breadth"):
            return None
        remaining = (
            deadline - monotonic_time.monotonic()
            if deadline is not None
            else self._breadth_request_timeout()
        )
        request_timeout = min(self._breadth_request_timeout(), remaining)
        # Leave a short window for the signed XHR after Chameleon creates its
        # per-request cookie.  Do not spawn a browser or retain cookies.
        cookie_timeout = min(self._breadth_request_timeout(), max(0.0, remaining - 0.2))
        if cookie_timeout < 0.2 or request_timeout <= 0:
            diagnostics.append("同花顺涨跌分布剩余预算不足，已跳过。")
            return None
        try:
            getter = self.ths_cookie_getter or read_ths_browser_cookie
            cookie = getter(cookie_timeout)
        except Exception as exc:
            diagnostics.append(f"同花顺涨跌分布校验失败：{exc}")
            return None
        if not cookie:
            diagnostics.append("同花顺涨跌分布校验未在预算内完成。")
            return None

        headers = {**THS_MARKET_SCORE_HEADERS, "Cookie": cookie}
        urls = sorted(THS_MARKET_INDEXFLASH_URLS, key=lambda url: not url.startswith("https://"))
        for url in urls:
            if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "market-breadth"):
                return None
            remaining = (
                deadline - monotonic_time.monotonic()
                if deadline is not None
                else self._breadth_request_timeout()
            )
            if remaining <= 0:
                return None
            try:
                response = self.requester(
                    url,
                    timeout=min(self._breadth_request_timeout(), remaining),
                    headers=headers,
                )
                response.raise_for_status()
                payload = response.json() or {}
            except Exception as exc:
                diagnostics.append(f"同花顺涨跌分布请求失败：{exc}")
                continue
            result = payload.get("result") if isinstance(payload, dict) else None
            distribution = result.get("zdfb_data") if isinstance(result, dict) else None
            up = _parse_int(distribution.get("znum")) if isinstance(distribution, dict) else None
            down = _parse_int(distribution.get("dnum")) if isinstance(distribution, dict) else None
            if up is None or down is None:
                diagnostics.append("同花顺涨跌分布响应缺少 znum/dnum。")
                continue
            return MarketBreadth(
                up=up,
                down=down,
                flat=0,
                total=up + down,
                source="ths-indexflash-breadth",
            )
        return None

    def _breadth_is_complete(
        self,
        breadth: MarketBreadth,
        local_symbol_count: int,
        diagnostics: list[str],
    ) -> bool:
        if is_valid_full_market_breadth(breadth, local_symbol_count):
            return True
        ratio_text = (
            f"，本地股票池={local_symbol_count}，比例={breadth.total / local_symbol_count:.1%}"
            if local_symbol_count > 0
            else "，本地股票池不可用"
        )
        diagnostics.append(
            f"全市场红绿家数不完整：source={breadth.source} total={breadth.total}{ratio_text}，已判定该来源失败。"
        )
        return False

    def _fetch_sina_breadth(self) -> MarketBreadth | None:
        deadline = monotonic_time.monotonic() + self.breadth_time_budget
        symbols = self._latest_local_symbols()
        if not symbols:
            return None
        sina_symbols = [sina_symbol for symbol in symbols if (sina_symbol := self._sina_stock_symbol(symbol))]
        if not sina_symbols:
            return None

        up = 0
        down = 0
        flat = 0
        seen: set[str] = set()
        for start in range(0, len(sina_symbols), SINA_BREADTH_BATCH_SIZE):
            remaining = deadline - monotonic_time.monotonic()
            if remaining <= 0:
                return None
            batch = sina_symbols[start : start + SINA_BREADTH_BATCH_SIZE]
            try:
                response = self.requester(
                    "https://hq.sinajs.cn/list=" + ",".join(batch),
                    timeout=min(self.timeout, max(0.2, remaining)),
                    headers=SINA_HEADERS,
                )
                response.raise_for_status()
            except Exception:
                logger.warning("silent failure in _fetch_sina_breadth", exc_info=True)
                return None
            raw_content = getattr(response, "content", b"")
            if raw_content:
                text = raw_content.decode("gbk", errors="ignore")
            else:
                response.encoding = response.encoding or "gbk"
                text = response.text
            decoded = _decode_sina_response(text)
            if not decoded:
                continue
            for sina_symbol in batch:
                values = decoded.get(sina_symbol, [])
                if len(values) < 4:
                    continue
                previous_close = _parse_float(values[2])
                last = _parse_float(values[3])
                if previous_close is None or previous_close <= 0 or last is None or last <= 0:
                    continue
                symbol = normalize_symbol(sina_symbol[-6:])
                if symbol in seen:
                    continue
                seen.add(symbol)
                if last > previous_close:
                    up += 1
                elif last < previous_close:
                    down += 1
                else:
                    flat += 1
        total = up + down + flat
        if total == 0:
            return None
        return MarketBreadth(
            up=up,
            down=down,
            flat=flat,
            total=total,
            source="sina-a-share-live",
        )

    def _breadth_request_timeout(self) -> float:
        timeout = self.breadth_source_timeout
        if self.breadth_time_budget is not None:
            timeout = min(timeout, max(0.2, self.breadth_time_budget))
        return min(self.timeout, timeout)

    def _latest_local_symbols(self) -> list[str]:
        try:
            latest = self.warehouse.read_latest_daily_bars(days=1)
        except Exception:
            logger.warning("silent failure in _latest_local_symbols", exc_info=True)
            return []
        if latest.empty or "symbol" not in latest.columns:
            return []
        symbols: list[str] = []
        seen: set[str] = set()
        for value in latest["symbol"].dropna():
            symbol = normalize_symbol(str(value))
            if symbol and symbol not in seen:
                symbols.append(symbol)
                seen.add(symbol)
        return symbols

    def _sina_stock_symbol(self, symbol: str) -> str | None:
        return a_share_market_symbol(symbol)

    def _tencent_stock_symbol(self, symbol: str) -> str | None:
        return a_share_market_symbol(symbol)

    def _fetch_tencent_breadth(self, diagnostics: list[str]) -> MarketBreadth | None:
        deadline = monotonic_time.monotonic() + self.breadth_time_budget
        symbols = self._latest_local_symbols()
        if not symbols:
            return None
        quote_symbols = [quote for symbol in symbols if (quote := self._tencent_stock_symbol(symbol))]
        up = 0
        down = 0
        flat = 0
        seen: set[str] = set()
        for start in range(0, len(quote_symbols), SINA_BREADTH_BATCH_SIZE):
            remaining = deadline - monotonic_time.monotonic()
            if remaining <= 0:
                diagnostics.append("Tencent 批量实时个股红绿家数超时。")
                return None
            batch = quote_symbols[start : start + SINA_BREADTH_BATCH_SIZE]
            try:
                response = self.requester(
                    TENCENT_QUOTE_URL.format(symbols=",".join(batch)),
                    timeout=min(self.timeout, max(0.2, remaining)),
                    headers={"Referer": "https://stockapp.finance.qq.com/", "User-Agent": "Mozilla/5.0"},
                )
                response.raise_for_status()
            except Exception as exc:
                diagnostics.append(f"Tencent 批量实时个股请求失败：{exc}")
                return None
            response.encoding = response.encoding or "gbk"
            for segment in response.text.split(";"):
                if '="' not in segment or "~" not in segment:
                    continue
                key = segment.split("v_", 1)[-1].split("=", 1)[0].strip()
                values = segment.split("=", 1)[1].strip().strip('"').split("~")
                if len(values) < 5:
                    continue
                last = _parse_float(values[3])
                previous_close = _parse_float(values[4])
                symbol = normalize_symbol(key[-6:])
                if not symbol or symbol in seen or last is None or previous_close is None or previous_close <= 0:
                    continue
                seen.add(symbol)
                if last > previous_close:
                    up += 1
                elif last < previous_close:
                    down += 1
                else:
                    flat += 1
        total = up + down + flat
        if total == 0:
            return None
        return MarketBreadth(up=up, down=down, flat=flat, total=total, source="tencent-a-share-live")

    def _fetch_akshare_breadth(self, diagnostics: list[str]) -> MarketBreadth | None:
        try:
            import akshare as ak

            frame = ak.stock_zh_a_spot_em()
        except Exception as exc:
            diagnostics.append(f"AKShare 实时个股红绿家数读取失败：{exc}")
            return None
        if frame is None or frame.empty:
            diagnostics.append("AKShare 实时个股红绿家数返回空数据。")
            return None
        change_column = next((column for column in ["涨跌幅", "change_pct", "pct_chg"] if column in frame.columns), None)
        code_column = next((column for column in ["代码", "股票代码", "symbol", "code"] if column in frame.columns), None)
        if change_column is None or code_column is None:
            diagnostics.append("AKShare 实时个股红绿家数字段不完整。")
            return None
        data = frame[[code_column, change_column]].copy()
        data[code_column] = data[code_column].astype(str).map(normalize_symbol)
        data[change_column] = pd.to_numeric(data[change_column], errors="coerce")
        data = data.dropna(subset=[code_column, change_column])
        data = data[data[code_column].astype(str).str.fullmatch(r"\d{6}")]
        if data.empty:
            return None
        up = int((data[change_column] > 0).sum())
        down = int((data[change_column] < 0).sum())
        flat = int((data[change_column] == 0).sum())
        return MarketBreadth(up=up, down=down, flat=flat, total=up + down + flat, source="akshare-a-share-live")

    def _fetch_akshare_breadth_with_timeout(self, diagnostics: list[str]) -> MarketBreadth | None:
        timeout = self._breadth_request_timeout()
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._fetch_akshare_breadth, diagnostics)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            diagnostics.append(f"AKShare 实时个股红绿家数读取超时：{timeout:g}秒。")
            return None
        except Exception as exc:
            diagnostics.append(f"AKShare 实时个股红绿家数读取失败：{exc}")
            return None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _fetch_heavy_breadth(self, diagnostics: list[str]) -> MarketBreadth | None:
        if self._heavy_market_provider is None:
            self._heavy_market_provider = HeavyMarketCrawlerProvider(
                requester=self.requester,
                timeout=min(self.timeout, 1.2),
                browser_provider=BrowserMarketProvider(timeout=min(self.timeout, 2.5)),
            )
        breadth = self._heavy_market_provider.fetch_breadth()
        if breadth is None:
            diagnostics.append("重型公开行情爬虫未取得完整红绿家数。")
        return breadth

    def _fetch_eastmoney_breadth(self, diagnostics: list[str]) -> MarketBreadth | None:
        try:
            response = self.requester(
                EASTMONEY_A_SPOT_URL,
                timeout=min(self.timeout, 2.5),
                headers={"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
                params={
                    "pn": "1",
                    "pz": "6000",
                    "po": "1",
                    "np": "1",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f3",
                    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                    "fields": "f12,f14,f2,f3",
                },
            )
            response.raise_for_status()
            payload = response.json() or {}
        except Exception as exc:
            diagnostics.append(f"东方财富轻量 spot 红绿家数读取失败：{exc}")
            return None
        rows = payload.get("data", {}).get("diff", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list) or not rows:
            diagnostics.append("东方财富轻量 spot 红绿家数返回空数据。")
            return None
        up = 0
        down = 0
        flat = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = normalize_symbol(str(row.get("f12") or ""))
            change_pct = _parse_float(row.get("f3"))
            price = _parse_float(row.get("f2"))
            if not code or change_pct is None or price is None or price <= 0:
                continue
            if change_pct > 0:
                up += 1
            elif change_pct < 0:
                down += 1
            else:
                flat += 1
        total = up + down + flat
        if total == 0:
            diagnostics.append("东方财富轻量 spot 红绿家数字段校验后为空。")
            return None
        return MarketBreadth(up=up, down=down, flat=flat, total=total, source="eastmoney-a-share-spot")

    def _call_eastmoney_sector_rows(
        self,
        fs_values: str | list[str],
        diagnostics: list[str],
        source_label: str,
    ) -> list[dict]:
        try:
            return self._fetch_eastmoney_sector_rows(fs_values, diagnostics=diagnostics, source_label=source_label)
        except TypeError:
            # Compatibility for tests that monkeypatch the old one-argument helper.
            return self._fetch_eastmoney_sector_rows(fs_values)

    def _fetch_eastmoney_sector_rows(
        self,
        fs_values: str | list[str],
        diagnostics: list[str] | None = None,
        source_label: str = "eastmoney-sector",
    ) -> list[dict]:
        diagnostics = diagnostics if diagnostics is not None else []
        values = [fs_values] if isinstance(fs_values, str) else fs_values
        for url in EASTMONEY_SECTOR_URLS:
            for fs in values:
                payload = self._request_eastmoney_sector_payload(url, fs, diagnostics, source_label)
                rows = payload.get("data", {}).get("diff", []) if isinstance(payload, dict) else []
                valid = self._normalize_sector_rows(rows)
                if len(valid) >= MIN_CONTROLLED_BACKUP_SECTOR_ROWS:
                    diagnostics.append(
                        f"{source_label} controlled backup accepted host={url} fs={fs} valid_rows={len(valid)}."
                    )
                    return valid
                if rows:
                    diagnostics.append(
                        f"{source_label} controlled backup rejected host={url} fs={fs}: "
                        f"valid_rows={len(valid)} below_min={MIN_CONTROLLED_BACKUP_SECTOR_ROWS}."
                    )
        return []

    def _request_eastmoney_sector_payload(
        self,
        url: str,
        fs: str,
        diagnostics: list[str] | None = None,
        source_label: str = "eastmoney-sector",
    ) -> dict:
        try:
            response = self.requester(
                url,
                timeout=self._sector_request_timeout(max_seconds=2.5),
                headers={"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
                params={
                    "pn": "1",
                    "pz": "50",
                    "po": "1",
                    "np": "1",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f3",
                    "fs": fs,
                    "fields": "f2,f3,f4,f8,f12,f14,f20,f104,f105,f128,f136",
                },
            )
            response.raise_for_status()
            payload = response.json() or {}
        except Exception as exc:
            if diagnostics is not None:
                diagnostics.append(
                    f"{source_label} controlled backup request failed host={url} fs={fs}: {exc}"
                )
            return {}
        if not isinstance(payload, dict):
            if diagnostics is not None:
                diagnostics.append(
                    f"{source_label} controlled backup invalid payload host={url} fs={fs}: "
                    f"type={type(payload).__name__}"
                )
            return {}
        return payload

    def _normalize_sector_rows(self, rows: object) -> list[dict]:
        if not isinstance(rows, list):
            return []
        valid: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("f14") or "").strip()
            code = str(row.get("f12") or "").strip()
            if not name or not code:
                continue
            if _normalize_sector_change_pct(row) is None:
                continue
            row["_change_pct_unit"] = "percent"
            valid.append(row)
        return valid

    def _fetch_akshare_sector_rows(self, board_type: str) -> list[dict]:
        try:
            import akshare as ak

            if board_type == "concept":
                frame = ak.stock_board_concept_name_em()
            else:
                frame = ak.stock_board_industry_name_em()
        except Exception:
            logger.warning("silent failure in _fetch_akshare_sector_rows", exc_info=True)
            return []
        if frame is None or getattr(frame, "empty", True):
            return []
        rows: list[dict] = []
        for _, item in frame.head(50).iterrows():
            row = {
                "f12": item.get("板块代码") or item.get("代码") or item.get("code") or "",
                "f14": item.get("板块名称") or item.get("名称") or item.get("name") or "",
                "f3": item.get("涨跌幅") or item.get("change_pct") or item.get("涨跌幅%") or None,
                "f128": item.get("领涨股票") or item.get("领涨股") or "",
            }
            if _normalize_sector_change_pct(row) is not None and str(row["f14"]).strip():
                rows.append(row)
        return rows

    def _fetch_akshare_sector_rows_with_timeout(
        self,
        board_type: str,
        source: str,
        diagnostics: list[str],
    ) -> list[dict]:
        timeout = self._sector_request_timeout()
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._fetch_akshare_sector_rows, board_type)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            diagnostics.append(f"{source} strong-sector source timeout after {timeout:g}s.")
            return []
        except Exception as exc:
            diagnostics.append(f"{source} strong-sector source failed: {exc}")
            return []
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _fetch_sina_sectors(self) -> list[SectorMover]:
        try:
            response = self.requester(
                "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php",
                timeout=self._sector_request_timeout(),
                headers={
                    "Referer": "https://finance.sina.com.cn/",
                    "User-Agent": MINIMAL_USER_AGENT,
                },
            )
            response.raise_for_status()
        except Exception:
            logger.warning("silent failure in _fetch_sina_sectors", exc_info=True)
            return []
        response.encoding = response.encoding or "gbk"
        text = response.text
        rows: list[dict] = []
        for match in re.finditer(r'"[^"]+"\s*:\s*"([^"]+)"', text):
            values = match.group(1).split(",")
            if len(values) < 6:
                continue
            name = values[1].strip()
            pct_text = values[5].strip()
            leader = values[8].strip() if len(values) > 8 else ""
            if name and _parse_float(pct_text) is not None:
                rows.append(
                    {
                        "name": name,
                        "change_pct": pct_text,
                        "_change_pct_unit": "percent",
                        "leading_symbol": leader,
                    }
                )
        if rows:
            return self._parse_sector_rows(rows, "sina-sector")
        for chunk in text.split("},"):
            if "name:" not in chunk or "changepercent:" not in chunk:
                continue
            try:
                name = chunk.split("name:", 1)[1].split(",", 1)[0].strip("'\" ")
                pct_text = chunk.split("changepercent:", 1)[1].split(",", 1)[0].strip("'\" ")
                leader = ""
                if "symbol:" in chunk:
                    leader = chunk.split("symbol:", 1)[1].split(",", 1)[0].strip("'\" ")
                rows.append(
                    {
                        "name": name,
                        "change_pct": pct_text,
                        "_change_pct_unit": "percent",
                        "leading_symbol": leader,
                    }
                )
            except Exception:
                continue
        return self._parse_sector_rows(rows, "sina-sector")

    def _fetch_ths_hot_topic_rows(self) -> list[dict]:
        today = datetime.now().date()
        for offset in range(3):
            target = today - timedelta(days=offset)
            try:
                response = self.requester(
                    THS_HOT_TOPIC_URL.format(date=target.isoformat()),
                    timeout=self._sector_request_timeout(),
                    headers=THS_HOT_TOPIC_HEADERS,
                )
                response.raise_for_status()
                payload = response.json() or {}
            except Exception:
                continue
            if payload.get("errocode") not in (0, "0", None):
                continue
            rows = payload.get("data") or []
            if not isinstance(rows, list):
                continue
            if rows:
                return _aggregate_ths_hot_topic_rows(rows)
        return []

    def _fetch_ths_concept_section_rows(
        self,
        diagnostics: list[str] | None = None,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> list[dict]:
        diagnostics = diagnostics if diagnostics is not None else []
        try:
            response = self._request_public_html(
                THS_CONCEPT_SECTION_URL,
                timeout=self._sector_request_timeout(),
                source="ths-concept-section",
                diagnostics=diagnostics,
                deadline=deadline,
                headers=THS_HEADERS,
            )
        except Exception:
            logger.warning("silent failure in _fetch_ths_concept_section_rows", exc_info=True)
            if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
                return []
            return []
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        response.encoding = response.encoding or "gbk"
        soup = BeautifulSoup(response.text, "html.parser")
        node = soup.select_one("#gnSection")
        raw_value = str(node.get("value") or "") if node else ""
        if not raw_value:
            return []
        try:
            payload = json.loads(raw_value)
        except (TypeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []
        rows: list[dict] = []
        seen: set[str] = set()
        for item in payload.values():
            if not isinstance(item, dict):
                continue
            name = str(item.get("platename") or "").strip()
            board_code = str(item.get("platecode") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            rows.append(
                {
                    "code": board_code,
                    "f12": board_code,
                    "name": name,
                    "f14": name,
                    "change_pct": item.get("199112"),
                    "_change_pct_unit": "percent",
                    "_source": "ths-concept-section",
                }
            )
        rows.sort(key=lambda row: _normalize_sector_change_pct(row) or float("-inf"), reverse=True)
        return rows

    def _fetch_ths_industry_html_rows(
        self,
        max_pages: int = 3,
        diagnostics: list[str] | None = None,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> list[dict]:
        diagnostics = diagnostics if diagnostics is not None else []
        rows_out: list[dict] = []
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
                return []
            try:
                response = self._request_public_html(
                    THS_INDUSTRY_HTML_URL.format(page=page),
                    timeout=self._sector_request_timeout(),
                    source="ths-industry-html",
                    diagnostics=diagnostics,
                    deadline=deadline,
                    headers=THS_HEADERS,
                )
            except Exception:
                logger.warning("silent failure in _fetch_ths_industry_html_rows", exc_info=True)
                break
            if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
                return []
            response.encoding = response.encoding or "gbk"
            soup = BeautifulSoup(response.text, "html.parser")
            table_rows = soup.select("table.m-table.m-pager-table tbody tr")
            if not table_rows:
                break
            added = 0
            for row in table_rows:
                cells = [" ".join(cell.get_text(" ", strip=True).split()) for cell in row.find_all("td")]
                if len(cells) < 10:
                    continue
                links = row.find_all("a", href=True)
                board_code = None
                leader_symbol = None
                for link in links:
                    href = str(link.get("href") or "")
                    if board_code is None and "/thshy/detail/code/" in href:
                        board_code = _extract_code_from_href(href, THS_BOARD_CODE_RE)
                    if leader_symbol is None and "stockpage.10jqka.com.cn" in href:
                        leader_symbol = _extract_code_from_href(href, THS_STOCK_CODE_RE)
                dedupe_key = board_code or cells[1]
                if not dedupe_key or dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                rows_out.append(
                    {
                        "code": board_code or "",
                        "f12": board_code or "",
                        "name": cells[1],
                        "f14": cells[1],
                        "change_pct": cells[2],
                        "_change_pct_unit": "percent",
                        "leading_symbol": normalize_symbol(leader_symbol) if leader_symbol else None,
                        "up_count": _parse_int(cells[6]) or 0,
                        "down_count": _parse_int(cells[7]) or 0,
                    }
                )
                added += 1
            if added == 0:
                break
        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "strong-sector"):
            return []
        return rows_out

    def _call_ths_market_summary_breadth(
        self,
        diagnostics: list[str],
        deadline: float | None,
        cancel_event: Event | None,
    ) -> MarketBreadth | None:
        try:
            return self._fetch_ths_market_summary_breadth(
                diagnostics=diagnostics,
                deadline=deadline,
                cancel_event=cancel_event,
            )
        except TypeError:
            return self._fetch_ths_market_summary_breadth()

    def _fetch_ths_market_summary_breadth(
        self,
        diagnostics: list[str] | None = None,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> MarketBreadth | None:
        diagnostics = diagnostics if diagnostics is not None else []
        try:
            response = self._request_public_html(
                THS_MARKET_SUMMARY_URL,
                timeout=self.timeout,
                source="ths-market-summary",
                diagnostics=diagnostics,
                deadline=deadline,
                headers=THS_HEADERS,
            )
            if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "market-breadth"):
                return None
            response.encoding = response.encoding or "gbk"
            text = BeautifulSoup(response.text, "html.parser").get_text(" ", strip=True)
            match = THS_BREADTH_RE.search(text)
            if match:
                up, down, flat = (int(item) for item in match.groups())
                return MarketBreadth(
                    up=up,
                    down=down,
                    flat=flat,
                    total=up + down + flat,
                    source="ths-market-summary",
                )
        except Exception as exc:
            diagnostics.append(f"同花顺市场总览读取失败：{exc}")

        if self._source_chain_cancelled(cancel_event, deadline, diagnostics, "market-breadth"):
            return None
        industry_rows = self._call_ths_industry_html_rows(diagnostics, deadline, cancel_event)
        if not industry_rows:
            return None
        up = sum(int(row.get("up_count") or 0) for row in industry_rows)
        down = sum(int(row.get("down_count") or 0) for row in industry_rows)
        if up + down == 0:
            return None
        total = self._latest_local_symbol_count()
        if total >= up + down:
            flat = total - up - down
        else:
            flat = 0
            total = up + down
        return MarketBreadth(
            up=up,
            down=down,
            flat=flat,
            total=total,
            source="ths-market-summary",
        )

    def _latest_local_symbol_count(self) -> int:
        try:
            bars = self.warehouse.read_latest_daily_bars(days=3)
        except Exception:
            return 0
        if bars.empty or "trade_date" not in bars.columns:
            return 0
        data = bars.copy()
        data["trade_date"] = pd.to_datetime(data["trade_date"])
        latest_date = data["trade_date"].max()
        latest = data[data["trade_date"] == latest_date]
        return int(latest["symbol"].astype(str).nunique())

    def _coverage_symbol_count(self) -> int:
        """本地股票池规模（仓库侧 10 分钟 TTL 缓存 + 写入失效）。

        只吃缓存：缓存未热返回 -1（哨兵，调用方据此留 diagnostics）并触发
        后台预热，绝不阻塞行情链路；真实为 0（空仓）与"未热"必须可区分，
        否则排障信息会误导。"""
        cached = self.warehouse.cached_symbol_count()
        if cached is not None:
            return cached
        self._warm_symbol_count_in_background()
        return -1

    def _warm_symbol_count_in_background(self) -> None:
        if not self._symbol_count_warmup_lock.acquire(blocking=False):
            return  # 已有预热在跑
        def _warm() -> None:
            try:
                self.warehouse.refresh_symbol_count()
            except Exception:  # noqa: BLE001 - 预热失败不影响行情链路
                pass
            finally:
                self._symbol_count_warmup_lock.release()

        Thread(target=_warm, name="realtime-symbol-count-warmup", daemon=True).start()

    def _build_live_message(
        self,
        live_breadth: MarketBreadth | None,
        live_sectors: list[SectorMover],
        index_source: str | None = "ashare-sina",
    ) -> str:
        index_label = {
            "cls-quote-index": "财联社指数",
            "ashare-sina": "Ashare/Sina",
        }.get(index_source, "实时接口")
        breadth_label = {
            "cls-quote-breadth": "财联社涨跌分布",
            "ths-indexflash-breadth": "同花顺涨跌分布",
            "ths-market-summary": "同花顺市场总览",
            "sina-a-share-live": "新浪实时个股",
            "tencent-a-share-live": "腾讯实时个股",
            "akshare-a-share-live": "AKShare 实时个股",
            "heavy-market-crawler": "重型公开行情爬虫",
            "browser-market-provider": "浏览器公开行情爬虫",
            "eastmoney-a-share-spot": "东方财富轻量 spot 备选",
        }.get(live_breadth.source if live_breadth else None)
        sector_label = {
            "cls-hot-plate": "财联社热门板块",
            "ths-hot-reason": "同花顺热点归因",
            "ths-concept-section": "同花顺概念题材板块",
            "ths-industry-html": "同花顺行业板块总览",
            "eastmoney-sector": "东方财富概念板块备选",
            "eastmoney-industry-sector": "东方财富行业板块备选",
            "akshare-sector": "AKShare 概念板块备选",
            "akshare-industry-sector": "AKShare 行业板块备选",
            "sina-sector": "新浪行业板块",
        }.get(live_sectors[0].source if live_sectors else None)

        if sector_label and breadth_label:
            return f"实时指数来自{index_label}，强势题材来自{sector_label}，红绿家数来自{breadth_label}。"
        if sector_label:
            return f"实时指数来自{index_label}，强势题材来自{sector_label}，红绿家数暂不可用，未展示全市场宽度。"
        if breadth_label:
            return f"实时指数来自{index_label}，红绿家数来自{breadth_label}，强势题材暂不可用。"
        return f"实时指数来自{index_label}，红绿家数与强势题材暂不可用，已保留可用的最近数据。"

    def _snapshot_from_local(
        self,
        now: datetime,
        *,
        sector_rows: list[dict] | None = None,
        skip_topic_fetch: bool = False,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> RealtimeMarketSnapshot:
        try:
            bars = self.warehouse.read_latest_daily_bars(days=3)
        except Exception as exc:
            return RealtimeMarketSnapshot(
                status="unavailable",
                source="local",
                updated_at=now,
                market_phase=_market_phase(now),
                message=f"实时行情不可用，本地数据读取失败：{exc}",
                diagnostics=[f"本地最近交易日读取失败：{exc}"],
            )
        if bars.empty:
            return RealtimeMarketSnapshot(
                status="unavailable",
                source="local",
                updated_at=now,
                market_phase=_market_phase(now),
                message="实时行情不可用，本地历史数据为空。",
                diagnostics=["本地最近交易日为空，无法生成兜底快照。"],
            )

        data = bars.copy()
        data["trade_date"] = pd.to_datetime(data["trade_date"])
        recent_dates = sorted(data["trade_date"].drop_duplicates().tolist())[-3:]
        latest_date = recent_dates[-1]
        previous_date = recent_dates[-2] if len(recent_dates) >= 2 else None
        prior_date = recent_dates[-3] if len(recent_dates) >= 3 else None
        latest = self._with_previous_close(data, latest_date, previous_date)
        yesterday = (
            self._with_previous_close(data, previous_date, prior_date)
            if previous_date is not None and prior_date is not None
            else pd.DataFrame()
        )

        up = int((latest["change_pct"] > 0).sum())
        down = int((latest["change_pct"] < 0).sum())
        flat = int((latest["change_pct"] == 0).sum())
        breadth = MarketBreadth(up=up, down=down, flat=flat, total=int(len(latest)), source="local-latest")
        diagnostics = [f"已使用本地最近交易日 {latest_date.date()} 作为兜底快照。"]
        coverage_symbol_count = self._coverage_symbol_count()
        if coverage_symbol_count < 0:
            diagnostics.append("本地股票池计数缓存未热（后台预热中），本轮无法校验兜底宽度的完整性。")
            coverage_symbol_count = 0
        if not is_valid_full_market_breadth(breadth, coverage_symbol_count):
            ratio_text = (
                f"，本地股票池={coverage_symbol_count}，比例={breadth.total / coverage_symbol_count:.1%}"
                if coverage_symbol_count > 0
                else "，本地股票池不可用"
            )
            diagnostics.append(
                f"全市场红绿家数不完整：source=local-latest total={breadth.total}{ratio_text}，已隐藏该宽度统计。"
            )
            breadth = None

        pseudo_index = MarketIndexQuote(
            symbol="local-market",
            name="本地全市场",
            last=float(latest["close"].mean()),
            previous_close=float(latest["previous_close"].mean()),
            change=float(latest["close"].mean() - latest["previous_close"].mean()),
            change_pct=float(latest["change_pct"].mean()),
            source="local-latest",
            updated_at=now,
        )
        local_sectors = self._local_market_groups(
            latest,
            source="local-market-group",
            sector_rows=sector_rows,
            skip_topic_fetch=skip_topic_fetch,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        yesterday_sectors = self._local_market_groups(
            yesterday,
            source="local-yesterday-group",
            sector_rows=sector_rows,
            skip_topic_fetch=skip_topic_fetch,
            deadline=deadline,
            cancel_event=cancel_event,
        )
        message = _append_yesterday_sector_note(
            f"实时行情源暂不可用，已使用本地最近交易日 {latest_date.date()} 数据。",
            yesterday_sectors,
        )
        return RealtimeMarketSnapshot(
            status="stale",
            source="local-latest",
            updated_at=now,
            market_phase=_market_phase(now),
            indexes=[pseudo_index],
            breadth=breadth,
            strong_sectors=local_sectors,
            yesterday_strong_sectors=yesterday_sectors,
            message=message,
            diagnostics=diagnostics,
        )

    def _with_previous_close(
        self,
        data: pd.DataFrame,
        target_date: pd.Timestamp,
        previous_date: pd.Timestamp | None,
    ) -> pd.DataFrame:
        current = data[data["trade_date"] == target_date].copy()
        if previous_date is not None:
            previous = data[data["trade_date"] == previous_date][["symbol", "close"]].rename(
                columns={"close": "previous_close"}
            )
            current = current.merge(previous, on="symbol", how="left")
        if "previous_close" not in current:
            current["previous_close"] = current["open"]
        current["previous_close"] = current["previous_close"].fillna(current["open"])
        current["change_pct"] = (current["close"] / current["previous_close"]) - 1
        current["change_pct"] = current["change_pct"].replace([float("inf"), -float("inf")], pd.NA)
        return current

    def _local_market_groups(
        self,
        latest: pd.DataFrame,
        source: str,
        sector_rows: list[dict] | None = None,
        *,
        skip_topic_fetch: bool = False,
        deadline: float | None = None,
        cancel_event: Event | None = None,
    ) -> list[SectorMover]:
        sector_rows = [] if sector_rows is None else sector_rows
        if latest.empty:
            return []
        if not sector_rows:
            if skip_topic_fetch:
                return []
            sector_rows = self._fetch_ths_hot_topic_rows()
        if not sector_rows:
            return []
        rows: list[SectorMover] = []
        data = latest.copy()
        data["symbol"] = data["symbol"].astype(str).map(normalize_symbol)
        for board in sector_rows[:20]:
            board_code = str(board.get("f12") or board.get("code") or "").strip()
            board_name = str(board.get("f14") or board.get("name") or "").strip()
            members = [normalize_symbol(item) for item in board.get("members") or [] if str(item).strip()]
            if not board_name:
                continue
            if not members:
                if not board_code:
                    continue
                members = self._fetch_board_members(
                    board_code,
                    cancel_event=cancel_event,
                    deadline=deadline,
                )
            if not members:
                continue
            valid = data[data["symbol"].isin(members)].dropna(subset=["change_pct"])
            if valid.empty:
                continue
            leader = valid.sort_values("change_pct", ascending=False).iloc[0]
            rows.append(
                SectorMover(
                    name=board_name,
                    change_pct=float(valid["change_pct"].mean()),
                    leading_symbol=normalize_symbol(str(leader["symbol"])),
                    source=source,
                )
            )
        return sorted(rows, key=lambda item: item.change_pct, reverse=True)[:10]

    def _fetch_board_members(
        self,
        board_code: str,
        *,
        cancel_event: Event | None = None,
        deadline: float | None = None,
    ) -> list[str]:
        cached = self._sector_member_cache.get(board_code)
        if cached is not None:
            return cached
        if board_code.isdigit() and board_code.startswith("88"):
            members = self._fetch_ths_board_members(board_code)
        else:
            members = []
        # The freshly read members are always returned to the current worker,
        # but they are committed to the cross-request shared cache ONLY when the
        # request is still within budget AND the cancel event has not been set
        # (double-checked under the state lock to prevent TOCTOU races).
        if not self._context_expired(cancel_event, deadline):
            with self._state_lock:
                if cancel_event is None or not cancel_event.is_set():
                    self._sector_member_cache[board_code] = members
        return members

    def _fetch_ths_board_members(self, board_code: str) -> list[str]:
        try:
            response = self.requester(
                THS_INDUSTRY_DETAIL_URL.format(board_code=board_code),
                timeout=self.timeout,
                headers=THS_HEADERS,
            )
            response.raise_for_status()
        except Exception:
            logger.warning("silent failure in _fetch_ths_board_members", exc_info=True)
            return []
        response.encoding = response.encoding or "gbk"
        soup = BeautifulSoup(response.text, "html.parser")
        rows = soup.select("table.m-table.m-pager-table tbody tr")
        members: list[str] = []
        seen: set[str] = set()
        for row in rows:
            cells = [" ".join(cell.get_text(" ", strip=True).split()) for cell in row.find_all("td")]
            if len(cells) < 3:
                continue
            symbol = normalize_symbol(cells[1])
            if not symbol or not symbol.isdigit() or symbol in seen:
                continue
            seen.add(symbol)
            members.append(symbol)
        return members
