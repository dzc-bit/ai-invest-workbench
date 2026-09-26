from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import deque
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pandas as pd
import requests

from astock_backtester.ai import AiService
from astock_backtester.ai.errors import AiError
from astock_backtester.ai.models import AiChatRequest, AiConfigUpdate
from astock_backtester.ai.optimizer import (
    GridTooLargeError,
    build_optimize_insight_context,
    normalize_grid,
    run_optimization,
)
from astock_backtester.backtest_runner import run_configured_backtest
from astock_backtester.condition_parser import validate_condition_text, validate_exit_condition_text
from astock_backtester.data.briefing import MarketBriefingProvider
from astock_backtester.data.cache import LocalCache
from astock_backtester.data.capital_flow_crawler import CapitalFlowCrawler
from astock_backtester.data.cls_finance import ClsFinanceProvider
from astock_backtester.data.importer import read_daily_bars
from astock_backtester.data.market_commentary import (
    MarketCommentaryProvider,
    build_local_brief_commentary,
)
from astock_backtester.data.news import MarketNewsProvider
from astock_backtester.data.news_summary import MarketNewsSummaryProvider
from astock_backtester.data.operations import (
    build_daily_bars_coverage,
    fetch_capital_flow_into_cache,
    fetch_daily_bars_into_cache,
    import_daily_bars_into_cache,
    refresh_symbol_lifecycle,
)
from astock_backtester.data.providers import (
    ADataProvider,
    AkshareProvider,
    CompositeProvider,
    HttpAStockProvider,
    normalize_symbol,
)
from astock_backtester.data.realtime import RealtimeMarketProvider, unavailable_market_snapshot
from astock_backtester.data.risk import RiskAlertProvider
from astock_backtester.data.sync import SyncJobManager
from astock_backtester.data.warehouse import Warehouse
from astock_backtester.models import (
    BacktestSettings,
    ClsFinanceResponse,
    DataOperationResult,
    DatasetCoverage,
    ServiceHealth,
    ServiceLogEntry,
    StockSymbolValidationResult,
    StrategyConfig,
)
from astock_backtester.recommended_strategies import recommended_strategies

HEALTH_COVERAGE_WAIT_SECONDS = 0.1
HEALTH_COVERAGE_REFRESH_TTL_SECONDS = 60.0
BACKTEST_WARMUP_CALENDAR_DAYS = 120
# /ai/optimize 流的静默心跳：与 chat 流（facade.AI_STREAM_HEARTBEAT_SECONDS）同思路，
# 单个网格组合的回测 + AI 解读可静默数分钟，前端 120 秒空闲超时会把任务误杀成中断。
AI_OPTIMIZE_HEARTBEAT_SECONDS = 15.0
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


class ClientDisconnected(Exception):
    pass


class LocalDataUnavailable(ValueError):
    """Raised when the local warehouse/cache has no data for the request."""


def _stream_error_code(exc: Exception) -> str:
    """Stable machine-readable error codes consumed by the frontend."""
    if isinstance(exc, LocalDataUnavailable):
        return "no_local_data"
    if isinstance(exc, KeyError):
        return "payload_error"
    if isinstance(exc, ValueError):
        return "validation_error"
    return "request_failed"


class DataServiceState:
    def __init__(self, cache_dir: str | Path, port: int) -> None:
        self.cache = LocalCache(cache_dir)
        self.warehouse = Warehouse(cache_dir)
        self.akshare_provider = AkshareProvider()
        self.provider = CompositeProvider([HttpAStockProvider(), ADataProvider(), self.akshare_provider])
        self.capital_flow_crawler = CapitalFlowCrawler()
        self.sync_manager = SyncJobManager(
            warehouse=self.warehouse,
            provider=self.provider,
            cache=self.cache,
            capital_flow_fetcher=self._fetch_capital_flow,
        )
        self.realtime_provider = RealtimeMarketProvider(self.warehouse)
        self.news_provider = MarketNewsProvider()
        self.news_summary_provider = MarketNewsSummaryProvider(self.news_provider)
        self.briefing_provider = MarketBriefingProvider()
        self.finance_provider = ClsFinanceProvider()
        self.commentary_provider = MarketCommentaryProvider(
            self.realtime_provider,
            self.news_provider,
            briefing_provider=self.briefing_provider,
        )
        self.risk_provider = RiskAlertProvider(self.warehouse)
        self._ai_service: AiService | None = None
        self._ai_lock = Lock()
        self.port = port
        self.started_at = datetime.now(UTC)
        self.instance_id = str(uuid4())
        self.process_id = os.getpid()
        self.executable_path = str(Path(sys.executable).resolve())
        self.executable_sha256 = self._hash_executable(self.executable_path)
        self.logs: deque[dict[str, str]] = deque(maxlen=100)
        self._coverage_lock = Lock()
        self._coverage_refreshing = False
        # 写入发生在刷新进行中时置位：当前刷新结束后必须再跑一轮，
        # 保证"写入之后一定有一次以写入后数据为输入的刷新"。
        self._coverage_refresh_dirty = False
        self._coverage_snapshot = self._empty_coverage()
        self._coverage_refreshed_at: datetime | None = None
        self.log("info", "local data service started")

    def log(self, level: str, message: str) -> None:
        self.logs.appendleft(
            {
                "level": level,
                "message": message,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    def ai_service(self) -> AiService:
        """Lazy AI subsystem: nothing is constructed until an /ai/* route runs."""
        with self._ai_lock:
            if self._ai_service is None:
                self._ai_service = AiService(cache_dir=self.cache.root, backend=self, log=self.log)
            return self._ai_service

    def _fetch_capital_flow(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
        *,
        skip_eastmoney: bool = False,
    ) -> dict[str, Any]:
        return self.capital_flow_crawler.fetch_many_fund_flows(
            symbols,
            start_date,
            end_date,
            timeout=15,
            skip_eastmoney=skip_eastmoney,
        )

    def _hash_executable(self, path: str) -> str | None:
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return None

    def identity_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "cache_path": str(self.cache.root.resolve()),
            "port": self.port,
            "process_id": self.process_id,
            "executable_path": self.executable_path,
            "executable_sha256": self.executable_sha256,
            "started_at": self.started_at.isoformat(),
            "instance_id": self.instance_id,
        }

    def coverage_snapshot(self) -> list[DatasetCoverage]:
        with self._coverage_lock:
            return [item.model_copy(deep=True) for item in self._coverage_snapshot]

    def set_coverage_snapshot(self, items: list[DatasetCoverage]) -> None:
        """Adopt a coverage snapshot computed as part of a write operation.

        ``DataOperationResult.coverage`` is fresh (it is computed right after
        the rows land), so a route that just wrote data can hand it straight to
        the state instead of leaving ``/health`` to disagree with the response
        body until the next background refresh. Public API: callers must not
        touch ``_coverage_snapshot`` directly (§15.3).
        """
        if not items:
            return
        with self._coverage_lock:
            self._coverage_snapshot = [item.model_copy(deep=True) for item in items]
            self._coverage_refreshed_at = datetime.now(UTC)

    def health_payload(self) -> ServiceHealth:
        refresh_finished = self.start_coverage_refresh()
        if refresh_finished is not None:
            refresh_finished.wait(HEALTH_COVERAGE_WAIT_SECONDS)
        with self._coverage_lock:
            coverage_refreshing = self._coverage_refreshing
        return ServiceHealth(
            **self.identity_payload(),
            coverage=self.coverage_snapshot(),
            coverage_refreshing=coverage_refreshing,
        )

    def diagnostics_payload(self) -> dict[str, Any]:
        """Aggregate the last known success/failure state of the upstream sources.

        Only providers that keep persistent state are reported (realtime /
        news / finance); reading this endpoint never triggers a network fetch.
        """
        sources: list[dict[str, Any]] = []

        realtime: dict[str, Any] = {
            "source": "realtime",
            "ok": False,
            "seconds_since_success": None,
            "diagnostics": ["尚未有成功快照。"],
        }
        try:
            retained = self.realtime_provider.retained_successful_snapshot()
        except Exception as exc:
            retained = None
            realtime["diagnostics"] = [f"实时行情状态读取失败：{exc}"]
        if retained is not None:
            age = (datetime.now(UTC) - retained.updated_at).total_seconds()
            realtime.update(
                {
                    "ok": retained.status != "unavailable",
                    "status": retained.status,
                    "snapshot_source": retained.source,
                    "updated_at": retained.updated_at.isoformat(),
                    "seconds_since_success": max(0.0, age),
                    "diagnostics": list(retained.diagnostics),
                }
            )
        sources.append(realtime)

        for name, provider in (("news", self.news_provider), ("finance", self.finance_provider)):
            entry: dict[str, Any] = {"source": name, "ok": False, "diagnostics": ["尚未有成功拉取记录。"]}
            reader = getattr(provider, "recent_success", None)
            if callable(reader):
                try:
                    entry.update(reader())
                except Exception as exc:
                    entry["diagnostics"] = [f"状态读取失败：{exc}"]
            sources.append(entry)

        return {
            "ok": True,
            "generated_at": datetime.now(UTC).isoformat(),
            "sources": sources,
        }

    def _has_fresh_coverage_snapshot(self) -> bool:
        if not any(item.symbols > 0 for item in self._coverage_snapshot):
            return False
        if self._coverage_refreshed_at is None:
            return False
        age = (datetime.now(UTC) - self._coverage_refreshed_at).total_seconds()
        return age < HEALTH_COVERAGE_REFRESH_TTL_SECONDS

    def start_coverage_refresh(self, *, force: bool = False) -> Event | None:
        """Start a background coverage refresh unless one is already running.

        ``force`` (used right after a write) must never be answered by a
        snapshot read from *before* that write. When a refresh is already
        running, ``force`` only marks it dirty; the running loop re-reads the
        warehouse once more before it stops, so "写入之后一定有一次以写入后
        数据为输入的刷新". The returned event resolves only after every dirty
        round, so waiting on it guarantees a post-write read has happened.
        """
        with self._coverage_lock:
            if self._coverage_refreshing:
                if force:
                    self._coverage_refresh_dirty = True
                return None
            if not force and self._has_fresh_coverage_snapshot():
                return None
            self._coverage_refreshing = True
            self._coverage_refresh_dirty = False
        finished = Event()

        def refresh() -> None:
            try:
                while True:
                    coverage = self._read_coverage_snapshot()
                    with self._coverage_lock:
                        self._coverage_snapshot = [item.model_copy(deep=True) for item in coverage]
                        self._coverage_refreshed_at = datetime.now(UTC)
                        if self._coverage_refresh_dirty:
                            # 写入发生在本轮读取之后：清标记并再读一轮，
                            # 绝不把写前数据当最终快照。
                            self._coverage_refresh_dirty = False
                            continue
                        # 判定"无待补跑"与下调 refreshing 必须在同一把锁里，
                        # 否则夹在两者之间的 force 会置了 dirty 却没人消费。
                        self._coverage_refreshing = False
                        break
            finally:
                with self._coverage_lock:
                    self._coverage_refreshing = False
                finished.set()

        Thread(target=refresh, daemon=True).start()
        return finished

    def _read_coverage_snapshot(self) -> list[DatasetCoverage]:
        try:
            coverage = self.warehouse.coverage()
            if any(item.symbols > 0 for item in coverage):
                return coverage
        except Exception as exc:
            # 分区损坏会走到这里。回退到 cache 是为了不阻塞 UI，但必须让用户
            # 看到"这是损坏不是缺失"，否则会误以为数据没同步而去反复重拉。
            corrupt = getattr(self.warehouse, "corrupt_partitions", None) or {}
            if corrupt:
                self.log(
                    "error",
                    "数据仓分区损坏，无法读取 coverage（这不是数据缺失，请重建分区）："
                    + "；".join(f"{path}（{err}）" for path, err in corrupt.items()),
                )
            else:
                self.log("warning", f"warehouse coverage read failed; falling back to cache: {exc}")
        try:
            return self.cache.coverage()
        except Exception as exc:
            self.log("warning", f"cache coverage read failed; returning an empty snapshot: {exc}")
            return self._empty_coverage()

    def _empty_coverage(self) -> list[DatasetCoverage]:
        return [
            DatasetCoverage(dataset="daily_bars", symbols=0, start_date=None, end_date=None),
            DatasetCoverage(dataset="capital_flow", symbols=0, start_date=None, end_date=None),
            DatasetCoverage(dataset="market_cap", symbols=0, start_date=None, end_date=None),
        ]

    def sync_symbols(self, start_date: str | None, end_date: str | None) -> list[str]:
        try:
            symbols = self.warehouse.read_daily_symbols(require_ohlc=True)
            if symbols:
                return self._exclude_delisted_symbols(symbols)
        except Exception as exc:
            self.log(
                "warning",
                f"warehouse symbol read failed; falling back to provider: {exc}",
            )
        return self.provider.list_symbols()

    def _exclude_delisted_symbols(self, symbols: list[str]) -> list[str]:
        """Drop symbols whose lifecycle record marks them delisted; failures
        never block the sync (a missing lifecycle table just means no filter)."""
        try:
            delisted = self.warehouse.read_delisted_symbols()
        except Exception as exc:
            self.log("warning", f"symbol lifecycle read failed; keeping all warehouse symbols: {exc}")
            return symbols
        if not delisted:
            return symbols
        kept = [symbol for symbol in symbols if symbol not in delisted]
        self.log(
            "info",
            f"symbol lifecycle: excluding {len(symbols) - len(kept)} delisted symbols from the sync pool",
        )
        return kept

    def refresh_symbol_lifecycle_best_effort(self) -> dict[str, Any] | None:
        """Refresh ``symbol_lifecycle`` from the current-market source list.

        Runs right before a full-market sync so new listings get listing dates
        and confirmed delistings stop being re-fetched. Never raises: a flaky
        upstream only costs us the refresh, not the sync itself.
        """
        try:
            listings = self.provider.list_symbol_listings()
        except Exception as exc:
            self.log("warning", f"symbol lifecycle refresh skipped; source list unavailable: {exc}")
            return None
        try:
            summary = refresh_symbol_lifecycle(self.warehouse, listings)
        except Exception as exc:
            self.log("warning", f"symbol lifecycle refresh failed: {exc}")
            return None
        self.log(
            "info",
            "symbol lifecycle refreshed: "
            f"{summary.get('listed_upserts', 0)} listed / {summary.get('delisted_upserts', 0)} delisted",
        )
        return summary

    def validate_stock_symbols(self, symbols: list[str]) -> StockSymbolValidationResult:
        normalized_symbols = [normalize_symbol(symbol) for symbol in symbols]
        normalized_symbols = [symbol for symbol in normalized_symbols if symbol]
        source = "local-warehouse"
        known_symbols: list[str] = []
        try:
            known_symbols = self.warehouse.read_daily_symbols(require_ohlc=True)
        except Exception as exc:
            self.log("warning", f"warehouse symbol validation read failed: {exc}")
            known_symbols = []
        if not known_symbols:
            source = "provider-list"
            try:
                known_symbols = self.provider.list_symbols()
            except Exception as exc:
                self.log("warning", f"provider symbol validation read failed: {exc}")
                known_symbols = []

        known = {normalize_symbol(symbol) for symbol in known_symbols}
        valid_symbols = [symbol for symbol in normalized_symbols if symbol in known]
        invalid_symbols = [symbol for symbol in normalized_symbols if symbol not in known]
        return StockSymbolValidationResult(
            ok=not invalid_symbols,
            valid_symbols=valid_symbols,
            invalid_symbols=invalid_symbols,
            normalized_symbols=normalized_symbols,
            source=source,
        )


def _retained_realtime_snapshot(provider: Any, exc: Exception):
    retained = provider.retained_successful_snapshot()
    if retained is None:
        return None
    snapshot = retained.model_copy(deep=True)
    snapshot.status = "stale"
    snapshot.updated_at = datetime.now(UTC)
    snapshot.source = (
        snapshot.source
        if snapshot.source.endswith("+service-retained-last-success")
        else f"{snapshot.source}+service-retained-last-success"
    )
    snapshot.message = "实时行情接口暂不可用，沿用最近成功行情快照。"
    snapshot.diagnostics = [
        *snapshot.diagnostics,
        f"实时行情接口失败：{exc}",
        f"沿用最近成功行情快照：{retained.updated_at.isoformat()}。",
    ]
    return snapshot


def _require_ohlc_rows(frame: pd.DataFrame) -> pd.DataFrame:
    ohlc_columns = ["open", "high", "low", "close"]
    if frame.empty or not all(column in frame for column in ohlc_columns):
        return pd.DataFrame()
    return frame.dropna(subset=ohlc_columns).reset_index(drop=True)


def _backtest_read_start_date(settings: BacktestSettings) -> str:
    start = pd.Timestamp(settings.start_date) - pd.Timedelta(days=BACKTEST_WARMUP_CALENDAR_DAYS)
    return start.date().isoformat()


class DataServiceServer(ThreadingHTTPServer):
    def __init__(self, host: str, port: int, cache_dir: str | Path) -> None:
        super().__init__((host, port), DataServiceHandler)
        self.state = DataServiceState(cache_dir, self.server_address[1])


class DataServiceHandler(BaseHTTPRequestHandler):
    server: DataServiceServer

    # NDJSON 流是逐事件 write+flush 的小包；默认开着 Nagle 会让 Windows 回环
    # 把它们攒起来等延迟 ACK，表现为 AI 出字一顿一顿。
    disable_nagle_algorithm = True

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
            self.wfile.write(body)
        except CLIENT_DISCONNECT_ERRORS:
            return

    def _send_ndjson_headers(self, status: HTTPStatus = HTTPStatus.OK) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
        except CLIENT_DISCONNECT_ERRORS as exc:
            raise ClientDisconnected from exc

    def _write_ndjson(self, payload: dict[str, Any]) -> None:
        try:
            body = (
                json.dumps(self._jsonable(payload), ensure_ascii=False, default=str) + "\n"
            ).encode("utf-8")
            self.wfile.write(body)
            self.wfile.flush()
        except CLIENT_DISCONNECT_ERRORS as exc:
            raise ClientDisconnected from exc

    def _jsonable(self, value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if isinstance(value, dict):
            return {key: self._jsonable(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._jsonable(item) for item in value]
        if isinstance(value, tuple):
            return [self._jsonable(item) for item in value]
        return value

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _read_backtest_frame(self, settings: BacktestSettings) -> Any:
        symbols = settings.custom_symbols if settings.stock_pool == "custom" else None
        frame = self.server.state.warehouse.read_daily_bars(
            symbols=symbols,
            start_date=_backtest_read_start_date(settings),
            end_date=str(settings.end_date),
            require_ohlc=True,
        )
        if frame.empty:
            frame = _require_ohlc_rows(self.server.state.cache.read_daily_bars())
        if frame.empty:
            raise LocalDataUnavailable(
                "No cached daily bars found. Import or fetch data before running a configured backtest."
            )
        return frame

    def _run_backtest_stream(self, payload: dict[str, Any]) -> None:
        try:
            self._send_ndjson_headers()
            self._write_ndjson({"type": "phase", "phase": "校验参数"})
            strategy = StrategyConfig.model_validate(payload["strategy"])
            settings = BacktestSettings.model_validate(payload["settings"])
            self._write_ndjson({"type": "phase", "phase": "读取本地数据"})
            frame = self._read_backtest_frame(settings)
            self._write_ndjson({"type": "data_loaded", "rows": len(frame)})
            self._write_ndjson({"type": "phase", "phase": "计算指标与撮合交易"})

            def write_backtest_event(event: dict[str, Any]) -> None:
                payload = dict(event)
                trade = payload.get("trade")
                if trade is not None and hasattr(trade, "model_dump"):
                    payload["trade"] = trade.model_dump(mode="json")
                self._write_ndjson(payload)

            result = run_configured_backtest(
                frame,
                strategy,
                settings,
                on_event=write_backtest_event,
            )
            self._write_ndjson({"type": "phase", "phase": "生成结果"})
            self._write_ndjson({"type": "result", "result": result.model_dump(mode="json")})
        except ClientDisconnected:
            return
        except Exception as exc:
            self.server.state.log("error", str(exc))
            try:
                self._write_ndjson({"type": "error", "message": str(exc), "code": _stream_error_code(exc)})
            except ClientDisconnected:
                return

    def _run_realtime_snapshot_stream(self) -> None:
        try:
            self._send_ndjson_headers()
            for event in self.server.state.realtime_provider.market_snapshot_events():
                self._write_ndjson(event)
        except ClientDisconnected:
            return
        except Exception as exc:
            self.server.state.log("error", f"realtime market snapshot stream failed: {exc}")
            snapshot = _retained_realtime_snapshot(self.server.state.realtime_provider, exc)
            if snapshot is None:
                snapshot = unavailable_market_snapshot(
                    "实时行情接口暂不可用，已保留页面最近数据。",
                    diagnostics=[f"实时行情接口失败：{exc}"],
                )
            try:
                self._write_ndjson({"type": "error", "message": str(exc), "code": "request_failed"})
                self._write_ndjson({"type": "result", "snapshot": snapshot})
            except ClientDisconnected:
                return

    def _run_ai_chat_stream(self, payload: dict[str, Any]) -> None:
        try:
            request = AiChatRequest.model_validate(payload)
        except ValueError as exc:
            self.server.state.log("error", f"ai chat payload invalid: {exc}")
            self._send_json({"code": "validation_error", "message": f"AI 请求参数不合法：{exc}"}, HTTPStatus.BAD_REQUEST)
            return
        generator = self.server.state.ai_service().chat_stream(request)
        try:
            self._send_ndjson_headers()
            for event in generator:
                self._write_ndjson(event)
        except ClientDisconnected:
            return
        except AiError as exc:
            self.server.state.log("error", f"ai chat failed: {exc}")
            self._write_ai_error_event(generator, exc.code, str(exc))
        except Exception as exc:
            self.server.state.log("error", f"ai chat failed: {exc}")
            self._write_ai_error_event(generator, "request_failed", str(exc))

    def _write_ai_error_event(self, generator: Any, code: str, message: str) -> None:
        generator.close()
        try:
            self._write_ndjson({"type": "error", "code": code, "message": message})
        except ClientDisconnected:
            return

    def _run_ai_optimize_stream(self, payload: dict[str, Any]) -> None:
        """Grid-search the strategy's numeric knobs, then one AI commentary."""
        try:
            strategy = StrategyConfig.model_validate(payload["strategy"])
            settings = BacktestSettings.model_validate(payload["settings"])
            grid = normalize_grid(payload.get("grid") or {})
        except KeyError as exc:
            self._send_json({"code": "payload_error", "message": f"missing request field: {exc}"}, HTTPStatus.BAD_REQUEST)
            return
        except (ValueError, GridTooLargeError) as exc:
            code = "grid_too_large" if isinstance(exc, GridTooLargeError) else "validation_error"
            self._send_json({"code": code, "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        write_lock = Lock()
        stop_heartbeat = Event()

        def write_event(event: dict[str, Any]) -> None:
            with write_lock:
                self._write_ndjson(event)

        def beat() -> None:
            # 网格里单个组合的回测 + AI 解读可以静默几分钟；与 chat 流一样
            # 靠心跳保活，避免前端 120 秒空闲超时把正在跑的任务误杀成中断。
            while not stop_heartbeat.wait(AI_OPTIMIZE_HEARTBEAT_SECONDS):
                try:
                    with write_lock:
                        self._write_ndjson({"type": "heartbeat"})
                except ClientDisconnected:
                    return

        heartbeat_thread: Thread | None = None
        try:
            self._send_ndjson_headers()
            heartbeat_thread = Thread(target=beat, name="ai-optimize-heartbeat", daemon=True)
            heartbeat_thread.start()
            self._write_ndjson({"type": "phase", "phase": "读取本地数据"})
            frame = self._read_backtest_frame(settings)
            if frame.empty:
                raise LocalDataUnavailable("No cached daily bars found for the optimization range.")

            summary = run_optimization(frame, strategy, settings, grid, write_event)
            self._write_ndjson({"type": "phase", "phase": "生成 AI 解读"})
            insight: str | None = None
            insight_error: str | None = None
            try:
                ai_service = self.server.state.ai_service()
                response = ai_service.insight_oneshot(
                    "results_overview",
                    build_optimize_insight_context(summary),
                )
                insight = response.get("text")
            except Exception as exc:  # noqa: BLE001 - grid result stays useful without AI
                self.server.state.log("warning", f"ai optimize insight failed: {exc}")
                insight_error = str(exc)
            write_event(
                {
                    "type": "result",
                    "result": {**summary, "insight": insight, "insight_error": insight_error},
                }
            )
        except ClientDisconnected:
            return
        except Exception as exc:
            self.server.state.log("error", f"ai optimize failed: {exc}")
            try:
                write_event({"type": "error", "message": str(exc), "code": _stream_error_code(exc)})
            except ClientDisconnected:
                return
        finally:
            stop_heartbeat.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=1.0)

    _ALLOWED_REVEAL_ORIGINS = {
        "tauri://localhost",
        "https://tauri.localhost",
        "http://tauri.localhost",
        "http://127.0.0.1:1420",
        "http://localhost:1420",
    }

    def _reveal_request_authorized(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if not (host.startswith("127.0.0.1") or host.startswith("localhost")):
            return False
        origin = self.headers.get("Origin")
        if origin and origin.lower() not in self._ALLOWED_REVEAL_ORIGINS:
            return False
        return True

    def _run_ai_events_stream(self) -> None:
        generator = self.server.state.ai_service().events_stream()
        try:
            self._send_ndjson_headers()
            for event in generator:
                self._write_ndjson(event)
        except ClientDisconnected:
            return
        except Exception as exc:
            self.server.state.log("error", f"ai events stream failed: {exc}")
            generator.close()

    def do_OPTIONS(self) -> None:
        self._send_json({"ok": True})

    def do_GET(self) -> None:
        if self.path == "/ping":
            self._send_json({"ok": True})
            return
        if self.path == "/identity":
            self._send_json(self.server.state.identity_payload())
            return
        if self.path == "/health":
            health = self.server.state.health_payload()
            self._send_json(health.model_dump(mode="json"))
            return
        if self.path == "/diagnostics/sources":
            try:
                self._send_json(self.server.state.diagnostics_payload())
            except Exception as exc:
                self.server.state.log("error", f"diagnostics sources failed: {exc}")
                self._send_json(
                    {"ok": False, "code": "request_failed", "message": str(exc), "sources": []},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if self.path == "/diagnostics/data-gaps":
            """缺口画像：停更分布 / 疑似写入失败日 / 字段尾部（warehouse 缓存，只读不触发抓取）。"""
            try:
                profile = self.server.state.warehouse.data_gap_profile()
                # 分区损坏必须与"数据缺失"分开暴露：否则损坏会被当成缺口，
                # 反复触发全量重拉（历史上正是"补了又没补上"的成因之一）。
                corrupt = getattr(self.server.state.warehouse, "corrupt_partitions", None) or {}
                self._send_json(
                    {
                        "ok": bool(profile.get("available")),
                        "generated_at": datetime.now(UTC).isoformat(),
                        "profile": profile,
                        "warehouse_health": {
                            "corrupt_partitions": corrupt,
                            "healthy": not corrupt,
                        },
                    }
                )
            except Exception as exc:
                self.server.state.log("error", f"data gap profile failed: {exc}")
                # 画像失败时最需要 health：分区损坏正是 data_gap_profile() 抛异常的
                # 常见原因，若不在这里带上，前端只能看到 400、把损坏误判为缺失。
                corrupt = getattr(self.server.state.warehouse, "corrupt_partitions", None) or {}
                self._send_json(
                    {
                        "ok": False,
                        "code": "request_failed",
                        "message": str(exc),
                        "profile": {"available": False},
                        "warehouse_health": {
                            "corrupt_partitions": corrupt,
                            "healthy": not corrupt,
                        },
                    },
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if self.path == "/logs/recent":
            self._send_json({"items": list(self.server.state.logs)})
            return
        if self.path == "/realtime/market-snapshot/stream":
            self._run_realtime_snapshot_stream()
            return
        if self.path == "/realtime/market-snapshot":
            try:
                snapshot = self.server.state.realtime_provider.market_snapshot()
            except Exception as exc:
                self.server.state.log("error", f"realtime market snapshot failed: {exc}")
                snapshot = _retained_realtime_snapshot(self.server.state.realtime_provider, exc)
                if snapshot is None:
                    snapshot = unavailable_market_snapshot(
                        "实时行情接口暂不可用，已保留页面最近数据。",
                        diagnostics=[f"实时行情接口失败：{exc}"],
                    )
            self._send_json(snapshot.model_dump(mode="json"))
            return
        if self.path == "/market/news":
            news = self.server.state.news_provider.latest_news()
            self._send_json(news.model_dump(mode="json"))
            return
        if self.path == "/market/commentary":
            try:
                commentary = self.server.state.commentary_provider.current_commentary()
            except (TimeoutError, RuntimeError, requests.RequestException) as exc:
                self.server.state.log("error", f"market commentary failed: {exc}")
                commentary = build_local_brief_commentary(
                    diagnostics=[f"行情评价接口失败：{exc}"],
                )
            self._send_json(commentary.model_dump(mode="json"))
            return
        if self.path == "/market/finance":
            try:
                finance = self.server.state.finance_provider.current_board()
            except Exception as exc:
                self.server.state.log("error", f"market finance failed: {exc}")
                finance = ClsFinanceResponse(
                    updated_at=datetime.now(UTC),
                    diagnostics=[f"财联社看盘接口失败：{exc}"],
                )
            self._send_json(finance.model_dump(mode="json"))
            return
        if self.path == "/market/news-summary":
            summary = self.server.state.news_summary_provider.latest_summary()
            self._send_json(summary.model_dump(mode="json"))
            return
        if self.path == "/market/fupan":
            briefing = self.server.state.briefing_provider.latest_fupan()
            self._send_json(briefing.model_dump(mode="json"))
            return
        if self.path == "/market/zaopan":
            briefing = self.server.state.briefing_provider.latest_zaopan()
            self._send_json(briefing.model_dump(mode="json"))
            return
        if self.path == "/risk/alerts":
            alerts = self.server.state.risk_provider.current_alerts()
            self._send_json(alerts.model_dump(mode="json"))
            return
        if self.path == "/strategy/recommended":
            self._send_json(recommended_strategies(self.server.state.coverage_snapshot()).model_dump(mode="json"))
            return
        if self.path == "/ai/status":
            self._send_json(self.server.state.ai_service().status().model_dump(mode="json"))
            return
        if self.path == "/ai/news":
            self._send_json(self.server.state.ai_service().news_digest_view())
            return
        if self.path == "/ai/config":
            self._send_json(self.server.state.ai_service().config_view())
            return
        if self.path == "/ai/config/reveal":
            # Local-only endpoint so the desktop app can display the user's own key.
            # DNS-rebinding guard (Host) + webview-origin guard (Origin): a browser
            # page from a foreign origin is rejected even without PNA support.
            if not self._reveal_request_authorized():
                self._send_json({"code": "forbidden", "message": "仅限本机桌面端访问"}, HTTPStatus.FORBIDDEN)
                return
            self._send_json({"api_key": self.server.state.ai_service().reveal_api_key()})
            return
        if self.path == "/ai/events/stream":
            self._run_ai_events_stream()
            return
        if self.path == "/ai/sessions":
            self._send_json({"items": self.server.state.ai_service().list_sessions()})
            return
        if self.path == "/ai/memories":
            self._send_json(self.server.state.ai_service().list_memories())
            return
        if self.path.startswith("/ai/session"):
            query = parse_qs(urlsplit(self.path).query)
            session_id = str((query.get("session_id") or [""])[0])
            try:
                self._send_json(self.server.state.ai_service().session_view(session_id))
            except AiError as exc:
                self._send_json({"code": exc.code, "message": str(exc)}, HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self.server.state.log("error", f"ai session read failed: {exc}")
                self._send_json({"code": "request_failed", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if self.path == "/ai/reports":
            try:
                self._send_json(self.server.state.ai_service().list_reports())
            except Exception as exc:
                self.server.state.log("error", f"ai reports list failed: {exc}")
                self._send_json(
                    {"code": "request_failed", "message": str(exc), "items": []},
                    HTTPStatus.BAD_REQUEST,
                )
            return
        if self.path.startswith("/ai/report/file"):
            query = parse_qs(urlsplit(self.path).query)
            name = str((query.get("name") or [""])[0])
            try:
                self._send_json(self.server.state.ai_service().read_report(name))
            except AiError as exc:
                self._send_json({"code": exc.code, "message": str(exc)}, HTTPStatus.NOT_FOUND)
            except Exception as exc:
                self.server.state.log("error", f"ai report read failed: {exc}")
                self._send_json({"code": "request_failed", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if self.path.startswith("/sync/jobs/"):
            job_id = self.path.rsplit("/", 1)[-1]
            job = self.server.state.sync_manager.get_job(job_id)
            if job is None:
                self._send_json({"code": "not_found", "message": job_id}, HTTPStatus.NOT_FOUND)
                return
            self._send_json({"job": job.model_dump(mode="json")})
            return
        self._send_json({"code": "not_found", "message": self.path}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        try:
            payload = self._read_json()
        except CLIENT_DISCONNECT_ERRORS:
            return
        try:
            if self.path == "/coverage/daily-bars":
                symbols = payload.get("symbols")
                if not symbols:
                    self._send_json({"items": []})
                    return
                self._send_json(
                    build_daily_bars_coverage(
                        self.server.state.cache,
                        self.server.state.warehouse,
                        symbols=symbols,
                        start_date=payload.get("start_date"),
                        end_date=payload.get("end_date"),
                    ).model_dump(mode="json")
                )
                return
            if self.path == "/sync/full-market":
                start_date = payload.get("start_date", "2015-01-01")
                end_date = payload["end_date"]
                if not payload.get("symbols"):
                    self.server.state.refresh_symbol_lifecycle_best_effort()
                symbols = payload.get("symbols") or self.server.state.sync_symbols(start_date, end_date)
                if not symbols:
                    raise ValueError("No symbols available for full-market sync.")
                job = self.server.state.sync_manager.start_full_market(
                    symbols=symbols,
                    start_date=start_date,
                    end_date=end_date,
                )
                self.server.state.log(
                    "info",
                    f"Full-market sync {job.status}: {job.completed_symbols}/{job.total_symbols} symbols",
                )
                self._send_json({"job": job.model_dump(mode="json")})
                return
            if self.path == "/import/daily-bars":
                if payload.get("source") == "sample":
                    from astock_backtester.sample_data import sample_daily_bars

                    frame = sample_daily_bars()
                else:
                    frame = read_daily_bars(payload["path"])
                result = import_daily_bars_into_cache(
                    cache=self.server.state.cache,
                    warehouse=self.server.state.warehouse,
                    frame=frame,
                    source=str(payload.get("source", "file")),
                )
                for entry in result.logs:
                    self.server.state.log(entry.level, entry.message)
                if result.coverage:
                    self.server.state.set_coverage_snapshot(result.coverage)
                self.server.state.start_coverage_refresh(force=True)
                self._send_json(result.model_dump(mode="json"))
                return
            if self.path == "/fetch/daily-bars":
                result = fetch_daily_bars_into_cache(
                    cache=self.server.state.cache,
                    warehouse=self.server.state.warehouse,
                    fetcher=self._fetch_daily_bars_from_provider,
                    capital_flow_fetcher=self._fetch_capital_flow_from_crawler,
                    symbols=payload["symbols"],
                    start_date=payload["start_date"],
                    end_date=payload["end_date"],
                )
                for entry in result.logs:
                    self.server.state.log(entry.level, entry.message)
                if result.coverage:
                    self.server.state.set_coverage_snapshot(result.coverage)
                self.server.state.start_coverage_refresh(force=True)
                self._send_json(result.model_dump(mode="json"))
                return
            if self.path == "/fetch/capital-flow":
                symbols = payload.get("symbols") or []
                if not symbols:
                    symbols = self._capital_flow_backfill_symbols(payload["start_date"], payload["end_date"])
                    if not symbols:
                        # 缺口口径下"没活干"是正常结果（200），不是失败；
                        # 绝不启动 0 元素任务冒充已补齐（§9 缺口基准）。
                        # 响应体走 DataOperationResult.model_dump，与其它路径字段同构
                        # （前端 types.ts 的 filled_missing_rows 因此不会在这里缺席）。
                        self._send_json(
                            DataOperationResult(
                                status="ok",
                                imported_rows=0,
                                returned_rows=0,
                                filled_missing_rows=0,
                                requested_symbols=[],
                                fetched_symbols=[],
                                missing_symbols=[],
                                skipped_symbols=[],
                                coverage=self.server.state.coverage_snapshot(),
                                logs=[
                                    ServiceLogEntry(
                                        level="info",
                                        message="窗口内没有需要补齐的资金流缺口，未启动补齐任务。",
                                    )
                                ],
                                diagnostics=[
                                    {
                                        "code": "capital_flow_backfill_no_gaps",
                                        "source": "capital_flow_crawler",
                                        "start_date": payload["start_date"],
                                        "end_date": payload["end_date"],
                                        "requested_symbols": 0,
                                    }
                                ],
                                failures=[],
                            ).model_dump(mode="json")
                        )
                        return
                    job = self.server.state.sync_manager.start_capital_flow_backfill(
                        symbols=symbols,
                        start_date=payload["start_date"],
                        end_date=payload["end_date"],
                    )
                    self.server.state.log(
                        "info",
                        f"Capital-flow backfill {job.status}: {job.completed_symbols}/{job.total_symbols} symbols",
                    )
                    # 任务刚启动，实际补了多少行还没发生：filled_missing_rows 必须显式为 0，
                    # 不能让前端因为字段缺席而退回别的展示口径。响应体同样走模型保证字段同构。
                    response = DataOperationResult(
                        status="ok",
                        imported_rows=0,
                        returned_rows=0,
                        filled_missing_rows=0,
                        requested_symbols=symbols,
                        fetched_symbols=[],
                        missing_symbols=[],
                        skipped_symbols=[],
                        coverage=self.server.state.coverage_snapshot(),
                        logs=[
                            ServiceLogEntry(
                                level="info",
                                message=f"Capital-flow backfill started for {len(symbols)} symbols",
                            )
                        ],
                        diagnostics=[
                            {
                                "code": "capital_flow_backfill_job_started",
                                "source": "capital_flow_crawler",
                                "job_id": job.job_id,
                                "requested_symbols": len(symbols),
                            }
                        ],
                        failures=[],
                    ).model_dump(mode="json")
                    response["job"] = job.model_dump(mode="json")
                    self._send_json(response)
                    return
                result = fetch_capital_flow_into_cache(
                    cache=self.server.state.cache,
                    warehouse=self.server.state.warehouse,
                    capital_flow_fetcher=self._fetch_capital_flow_from_crawler,
                    symbols=symbols,
                    start_date=payload["start_date"],
                    end_date=payload["end_date"],
                )
                for entry in result.logs:
                    self.server.state.log(entry.level, entry.message)
                if result.coverage:
                    self.server.state.set_coverage_snapshot(result.coverage)
                self.server.state.start_coverage_refresh(force=True)
                self._send_json(result.model_dump(mode="json"))
                return
            if self.path.startswith("/sync/jobs/") and self.path.endswith("/cancel"):
                job_id = self.path.removesuffix("/cancel").rsplit("/", 1)[-1]
                job = self.server.state.sync_manager.cancel_job(job_id)
                if job is None:
                    self._send_json({"code": "not_found", "message": job_id}, HTTPStatus.NOT_FOUND)
                    return
                self.server.state.log("info", f"Sync job cancellation requested: {job_id}")
                self._send_json({"job": job.model_dump(mode="json")})
                return
            if self.path == "/run/backtest/stream":
                self._run_backtest_stream(payload)
                return
            if self.path == "/strategy/conditions/validate":
                mode = str(payload.get("mode", "entry")).strip().lower()
                if mode == "exit":
                    result = validate_exit_condition_text(str(payload.get("text", "")))
                else:
                    result = validate_condition_text(str(payload.get("text", "")))
                self._send_json(result.model_dump(mode="json"))
                return
            if self.path == "/symbols/validate":
                symbols = payload.get("symbols") or []
                if not isinstance(symbols, list):
                    raise ValueError("symbols must be a list")
                result = self.server.state.validate_stock_symbols([str(symbol) for symbol in symbols])
                self._send_json(result.model_dump(mode="json"))
                return
            if self.path == "/ai/config":
                request = AiConfigUpdate.model_validate(payload)
                self._send_json(self.server.state.ai_service().save_config(request.model_dump()))
                return
            if self.path == "/ai/conditions/parse":
                text = str(payload.get("text", "")).strip()
                if not text:
                    raise ValueError("自然语言条件不能为空。")
                if len(text) > 600:
                    raise ValueError("自然语言条件过长，请拆成多句或精简后重试。")
                self._send_json(self.server.state.ai_service().parse_conditions(text))
                return
            if self.path == "/ai/insight/oneshot":
                scene = str(payload.get("scene", "")).strip()
                if not scene:
                    raise ValueError("缺少点评场景 scene。")
                self._send_json(
                    self.server.state.ai_service().insight_oneshot(scene, payload.get("context"))
                )
                return
            if self.path == "/ai/overfit/check":
                self._send_json(self.server.state.ai_service().overfit_check(payload))
                return
            if self.path == "/ai/optimize":
                self._run_ai_optimize_stream(payload)
                return
            if self.path == "/ai/session/delete":
                session_id = str(payload.get("session_id", "")).strip()
                if not session_id:
                    raise ValueError("缺少要删除的 session_id。")
                deleted = self.server.state.ai_service().delete_session(session_id)
                self._send_json({"session_id": session_id, "deleted": deleted})
                return
            if self.path == "/ai/memory/update":
                memory_id = str(payload.get("id", "")).strip()
                if not memory_id:
                    raise ValueError("缺少要修改的记忆 id。")
                self._send_json(
                    self.server.state.ai_service().update_memory(
                        memory_id,
                        content=str(payload.get("content", "")),
                        category=payload.get("category"),
                        weight=payload.get("weight"),
                    )
                )
                return
            if self.path == "/ai/memory/delete":
                memory_id = str(payload.get("id", "")).strip()
                if not memory_id:
                    raise ValueError("缺少要删除的记忆 id。")
                self._send_json(self.server.state.ai_service().delete_memory(memory_id))
                return
            if self.path == "/sync/missing-only":
                # 「只补缺口」：补齐入口以缺口为基准（§9）——只对
                # incomplete_symbols 名单发起抓取，不做全量扫描。
                # 注意：incomplete_symbols 本身是一次窗口完整性快照（真实仓
                # 秒级~十几秒），这里**有意**同步执行——它是显式低频按钮
                # （前端 LONG_RUNNING 超时），且名单算出后抓取转后台作业；
                # 与 /health 那条"不得同步阻塞重型扫描"的红线不冲突，因为
                # 本端点不承载任何轮询/连接性判定。
                end_date = str(payload.get("end_date") or date.today().isoformat())
                start_date = str(payload.get("start_date") or (date.today() - timedelta(days=30)).isoformat())
                # 缺口名单前 best-effort 刷新 lifecycle（§9 同款：全市场同步入口
                # 也先刷新），真退市股先标 delisted 再算名单，避免每轮重复补
                # 停更股；失败只记日志、名单退回旧口径。
                self.server.state.refresh_symbol_lifecycle_best_effort()
                missing = self.server.state.sync_manager.incomplete_symbols(start_date, end_date)
                if not missing:
                    self._send_json(
                        {
                            "started": False,
                            "reason": "窗口内没有不完整的股票，无需补齐。",
                            "missing_symbols": 0,
                            "start_date": start_date,
                            "end_date": end_date,
                        }
                    )
                    return
                job = self.server.state.sync_manager.start_full_market(
                    symbols=missing,
                    start_date=start_date,
                    end_date=end_date,
                )
                self.server.state.log(
                    "info",
                    f"Missing-only sync {job.status}: {len(missing)} incomplete symbols",
                )
                self._send_json(
                    {
                        "started": True,
                        "missing_symbols": len(missing),
                        "start_date": start_date,
                        "end_date": end_date,
                        "job": job.model_dump(mode="json"),
                    }
                )
                return
            if self.path == "/ai/chat/stream":
                self._run_ai_chat_stream(payload)
                return
            self._send_json({"code": "not_found", "message": self.path}, HTTPStatus.NOT_FOUND)
        except LocalDataUnavailable as exc:
            self.server.state.log("error", str(exc))
            self._send_json({"code": "no_local_data", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
        except AiError as exc:
            self.server.state.log("error", f"ai request failed: {exc}")
            self._send_json({"code": exc.code, "message": str(exc)}, HTTPStatus.BAD_REQUEST)
        except ValueError as exc:
            self.server.state.log("error", str(exc))
            self._send_json({"code": "validation_error", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
        except KeyError as exc:
            self.server.state.log("error", str(exc))
            self._send_json(
                {"code": "payload_error", "message": f"missing request field: {exc}"},
                HTTPStatus.BAD_REQUEST,
            )
        except Exception as exc:
            self.server.state.log("error", str(exc))
            self._send_json({"code": "request_failed", "message": str(exc)}, HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _fetch_daily_bars_from_provider(self, symbols: list[str], start_date: str, end_date: str) -> pd.DataFrame:
        frames = [
            frame
            for symbol in symbols
            if not (frame := self.server.state.provider.fetch_daily_bars(symbol, start_date, end_date)).empty
        ]
        if not frames:
            return pd.DataFrame()
        return pd.concat(frames, ignore_index=True)

    def _fetch_capital_flow_from_crawler(self, symbols: list[str], start_date: str, end_date: str) -> dict[str, Any]:
        return self.server.state._fetch_capital_flow(symbols, start_date, end_date)

    def _capital_flow_backfill_symbols(self, start_date: str | None = None, end_date: str | None = None) -> list[str]:
        """Resolve the symbol list for an empty-symbols capital-flow backfill.

        四分支（缺口基准，§9）：

        1. 本地有 OHLC 且给了窗口，缺口非空 → 返回本地与缺口名单的交集；
        2. 同上但窗口内缺口为空 → 返回 ``[]``，**绝不**回落成全市场抓取
           （调用方据此返回 no-gaps diagnostics，不启动任务）；
        3. 本地没有任何 OHLC 行（bootstrap）→ 走 ``provider.list_symbols()``
           全量补齐，本地缺口口径无从谈起；
        4. 缺口检查抛异常 → 保守回落本地全量 + warning，不让一次读仓失败
           把补齐静默变成空操作。
        """
        local_symbols: set[str] = set()
        try:
            local_symbols.update(str(symbol) for symbol in self.server.state.warehouse.read_daily_symbols(require_ohlc=True))
        except Exception as exc:
            self.server.state.log(
                "warning",
                f"capital-flow symbol read failed from warehouse: {exc}",
            )
        if not local_symbols:
            # 分支 3：bootstrap。本地无日线行时缺口名单必然是空的，
            # 但那意味着"没数据"而不是"没缺口"，所以按数据源名单全量补。
            try:
                provider_symbols = {str(symbol) for symbol in self.server.state.provider.list_symbols()}
            except Exception as exc:
                self.server.state.log("warning", f"capital-flow provider symbol read failed: {exc}")
                provider_symbols = set()
            if not provider_symbols:
                raise ValueError("No symbols available for capital-flow backfill.")
            return sorted(provider_symbols)

        if start_date and end_date:
            try:
                missing_symbols = self.server.state.warehouse.read_capital_flow_missing_symbols(start_date, end_date)
            except Exception as exc:
                # 分支 4
                self.server.state.log("warning", f"capital-flow coverage inspection failed: {exc}")
                return sorted(local_symbols)
            if not missing_symbols:
                # 分支 2
                self.server.state.log(
                    "info",
                    f"capital-flow backfill skipped: 窗口 {start_date}~{end_date} 内无资金流缺口",
                )
                return []
            # 分支 1
            return sorted(local_symbols & missing_symbols)

        return sorted(local_symbols)


def create_server(host: str, port: int, cache_dir: str | Path) -> DataServiceServer:
    return DataServiceServer(host, port, cache_dir)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cache-dir", required=True)
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.cache_dir)
    server.serve_forever()


if __name__ == "__main__":
    main()
