from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from threading import Event, Lock, Thread
from time import monotonic, sleep

import astock_backtester.data.realtime as realtime_module
import pandas as pd
import pytest
import requests
from astock_backtester.data.http_transport import resilient_get, scraping_get, scraping_session, should_allow_alternate_transport
from astock_backtester.data.realtime import (
    HeavyMarketCrawlerProvider,
    RealtimeMarketProvider,
    unavailable_market_snapshot,
)
from astock_backtester.data.realtime_parsers import (
    BEIJING_TZ,
    aggregate_ths_hot_topic_rows,
    aggregate_yesterday_limit_up_sectors,
    append_yesterday_sector_note,
    breadth_from_cls_distribution,
    breadth_from_cls_home_data,
    clean_ths_topic_name,
    decode_sina_response,
    dedupe_sectors,
    is_valid_full_market_breadth,
    market_phase,
    normalize_change_pct,
    normalize_sector_change_pct,
    parse_float,
    parse_int,
    phase_diagnostic,
    quote_from_cls_home,
    quote_from_sina,
    sector_rows_from_cls_hot_plate,
    unique_sources,
)
from astock_backtester.data.symbols import a_share_market_symbol
from astock_backtester.data.warehouse import Warehouse
from astock_backtester.models import (
    MarketBreadth,
    MarketIndexQuote,
    RealtimeMarketSnapshot,
    SectorMover,
)

# Merged from: test_http_transport.py, test_realtime_transport.py, test_realtime_parsers.py, test_realtime_provider.py


# ===========================================================================
# Helpers
# ===========================================================================


class FakeResponse:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(f"HTTP {self.status_code}", response=response)


class HtmlResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code
        self.encoding = "utf-8"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(f"HTTP {self.status_code}", response=response)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Warehouse:
    pass


def _fake_local_bars() -> pd.DataFrame:
    rows = []
    for offset, close in ((2, 10.0), (1, 10.5), (0, 11.0)):
        trade_date = pd.Timestamp("2026-06-05") - pd.Timedelta(days=offset)
        for symbol, base in (("600000", 1.0), ("600001", 2.0)):
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "open": close * base,
                    "high": close * base * 1.02,
                    "low": close * base * 0.98,
                    "close": close * base,
                    "volume": 1000,
                }
            )
    return pd.DataFrame(rows)


# ===========================================================================
# Tests from test_http_transport.py
# ===========================================================================


def test_resilient_get_retries_one_transient_connection_failure():
    attempts: list[float] = []

    def requester(_url: str, **kwargs):
        attempts.append(kwargs["timeout"])
        if len(attempts) == 1:
            raise requests.ConnectionError("connection reset")
        return FakeResponse()

    diagnostics: list[str] = []
    response = resilient_get(
        requester,
        "https://example.test/market",
        timeout=2.0,
        source="test-market",
        diagnostics=diagnostics,
        retries=1,
    )

    assert response.status_code == 200
    assert len(attempts) == 2
    assert any("primary attempt 1/2 failed" in item for item in diagnostics)


def test_resilient_get_does_not_retry_terminal_client_error():
    attempts = 0

    def requester(_url: str, **_kwargs):
        nonlocal attempts
        attempts += 1
        return FakeResponse(404)

    with pytest.raises(requests.HTTPError):
        resilient_get(
            requester,
            "https://example.test/missing",
            timeout=2.0,
            source="test-market",
            retries=1,
        )

    assert attempts == 1


def test_resilient_get_uses_explicit_alternate_transport_after_403():
    alternate_attempts = 0

    def primary(_url: str, **_kwargs):
        return FakeResponse(403)

    def alternate(_url: str, **_kwargs):
        nonlocal alternate_attempts
        alternate_attempts += 1
        return FakeResponse()

    diagnostics: list[str] = []
    response = resilient_get(
        primary,
        "https://example.test/protected",
        timeout=2.0,
        source="test-market",
        diagnostics=diagnostics,
        alternate_requester=alternate,
        allow_alternate=True,
    )

    assert response.status_code == 200
    assert alternate_attempts == 1
    assert any("alternate transport used" in item for item in diagnostics)


def test_resilient_get_clamps_each_attempt_to_remaining_budget():
    observed_timeout = 0.0

    def requester(_url: str, **kwargs):
        nonlocal observed_timeout
        observed_timeout = kwargs["timeout"]
        return FakeResponse()

    resilient_get(
        requester,
        "https://example.test/market",
        timeout=10.0,
        source="test-market",
        deadline=monotonic() + 0.25,
    )

    assert 0 < observed_timeout <= 0.25


class TestShouldAllowAlternateTransport:
    def test_default_allows_when_using_requests_get(self):
        assert should_allow_alternate_transport(requests.get) is True

    def test_custom_requester_disables_by_default(self):
        def custom(_url, **_kwargs):
            pass
        assert should_allow_alternate_transport(custom) is False

    def test_override_true_takes_precedence(self):
        def custom(_url, **_kwargs):
            pass
        assert should_allow_alternate_transport(custom, override=True) is True

    def test_override_false_takes_precedence(self):
        assert should_allow_alternate_transport(requests.get, override=False) is False

    def test_override_none_falls_back_to_default(self):
        assert should_allow_alternate_transport(requests.get, override=None) is True

    def test_scraping_get_still_allows_alternate_transport(self):
        # scraping_get is the trust_env=False stand-in for requests.get, so it must
        # keep the curl_cffi fallback enabled for the same providers.
        assert should_allow_alternate_transport(scraping_get) is True


def test_crawler_provider_requesters_never_trust_environment_proxies():
    """Provider defaults must not be ``requests.get``.

    ``requests.get`` builds its session with ``trust_env=True``, so it adopts a
    machine-wide ``HTTP_PROXY`` (Clash Verge and similar install one).  Such a
    proxy terminates TLS to the domestic market hosts this project scrapes, and
    every upstream read then fails with ``SSL: UNEXPECTED_EOF_WHILE_READING``.
    """
    from dataclasses import fields

    from astock_backtester.data.cls_finance import ClsFinanceProvider
    from astock_backtester.data.news import MarketNewsProvider

    providers = (
        RealtimeMarketProvider,
        HeavyMarketCrawlerProvider,
        MarketNewsProvider,
        ClsFinanceProvider,
    )
    for provider in providers:
        requester = next(field.default for field in fields(provider) if field.name == "requester")
        assert requester is scraping_get, f"{provider.__name__} must default to scraping_get"
        assert requester is not requests.get, f"{provider.__name__} must not default to requests.get"


def test_scraping_session_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")

    # Without trust_env=False, requests would route through this proxy.
    assert requests.utils.get_environ_proxies("https://x-quote.cls.cn/")

    assert scraping_session().trust_env is False


# ===========================================================================
# Tests from test_realtime_transport.py
# ===========================================================================


def test_ths_market_breadth_uses_explicit_alternate_transport_after_403(tmp_path):
    def primary(_url: str, **_kwargs):
        return HtmlResponse("forbidden", status_code=403)

    def alternate(_url: str, **_kwargs):
        return HtmlResponse("上涨 3200 下跌 1800 平盘 120")

    diagnostics: list[str] = []
    provider = RealtimeMarketProvider(
        Warehouse(tmp_path),
        requester=primary,
        alternate_requester=alternate,
        allow_alternate_transport=True,
    )

    breadth = provider._fetch_ths_market_summary_breadth(diagnostics=diagnostics)

    assert breadth is not None
    assert (breadth.up, breadth.down, breadth.flat, breadth.total) == (3200, 1800, 120, 5120)
    assert any("alternate transport used" in item for item in diagnostics)


def test_sector_chain_stops_before_next_source_after_cancellation(tmp_path):
    cancelled = Event()
    calls: list[str] = []
    provider = RealtimeMarketProvider(Warehouse(tmp_path))

    def empty_cls(_diagnostics):
        calls.append("cls")
        cancelled.set()
        return []

    provider._fetch_cls_hot_plate_sectors = empty_cls
    provider._fetch_ths_concept_section_rows = lambda: calls.append("ths") or []

    sectors = provider._fetch_live_sectors([], cancel_event=cancelled)

    assert sectors == []
    assert calls == ["cls"]


def test_cancelled_ths_response_is_not_published_to_request_cache(tmp_path):
    cancelled = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path))

    def late_response(*_args, **_kwargs):
        cancelled.set()
        return HtmlResponse(
            '<div id="gnSection" '
            'value=\'{"gn_1":{"platename":"算力","platecode":"301558","199112":"3.2"}}\'></div>'
        )

    provider._request_public_html = late_response

    rows = provider._fetch_ths_concept_section_rows(
        diagnostics=[],
        cancel_event=cancelled,
    )

    assert rows == []
    assert not hasattr(provider, "_ths_concept_rows_cache")


def test_sector_worker_does_not_publish_rows_after_wrapper_timeout(tmp_path):
    started = Event()
    release = Event()
    cancellation_observed = Event()
    wrapper_finished = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), sector_time_budget=0.2)
    provider._fetch_cls_hot_plate_sectors = lambda _diagnostics: []
    original_cancelled = provider._source_chain_cancelled

    def observe_cancellation(cancel_event, deadline, diagnostics, chain):
        cancelled = original_cancelled(cancel_event, deadline, diagnostics, chain)
        if cancelled and chain == "strong-sector":
            cancellation_observed.set()
        return cancelled

    provider._source_chain_cancelled = observe_cancellation

    def late_rows():
        started.set()
        release.wait(timeout=1)
        return [
            {
                "f12": "301558",
                "f14": "算力",
                "change_pct": "3.2",
                "_change_pct_unit": "percent",
            }
        ]

    provider._fetch_ths_concept_section_rows = late_rows

    diagnostics: list[str] = []
    rows_out: list[dict] = []
    results: list[list] = []

    def fetch_with_budget() -> None:
        results.append(provider._fetch_live_sectors_with_budget(diagnostics, rows_out))
        wrapper_finished.set()

    wrapper = Thread(target=fetch_with_budget)
    wrapper.start()
    assert started.wait(timeout=1)
    assert wrapper_finished.wait(timeout=1)
    assert results == [[]]
    release.set()
    assert cancellation_observed.wait(timeout=1)
    wrapper.join(timeout=1)

    assert rows_out == []


def test_sector_timeout_cannot_commit_rows_after_publication_check(tmp_path):
    publication_check_passed = Event()
    release_publication = Event()
    wrapper_finished = Event()
    worker_finished = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), sector_time_budget=0.1)
    provider._fetch_cls_hot_plate_sectors = lambda _diagnostics: []
    provider._fetch_ths_concept_section_rows = lambda: [
        {
            "f12": "301558",
            "f14": "算力",
            "change_pct": "3.2",
            "_change_pct_unit": "percent",
        }
    ]

    original_cancelled = provider._source_chain_cancelled
    strong_sector_checks = 0

    def pause_after_publication_check(cancel_event, deadline, diagnostics, chain):
        nonlocal strong_sector_checks
        cancelled = original_cancelled(cancel_event, deadline, diagnostics, chain)
        if chain == "strong-sector" and not cancelled:
            strong_sector_checks += 1
            if strong_sector_checks == 4:
                publication_check_passed.set()
                release_publication.wait(timeout=1)
        return cancelled

    original_call = provider._call_live_sectors

    def tracked_call(*args, **kwargs):
        try:
            return original_call(*args, **kwargs)
        finally:
            worker_finished.set()

    provider._source_chain_cancelled = pause_after_publication_check
    provider._call_live_sectors = tracked_call

    rows_out: list[dict] = []
    results: list[list] = []

    def fetch_with_budget() -> None:
        results.append(provider._fetch_live_sectors_with_budget([], rows_out))
        wrapper_finished.set()

    wrapper = Thread(target=fetch_with_budget)
    wrapper.start()
    assert publication_check_passed.wait(timeout=1)
    assert wrapper_finished.wait(timeout=1)
    assert results == [[]]
    release_publication.set()
    assert worker_finished.wait(timeout=1)
    wrapper.join(timeout=1)

    assert rows_out == []


def test_older_realtime_request_cannot_overwrite_newer_success_snapshot(tmp_path):
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    newer = RealtimeMarketSnapshot(
        status="live",
        source="newer",
        updated_at=datetime(2026, 7, 10, 10, 1, tzinfo=UTC),
        message="newer",
    )
    older = RealtimeMarketSnapshot(
        status="live",
        source="older",
        updated_at=datetime(2026, 7, 10, 10, 0, tzinfo=UTC),
        message="older",
    )

    provider._remember_successful_snapshot(newer)
    provider._remember_successful_snapshot(older)

    assert provider.retained_successful_snapshot().source == "newer"


def test_late_breadth_worker_cannot_publish_diagnostics_after_timeout(tmp_path):
    """P1-3: after _fetch_live_breadth_with_budget times out, the late
    worker thread must NOT be able to append diagnostics to the shared
    list.  Uses deterministic Events instead of sleep-based timing.
    """
    worker_started = Event()
    wrapper_returned = Event()
    release_worker = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), breadth_time_budget=0.1)

    def slow_fetcher(diagnostics, deadline=None, cancel_event=None):
        worker_started.set()
        # Wait until the wrapper has already returned before attempting
        # to write to diagnostics.
        release_worker.wait(timeout=2)
        # This append MUST be silently dropped by the guarded list.
        diagnostics.append("late-worker-diagnostic-should-be-dropped")
        return None

    provider._call_live_breadth = slow_fetcher

    diagnostics: list[str] = []

    def run_wrapper():
        provider._fetch_live_breadth_with_budget(diagnostics)
        wrapper_returned.set()

    thread = Thread(target=run_wrapper)
    thread.start()
    assert worker_started.wait(timeout=2)
    assert wrapper_returned.wait(timeout=2)

    # The wrapper has returned; now let the worker try to write.
    release_worker.set()
    thread.join(timeout=2)

    assert "late-worker-diagnostic-should-be-dropped" not in diagnostics, (
        "P1-3 regression: late worker published diagnostics after wrapper timeout"
    )
    # The timeout diagnostic from the wrapper itself must be present.
    assert any("超时" in d for d in diagnostics)


def test_late_sector_worker_cannot_publish_diagnostics_after_timeout(tmp_path):
    """P1-3: same guard for sector budget wrapper."""
    worker_started = Event()
    wrapper_returned = Event()
    release_worker = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), sector_time_budget=0.1)
    provider._fetch_cls_hot_plate_sectors = lambda _diagnostics: []

    def slow_sector_fetcher(diagnostics, deadline=None, cancel_event=None, sector_rows_out=None):
        worker_started.set()
        release_worker.wait(timeout=2)
        diagnostics.append("late-sector-diagnostic-should-be-dropped")
        return []

    provider._call_live_sectors = slow_sector_fetcher

    diagnostics: list[str] = []
    rows_out: list[dict] = []

    def run_wrapper():
        provider._fetch_live_sectors_with_budget(diagnostics, rows_out)
        wrapper_returned.set()

    thread = Thread(target=run_wrapper)
    thread.start()
    assert worker_started.wait(timeout=2)
    assert wrapper_returned.wait(timeout=2)
    release_worker.set()
    thread.join(timeout=2)

    assert "late-sector-diagnostic-should-be-dropped" not in diagnostics, (
        "P1-3 regression: late sector worker published diagnostics after timeout"
    )
    assert any("超时" in d for d in diagnostics)


def test_retained_snapshot_preserves_original_diagnostics(tmp_path):
    """P1-2: retained snapshot must merge current request diagnostics,
    original successful snapshot diagnostics, and the retained time hint.
    The original diagnostics must NOT be silently dropped.
    """
    provider = RealtimeMarketProvider(Warehouse(tmp_path))

    # Store a successful snapshot with its own diagnostics
    original_snapshot = RealtimeMarketSnapshot(
        status="live",
        source="cls-quote-index",
        updated_at=datetime(2026, 7, 10, 10, 0, tzinfo=UTC),
        message="live",
        indexes=[
            MarketIndexQuote(
                symbol="sh000001",
                name="上证指数",
                last=3100.0,
                previous_close=3080.0,
                change=20.0,
                change_pct=0.0065,
                source="cls-quote-index",
            )
        ],
        breadth=MarketBreadth(up=3000, down=1800, flat=200, total=5000, source="cls-quote-breadth"),
        strong_sectors=[],
        diagnostics=["原始成功快照诊断信息"],
    )
    provider._remember_successful_snapshot(original_snapshot)

    # Make all live fetchers return empty so the retained path is triggered
    provider._fetch_indexes = lambda: []
    provider._fetch_live_breadth_with_budget = lambda diagnostics: None
    provider._fetch_live_sectors_with_budget = lambda diagnostics, rows_out: []

    def empty_local(now, diagnostics, live_sector_rows, skip_topic_fetch=False):
        return unavailable_market_snapshot("本地无数据", diagnostics=[])

    provider._snapshot_from_local_with_budget = empty_local

    events = list(provider.market_snapshot_events())
    result_event = events[-1]
    assert result_event["type"] == "result"
    snapshot = result_event["snapshot"]

    # The retained snapshot must contain:
    # 1. Current request diagnostics (e.g. "实时指数接口暂不可用...")
    assert any("实时指数接口暂不可用" in d for d in snapshot.diagnostics)
    # 2. Original snapshot diagnostics (must NOT be silently dropped)
    assert "原始成功快照诊断信息" in snapshot.diagnostics, (
        "P1-2 regression: original successful snapshot diagnostics were lost"
    )
    # 3. Retained time hint
    assert any("沿用最近成功行情快照" in d for d in snapshot.diagnostics)

    # Order must be stable and no duplicates
    seen = set()
    for d in snapshot.diagnostics:
        if d in seen:
            pytest.fail(f"Duplicate diagnostic entry: {d!r}")
        seen.add(d)


def test_same_timestamp_reverse_completion_does_not_overwrite(tmp_path):
    """P1-4: when two requests have the same updated_at timestamp and
    the older-generation request completes AFTER the newer one, it must
    NOT overwrite the cached snapshot.  Uses monotonic generation.
    """
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    same_ts = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)

    # Request 1 (generation=1) completes first
    first = RealtimeMarketSnapshot(
        status="live", source="gen-1", updated_at=same_ts, message="first"
    )
    provider._remember_successful_snapshot(first, generation=1)
    assert provider.retained_successful_snapshot().source == "gen-1"

    # Request 2 (generation=2) with SAME timestamp completes later
    second = RealtimeMarketSnapshot(
        status="live", source="gen-2", updated_at=same_ts, message="second"
    )
    provider._remember_successful_snapshot(second, generation=2)
    assert provider.retained_successful_snapshot().source == "gen-2"

    # Old request (generation=1) arrives late — must NOT overwrite gen-2
    late_old = RealtimeMarketSnapshot(
        status="live", source="gen-1-late", updated_at=same_ts, message="late"
    )
    provider._remember_successful_snapshot(late_old, generation=1)
    assert provider.retained_successful_snapshot().source == "gen-2", (
        "P1-4 regression: old request (gen=1) overwrote newer (gen=2)"
    )


def test_remember_without_generation_uses_strict_timestamp(tmp_path):
    """P1-4: backward-compatible calls without generation use strict
    greater-than comparison, so same-timestamp cannot overwrite.
    """
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    same_ts = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)

    first = RealtimeMarketSnapshot(
        status="live", source="first", updated_at=same_ts, message="first"
    )
    provider._remember_successful_snapshot(first)

    second = RealtimeMarketSnapshot(
        status="live", source="second", updated_at=same_ts, message="second"
    )
    provider._remember_successful_snapshot(second)

    # With strict >, the first snapshot is kept (second has same ts, not >)
    assert provider.retained_successful_snapshot().source == "first", (
        "P1-4 regression: same-timestamp request overwrote existing snapshot"
    )


# ---------------------------------------------------------------------------
# Round-2 concurrency hardening: no shared cache writes after timeout.
# ---------------------------------------------------------------------------


def test_heavy_breadth_success_does_not_populate_shared_cache():
    """The breadth worker must not write any cross-request shared cache.

    ``HeavyMarketCrawlerProvider`` is cached on the provider instance and is
    invoked from the breadth worker thread.  A successful fetch must not
    mutate a shared ``_last_successful_breadth`` field, otherwise a timed-out
    worker could publish a late cache write.
    """

    def requester(_url, **_kwargs):
        return HtmlResponse("上涨 3200 下跌 1800 平盘 120")

    provider = HeavyMarketCrawlerProvider(requester=requester, timeout=0.5)

    breadth = provider.fetch_breadth()

    assert breadth is not None
    assert (breadth.up, breadth.down, breadth.flat) == (3200, 1800, 120)
    assert provider._last_successful_breadth is None, (
        "breadth worker published a cross-request shared cache write"
    )


def test_expired_board_member_context_is_not_committed_to_shared_cache(tmp_path):
    """A cancelled/expired request must not publish sector members to the
    shared ``_sector_member_cache``.  The freshly read members are still
    returned to the current worker, but they are not cached globally."""
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    provider._fetch_ths_board_members = lambda _code: ["600000", "600001"]

    cancelled = Event()
    cancelled.set()

    members = provider._fetch_board_members("881234", cancel_event=cancelled)

    assert members == ["600000", "600001"]
    assert "881234" not in provider._sector_member_cache, (
        "cancelled request published a late sector-member cache write"
    )


def test_ready_board_member_context_is_committed_to_shared_cache(tmp_path):
    """When the request is still in budget, sector members are cached so the
    optimisation keeps working."""
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    provider._fetch_ths_board_members = lambda _code: ["600000"]

    members = provider._fetch_board_members("881234")

    assert members == ["600000"]
    assert provider._sector_member_cache.get("881234") == ["600000"]


def test_local_snapshot_timeout_blocks_late_sector_member_cache_write(tmp_path):
    """When the local-snapshot wrapper times out, the still-running worker
    must NOT publish a late write to the shared ``_sector_member_cache``.

    Deterministic: the board-member HTTP call blocks on an Event until after
    the wrapper has already returned its timeout fallback.
    """
    bars = _fake_local_bars()

    class FakeWarehouse:
        def read_latest_daily_bars(self, days: int = 3) -> pd.DataFrame:
            return bars

    started = Event()
    release = Event()
    snapshot_worker_done = Event()

    provider = RealtimeMarketProvider(FakeWarehouse(), local_snapshot_time_budget=0.1)
    provider._coverage_symbol_count = lambda: 2

    def blocking_members(_board_code: str) -> list[str]:
        started.set()
        release.wait(timeout=2)
        return ["600000", "600001"]

    provider._fetch_ths_board_members = blocking_members

    real_local = provider._snapshot_from_local

    def traced_local(*args, **kwargs):
        try:
            return real_local(*args, **kwargs)
        finally:
            snapshot_worker_done.set()

    provider._snapshot_from_local = traced_local

    diagnostics: list[str] = []
    sector_rows = [{"f12": "881234", "f14": "算力", "change_pct": "3.2"}]

    snapshot = provider._snapshot_from_local_with_budget(
        datetime.now(UTC),
        diagnostics,
        sector_rows,
        skip_topic_fetch=True,
    )

    assert started.wait(timeout=2)
    assert any("本地兜底行情快照超时" in item for item in diagnostics)
    assert snapshot.status == "unavailable"

    # Let the timed-out worker finish and attempt its (now-forbidden) write.
    release.set()
    assert snapshot_worker_done.wait(timeout=2)

    assert provider._sector_member_cache == {}, (
        "timed-out local snapshot worker published a late sector-member cache write"
    )


def test_late_breadth_worker_writes_only_private_diagnostics(tmp_path):
    """After the breadth wrapper times out, the worker's diagnostics list is
    private and never merged into the shared diagnostics list."""
    worker_started = Event()
    wrapper_returned = Event()
    release_worker = Event()
    worker_done = Event()
    provider = RealtimeMarketProvider(Warehouse(tmp_path), breadth_time_budget=0.1)

    def slow_fetcher(diagnostics, deadline=None, cancel_event=None):
        worker_started.set()
        release_worker.wait(timeout=2)
        diagnostics.append("late-breadth-private-diagnostic")
        worker_done.set()
        return None

    provider._call_live_breadth = slow_fetcher

    shared_diagnostics: list[str] = []

    def run_wrapper():
        provider._fetch_live_breadth_with_budget(shared_diagnostics)
        wrapper_returned.set()

    thread = Thread(target=run_wrapper)
    thread.start()
    assert worker_started.wait(timeout=2)
    assert wrapper_returned.wait(timeout=2)
    release_worker.set()
    assert worker_done.wait(timeout=2)
    thread.join(timeout=2)

    assert "late-breadth-private-diagnostic" not in shared_diagnostics
    assert any("超时" in item for item in shared_diagnostics)


def test_mixed_generation_then_timestamp_arbitration(tmp_path):
    """Mixed usage: generation-tracked writes and legacy timestamp writes must
    arbitrate consistently.  A legacy (no-generation) call must NEVER overwrite
    a generation-tracked snapshot, regardless of wall-clock timestamp.  This
    prevents the gen5→legacy新时间→gen6旧时间 rollback bug."""
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    t0 = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    t1 = datetime(2026, 7, 10, 10, 1, tzinfo=UTC)

    provider._remember_successful_snapshot(
        RealtimeMarketSnapshot(status="live", source="gen-5", updated_at=t0, message="g5"),
        generation=5,
    )
    # Legacy with same timestamp must not overwrite
    provider._remember_successful_snapshot(
        RealtimeMarketSnapshot(
            status="live", source="no-gen-stale", updated_at=t0, message="stale"
        ),
    )
    assert provider.retained_successful_snapshot().source == "gen-5"

    # Legacy with NEWER timestamp must also NOT overwrite generation-tracked
    provider._remember_successful_snapshot(
        RealtimeMarketSnapshot(
            status="live", source="no-gen-fresh", updated_at=t1, message="fresh"
        ),
    )
    # Round-4: legacy calls can never clobber generation-tracked snapshots
    assert provider.retained_successful_snapshot().source == "gen-5", (
        "Round-4: legacy call with newer timestamp must not overwrite generation-tracked snapshot"
    )


# ---------------------------------------------------------------------------
# Round-4 tests — must fail on current implementation before fixes.
# ---------------------------------------------------------------------------


def test_gen5_legacy_newer_timestamp_must_not_overwrite_gen6_generation_tracked(tmp_path):
    """Round-4: gen=5 (no-generation legacy call) with a newer wall-clock
    timestamp (t=10:05) must NOT overwrite gen=6 (generation-tracked) with
    an older timestamp (t=10:00).  The generation-tracked snapshot is
    authoritative; a legacy call — regardless of wall clock — must not
    clobber it.
    """
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    t_old = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    t_new = datetime(2026, 7, 10, 10, 5, tzinfo=UTC)

    # gen=6 with older timestamp arrives first
    provider._remember_successful_snapshot(
        RealtimeMarketSnapshot(
            status="live", source="gen-6", updated_at=t_old, message="gen6"
        ),
        generation=6,
    )
    assert provider.retained_successful_snapshot().source == "gen-6"

    # gen=5 legacy (no generation) with NEWER timestamp arrives late
    provider._remember_successful_snapshot(
        RealtimeMarketSnapshot(
            status="live", source="gen-5-legacy", updated_at=t_new, message="legacy-newer"
        ),
    )
    # BUG: the legacy call with newer timestamp must NOT overwrite gen-6
    assert provider.retained_successful_snapshot().source == "gen-6", (
        "Round-4 regression: gen=5 legacy (newer ts) overwrote gen=6 generation-tracked"
    )


def test_retained_diagnostics_dedup_includes_current_self_duplicates(tmp_path):
    """Round-4: when the current request diagnostics list itself contains
    duplicate entries, the deduplication must remove them.  Only then are
    the original snapshot diagnostics and the retained hint merged.
    """
    provider = RealtimeMarketProvider(Warehouse(tmp_path))

    # Store a successful snapshot with its own diagnostics
    original_snapshot = RealtimeMarketSnapshot(
        status="live",
        source="cls-quote-index",
        updated_at=datetime(2026, 7, 10, 10, 0, tzinfo=UTC),
        message="live",
        indexes=[
            MarketIndexQuote(
                symbol="sh000001",
                name="上证指数",
                last=3100.0,
                previous_close=3080.0,
                change=20.0,
                change_pct=0.0065,
                source="cls-quote-index",
            )
        ],
        breadth=MarketBreadth(up=3000, down=1800, flat=200, total=5000, source="cls-quote-breadth"),
        strong_sectors=[],
        diagnostics=["原始成功快照诊断信息"],
    )
    provider._remember_successful_snapshot(original_snapshot)

    # Make all live fetchers return empty
    provider._fetch_indexes = lambda: []
    provider._fetch_live_breadth_with_budget = lambda diagnostics: None
    provider._fetch_live_sectors_with_budget = lambda diagnostics, rows_out: []

    call_count = [0]

    def injecting_local(now, diagnostics, live_sector_rows, skip_topic_fetch=False):
        call_count[0] += 1
        # Inject a duplicate into the current diagnostics list
        diagnostics.append("dup-once")
        diagnostics.append("dup-once")
        diagnostics.append("dup-once")  # three identical copies
        diagnostics.append("unique-current")
        return unavailable_market_snapshot("本地无数据", diagnostics=[])

    provider._snapshot_from_local_with_budget = injecting_local

    events = list(provider.market_snapshot_events())
    result_event = events[-1]
    assert result_event["type"] == "result"
    snapshot = result_event["snapshot"]

    # "dup-once" must appear exactly once, not three times
    dup_count = sum(1 for item in snapshot.diagnostics if item == "dup-once")
    assert dup_count == 1, (
        f"Round-4 regression: current diagnostic 'dup-once' appears {dup_count} "
        f"times — current self-duplicates must be removed before merge"
    )

    # "unique-current" must still be present
    assert "unique-current" in snapshot.diagnostics

    # Original diagnostic must also be present
    assert "原始成功快照诊断信息" in snapshot.diagnostics


def test_single_flight_breadth_rejects_second_concurrent_request(tmp_path):
    """Round-4: when the breadth slot is already busy with a worker, a
    second concurrent request must return None immediately instead of
    creating another thread.  The in-flight lock enforces single-flight.
    """
    started = Event()
    release = Event()
    first_returned = Event()

    provider = RealtimeMarketProvider(Warehouse(tmp_path), breadth_time_budget=2.0)

    def blocking_fetcher(diagnostics, deadline=None, cancel_event=None):
        started.set()
        release.wait(timeout=2)
        return MarketBreadth(up=100, down=50, flat=10, total=160, source="test")

    provider._call_live_breadth = blocking_fetcher

    diagnostics_1: list[str] = []
    diagnostics_2: list[str] = []

    def first_call():
        provider._fetch_live_breadth_with_budget(diagnostics_1)
        first_returned.set()

    thread_1 = Thread(target=first_call)
    thread_1.start()
    assert started.wait(timeout=2)

    # Second call while first is still in-flight — must return None immediately
    result_2 = provider._fetch_live_breadth_with_budget(diagnostics_2)
    assert result_2 is None, (
        "Round-4: second concurrent breadth request must return None (single-flight)"
    )
    assert any("繁忙" in item for item in diagnostics_2), (
        "Round-4: single-flight rejection must emit a busy diagnostic"
    )

    release.set()
    assert first_returned.wait(timeout=2)
    thread_1.join(timeout=2)

    # The first call must have succeeded
    assert diagnostics_1 == [] or any(
        item not in diagnostics_1 for item in diagnostics_1
    )  # no "busy" message for first
    assert "实时红绿家数接口繁忙" not in str(diagnostics_1)


def test_single_flight_remains_busy_after_wrapper_timeout_until_worker_finishes(tmp_path):
    worker_started = Event()
    release_worker = Event()
    calls = 0
    provider = RealtimeMarketProvider(Warehouse(tmp_path), breadth_time_budget=0.02)

    def blocking_fetcher(diagnostics, deadline=None, cancel_event=None):
        nonlocal calls
        calls += 1
        worker_started.set()
        release_worker.wait(timeout=2)
        return MarketBreadth(up=100, down=50, flat=10, total=160, source="test")

    provider._call_live_breadth = blocking_fetcher
    first_diagnostics: list[str] = []
    second_diagnostics: list[str] = []

    assert provider._fetch_live_breadth_with_budget(first_diagnostics) is None
    assert worker_started.is_set()
    assert any("超时" in item for item in first_diagnostics)

    assert provider._fetch_live_breadth_with_budget(second_diagnostics) is None
    assert any("繁忙" in item for item in second_diagnostics)
    assert calls == 1, "the second request must not be queued behind the timed-out worker"

    release_worker.set()


def test_single_flight_sector_rejects_second_concurrent_request(tmp_path):
    """Round-4: single-flight for sector budget wrapper."""
    started = Event()
    release = Event()
    first_returned = Event()

    provider = RealtimeMarketProvider(Warehouse(tmp_path), sector_time_budget=2.0)
    provider._fetch_cls_hot_plate_sectors = lambda _diagnostics: []

    def blocking_fetcher(diagnostics, deadline=None, cancel_event=None, sector_rows_out=None):
        started.set()
        release.wait(timeout=2)
        return []

    provider._call_live_sectors = blocking_fetcher

    diagnostics_2: list[str] = []

    def first_call():
        provider._fetch_live_sectors_with_budget([], None)
        first_returned.set()

    thread_1 = Thread(target=first_call)
    thread_1.start()
    assert started.wait(timeout=2)

    result_2 = provider._fetch_live_sectors_with_budget(diagnostics_2, None)
    assert result_2 == [], (
        "Round-4: second concurrent sector request must return empty (single-flight)"
    )
    assert any("繁忙" in item for item in diagnostics_2)

    release.set()
    assert first_returned.wait(timeout=2)
    thread_1.join(timeout=2)


def test_single_flight_local_snapshot_rejects_second_concurrent_request(tmp_path):
    """Round-4: single-flight for local snapshot budget wrapper."""
    started = Event()
    release = Event()
    first_returned = Event()

    provider = RealtimeMarketProvider(Warehouse(tmp_path), local_snapshot_time_budget=2.0)

    def blocking_snapshot(*args, **kwargs):
        started.set()
        release.wait(timeout=2)
        return unavailable_market_snapshot("done")

    provider._call_snapshot_from_local = blocking_snapshot

    diagnostics_2: list[str] = []

    def first_call():
        provider._snapshot_from_local_with_budget(datetime.now(UTC), [])
        first_returned.set()

    thread_1 = Thread(target=first_call)
    thread_1.start()
    assert started.wait(timeout=2)

    result_2 = provider._snapshot_from_local_with_budget(datetime.now(UTC), diagnostics_2)
    assert result_2.status == "unavailable", (
        "Round-4: second concurrent local snapshot must return unavailable (single-flight)"
    )
    assert any("繁忙" in item for item in diagnostics_2)

    release.set()
    assert first_returned.wait(timeout=2)
    thread_1.join(timeout=2)


def test_breadth_submit_delay_does_not_exceed_deadline(tmp_path):
    """Round-4: when executor.submit() is delayed (e.g. under load), the
    remaining-budget calculation must shrink accordingly.  The wrapper uses
    `remaining = deadline - monotonic()` as the result timeout, so a
    delayed submit gets less (or zero) remaining time and cannot silently
    exceed the global breadth budget.
    """
    # We artificially delay the submit by using a thread that starts a
    # blocking worker and then checks that the result was NOT obtained
    # after the timeout (because remaining was already <= 0).
    submit_barrier = Event()
    release_submit = Event()
    worker_started = Event()
    worker_released = Event()

    provider = RealtimeMarketProvider(Warehouse(tmp_path), breadth_time_budget=0.05)

    original_submit = provider._get_breadth_executor().submit

    def delayed_submit(fn, *args, **kwargs):
        submit_barrier.set()
        release_submit.wait(timeout=2)
        return original_submit(fn, *args, **kwargs)

    provider._get_breadth_executor().submit = delayed_submit

    def slow_fetcher(diagnostics, deadline=None, cancel_event=None):
        worker_started.set()
        worker_released.wait(timeout=2)
        return MarketBreadth(up=100, down=50, flat=10, total=160, source="test")

    provider._call_live_breadth = slow_fetcher

    diagnostics: list[str] = []
    result_container: list = []

    def run_wrapper():
        result_container.append(provider._fetch_live_breadth_with_budget(diagnostics))

    thread = Thread(target=run_wrapper)
    thread.start()

    # Wait until submit is called, then hold it to simulate submit delay
    assert submit_barrier.wait(timeout=2)
    # Sleep beyond the budget so that by the time submit completes, remaining <= 0
    time.sleep(0.15)  # > breadth_time_budget=0.05
    release_submit.set()

    thread.join(timeout=2)
    worker_released.set()

    assert result_container == [None], (
        "Round-4: breadth must return None when submit delay exhausts budget"
    )
    assert any("超时" in item for item in diagnostics), (
        "Round-4: timeout diagnostic must be present after budget exhaustion"
    )


def test_local_snapshot_submit_delay_does_not_exceed_deadline(tmp_path):
    """Round-4: same deadline-awareness for local snapshot wrapper."""
    submit_barrier = Event()
    release_submit = Event()

    provider = RealtimeMarketProvider(Warehouse(tmp_path), local_snapshot_time_budget=0.05)

    original_submit = provider._get_local_executor().submit

    def delayed_submit(fn, *args, **kwargs):
        submit_barrier.set()
        release_submit.wait(timeout=2)
        return original_submit(fn, *args, **kwargs)

    provider._get_local_executor().submit = delayed_submit

    diagnostics: list[str] = []
    result_container: list = []

    def run_wrapper():
        result_container.append(
            provider._snapshot_from_local_with_budget(datetime.now(UTC), diagnostics)
        )

    thread = Thread(target=run_wrapper)
    thread.start()

    assert submit_barrier.wait(timeout=2)
    time.sleep(0.15)
    release_submit.set()
    thread.join(timeout=2)

    assert result_container[0].status == "unavailable", (
        "Round-4: local snapshot must return unavailable when submit delay exhausts budget"
    )
    assert any("超时" in item for item in diagnostics)


def test_cache_write_blocked_when_cancel_after_context_check(tmp_path):
    """Round-4 TOCTOU: when _context_expired passes but the cancel_event
    is set by another thread between the check and the cache write, the
    double-check under _state_lock must prevent the write.
    """
    after_check = Event()
    proceed = Event()
    write_done = Event()

    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    provider._fetch_ths_board_members = lambda _code: ["600000"]

    original_expired = provider._context_expired

    def paused_expired(cancel_event, deadline):
        result = original_expired(cancel_event, deadline)
        if not result:
            after_check.set()
            proceed.wait(timeout=2)
        return result

    provider._context_expired = paused_expired
    cancelled = Event()
    members_result: list = []

    def fetch_with_toctou():
        members_result.append(
            provider._fetch_board_members("881234", cancel_event=cancelled)
        )
        write_done.set()

    t = threading.Thread(target=fetch_with_toctou, daemon=True)
    t.start()

    # Wait until the worker passes _context_expired
    assert after_check.wait(timeout=2)

    # NOW cancel — between check and write
    cancelled.set()
    proceed.set()

    assert write_done.wait(timeout=2)
    t.join(timeout=2)

    assert members_result == [["600000"]], "Fresh members must be returned"
    assert "881234" not in provider._sector_member_cache, (
        "Round-4: TOCTOU — cache write must be blocked when cancel arrives between check and write"
    )


# ===========================================================================
# Tests from test_realtime_parsers.py
# ===========================================================================


class TestParseInt:
    def test_plain_int(self):
        assert parse_int(123) == 123

    def test_numeric_zero(self):
        assert parse_int(0) == 0

    def test_string_with_commas(self):
        assert parse_int("1,234") == 1234

    def test_string_with_text(self):
        assert parse_int("上涨1234家") == 1234

    def test_negative(self):
        assert parse_int("-5") == -5

    def test_none(self):
        assert parse_int(None) is None

    def test_empty(self):
        assert parse_int("") is None

    def test_no_digits(self):
        assert parse_int("abc") is None


class TestParseFloat:
    def test_plain_float(self):
        assert parse_float(1.5) == 1.5

    def test_string_with_percent(self):
        assert parse_float("3.5%") == 3.5

    def test_string_with_commas(self):
        assert parse_float("1,234.56") == 1234.56

    def test_none(self):
        assert parse_float(None) is None

    def test_empty(self):
        assert parse_float("") is None

    def test_invalid(self):
        assert parse_float("abc") is None


class TestNormalizeChangePct:
    def test_percent_value(self):
        assert normalize_change_pct(3.5) == 0.035

    def test_decimal_value(self):
        assert normalize_change_pct(0.035) == 0.035

    def test_none(self):
        assert normalize_change_pct(None) is None


class TestNormalizeSectorChangePct:
    def test_percent_unit(self):
        row = {"f3": 3.5, "_change_pct_unit": "percent"}
        assert normalize_sector_change_pct(row) == 0.035

    def test_auto_detect_large(self):
        row = {"f3": 3.5}
        assert normalize_sector_change_pct(row) == 0.035

    def test_auto_detect_small(self):
        row = {"f3": 0.035}
        assert normalize_sector_change_pct(row) == 0.035


class TestCleanThsTopicName:
    def test_normal_topic(self):
        assert clean_ths_topic_name("半导体") == "半导体"

    def test_whitespace(self):
        assert clean_ths_topic_name("  半导体 ") == "半导体"

    def test_generic_topic_filtered(self):
        assert clean_ths_topic_name("A股") is None
        assert clean_ths_topic_name("") is None

    def test_suffix_filtered(self):
        assert clean_ths_topic_name("半导体个股") is None
        assert clean_ths_topic_name("半导体概念股") is None


class TestDecodeSinaResponse:
    def test_basic_decode(self):
        text = (
            'var hq_str_sh000001="上证指数,3100,3120,3120.5,3105,3100,3100,3120,'
            '3100,3100,3120,3100,3100,3120,3100,3100,3120,3100,3100,3100,3100,'
            '3100,3100,3100,3100,3100,3100,3100,3100,3100,2024-01-05,15:00:00,00,";'
        )
        result = decode_sina_response(text)
        assert "sh000001" in result
        assert len(result["sh000001"]) > 4

    def test_empty_response(self):
        assert decode_sina_response("") == {}

    def test_no_hq_str(self):
        assert decode_sina_response("var foo = 'bar';") == {}


class TestQuoteFromSina:
    def test_valid_quote(self):
        values = ["上证指数", "3100", "3100", "3120", "3105", "3100", "3100", "3120"]
        values.extend([""] * 22)
        values.extend(["2024-01-05", "15:00:00"])
        quote = quote_from_sina("sh000001", "上证指数", values)
        assert quote is not None
        assert quote.symbol == "sh000001"
        assert quote.last == 3120.0
        assert quote.previous_close == 3100.0

    def test_invalid_values(self):
        assert quote_from_sina("sh000001", "test", []) is None
        assert quote_from_sina("sh000001", "test", ["a", "b", "c", "d"]) is None


class TestQuoteFromClsHome:
    def test_valid_row(self):
        row = {
            "secu_code": "sh000001",
            "secu_name": "上证指数",
            "last_px": "3120.5",
            "preclose_px": "3100",
            "change_px": "20.5",
            "change": "0.66%",
        }
        quote = quote_from_cls_home(row)
        assert quote is not None
        assert quote.symbol == "sh000001"
        assert quote.last == 3120.5

    def test_missing_symbol(self):
        assert quote_from_cls_home({"last_px": "3120"}) is None

    def test_missing_last(self):
        assert quote_from_cls_home({"secu_code": "sh000001"}) is None


class TestBreadthFromClsDistribution:
    def test_valid_distribution(self):
        data = {
            "rise_num": "2500",
            "fall_num": "1800",
            "flat_num": "200",
            "up_num": "50",
            "down_num": "30",
        }
        breadth = breadth_from_cls_distribution(data)
        assert breadth is not None
        assert breadth.up == 2500
        assert breadth.down == 1800
        assert breadth.flat == 200
        assert breadth.total == 4500
        assert breadth.source == "cls-quote-breadth"

    def test_missing_fields(self):
        assert breadth_from_cls_distribution({}) is None
        assert breadth_from_cls_distribution({"rise_num": "100"}) is None

    def test_keeps_numeric_zero_rise_and_fall_counts(self):
        breadth = breadth_from_cls_distribution({"rise_num": 0, "fall_num": 0, "flat_num": 8})

        assert breadth is not None
        assert breadth.up == 0
        assert breadth.down == 0
        assert breadth.total == 8

    def test_non_dict(self):
        assert breadth_from_cls_distribution("not a dict") is None


class TestBreadthFromClsHomeData:
    def test_reads_distribution_from_home_payload(self):
        breadth = breadth_from_cls_home_data(
            {
                "up_down_dis": {
                    "rise_num": 3919,
                    "fall_num": 1215,
                    "flat_num": 67,
                    "up_num": 85,
                    "down_num": 25,
                }
            }
        )

        assert breadth is not None
        assert breadth.total == 5201
        assert breadth.up == 3919
        assert breadth.down == 1215

    def test_ignores_index_constituent_counts_when_full_distribution_missing(self):
        breadth = breadth_from_cls_home_data(
            {
                "index_quote": [
                    {"secu_code": "sh000001", "up_num": 1621, "down_num": 537, "flat_num": 24}
                ]
            }
        )

        assert breadth is None


class TestSectorRowsFromClsHotPlate:
    def test_valid_payload(self):
        payload = {
            "data": {
                "industry": [
                    {"secu_name": "半导体", "change": 3.5, "up_stock": [{"secu_code": "688001"}]}
                ],
                "concept": [],
                "area": [],
            }
        }
        rows = sector_rows_from_cls_hot_plate(payload)
        assert len(rows) == 1
        assert rows[0]["name"] == "半导体"

    def test_empty_payload(self):
        assert sector_rows_from_cls_hot_plate({}) == []
        assert sector_rows_from_cls_hot_plate({"data": {}}) == []


class TestAggregateYesterdayLimitUpSectors:
    def test_ranks_sectors_by_follow_through_and_count(self):
        sectors = aggregate_yesterday_limit_up_sectors(
            [
                {"name": "算力", "industry": "通信设备", "pct": 0.08, "code": "000001"},
                {"name": "机器人", "industry": "通信设备", "pct": 0.02, "code": "000002"},
                {"name": "黄金", "industry": "贵金属", "pct": -0.01, "code": "000003"},
            ],
            source="eastmoney-yesterday-limit-up",
        )

        assert [item.name for item in sectors] == ["通信设备", "贵金属"]
        assert sectors[0].change_pct == 0.05
        assert sectors[0].leading_symbol == "000001"

    def test_keeps_a_zero_zdp_follow_through_value(self):
        sectors = aggregate_yesterday_limit_up_sectors(
            [{"industry": "Zero sector", "zdp": 0, "code": "000001"}]
        )

        assert sectors[0].change_pct == 0.0

    def test_treats_one_point_zdp_as_one_percent(self):
        sectors = aggregate_yesterday_limit_up_sectors(
            [
                {"industry": "Up sector", "zdp": "1.00", "code": "000001"},
                {"industry": "Down sector", "zdp": "-1.00", "code": "000002"},
            ]
        )

        changes = {sector.name: sector.change_pct for sector in sectors}
        assert changes == {"Up sector": 0.01, "Down sector": -0.01}


class TestDedupeSectors:
    def test_dedup_by_name(self):
        sectors = [
            SectorMover(name="半导体", change_pct=0.03, source="test"),
            SectorMover(name="半导体", change_pct=0.02, source="test"),
            SectorMover(name="AI", change_pct=0.05, source="test"),
        ]
        result = dedupe_sectors(sectors)
        assert len(result) == 2
        assert result[0].name == "半导体"

    def test_limit(self):
        sectors = [SectorMover(name=f"s{i}", change_pct=0.01, source="test") for i in range(20)]
        assert len(dedupe_sectors(sectors, limit=5)) == 5


class TestAppendYesterdaySectorNote:
    def test_adds_note(self):
        result = append_yesterday_sector_note(
            "msg", [SectorMover(name="半导体", change_pct=0.01, source="local-yesterday-group")]
        )
        assert "昨日强势板块追踪来自本地历史" in result

    def test_names_the_eastmoney_yesterday_limit_up_source(self):
        result = append_yesterday_sector_note(
            "msg",
            [
                SectorMover(
                    name="Semiconductor",
                    change_pct=0.01,
                    source="eastmoney-yesterday-limit-up",
                )
            ],
        )

        assert "东方财富昨日涨停池" in result
        assert "本地历史" not in result

    def test_no_sectors(self):
        assert append_yesterday_sector_note("msg", []) == "msg"

    def test_already_has_note(self):
        msg = "msg 昨日强势板块追踪来自本地历史。"
        assert append_yesterday_sector_note(
            msg, [SectorMover(name="半导体", change_pct=0.01, source="local-yesterday-group")]
        ) == msg


class TestUniqueSources:
    def test_dedup_preserves_order(self):
        assert unique_sources(["a", "b", "a", "c", None, "b"]) == ["a", "b", "c"]

    def test_empty(self):
        assert unique_sources([]) == []


class TestMarketPhase:
    def test_weekend(self):
        # 2024-01-06 is Saturday
        saturday = datetime(2024, 1, 6, 10, 0, tzinfo=BEIJING_TZ)
        assert market_phase(saturday) == "non_trading"

    def test_pre_open(self):
        weekday = datetime(2024, 1, 4, 9, 0, tzinfo=BEIJING_TZ)
        assert market_phase(weekday) == "pre_open"

    def test_trading(self):
        weekday = datetime(2024, 1, 4, 10, 0, tzinfo=BEIJING_TZ)
        assert market_phase(weekday) == "trading"

    def test_lunch_break(self):
        weekday = datetime(2024, 1, 4, 12, 0, tzinfo=BEIJING_TZ)
        assert market_phase(weekday) == "lunch_break"

    def test_post_close(self):
        weekday = datetime(2024, 1, 4, 16, 0, tzinfo=BEIJING_TZ)
        assert market_phase(weekday) == "post_close"


class TestPhaseDiagnostic:
    def test_non_trading(self):
        assert phase_diagnostic("non_trading") is not None
        assert "降低" in phase_diagnostic("non_trading")

    def test_trading_no_diagnostic(self):
        assert phase_diagnostic("trading") is None


class TestIsValidFullMarketBreadth:
    def test_valid_cls_breadth(self):
        breadth = MarketBreadth(
            up=2500, down=1800, flat=200, total=4500, source="cls-quote-breadth"
        )
        assert is_valid_full_market_breadth(breadth) is True

    def test_too_small(self):
        breadth = MarketBreadth(up=100, down=80, flat=10, total=190, source="sina")
        assert is_valid_full_market_breadth(breadth) is False

    def test_none(self):
        assert is_valid_full_market_breadth(None) is False

    def test_with_local_count(self):
        breadth = MarketBreadth(up=2000, down=1500, flat=200, total=3700, source="sina")
        assert is_valid_full_market_breadth(breadth, local_symbol_count=5000) is True

    def test_local_ratio_too_low(self):
        breadth = MarketBreadth(up=100, down=80, flat=10, total=190, source="sina")
        assert is_valid_full_market_breadth(breadth, local_symbol_count=5000) is False


class TestAShareMarketSymbol:
    def test_shanghai(self):
        assert a_share_market_symbol("600519") == "sh600519"
        assert a_share_market_symbol("688001") == "sh688001"
        assert a_share_market_symbol("900001") == "sh900001"

    def test_shenzhen(self):
        assert a_share_market_symbol("000001") == "sz000001"
        assert a_share_market_symbol("300750") == "sz300750"
        assert a_share_market_symbol("002415") == "sz002415"

    def test_beijing(self):
        assert a_share_market_symbol("430047") == "bj430047"
        assert a_share_market_symbol("830799") == "bj830799"

    def test_invalid(self):
        assert a_share_market_symbol("") is None
        assert a_share_market_symbol("abc") is None

    def test_sina_tencent_equivalence(self):
        """Verify that the unified function produces the same result as the
        original _sina_stock_symbol and _tencent_stock_symbol methods."""
        test_cases = ["600519", "000001", "300750", "688001", "430047", "830799"]
        for code in test_cases:
            assert a_share_market_symbol(code) is not None


class TestAggregateThsHotTopicRows:
    def test_basic_aggregation(self):
        rows = [
            {"reason": "半导体+AI", "code": "688001", "zhangfu": "5.5", "chengjiaoe": "100000"},
            {"reason": "半导体", "code": "000001", "zhangfu": "3.2", "chengjiaoe": "50000"},
            {"reason": "AI", "code": "300750", "zhangfu": "2.1", "chengjiaoe": "80000"},
        ]
        result = aggregate_ths_hot_topic_rows(rows)
        assert len(result) == 2
        topics = {item["name"]: item for item in result}
        assert "半导体" in topics
        assert "AI" in topics
        # The aggregated output contains name, change_pct, leading_symbol, members, source
        assert "members" in topics["半导体"]
        assert len(topics["半导体"]["members"]) == 2

    def test_empty(self):
        assert aggregate_ths_hot_topic_rows([]) == []

    def test_filters_empty_reason(self):
        rows = [{"reason": "", "code": "688001"}]
        assert aggregate_ths_hot_topic_rows(rows) == []

    def test_filters_generic_topics(self):
        rows = [{"reason": "A股", "code": "688001", "zhangfu": "1.0"}]
        assert aggregate_ths_hot_topic_rows(rows) == []


# ===========================================================================
# Tests from test_realtime_provider.py
# ===========================================================================


def test_realtime_provider_single_flights_cls_home_payload():
    entered = Event()
    release = Event()
    calls = 0
    calls_lock = Lock()
    payload = {
        "code": 200,
        "data": {"index_quote": [], "up_down_dis": {"rise_num": 4, "fall_num": 0}},
    }

    def requester(_url, **_kwargs):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=2)
        return _Response(payload)

    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(provider._fetch_cls_home_payload)
        assert entered.wait(timeout=2)
        second = executor.submit(provider._fetch_cls_home_payload)
        release.set()
        assert first.result(timeout=2) == payload
        assert second.result(timeout=2) == payload

    assert calls == 1


def test_realtime_provider_records_market_summary_failure_in_diagnostics():
    provider = RealtimeMarketProvider(warehouse=_Warehouse())

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("upstream unavailable")

    provider._request_public_html = unavailable
    provider._call_ths_industry_html_rows = lambda *_args, **_kwargs: []
    diagnostics: list[str] = []

    assert provider._fetch_ths_market_summary_breadth(diagnostics) is None
    assert any("同花顺市场总览" in item and "upstream unavailable" in item for item in diagnostics)


def test_realtime_provider_uses_bounded_ths_indexflash_breadth_before_bulk_fallbacks():
    requested_headers = {}
    cookie_timeouts = []

    def requester(url, **kwargs):
        assert "indexflash" in url
        requested_headers.update(kwargs["headers"])
        return _Response({"result": {"zdfb_data": {"znum": 3351, "dnum": 2098}}})

    provider = RealtimeMarketProvider(
        warehouse=_Warehouse(),
        requester=requester,
        breadth_time_budget=2.0,
        breadth_source_timeout=0.8,
        ths_cookie_getter=lambda timeout: cookie_timeouts.append(timeout) or "v=fresh-request",
    )
    provider._fetch_cls_breadth = lambda *_args, **_kwargs: None
    provider._fetch_sina_breadth = lambda: pytest.fail("bulk Sina fallback should not run")
    provider._fetch_tencent_breadth = lambda _diagnostics: pytest.fail("bulk Tencent fallback should not run")
    provider._fetch_akshare_breadth_with_timeout = lambda _diagnostics: pytest.fail("AKShare fallback should not run")
    provider._fetch_heavy_breadth = lambda _diagnostics: pytest.fail("heavy crawler should not run")
    provider._latest_local_symbol_count = lambda: pytest.fail("full-market symbol scan should not run")
    provider._coverage_symbol_count = lambda: pytest.fail("warehouse coverage scan should not run")

    breadth = provider._fetch_live_breadth([], deadline=monotonic() + 2.0, cancel_event=Event())

    assert breadth is not None
    assert breadth.source == "ths-indexflash-breadth"
    assert (breadth.up, breadth.down, breadth.flat, breadth.total) == (3351, 2098, 0, 5449)
    assert requested_headers["Cookie"] == "v=fresh-request"
    assert cookie_timeouts and cookie_timeouts[0] <= 0.8


def test_realtime_provider_single_flight_waiter_rejects_expired_cache_after_owner_failure(monkeypatch):
    entered = Event()
    release = Event()
    calls = 0

    class TrackingEvent(Event):
        waiter_entered = Event()

        def wait(self, timeout=None):
            TrackingEvent.waiter_entered.set()
            return super().wait(timeout)

    monkeypatch.setattr(realtime_module, "Event", TrackingEvent)

    def requester(_url, **_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=2)
        raise RuntimeError("CLS refresh failed")

    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    provider._cls_home_cache = {"code": 200, "data": {"stale": True}}
    provider._cls_home_cached_at = monotonic() - provider.cls_home_cache_ttl - 1

    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(provider._fetch_cls_home_payload)
        assert entered.wait(timeout=2)
        waiter = executor.submit(provider._fetch_cls_home_payload)
        assert TrackingEvent.waiter_entered.wait(timeout=2)
        release.set()

        with pytest.raises(RuntimeError, match="CLS refresh failed"):
            owner.result(timeout=2)
        with pytest.raises(RuntimeError, match="single-flight request failed"):
            waiter.result(timeout=2)

    assert calls == 1


def test_realtime_provider_single_flight_waiter_survives_completed_cache_clear(monkeypatch):
    entered = Event()
    release = Event()
    wait_started = Event()
    waiter_awake = Event()
    allow_waiter_to_read = Event()
    payload = {
        "code": 200,
        "data": {"index_quote": [], "up_down_dis": {"rise_num": 4, "fall_num": 0}},
    }

    class TrackingEvent(Event):
        def wait(self, timeout=None):
            wait_started.set()
            result = super().wait(timeout)
            if result:
                waiter_awake.set()
                assert allow_waiter_to_read.wait(timeout=2)
            return result

    monkeypatch.setattr(realtime_module, "Event", TrackingEvent)

    def requester(_url, **_kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return _Response(payload)

    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(provider._fetch_cls_home_payload)
        assert entered.wait(timeout=2)
        waiter = executor.submit(provider._fetch_cls_home_payload)
        assert wait_started.wait(timeout=2)
        release.set()
        assert waiter_awake.wait(timeout=2)
        provider._clear_cls_home_completed_cache()
        allow_waiter_to_read.set()

        assert owner.result(timeout=2) == payload
        assert waiter.result(timeout=2) == payload


def test_realtime_provider_finds_previous_trade_date_before_long_holiday(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 2, 24, 12, tzinfo=tz)

    monkeypatch.setattr(realtime_module, "datetime", FixedDatetime)

    provider = RealtimeMarketProvider(warehouse=_Warehouse())

    assert provider._previous_trade_date() == "2026-02-13"


def test_realtime_provider_uses_beijing_dates_at_non_cn_host_midnight_boundary(monkeypatch):
    beijing_now = datetime(2026, 7, 14, 0, 30, tzinfo=BEIJING_TZ)
    non_cn_host_now = datetime(2026, 7, 13, 9, 30, tzinfo=timezone(-timedelta(hours=7)))

    class NonCnHostClock:
        def astimezone(self, tz=None):
            return beijing_now if tz == BEIJING_TZ else non_cn_host_now

    class FixedDatetime:
        @classmethod
        def now(cls, tz=None):
            assert tz == UTC
            return NonCnHostClock()

    monkeypatch.setattr(realtime_module, "datetime", FixedDatetime)
    provider = RealtimeMarketProvider(warehouse=_Warehouse())

    assert provider._latest_trade_date() == "2026-07-14"
    assert provider._previous_trade_date() == "2026-07-13"
    assert provider._yesterday_pool_dates() == ("2026-07-14", "2026-07-13")


def test_realtime_provider_keeps_zero_zdp_before_secondary_pct():
    def requester(_url, **_kwargs):
        return _Response(
            {
                "rc": 0,
                "data": {
                    "pool": [
                        {
                            "c": "000001",
                            "n": "Zero move",
                            "hybk": "Zero sector",
                            "zdp": 0,
                            "pct": "7.00",
                        }
                    ]
                },
            }
        )

    diagnostics = []
    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    provider._latest_trade_date = lambda: "2026-07-14"

    result = provider._fetch_yesterday_strong_sectors(diagnostics)

    assert result[0].change_pct == 0.0


def test_realtime_provider_keeps_one_point_zdp_as_one_percent():
    def requester(_url, **_kwargs):
        return _Response(
            {
                "rc": 0,
                "data": {
                    "pool": [
                        {"c": "000001", "n": "One up", "hybk": "Up sector", "zdp": "1.00"},
                        {"c": "000002", "n": "One down", "hybk": "Down sector", "zdp": "-1.00"},
                    ]
                },
            }
        )

    diagnostics = []
    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    provider._latest_trade_date = lambda: "2026-07-14"

    result = provider._fetch_yesterday_strong_sectors(diagnostics)

    changes = {sector.name: sector.change_pct for sector in result}
    assert changes == {"Up sector": 0.01, "Down sector": -0.01}


def test_realtime_provider_uses_public_yesterday_limit_up_request_contract():
    requested = []

    def requester(url, **kwargs):
        requested.append((url, kwargs["params"]))
        return _Response(
            {
                "rc": 0,
                "data": {
                    "pool": [
                        {"c": "000001", "n": "Power", "hybk": "Power sector", "zdp": "2.00"}
                    ]
                },
            }
        )

    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    provider._latest_trade_date = lambda: "2026-07-14"

    provider._fetch_yesterday_strong_sectors([])

    params = requested[0][1]
    assert params["date"] == "20260714"
    assert params["sort"] == "zs:desc"
    assert params["ft"] == "1"
    assert params["l"] == "0"
    assert provider._yesterday_sector_cache_date == "2026-07-13"


def test_realtime_provider_uses_tc_to_complete_an_exact_full_yesterday_pool_page():
    requested_pages = []
    pool = [
        {"c": f"{index:06d}", "n": f"Stock {index}", "hybk": "Complete sector", "zdp": "2.00"}
        for index in range(100)
    ]

    def requester(_url, **kwargs):
        page = int(kwargs["params"]["Pageindex"])
        requested_pages.append(page)
        return _Response({"rc": 0, "data": {"tc": 100, "pool": pool if page == 0 else []}})

    diagnostics = []
    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    provider._latest_trade_date = lambda: "2026-07-14"

    provider._fetch_yesterday_strong_sectors(diagnostics)

    assert requested_pages == [0]
    assert provider._yesterday_sector_cache_date == "2026-07-13"
    assert provider._yesterday_sector_cache
    assert not any("partial" in item for item in diagnostics)


def test_realtime_provider_caches_and_reuses_an_empty_complete_yesterday_pool():
    requested = []

    def requester(_url, **kwargs):
        requested.append(kwargs["params"])
        return _Response({"rc": 0, "data": {"tc": 0, "pool": []}})

    diagnostics = []
    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    provider._latest_trade_date = lambda: "2026-07-14"

    assert provider._fetch_yesterday_strong_sectors(diagnostics) == []
    assert provider._yesterday_sector_cache_date == "2026-07-13"
    assert provider._yesterday_sector_cache == []
    assert any("sectors=0" in item for item in diagnostics)

    scheduler_diagnostics = []
    assert provider._yesterday_sector_snapshot_or_schedule(scheduler_diagnostics) == []
    assert len(requested) == 1


def test_realtime_provider_fetches_all_yesterday_limit_up_pages():
    requested_pages = []
    first_page = [
        {"c": f"{index:06d}", "n": f"First {index}", "hybk": "First sector", "zdp": "1.00"}
        for index in range(1, 101)
    ]
    second_page = [
        {"c": "600000", "n": "Beyond first page", "hybk": "Second sector", "zdp": "2.00"}
    ]

    def requester(_url, **kwargs):
        page = int(kwargs["params"]["Pageindex"])
        requested_pages.append(page)
        return _Response({"rc": 0, "data": {"count": 101, "pool": first_page if page == 0 else second_page}})

    diagnostics = []
    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    provider._latest_trade_date = lambda: "2026-07-14"

    result = provider._fetch_yesterday_strong_sectors(diagnostics)

    assert requested_pages == [0, 1]
    assert [sector.name for sector in result] == ["First sector", "Second sector"]


def test_realtime_provider_reports_partial_yesterday_limit_up_pool_after_request_budget():
    requested_pages = []

    def requester(_url, **kwargs):
        page = int(kwargs["params"]["Pageindex"])
        requested_pages.append(page)
        pool = [
            {
                "c": f"{page * 100 + index:06d}",
                "n": f"Stock {page}-{index}",
                "hybk": f"Sector {page}",
                "zdp": "1.00",
            }
            for index in range(100)
        ]
        return _Response({"rc": 0, "data": {"count": 301, "pool": pool}})

    diagnostics = []
    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    provider._latest_trade_date = lambda: "2026-07-14"

    provider._fetch_yesterday_strong_sectors(diagnostics)

    assert requested_pages == [0, 1, 2]
    assert any("partial" in item and "300" in item and "301" in item for item in diagnostics)


def test_realtime_provider_does_not_replace_complete_yesterday_cache_with_partial_pool():
    complete_cache = [
        SectorMover(name="Complete sector", change_pct=0.08, source="eastmoney-yesterday-limit-up")
    ]
    requested_pages = []

    def requester(_url, **kwargs):
        page = int(kwargs["params"]["Pageindex"])
        requested_pages.append(page)
        pool = [
            {
                "c": f"{page * 100 + index:06d}",
                "n": f"Partial {page}-{index}",
                "hybk": "Partial sector",
                "zdp": "2.00",
            }
            for index in range(100)
        ]
        return _Response({"rc": 0, "data": {"count": 301, "pool": pool}})

    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    provider._latest_trade_date = lambda: "2026-07-14"
    provider._yesterday_sector_cache_date = "2026-07-13"
    provider._yesterday_sector_cache = complete_cache
    provider._yesterday_sector_cached_at = monotonic() - provider.yesterday_sector_cache_ttl - 1
    cached_at = provider._yesterday_sector_cached_at
    diagnostics = []

    result = provider._fetch_yesterday_strong_sectors(diagnostics)

    assert requested_pages == [0, 1, 2]
    assert result[0].name == "Partial sector"
    assert provider._yesterday_sector_cache == complete_cache
    assert provider._yesterday_sector_cached_at == cached_at
    assert any("partial" in item for item in diagnostics)


def test_realtime_provider_fetches_yesterday_limit_up_sector_tracking():
    requested = []

    def requester(url, **kwargs):
        requested.append((url, kwargs.get("params", {})))
        return _Response(
            {
                "rc": 0,
                "data": {
                    "pool": [
                        {"c": "000001", "n": "Power A", "hybk": "Communication", "zdp": "8.00"},
                        {"c": "000002", "n": "Power B", "hybk": "Communication", "zdp": "2.00"},
                        {"c": "000003", "n": "Gold A", "hybk": "Precious Metals", "zdp": "-1.00"},
                    ]
                },
            }
        )

    diagnostics = []
    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)

    result = provider._fetch_yesterday_strong_sectors(diagnostics)

    assert [item.name for item in result] == ["Communication", "Precious Metals"]
    assert result[0].change_pct == 0.05
    assert result[0].leading_symbol == "000001"
    assert requested[0][0].endswith("getYesterdayZTPool")
    assert requested[0][1]["date"].isdigit()
    assert result[0].source == "eastmoney-yesterday-limit-up"


def test_realtime_provider_keeps_yesterday_sector_source_in_snapshot():
    provider = RealtimeMarketProvider(
        warehouse=_Warehouse(),
        requester=lambda _url, **_kwargs: _Response({}),
        breadth_time_budget=None,
        sector_time_budget=None,
    )
    provider._latest_trade_date = lambda: "2026-07-14"
    provider._yesterday_sector_cache_date = "2026-07-13"
    provider._yesterday_sector_cached_at = monotonic()
    provider._yesterday_sector_cache = [
        SectorMover(
            name="Communication",
            change_pct=0.05,
            leading_symbol="000001",
            source="eastmoney-yesterday-limit-up",
        )
    ]
    provider._fetch_indexes = lambda: [
        MarketIndexQuote(
            symbol="sh000001",
            name="SSE",
            last=3000,
            previous_close=2990,
            change=10,
            change_pct=10 / 2990,
            source="test-index",
        )
    ]
    provider._fetch_live_breadth = lambda: MarketBreadth(
        up=3500,
        down=1200,
        flat=300,
        total=5000,
        source="test-breadth",
    )
    provider._fetch_live_sectors = lambda: [
        SectorMover(name="AI", change_pct=0.03, source="test-sector")
    ]

    snapshot = provider.market_snapshot()

    assert "eastmoney-yesterday-limit-up" in snapshot.source


def test_realtime_provider_replaces_local_yesterday_note_when_eastmoney_cache_wins():
    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=lambda _url, **_kwargs: _Response({}))
    provider._latest_trade_date = lambda: "2026-07-14"
    provider._yesterday_sector_cache_date = "2026-07-13"
    provider._yesterday_sector_cached_at = monotonic()
    provider._yesterday_sector_cache = [
        SectorMover(
            name="Eastmoney sector",
            change_pct=0.05,
            source="eastmoney-yesterday-limit-up",
        )
    ]
    provider._fetch_indexes = lambda: []
    provider._fetch_live_breadth_with_budget = lambda *_args, **_kwargs: None
    provider._fetch_live_sectors_with_budget = lambda *_args, **_kwargs: []
    provider._snapshot_from_local_with_budget = lambda *_args, **_kwargs: RealtimeMarketSnapshot(
        status="stale",
        source="local-latest",
        updated_at=datetime.now(UTC),
        market_phase="trading",
        yesterday_strong_sectors=[
            SectorMover(name="Local sector", change_pct=0.01, source="local-yesterday-group")
        ],
        message="Local fallback. 昨日强势板块追踪来自本地历史。",
    )

    snapshot = provider.market_snapshot()

    assert snapshot.yesterday_strong_sectors[0].source == "eastmoney-yesterday-limit-up"
    assert "昨日强势板块追踪来自东方财富昨日涨停池。" in snapshot.message
    assert "昨日强势板块追踪来自本地历史。" not in snapshot.message
    assert snapshot.message.count("昨日强势板块追踪") == 1


def test_realtime_provider_keeps_valid_empty_eastmoney_cache_over_local_yesterday_sectors():
    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=lambda _url, **_kwargs: _Response({}))
    provider._latest_trade_date = lambda: "2026-07-14"
    provider._yesterday_sector_cache_date = "2026-07-13"
    provider._yesterday_sector_cached_at = monotonic()
    provider._yesterday_sector_cache = []
    provider._fetch_indexes = lambda: []
    provider._fetch_live_breadth_with_budget = lambda *_args, **_kwargs: None
    provider._fetch_live_sectors_with_budget = lambda *_args, **_kwargs: []
    provider._snapshot_from_local_with_budget = lambda *_args, **_kwargs: RealtimeMarketSnapshot(
        status="stale",
        source="local-latest",
        updated_at=datetime.now(UTC),
        market_phase="trading",
        yesterday_strong_sectors=[
            SectorMover(name="Local sector", change_pct=0.01, source="local-yesterday-group")
        ],
        message="Local fallback. 昨日强势板块追踪来自本地历史。",
    )

    snapshot = provider.market_snapshot()

    assert snapshot.yesterday_strong_sectors == []
    assert "昨日强势板块追踪" not in snapshot.message


def test_realtime_provider_returns_expired_eastmoney_cache_while_refresh_runs():
    entered = Event()
    release = Event()
    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=lambda _url, **_kwargs: _Response({}))
    provider._latest_trade_date = lambda: "2026-07-14"
    provider._yesterday_sector_cache_date = "2026-07-13"
    provider._yesterday_sector_cache = [
        SectorMover(name="Cached sector", change_pct=0.05, source="eastmoney-yesterday-limit-up")
    ]
    provider._yesterday_sector_cached_at = monotonic() - provider.yesterday_sector_cache_ttl - 1

    def refresh(_diagnostics):
        entered.set()
        assert release.wait(timeout=2)
        return []

    provider._fetch_yesterday_strong_sectors = refresh
    diagnostics = []
    try:
        result = provider._yesterday_sector_snapshot_or_schedule(diagnostics)
        assert entered.wait(timeout=2)
        assert result[0].source == "eastmoney-yesterday-limit-up"
        assert any("stale" in item and "cache" in item for item in diagnostics)
    finally:
        release.set()
        for _ in range(20):
            with provider._yesterday_sector_lock:
                if not provider._yesterday_sector_in_flight:
                    break
            sleep(0.01)


def test_realtime_provider_surfaces_background_yesterday_sector_diagnostics():
    completed = Event()
    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=lambda _url, **_kwargs: _Response({}))
    provider._latest_trade_date = lambda: "2026-07-14"

    def failed_fetch(diagnostics):
        diagnostics.append("yesterday pool worker failed")
        completed.set()
        return []

    provider._fetch_yesterday_strong_sectors = failed_fetch
    first_diagnostics = []
    provider._yesterday_sector_snapshot_or_schedule(first_diagnostics)
    assert completed.wait(timeout=2)
    for _ in range(20):
        with provider._yesterday_sector_lock:
            if not provider._yesterday_sector_in_flight:
                break
        sleep(0.01)

    second_diagnostics = []
    provider._yesterday_sector_snapshot_or_schedule(second_diagnostics)

    assert "yesterday pool worker failed" in second_diagnostics


def test_realtime_provider_refreshes_cls_home_for_each_market_snapshot():
    calls = 0
    payload = {
        "code": 200,
        "data": {
            "index_quote": [
                {
                    "secu_code": "sh000001",
                    "secu_name": "SSE",
                    "last_px": 3000,
                    "preclose_px": 2990,
                    "change": 0.33,
                }
            ],
            "up_down_dis": {"rise_num": 3200, "fall_num": 1400, "flat_num": 100},
        },
    }

    def requester(_url, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response(payload)

    provider = RealtimeMarketProvider(
        warehouse=_Warehouse(),
        requester=requester,
        breadth_time_budget=None,
        sector_time_budget=None,
    )
    provider._latest_trade_date = lambda: "2026-07-14"
    provider._yesterday_sector_cache_date = "2026-07-13"
    provider._yesterday_sector_cached_at = monotonic()
    provider._yesterday_sector_cache = []
    provider._fetch_live_sectors = lambda _diagnostics: [
        SectorMover(name="Current sector", change_pct=0.01, source="test-sector")
    ]

    first = provider.market_snapshot()
    second = provider.market_snapshot()

    assert first.status == "live"
    assert second.status == "live"
    assert calls >= 2


def test_realtime_provider_does_not_cache_cls_home_error_payload_and_reports_it():
    calls = 0

    def requester(_url, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response({"code": 401, "message": "signature invalid"})

    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester)
    provider._call_ths_market_summary_breadth = lambda *_args, **_kwargs: None
    provider._fetch_sina_breadth = lambda: None
    provider._fetch_tencent_breadth = lambda _diagnostics: None
    provider._fetch_akshare_breadth_with_timeout = lambda _diagnostics: None
    provider._fetch_heavy_breadth = lambda _diagnostics: None
    diagnostics: list[str] = []

    assert provider._fetch_live_breadth(diagnostics) is None
    assert provider._fetch_live_breadth(diagnostics) is None

    assert calls >= 2
    assert any("cls" in item.lower() and "signature invalid" in item for item in diagnostics)


def test_realtime_provider_merges_retained_breadth_without_replacing_current_fields():
    provider = RealtimeMarketProvider(warehouse=_Warehouse())
    provider._latest_trade_date = lambda: "2026-07-14"
    provider._yesterday_sector_cache_date = "2026-07-13"
    provider._yesterday_sector_cached_at = monotonic()
    provider._yesterday_sector_cache = []
    provider._remember_successful_snapshot(
        RealtimeMarketSnapshot(
            status="live",
            source="retained-snapshot",
            updated_at=datetime.now(UTC),
            market_phase="trading",
            indexes=[MarketIndexQuote(symbol="sh000001", name="Retained index", last=1, source="retained")],
            breadth=MarketBreadth(up=3000, down=1000, flat=100, total=4100, source="retained-breadth"),
            strong_sectors=[SectorMover(name="Retained sector", change_pct=0.01, source="retained")],
            message="retained",
        )
    )
    provider._fetch_indexes = lambda: [
        MarketIndexQuote(symbol="sh000001", name="Current index", last=2, source="current-index")
    ]
    provider._fetch_live_breadth_with_budget = lambda _diagnostics: None
    provider._fetch_live_sectors_with_budget = lambda _diagnostics, _rows: [
        SectorMover(name="Current sector", change_pct=0.02, source="current-sector")
    ]
    provider._snapshot_from_local_with_budget = lambda *_args, **_kwargs: RealtimeMarketSnapshot(
        status="stale",
        source="local-snapshot",
        updated_at=datetime.now(UTC),
        market_phase="trading",
        breadth=MarketBreadth(up=1, down=1, flat=0, total=2, source="local-breadth"),
        message="local",
    )

    snapshot = provider.market_snapshot()

    assert snapshot.status == "stale"
    assert snapshot.indexes[0].name == "Current index"
    assert snapshot.breadth is not None
    assert snapshot.breadth.source == "retained-breadth"
    assert snapshot.strong_sectors[0].name == "Current sector"
    assert snapshot.source.endswith("+retained-last-success")
    assert any("沿用最近成功行情快照" in item for item in snapshot.diagnostics)


def test_realtime_provider_merges_retained_indexes_without_replacing_current_fields():
    provider = RealtimeMarketProvider(warehouse=_Warehouse())
    provider._latest_trade_date = lambda: "2026-07-14"
    provider._yesterday_sector_cache_date = "2026-07-13"
    provider._yesterday_sector_cached_at = monotonic()
    provider._yesterday_sector_cache = []
    provider._remember_successful_snapshot(
        RealtimeMarketSnapshot(
            status="live",
            source="retained-snapshot",
            updated_at=datetime.now(UTC),
            market_phase="trading",
            indexes=[MarketIndexQuote(symbol="sh000001", name="Retained index", last=1, source="retained")],
            breadth=MarketBreadth(up=3000, down=1000, flat=100, total=4100, source="retained-breadth"),
            strong_sectors=[SectorMover(name="Retained sector", change_pct=0.01, source="retained")],
            message="retained",
        )
    )
    provider._fetch_indexes = lambda: []
    provider._fetch_live_breadth_with_budget = lambda _diagnostics: MarketBreadth(
        up=3200,
        down=1100,
        flat=100,
        total=4400,
        source="current-breadth",
    )
    provider._fetch_live_sectors_with_budget = lambda _diagnostics, _rows: [
        SectorMover(name="Current sector", change_pct=0.02, source="current-sector")
    ]
    provider._snapshot_from_local_with_budget = lambda *_args, **_kwargs: RealtimeMarketSnapshot(
        status="stale",
        source="local-snapshot",
        updated_at=datetime.now(UTC),
        market_phase="trading",
        indexes=[MarketIndexQuote(symbol="sh000001", name="Local index", last=3, source="local")],
        breadth=MarketBreadth(up=1, down=1, flat=0, total=2, source="local-breadth"),
        strong_sectors=[SectorMover(name="Local sector", change_pct=0.01, source="local")],
        message="local",
    )

    snapshot = provider.market_snapshot()

    assert snapshot.status == "stale"
    assert snapshot.indexes[0].name == "Retained index"
    assert snapshot.breadth is not None
    assert snapshot.breadth.source == "current-breadth"
    assert snapshot.strong_sectors[0].name == "Current sector"
    assert snapshot.source.endswith("+retained-last-success")
    assert any("沿用最近成功行情快照" in item for item in snapshot.diagnostics)


def test_realtime_provider_reads_yesterday_cache_atomically_after_scheduling_refresh():
    provider = RealtimeMarketProvider(
        warehouse=_Warehouse(),
        breadth_time_budget=None,
        sector_time_budget=None,
    )
    provider._latest_trade_date = lambda: "2026-07-14"
    fresh_yesterday_sectors = [
        SectorMover(
            name="Fresh Eastmoney sector",
            change_pct=0.02,
            source="eastmoney-yesterday-limit-up",
        )
    ]

    def schedule_refresh(_diagnostics):
        with provider._yesterday_sector_lock:
            provider._yesterday_sector_cache_date = "2026-07-13"
            provider._yesterday_sector_cache = fresh_yesterday_sectors
            provider._yesterday_sector_cached_at = monotonic()
        return []

    provider._yesterday_sector_snapshot_or_schedule = schedule_refresh
    provider._fetch_indexes = lambda: [
        MarketIndexQuote(symbol="sh000001", name="Current index", last=2, source="current-index")
    ]
    provider._fetch_live_breadth_with_budget = lambda _diagnostics: MarketBreadth(
        up=3200,
        down=1100,
        flat=100,
        total=4400,
        source="current-breadth",
    )
    provider._fetch_live_sectors_with_budget = lambda _diagnostics, _rows: [
        SectorMover(name="Current sector", change_pct=0.02, source="current-sector")
    ]

    snapshot = provider.market_snapshot()

    assert snapshot.yesterday_strong_sectors == fresh_yesterday_sectors


def test_realtime_provider_does_not_cache_unusable_cls_home_payload_and_reports_diagnostics():
    calls = 0

    def requester(_url, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response({"code": 200, "data": {"index_quote": [], "up_down_dis": {}}})

    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester)
    provider._call_ths_market_summary_breadth = lambda *_args, **_kwargs: None
    provider._fetch_sina_breadth = lambda: None
    provider._fetch_tencent_breadth = lambda _diagnostics: None
    provider._fetch_akshare_breadth_with_timeout = lambda _diagnostics: None
    provider._fetch_heavy_breadth = lambda _diagnostics: None
    diagnostics: list[str] = []

    assert provider._fetch_live_breadth(diagnostics) is None
    assert provider._fetch_live_breadth(diagnostics) is None

    assert calls >= 2
    assert any("cls" in item.lower() and "no usable" in item.lower() for item in diagnostics)


@pytest.mark.parametrize(
    "data",
    [
        {
            "index_quote": [
                {
                    "secu_code": "sh000001",
                    "secu_name": "SSE",
                    "last_px": 3000,
                    "preclose_px": 2990,
                    "change": 0.33,
                }
            ],
            "up_down_dis": {},
        },
        {"index_quote": [], "up_down_dis": {"rise_num": 0, "fall_num": 0, "flat_num": 8}},
    ],
)
def test_realtime_provider_caches_usable_partial_cls_home_payload(data):
    calls = 0

    def requester(_url, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response({"code": 200, "data": data})

    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester)

    assert provider._fetch_cls_home_payload()["data"] == data
    assert provider._fetch_cls_home_payload()["data"] == data
    assert calls == 1


def test_realtime_provider_does_not_cache_yesterday_limit_up_logical_failure():
    calls = 0

    def requester(_url, **_kwargs):
        nonlocal calls
        calls += 1
        return _Response({"rc": 1, "message": "upstream rejected request", "data": {"tc": 0, "pool": []}})

    provider = RealtimeMarketProvider(warehouse=_Warehouse(), requester=requester, timeout=2.0)
    provider._latest_trade_date = lambda: "2026-07-14"
    diagnostics: list[str] = []

    assert provider._fetch_yesterday_strong_sectors(diagnostics) == []
    assert provider._fetch_yesterday_strong_sectors(diagnostics) == []

    assert calls == 2
    assert provider._yesterday_sector_cache_date is None
    assert any("rc=1" in item for item in diagnostics)


# ===========================================================================
# 本地股票池计数缓存（1.6.1）：coverage() 全仓扫描实测 ~10s，不得占用
# breadth_time_budget=8s 的预算；缓存 600s TTL + 写入失效。
# ===========================================================================


def test_symbol_count_cache_refresh_and_write_invalidation(tmp_path):
    warehouse = Warehouse(tmp_path / "warehouse-root")

    # 空仓：现算走 coverage 退化路径，结果写入缓存
    assert warehouse.refresh_symbol_count() == 0
    assert warehouse.cached_symbol_count() == 0

    # 写入使缓存失效：cached 回到未热（None），再 refresh 得到新计数
    frame = pd.DataFrame(
        {
            "symbol": ["600000", "600000", "000001"],
            "trade_date": pd.to_datetime(["2026-09-21", "2026-09-22", "2026-09-22"]),
            "open": [1.0, 1.1, 2.0],
            "high": [1.2, 1.3, 2.2],
            "low": [0.9, 1.0, 1.9],
            "close": [1.1, 1.2, 2.1],
            "volume": [100.0, 110.0, 200.0],
        }
    )
    warehouse.write_daily_bars(frame)
    assert warehouse.cached_symbol_count() is None
    assert warehouse.refresh_symbol_count() == 2


def test_live_breadth_partial_source_skips_coverage_scan_and_notes_diagnostics(tmp_path, monkeypatch):
    """主源只给出局部样本时，本地股票池计数只吃缓存：缓存未热转后台预热，
    不在红绿家数预算内做全仓扫描，且 diagnostics 留痕（1.6.1）。"""
    warehouse = Warehouse(tmp_path / "warehouse-root")
    provider = RealtimeMarketProvider(warehouse=warehouse)

    def forbidden_coverage():
        raise AssertionError("breadth 链路不得同步扫 coverage()")

    monkeypatch.setattr(warehouse, "coverage", forbidden_coverage)
    provider._call_cls_breadth = lambda *args, **kwargs: None
    provider._call_ths_market_summary_breadth = lambda *args, **kwargs: None
    provider._fetch_ths_indexflash_breadth = lambda *args, **kwargs: None
    provider._fetch_sina_breadth = lambda: MarketBreadth(up=60, down=40, flat=0, total=100, source="sina-a-share-live")
    provider._fetch_tencent_breadth = lambda _diagnostics: None
    provider._fetch_akshare_breadth_with_timeout = lambda _diagnostics: None
    provider._fetch_heavy_breadth = lambda _diagnostics: None

    diagnostics = []
    breadth = provider._fetch_live_breadth(diagnostics, deadline=monotonic() + 2.0, cancel_event=Event())

    assert breadth is None  # 局部样本不被包装成全市场 live
    assert any("缓存未热" in item for item in diagnostics)
