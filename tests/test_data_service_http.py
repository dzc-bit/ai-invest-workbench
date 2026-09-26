from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC
from threading import Event
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pandas as pd
from astock_backtester.data.news import _parse_time
from astock_backtester.data.realtime import HeavyMarketCrawlerProvider, RealtimeMarketProvider
from astock_backtester.data.warehouse import Warehouse
from astock_backtester.models import (
    DatasetCoverage,
    MarketBreadth,
    MarketIndexQuote,
    RealtimeMarketSnapshot,
    SectorMover,
)
from astock_backtester.sample_data import sample_daily_bars
from astock_backtester.service import (
    ClientDisconnected,
    DataServiceHandler,
    DataServiceState,
    create_server,
)

# Loopback traffic must never be routed through developer/system proxies
# (http_proxy env vars or the Windows registry proxy would hijack these requests).
_OPENER = build_opener(ProxyHandler({}))

# /realtime/market-snapshot runs its index/breadth/sector fetches concurrently, and the
# breadth chain alone may legitimately spend its full 8s budget (RealtimeMarketProvider
# .breadth_time_budget) before falling back to a 2s local snapshot -- so a request whose
# breadth sources all fail answers at roughly 10s. The client timeout has to stay above
# that server-side budget, otherwise this harness reports a client timeout for a response
# the server was still assembling.
_LOOPBACK_TIMEOUT_S = 15


def _request_json(method: str, url: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(request, timeout=_LOOPBACK_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))


def _request_ndjson(url: str, payload: dict) -> list[dict]:
    data = json.dumps(payload).encode("utf-8")
    request = Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with _OPENER.open(request, timeout=5) as response:
        return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line.strip()]


def _request_get_ndjson(url: str) -> list[dict]:
    request = Request(url, method="GET", headers={"Accept": "application/x-ndjson"})
    with _OPENER.open(request, timeout=5) as response:
        return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line.strip()]


def _fake_realtime_snapshot() -> RealtimeMarketSnapshot:
    from datetime import datetime

    return RealtimeMarketSnapshot(
        status="live",
        source="fake-live",
        updated_at=datetime(2026, 5, 27, 10, 30, tzinfo=UTC),
        indexes=[
            MarketIndexQuote(
                symbol="sh000001",
                name="上证指数",
                last=3100.0,
                previous_close=3080.0,
                change=20.0,
                change_pct=0.0064935,
                source="fake-live",
                updated_at=datetime(2026, 5, 27, 10, 30, tzinfo=UTC),
            )
        ],
        breadth=MarketBreadth(up=3200, down=1800, flat=120, total=5120, source="fake-live"),
        strong_sectors=[
            SectorMover(name="半导体", change_pct=0.038, leading_symbol="688001", source="fake-live")
        ],
        yesterday_strong_sectors=[
            SectorMover(name="机器人", change_pct=0.041, leading_symbol="300024", source="fake-yesterday")
        ],
        message="ok",
    )


def test_service_health_and_logs(tmp_path):
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        health = _request_json("GET", f"http://127.0.0.1:{port}/health")
        logs = _request_json("GET", f"http://127.0.0.1:{port}/logs/recent")

        assert health["ok"] is True
        assert health["port"] == port
        assert health["cache_path"] == str(tmp_path.resolve())
        assert health["process_id"] == os.getpid()
        assert isinstance(health["executable_path"], str)
        assert isinstance(health["executable_sha256"], str)
        assert len(health["executable_sha256"]) == 64
        assert isinstance(health["started_at"], str)
        assert isinstance(health["instance_id"], str)
        assert isinstance(logs["items"], list)
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_default_daily_provider_prefers_http_before_legacy_and_akshare(tmp_path):
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)

    provider_names = [provider.name for provider in server.state.provider.providers]

    assert provider_names == ["http", "adata", "akshare"]


def test_diagnostics_data_gaps_endpoint(tmp_path):
    """缺失数据监控端点：AI 与前端共用 warehouse 缺口画像（只读，不触发抓取）。"""
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        payload = _request_json("GET", f"http://127.0.0.1:{port}/diagnostics/data-gaps")
        assert payload["ok"] is False  # 空仓 → 明确不可用而不是异常
        assert payload["profile"]["available"] is False
        assert payload["generated_at"]

        server.state.warehouse.write_daily_bars(
            pd.DataFrame(
                {
                    "symbol": ["000001", "000002"],
                    "trade_date": ["2026-06-01", "2026-06-01"],
                    "open": [10.0, 10.0],
                    "high": [10.5, 10.5],
                    "low": [9.8, 9.8],
                    "close": [10.2, 10.2],
                    "volume": [1000, 1000],
                }
            )
        )
        payload = _request_json("GET", f"http://127.0.0.1:{port}/diagnostics/data-gaps")
        assert payload["ok"] is True
        assert payload["profile"]["daily_bars"]["symbols"] == 2
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_health_returns_json_when_warehouse_coverage_fails(tmp_path):
    class BrokenWarehouse:
        def coverage(self):
            raise RuntimeError("bad warehouse partition")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.cache.write_daily_bars(sample_daily_bars())
    server.state.warehouse = BrokenWarehouse()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        health = _request_json("GET", f"http://127.0.0.1:{port}/health")

        assert health["ok"] is True
        for _ in range(10):
            if not health.get("coverage_refreshing"):
                break
            time.sleep(0.2)
            health = _request_json("GET", f"http://127.0.0.1:{port}/health")
        assert health["coverage"][0]["symbols"] >= 1
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_state_records_warehouse_coverage_failure_before_cache_fallback(
    tmp_path,
    monkeypatch,
):
    state = DataServiceState(tmp_path, port=0)

    def broken_coverage():
        raise OSError("corrupt warehouse partition")

    monkeypatch.setattr(state.warehouse, "coverage", broken_coverage)

    state._read_coverage_snapshot()

    assert any("warehouse coverage read failed" in item["message"] for item in state.logs)


def test_capital_flow_backfill_records_symbol_source_failure_before_provider_fallback(
    tmp_path,
    monkeypatch,
):
    state = DataServiceState(tmp_path, port=0)

    def broken_symbols(**_kwargs):
        raise OSError("corrupt warehouse partition")

    monkeypatch.setattr(state.warehouse, "read_daily_symbols", broken_symbols)
    monkeypatch.setattr(state.provider, "list_symbols", lambda: ["000001"])
    handler = object.__new__(DataServiceHandler)
    handler.server = SimpleNamespace(state=state)

    assert handler._capital_flow_backfill_symbols() == ["000001"]
    assert any("capital-flow symbol read failed" in item["message"] for item in state.logs)


def test_capital_flow_backfill_returns_empty_when_window_has_no_gaps(tmp_path, monkeypatch):
    state = DataServiceState(tmp_path, port=0)

    monkeypatch.setattr(state.warehouse, "read_daily_symbols", lambda **_kwargs: ["000001"])
    monkeypatch.setattr(state.warehouse, "read_capital_flow_missing_symbols", lambda start_date, end_date: set())
    handler = object.__new__(DataServiceHandler)
    handler.server = SimpleNamespace(state=state)

    assert handler._capital_flow_backfill_symbols("2026-05-26", "2026-05-29") == []
    assert any("无资金流缺口" in item["message"] for item in state.logs)


def test_capital_flow_backfill_keeps_full_local_list_when_gap_check_fails(tmp_path, monkeypatch):
    state = DataServiceState(tmp_path, port=0)

    monkeypatch.setattr(state.warehouse, "read_daily_symbols", lambda **_kwargs: ["000001", "000002"])

    def broken_gap_check(start_date, end_date):
        raise OSError("corrupt warehouse partition")

    monkeypatch.setattr(state.warehouse, "read_capital_flow_missing_symbols", broken_gap_check)
    handler = object.__new__(DataServiceHandler)
    handler.server = SimpleNamespace(state=state)

    assert handler._capital_flow_backfill_symbols("2026-05-26", "2026-05-29") == ["000001", "000002"]
    assert any("capital-flow coverage inspection failed" in item["message"] for item in state.logs)


def test_sync_symbols_records_internal_attribute_error_before_provider_fallback(
    tmp_path,
    monkeypatch,
):
    state = DataServiceState(tmp_path, port=0)

    def broken_symbols(**_kwargs):
        raise AttributeError("broken warehouse schema")

    monkeypatch.setattr(state.warehouse, "read_daily_symbols", broken_symbols)
    monkeypatch.setattr(state.provider, "list_symbols", lambda: ["000001"])

    assert state.sync_symbols(None, None) == ["000001"]
    assert any("warehouse symbol read failed" in item["message"] for item in state.logs)


def test_service_ping_is_lightweight(tmp_path):
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/ping")

        assert response == {"ok": True}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_identity_is_lightweight_and_reports_process_identity(tmp_path):
    class MustNotScanWarehouse:
        def coverage(self):
            raise AssertionError("/identity 不得同步触发全仓 coverage 扫描")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse = MustNotScanWarehouse()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/identity")

        # 旧断言 elapsed < 0.2 与耗时竞速；改为"coverage 从未被调用"的强断言
        assert response["ok"] is True
        assert response["port"] == port
        assert response["cache_path"] == str(tmp_path.resolve())
        assert response["process_id"] == os.getpid()
        assert isinstance(response["executable_path"], str)
        assert isinstance(response["executable_sha256"], str)
        assert len(response["executable_sha256"]) == 64
        assert "coverage" not in response
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_health_returns_cached_snapshot_while_coverage_refresh_is_slow(tmp_path):
    entered = Event()
    release = Event()

    class SlowWarehouse:
        def coverage(self):
            entered.set()
            assert release.wait(timeout=5), "coverage 扫描未被释放（测试收尾失败）"
            return [
                DatasetCoverage(dataset="daily_bars", symbols=99, start_date=None, end_date=None),
                DatasetCoverage(dataset="market_cap", symbols=0, start_date=None, end_date=None),
                DatasetCoverage(dataset="capital_flow", symbols=0, start_date=None, end_date=None),
            ]

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    warehouse = SlowWarehouse()
    server.state.warehouse = warehouse
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        health = _request_json("GET", f"http://127.0.0.1:{port}/health")

        # 握手断言替代 elapsed 竞速：health 返回时扫描肯定还没被放行——
        # 证明 /health 没有等待 60 秒级全仓扫描。
        assert release.is_set() is False
        assert health["ok"] is True
        assert health["coverage"][0]["dataset"] == "daily_bars"
    finally:
        release.set()
        server.shutdown()
        thread.join(timeout=5)


def test_service_health_does_not_restart_coverage_refresh_when_snapshot_is_fresh(tmp_path):
    class CountingWarehouse:
        def __init__(self):
            self.calls = 0

        def coverage(self):
            self.calls += 1
            time.sleep(0.15)
            return [
                DatasetCoverage(dataset="daily_bars", symbols=99, start_date=None, end_date=None),
                DatasetCoverage(dataset="market_cap", symbols=0, start_date=None, end_date=None),
                DatasetCoverage(dataset="capital_flow", symbols=0, start_date=None, end_date=None),
            ]

    warehouse = CountingWarehouse()
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse = warehouse
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        first = _request_json("GET", f"http://127.0.0.1:{port}/health")
        assert first["coverage_refreshing"] is True
        for _ in range(10):
            time.sleep(0.05)
            second = _request_json("GET", f"http://127.0.0.1:{port}/health")
            if not second["coverage_refreshing"]:
                break

        assert second["coverage_refreshing"] is False
        assert second["coverage"][0]["symbols"] == 99
        assert warehouse.calls == 1
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_coverage_refresh_reruns_with_post_write_data_when_forced_during_running_refresh(tmp_path, monkeypatch):
    from datetime import datetime

    state = DataServiceState(tmp_path, port=0)
    entered = Event()
    release = Event()
    reads: list[int] = []

    def fake_read_coverage():
        reads.append(len(reads))
        if len(reads) == 1:
            entered.set()
            assert release.wait(timeout=5), "首轮 coverage 读取未被释放（测试收尾失败）"
            return [DatasetCoverage(dataset="daily_bars", symbols=1, start_date=None, end_date=None)]
        return [DatasetCoverage(dataset="daily_bars", symbols=5, start_date=None, end_date=None)]

    monkeypatch.setattr(state, "_read_coverage_snapshot", fake_read_coverage)

    finished = state.start_coverage_refresh(force=True)
    assert finished is not None
    assert entered.wait(timeout=5)

    # 模拟"写入发生在刷新进行中"：路此刻请求 force refresh，写前数据即将落盘。
    written_at = datetime.now(UTC)
    assert state.start_coverage_refresh(force=True) is None
    release.set()
    assert finished.wait(timeout=5)

    assert len(reads) == 2, "写入之后必须补跑一轮以写入后数据为输入的刷新"
    assert [item.symbols for item in state.coverage_snapshot()] == [5]
    assert state._coverage_refreshed_at is not None
    # 不能用 `refreshed_at > written_at` 断言：两者都是 datetime.now(UTC)，
    # 在快机器上可以落在同一微秒（CI 实测相等，本机也可复现）。
    # 真正的不变量是"最终快照来自强制刷新之后的那一轮读取"——用序号断言：
    # 两次读取都发生过，且最终快照是第二次（symbols=5）的内容。
    assert len(reads) == 2 and state.coverage_snapshot()[0].symbols == 5
    assert state._coverage_refreshed_at >= written_at


def test_service_coverage_endpoint_returns_symbol_items(tmp_path):
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("POST", f"http://127.0.0.1:{port}/coverage/daily-bars", {})

        assert response["items"] == []
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_coverage_endpoint_skips_full_market_details_without_symbols(tmp_path):
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.cache.write_daily_bars(sample_daily_bars())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("POST", f"http://127.0.0.1:{port}/coverage/daily-bars", {})

        assert response["items"] == []
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_coverage_endpoint_filters_requested_symbols_and_dates(tmp_path):
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.cache.write_daily_bars(sample_daily_bars())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/coverage/daily-bars",
            {"symbols": ["AAA"], "start_date": "2024-01-02", "end_date": "2024-01-08"},
        )

        assert [item["symbol"] for item in response["items"]] == ["AAA"]
        assert response["items"][0]["missing_trade_dates"] == []
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_starts_full_market_sync_job(tmp_path):
    class FakeManager:
        def start_full_market(self, symbols, start_date, end_date):
            from datetime import date

            from astock_backtester.models import SyncJobStatus

            assert symbols == ["000001", "000002"]
            return SyncJobStatus(
                job_id="job-1",
                mode="full_market_bootstrap",
                status="completed",
                total_symbols=2,
                completed_symbols=2,
                failed_symbols=0,
                imported_rows=2,
                start_date=date.fromisoformat(start_date),
                end_date=date.fromisoformat(end_date),
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.sync_manager = FakeManager()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/sync/full-market",
            {"symbols": ["000001", "000002"], "start_date": "2015-01-01", "end_date": "2015-01-05"},
        )

        assert response["job"]["status"] == "completed"
        assert response["job"]["imported_rows"] == 2
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_uses_provider_symbols_for_full_market_sync_when_not_supplied(tmp_path):
    class FakeProvider:
        def list_symbols(self):
            return ["000001", "600519"]

    class FakeManager:
        def start_full_market(self, symbols, start_date, end_date):
            from datetime import date

            from astock_backtester.models import SyncJobStatus

            assert symbols == ["000001", "600519"]
            return SyncJobStatus(
                job_id="job-2",
                mode="full_market_bootstrap",
                status="completed",
                total_symbols=2,
                completed_symbols=2,
                failed_symbols=0,
                imported_rows=2,
                start_date=date.fromisoformat(start_date),
                end_date=date.fromisoformat(end_date),
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.provider = FakeProvider()
    server.state.sync_manager = FakeManager()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/sync/full-market",
            {"start_date": "2015-01-01", "end_date": "2015-01-05"},
        )

        assert response["job"]["status"] == "completed"
        assert response["job"]["total_symbols"] == 2
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_uses_local_warehouse_symbols_for_full_market_sync_before_provider_list(tmp_path):
    class SlowProvider:
        def __init__(self):
            self.list_called = False

        def list_symbols(self):
            self.list_called = True
            raise AssertionError("provider list_symbols should not run when local symbols exist")

    class FakeManager:
        def start_full_market(self, symbols, start_date, end_date):
            from datetime import date

            from astock_backtester.models import SyncJobStatus

            assert symbols == ["000001", "000002"]
            return SyncJobStatus(
                job_id="job-local-symbols",
                mode="full_market_bootstrap",
                status="completed",
                total_symbols=2,
                completed_symbols=2,
                failed_symbols=0,
                imported_rows=0,
                start_date=date.fromisoformat(start_date),
                end_date=date.fromisoformat(end_date),
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000002", "000001"],
                "trade_date": ["2026-06-18", "2026-06-18"],
                "open": [1.0, 1.0],
                "high": [1.0, 1.0],
                "low": [1.0, 1.0],
                "close": [1.0, 1.0],
                "volume": [1, 1],
                "float_market_cap": [100.0, 100.0],
            }
        )
    )
    provider = SlowProvider()
    server.state.provider = provider
    server.state.sync_manager = FakeManager()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/sync/full-market",
            {"start_date": "2026-06-18", "end_date": "2026-06-18"},
        )

        assert response["job"]["status"] == "completed"
        assert provider.list_called is False
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_realtime_stream_stops_without_secondary_write_after_client_disconnect():
    class StreamingProvider:
        def market_snapshot_events(self):
            yield {"type": "indexes", "indexes": []}
            yield {"type": "result", "snapshot": _fake_realtime_snapshot()}

    logs: list[tuple[str, str]] = []
    writes: list[dict] = []
    handler = object.__new__(DataServiceHandler)
    handler.server = SimpleNamespace(
        state=SimpleNamespace(
            realtime_provider=StreamingProvider(),
            log=lambda level, message: logs.append((level, message)),
        )
    )
    handler._send_ndjson_headers = lambda: None

    def disconnected_write(payload: dict) -> None:
        writes.append(payload)
        raise ClientDisconnected("client closed stream")

    handler._write_ndjson = disconnected_write

    handler._run_realtime_snapshot_stream()

    assert writes == [{"type": "indexes", "indexes": []}]
    assert logs == []


def test_json_response_write_treats_client_disconnect_as_normal_completion():
    writes: list[bytes] = []
    handler = object.__new__(DataServiceHandler)
    handler.send_response = lambda _status: None
    handler.send_header = lambda _key, _value: None
    handler.end_headers = lambda: None

    def disconnected_write(body: bytes) -> None:
        writes.append(body)
        raise BrokenPipeError("client closed JSON response")

    handler.wfile = SimpleNamespace(write=disconnected_write)

    handler._send_json({"ok": True})

    assert len(writes) == 1


def test_post_request_stops_without_error_response_when_client_disconnects_during_body_read():
    logs: list[tuple[str, str]] = []
    writes: list[dict] = []
    handler = object.__new__(DataServiceHandler)
    handler.path = "/symbols/validate"
    handler.server = SimpleNamespace(
        state=SimpleNamespace(log=lambda level, message: logs.append((level, message)))
    )

    def disconnected_read() -> dict:
        raise ConnectionResetError("client closed request body")

    handler._read_json = disconnected_read
    handler._send_json = lambda payload, _status=None: writes.append(payload)

    handler.do_POST()

    assert logs == []
    assert writes == []


def test_post_request_reports_upstream_connection_reset_as_domain_failure():
    logs: list[tuple[str, str]] = []
    writes: list[tuple[dict, object]] = []
    handler = object.__new__(DataServiceHandler)
    handler.path = "/symbols/validate"

    def reset_upstream(_symbols):
        raise ConnectionResetError("upstream provider reset")

    handler.server = SimpleNamespace(
        state=SimpleNamespace(
            log=lambda level, message: logs.append((level, message)),
            validate_stock_symbols=reset_upstream,
        )
    )
    handler._read_json = lambda: {"symbols": ["000001"]}
    handler._send_json = lambda payload, status=None: writes.append((payload, status))

    handler.do_POST()

    assert logs == [("error", "upstream provider reset")]
    assert writes[0][0] == {"code": "request_failed", "message": "upstream provider reset"}


def test_realtime_stream_reports_upstream_connection_reset_instead_of_treating_it_as_disconnect():
    class ResettingProvider:
        def market_snapshot_events(self):
            raise ConnectionResetError("upstream realtime reset")

        def retained_successful_snapshot(self):
            return None

    logs: list[tuple[str, str]] = []
    writes: list[dict] = []
    handler = object.__new__(DataServiceHandler)
    handler.server = SimpleNamespace(
        state=SimpleNamespace(
            realtime_provider=ResettingProvider(),
            log=lambda level, message: logs.append((level, message)),
        )
    )
    handler._send_ndjson_headers = lambda: None
    handler._write_ndjson = lambda payload: writes.append(payload)

    handler._run_realtime_snapshot_stream()

    assert logs == [("error", "realtime market snapshot stream failed: upstream realtime reset")]
    assert writes[0] == {
        "type": "error",
        "message": "upstream realtime reset",
        "code": "request_failed",
    }
    assert writes[1]["type"] == "result"


def test_service_uses_all_local_warehouse_symbols_for_recent_full_market_sync(tmp_path):
    class SlowProvider:
        def list_symbols(self):
            raise AssertionError("provider list_symbols should not run when local symbols exist")

    class FakeManager:
        def start_full_market(self, symbols, start_date, end_date):
            from datetime import date

            from astock_backtester.models import SyncJobStatus

            assert symbols == ["000001", "000002"]
            return SyncJobStatus(
                job_id="job-all-local-symbols",
                mode="full_market_bootstrap",
                status="completed",
                total_symbols=2,
                completed_symbols=2,
                imported_rows=0,
                start_date=date.fromisoformat(start_date),
                end_date=date.fromisoformat(end_date),
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000002"],
                "trade_date": ["2026-06-18", "2026-06-17"],
                "open": [1.0, 2.0],
                "high": [1.0, 2.0],
                "low": [1.0, 2.0],
                "close": [1.0, 2.0],
                "volume": [1, 1],
                "float_market_cap": [100.0, 200.0],
            }
        )
    )
    server.state.provider = SlowProvider()
    server.state.sync_manager = FakeManager()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/sync/full-market",
            {"start_date": "2026-06-18", "end_date": "2026-06-18"},
        )

        assert response["job"]["status"] == "completed"
        assert response["job"]["total_symbols"] == 2
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_coverage_endpoint_reads_warehouse_when_cache_is_empty(tmp_path):
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/coverage/daily-bars",
            {"symbols": ["AAA"], "start_date": "2024-01-02", "end_date": "2024-01-08"},
        )

        assert [item["symbol"] for item in response["items"]] == ["AAA"]
        assert response["items"][0]["rows"] >= 1
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_fetch_daily_bars_uses_configured_provider(tmp_path):
    class FakeProvider:
        def __init__(self):
            self.calls = []

        def fetch_daily_bars(self, symbol, start_date, end_date):
            self.calls.append((symbol, start_date, end_date))
            if symbol == "000002":
                return pd.DataFrame()
            return pd.DataFrame(
                {
                    "symbol": [symbol],
                    "trade_date": ["2026-05-26"],
                    "open": [10.0],
                    "high": [10.5],
                    "low": [9.8],
                    "close": [10.2],
                    "volume": [1000],
                    "amount": [10200.0],
                    "float_market_cap": [1000000000.0],
                    "total_market_cap": [1200000000.0],
                }
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    provider = FakeProvider()
    server.state.provider = provider

    class FakeCapitalFlowCrawler:
        def fetch_many_fund_flows(self, symbols, start_date, end_date, timeout=15, skip_eastmoney=False):
            return {
                "rows": [
                    {
                        "symbol": "000001",
                        "trade_date": "2026-05-26",
                        "main_net_inflow": 1_500_000.0,
                    }
                ],
                "failures": [],
                "diagnostics": [],
            }

    server.state.capital_flow_crawler = FakeCapitalFlowCrawler()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/fetch/daily-bars",
            {"symbols": ["000001", "000002"], "start_date": "2026-05-26", "end_date": "2026-05-29"},
        )

        assert provider.calls == [
            ("000001", "2026-05-26", "2026-05-29"),
            ("000002", "2026-05-26", "2026-05-29"),
        ]
        assert response["status"] == "partial"
        assert response["requested_symbols"] == ["000001", "000002"]
        assert response["fetched_symbols"] == ["000001"]
        assert response["missing_symbols"] == ["000002"]
        assert response["coverage"][0]["end_date"] == "2026-05-26"
        stored = server.state.warehouse.read_daily_bars(symbols=["000001"])
        assert stored["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2026-05-26"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_fetch_daily_bars_merges_capital_flow_from_configured_crawler(tmp_path):
    class FakeProvider:
        def fetch_daily_bars(self, symbol, start_date, end_date):
            return pd.DataFrame(
                {
                    "symbol": [symbol],
                    "trade_date": ["2026-05-26"],
                    "open": [10.0],
                    "high": [10.5],
                    "low": [9.8],
                    "close": [10.2],
                    "volume": [1000],
                    "amount": [10200.0],
                    "float_market_cap": [1000000000.0],
                    "total_market_cap": [1200000000.0],
                    "main_net_inflow": [float("nan")],
                }
            )

    class FakeCapitalFlowCrawler:
        def __init__(self):
            self.calls = []

        def fetch_many_fund_flows(self, symbols, start_date, end_date, timeout=15, skip_eastmoney=False):
            self.calls.append((symbols, start_date, end_date, timeout))
            return {
                "rows": [{"symbol": "000001", "trade_date": "2026-05-26", "main_net_inflow": 8800000.0}],
                "failures": [],
                "diagnostics": [],
            }

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    crawler = FakeCapitalFlowCrawler()
    server.state.provider = FakeProvider()
    server.state.capital_flow_crawler = crawler
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/fetch/daily-bars",
            {"symbols": ["000001"], "start_date": "2026-05-26", "end_date": "2026-05-29"},
        )

        assert crawler.calls == [(["000001"], "2026-05-26", "2026-05-29", 15)]
        assert response["status"] == "ok"
        assert response["diagnostics"][0]["code"] == "capital_flow_crawler_merge"
        stored = server.state.warehouse.read_daily_bars(symbols=["000001"])
        assert stored["main_net_inflow"].tolist() == [8800000.0]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_fetch_daily_bars_adopts_operation_coverage_into_state_snapshot(tmp_path, monkeypatch):
    import astock_backtester.service as service_module

    operation_coverage = [
        DatasetCoverage(
            dataset="daily_bars",
            symbols=7,
            start_date="2026-05-26",
            end_date="2026-05-29",
            missing_rows=0,
        )
    ]

    class FakeResult:
        logs: list = []

        def model_dump(self, mode="json"):
            return {
                "status": "ok",
                "imported_rows": 1,
                "requested_symbols": ["000001"],
                "fetched_symbols": ["000001"],
                "missing_symbols": [],
                "coverage": [],
                "logs": [],
                "diagnostics": [],
                "failures": [],
            }

    FakeResult.coverage = operation_coverage

    refresh_calls: list[bool] = []
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    monkeypatch.setattr(
        server.state,
        "start_coverage_refresh",
        lambda *, force=False: refresh_calls.append(force),
    )
    monkeypatch.setattr(service_module, "fetch_daily_bars_into_cache", lambda **_kwargs: FakeResult())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/fetch/daily-bars",
            {"symbols": ["000001"], "start_date": "2026-05-26", "end_date": "2026-05-29"},
        )

        assert response["status"] == "ok"
        assert refresh_calls == [True]
        assert [item.symbols for item in server.state.coverage_snapshot()] == [7]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_fetch_capital_flow_adopts_operation_coverage_into_state_snapshot(tmp_path, monkeypatch):
    import astock_backtester.service as service_module

    operation_coverage = [
        DatasetCoverage(
            dataset="capital_flow",
            symbols=3,
            start_date="2026-05-26",
            end_date="2026-05-29",
            missing_rows=0,
        )
    ]

    class FakeResult:
        logs: list = []

        def model_dump(self, mode="json"):
            return {
                "status": "ok",
                "imported_rows": 4,
                "requested_symbols": ["000001"],
                "fetched_symbols": ["000001"],
                "missing_symbols": [],
                "coverage": [],
                "logs": [],
                "diagnostics": [],
                "failures": [],
            }

    FakeResult.coverage = operation_coverage

    refresh_calls: list[bool] = []
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    monkeypatch.setattr(
        server.state,
        "start_coverage_refresh",
        lambda *, force=False: refresh_calls.append(force),
    )
    monkeypatch.setattr(service_module, "fetch_capital_flow_into_cache", lambda **_kwargs: FakeResult())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/fetch/capital-flow",
            {"symbols": ["000001"], "start_date": "2026-05-26", "end_date": "2026-05-29"},
        )

        assert response["status"] == "ok"
        assert refresh_calls == [True]
        assert [item.symbols for item in server.state.coverage_snapshot()] == [3]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_fetch_capital_flow_backfills_existing_rows_and_reports_failures(tmp_path):
    class FakeCapitalFlowCrawler:
        def fetch_many_fund_flows(self, symbols, start_date, end_date, timeout=15, skip_eastmoney=False):
            assert symbols == ["000001", "000002"]
            assert start_date == "2026-05-26"
            assert end_date == "2026-05-29"
            return {
                "rows": [{"symbol": "000001", "trade_date": "2026-05-26", "main_net_inflow": 8800000.0}],
                "failures": [{"symbol": "000002", "code": "network_error", "error": "remote disconnected"}],
                "diagnostics": [{"symbol": "000002", "code": "network_error", "message": "remote disconnected"}],
            }

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.capital_flow_crawler = FakeCapitalFlowCrawler()
    server.state.warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000002"],
                "trade_date": pd.to_datetime(["2026-05-26", "2026-05-26"]),
                "open": [10.0, 20.0],
                "high": [10.5, 20.5],
                "low": [9.8, 19.8],
                "close": [10.2, 20.2],
                "volume": [1000, 2000],
                "main_net_inflow": [float("nan"), float("nan")],
            }
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/fetch/capital-flow",
            {"symbols": ["000001", "000002"], "start_date": "2026-05-26", "end_date": "2026-05-29"},
        )

        assert response["status"] == "partial"
        assert response["imported_rows"] == 1
        assert response["fetched_symbols"] == ["000001"]
        assert response["missing_symbols"] == ["000002"]
        assert response["failures"] == [{"symbol": "000002", "code": "network_error", "error": "remote disconnected"}]
        assert response["diagnostics"][0]["code"] == "network_error"
        stored = server.state.warehouse.read_daily_bars(symbols=["000001", "000002"])
        assert stored.loc[stored["symbol"] == "000001", "main_net_inflow"].tolist() == [8800000.0]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_fetch_capital_flow_empty_symbols_starts_backfill_job_for_local_missing_flow_only(tmp_path):
    class FakeProvider:
        def list_symbols(self):
            return ["000002", "000003"]

    class FakeSyncManager:
        def __init__(self):
            self.calls = []

        def start_capital_flow_backfill(self, symbols, start_date, end_date):
            from datetime import date

            from astock_backtester.models import SyncJobStatus

            self.calls.append((symbols, start_date, end_date))
            return SyncJobStatus(
                job_id="flow-job",
                mode="capital_flow_backfill",
                status="running",
                total_symbols=len(symbols),
                completed_symbols=0,
                failed_symbols=0,
                imported_rows=0,
                current_symbol=symbols[0],
                start_date=date.fromisoformat(start_date),
                end_date=date.fromisoformat(end_date),
            )

        def get_job(self, job_id):
            return None

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.provider = FakeProvider()
    server.state.warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001", "000002"],
                "trade_date": pd.to_datetime(["2026-05-26", "2026-05-26"]),
                "open": [10.0, 20.0],
                "high": [10.5, 20.5],
                "low": [9.8, 19.8],
                "close": [10.2, 20.2],
                "volume": [1000, 2000],
                "main_net_inflow": [float("nan"), float("nan")],
            }
        )
    )
    manager = FakeSyncManager()
    server.state.sync_manager = manager
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/fetch/capital-flow",
            {"symbols": [], "start_date": "2026-05-26", "end_date": "2026-05-29"},
        )

        assert manager.calls == [(["000001", "000002"], "2026-05-26", "2026-05-29")]
        assert response["job"]["mode"] == "capital_flow_backfill"
        assert response["job"]["status"] == "running"
        # 任务刚启动：filled_missing_rows 必须显式为 0，与其它路径字段同构。
        assert response["filled_missing_rows"] == 0
        assert response["diagnostics"][0]["code"] == "capital_flow_backfill_job_started"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_empty_capital_flow_backfill_falls_back_to_provider_symbols_without_local_rows(tmp_path):
    class FakeProvider:
        def list_symbols(self):
            return ["000002", "000003"]

    class FakeSyncManager:
        def __init__(self):
            self.calls = []

        def start_capital_flow_backfill(self, symbols, start_date, end_date):
            from datetime import date

            from astock_backtester.models import SyncJobStatus

            self.calls.append((symbols, start_date, end_date))
            return SyncJobStatus(
                job_id="flow-job",
                mode="capital_flow_backfill",
                status="running",
                total_symbols=len(symbols),
                current_symbol=symbols[0],
                start_date=date.fromisoformat(start_date),
                end_date=date.fromisoformat(end_date),
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    manager = FakeSyncManager()
    server.state.provider = FakeProvider()
    server.state.sync_manager = manager
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/fetch/capital-flow",
            {"symbols": [], "start_date": "2026-05-26", "end_date": "2026-05-29"},
        )

        assert manager.calls == [(["000002", "000003"], "2026-05-26", "2026-05-29")]
        assert response["job"]["total_symbols"] == 2
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_fetch_capital_flow_empty_symbols_reports_no_gaps_without_starting_job(tmp_path):
    class FakeProvider:
        def __init__(self):
            self.calls = 0

        def list_symbols(self):
            self.calls += 1
            return ["000002", "000003"]

    class FakeSyncManager:
        def __init__(self):
            self.calls = []

        def start_capital_flow_backfill(self, symbols, start_date, end_date):
            self.calls.append((symbols, start_date, end_date))
            raise AssertionError("窗口内没有资金流缺口时不得启动补齐任务")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    provider = FakeProvider()
    server.state.provider = provider
    server.state.warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001"] * 4,
                "trade_date": pd.to_datetime(
                    ["2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29"]
                ),
                "open": [10.0] * 4,
                "high": [10.5] * 4,
                "low": [9.8] * 4,
                "close": [10.2] * 4,
                "volume": [1000] * 4,
                "main_net_inflow": [1_000_000.0] * 4,
            }
        )
    )
    manager = FakeSyncManager()
    server.state.sync_manager = manager
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        request = Request(
            f"http://127.0.0.1:{port}/fetch/capital-flow",
            data=json.dumps(
                {"symbols": [], "start_date": "2026-05-26", "end_date": "2026-05-29"}
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _OPENER.open(request, timeout=_LOOPBACK_TIMEOUT_S) as raw:
            # "没活干"是 200 正常结果，不是 validation_error。
            assert raw.status == 200
            response = json.loads(raw.read().decode("utf-8"))

        assert manager.calls == []
        assert provider.calls == 0
        assert response["status"] == "ok"
        assert "job" not in response
        assert "capital_flow_backfill_no_gaps" in {item["code"] for item in response["diagnostics"]}
        assert response["failures"] == []
        assert response["logs"] and response["logs"][-1]["message"]
        assert isinstance(response["coverage"], list)
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_streams_backtest_trade_events_before_final_result(tmp_path, basic_strategy, basic_settings):
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.cache.write_daily_bars(sample_daily_bars())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        events = _request_ndjson(
            f"http://127.0.0.1:{port}/run/backtest/stream",
            {
                "strategy": json.loads(basic_strategy.model_dump_json()),
                "settings": json.loads(basic_settings.model_dump_json()),
            },
        )

        assert [event["type"] for event in events][:2] == ["phase", "phase"]
        progress_index = next(index for index, event in enumerate(events) if event["type"] == "progress")
        opened_index = next(index for index, event in enumerate(events) if event["type"] == "trade_opened")
        trade_index = next(index for index, event in enumerate(events) if event["type"] == "trade_closed")
        result_index = next(index for index, event in enumerate(events) if event["type"] == "result")
        assert progress_index < result_index
        assert opened_index < trade_index
        assert trade_index < result_index
        assert events[progress_index]["message"].startswith("扫描")
        assert events[opened_index]["trade"]["sell_date"] is None
        assert events[trade_index]["trade"]["symbol"] == "AAA"
        assert events[result_index]["result"]["metrics"]["trade_count"] >= 1
        latest_matches = events[result_index]["result"]["latest_strategy_matches"]
        assert latest_matches["signal_date"] == "2024-01-08"
        assert latest_matches["trade_date"] == "2024-01-08"
        assert isinstance(latest_matches["matches"], list)
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_backtest_stream_reads_only_custom_symbols_from_warehouse(tmp_path, basic_strategy, basic_settings):
    class RecordingWarehouse(Warehouse):
        def __init__(self, root):
            super().__init__(root)
            self.read_calls = []

        def read_daily_bars(self, *args, **kwargs):
            self.read_calls.append(kwargs)
            return super().read_daily_bars(*args, **kwargs)

    warehouse = RecordingWarehouse(tmp_path)
    warehouse.write_daily_bars(sample_daily_bars())
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse = warehouse
    settings = basic_settings.model_copy(update={"stock_pool": "custom", "custom_symbols": ["AAA"]})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        events = _request_ndjson(
            f"http://127.0.0.1:{port}/run/backtest/stream",
            {
                "strategy": json.loads(basic_strategy.model_dump_json()),
                "settings": json.loads(settings.model_dump_json()),
            },
        )

        assert events[-1]["type"] == "result"
        assert warehouse.read_calls[0]["symbols"] == ["AAA"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_backtest_stream_reads_warmup_history_before_requested_start(tmp_path, basic_strategy, basic_settings):
    class RecordingWarehouse(Warehouse):
        def __init__(self, root):
            super().__init__(root)
            self.read_calls = []

        def read_daily_bars(self, *args, **kwargs):
            self.read_calls.append(kwargs)
            return super().read_daily_bars(*args, **kwargs)

    warehouse = RecordingWarehouse(tmp_path)
    warehouse.write_daily_bars(sample_daily_bars())
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse = warehouse
    settings = basic_settings.model_copy(
        update={
            "start_date": pd.Timestamp("2024-01-08").date(),
            "end_date": pd.Timestamp("2024-01-08").date(),
            "stock_pool": "custom",
            "custom_symbols": ["AAA"],
        }
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        events = _request_ndjson(
            f"http://127.0.0.1:{port}/run/backtest/stream",
            {
                "strategy": json.loads(basic_strategy.model_dump_json()),
                "settings": json.loads(settings.model_dump_json()),
            },
        )

        assert events[-1]["type"] == "result"
        assert warehouse.read_calls[0]["symbols"] == ["AAA"]
        assert warehouse.read_calls[0]["start_date"] < "2024-01-08"
        assert warehouse.read_calls[0]["end_date"] == "2024-01-08"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_streams_serialized_trade_blocked_event(tmp_path, basic_settings):
    from astock_backtester.models import (
        ConditionGroup,
        ConditionNode,
        ConditionOperator,
        StrategyConfig,
    )

    frame = pd.DataFrame(
        [
            {
                "symbol": "000001",
                "trade_date": pd.Timestamp("2024-01-02"),
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 1000,
                "is_suspended": False,
                "listing_days": 500,
                "float_market_cap": 2_000_000_000,
                "main_net_inflow": 0.0,
                "is_st": False,
            },
            {
                "symbol": "000001",
                "trade_date": pd.Timestamp("2024-01-03"),
                "open": 11.0,
                "high": 11.0,
                "low": 11.0,
                "close": 11.0,
                "pre_close": 10.0,
                "volume": 1000,
                "is_suspended": False,
                "listing_days": 501,
                "float_market_cap": 2_000_000_000,
                "main_net_inflow": 0.0,
                "is_st": False,
            },
        ]
    )
    strategy = StrategyConfig(
        name="block buy",
        entry_groups=[
            ConditionGroup(
                id="entry",
                operator=ConditionOperator.AND,
                conditions=[
                    ConditionNode(
                        id="cap",
                        condition_id="market_cap_between",
                        params={"min": 1_000_000_000, "max": 10_000_000_000},
                    )
                ],
            )
        ],
    )
    settings = basic_settings.model_copy(
        update={
            "start_date": pd.Timestamp("2024-01-02").date(),
            "end_date": pd.Timestamp("2024-01-03").date(),
            "fixed_holding_days": 1,
            "min_listing_days": 0,
            "limit_up_blocks_buy": True,
            "slippage_rate": 0,
            "fee_rate": 0,
            "stamp_tax_rate": 0,
        }
    )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(frame)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        events = _request_ndjson(
            f"http://127.0.0.1:{port}/run/backtest/stream",
            {
                "strategy": json.loads(strategy.model_dump_json()),
                "settings": json.loads(settings.model_dump_json()),
            },
        )

        blocked = next(event for event in events if event["type"] == "trade_blocked")
        result = next(event for event in events if event["type"] == "result")["result"]
        assert blocked["trade"]["blocked_reason"] == "次日开盘接近涨停，未买入：000001"
        assert blocked["trade"]["shares"] == 0
        assert blocked["trade"]["buy_amount"] == 0
        assert blocked["trade"]["pnl_pct"] is None
        assert result["metrics"]["trade_count"] == 0
        assert result["latest_strategy_matches"]["matches"][0]["symbol"] == "000001"
        assert isinstance(result["latest_strategy_matches"]["matches"][0]["rank_score"], (int, float))
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_uses_configured_provider(tmp_path):
    class FakeRealtimeProvider:
        def market_snapshot(self):
            from datetime import datetime

            from astock_backtester.models import (
                MarketBreadth,
                MarketIndexQuote,
                RealtimeMarketSnapshot,
                SectorMover,
            )

            return RealtimeMarketSnapshot(
                status="live",
                source="fake-live",
                updated_at=datetime(2026, 5, 27, 10, 30, tzinfo=UTC),
                indexes=[
                    MarketIndexQuote(
                        symbol="sh000001",
                        name="上证指数",
                        last=3100.0,
                        previous_close=3080.0,
                        change=20.0,
                        change_pct=0.0064935,
                        source="fake-live",
                        updated_at=datetime(2026, 5, 27, 10, 30, tzinfo=UTC),
                    )
                ],
                breadth=MarketBreadth(up=3200, down=1800, flat=120, total=5120, source="fake-live"),
                strong_sectors=[
                    SectorMover(name="半导体", change_pct=0.038, leading_symbol="688001", source="fake-live")
                ],
                yesterday_strong_sectors=[
                    SectorMover(name="机器人", change_pct=0.041, leading_symbol="300024", source="fake-yesterday")
                ],
                message="ok",
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.realtime_provider = FakeRealtimeProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["status"] == "live"
        assert response["source"] == "fake-live"
        assert response["indexes"][0]["name"] == "上证指数"
        assert response["breadth"]["up"] == 3200
        assert response["strong_sectors"][0]["name"] == "半导体"
        assert response["yesterday_strong_sectors"][0]["name"] == "机器人"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_streams_partial_events_before_result(tmp_path):
    class FakeRealtimeProvider:
        def market_snapshot_events(self):
            snapshot = _fake_realtime_snapshot()
            yield {"type": "indexes", "indexes": snapshot.indexes, "updated_at": snapshot.updated_at, "market_phase": snapshot.market_phase}
            yield {"type": "breadth", "breadth": snapshot.breadth, "updated_at": snapshot.updated_at, "market_phase": snapshot.market_phase}
            yield {
                "type": "sectors",
                "strong_sectors": snapshot.strong_sectors,
                "yesterday_strong_sectors": snapshot.yesterday_strong_sectors,
                "updated_at": snapshot.updated_at,
                "market_phase": snapshot.market_phase,
            }
            yield {"type": "result", "snapshot": snapshot}

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.realtime_provider = FakeRealtimeProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        events = _request_get_ndjson(f"http://127.0.0.1:{port}/realtime/market-snapshot/stream")

        assert [event["type"] for event in events] == ["indexes", "breadth", "sectors", "result"]
        assert events[0]["indexes"][0]["name"] == "上证指数"
        assert events[1]["breadth"]["total"] == 5120
        assert events[2]["strong_sectors"][0]["name"] == "半导体"
        assert events[-1]["snapshot"]["status"] == "live"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_returns_json_when_provider_raises(tmp_path):
    class BrokenRealtimeProvider:
        def market_snapshot(self):
            raise RuntimeError("upstream timeout")

        def retained_successful_snapshot(self):
            return None

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.realtime_provider = BrokenRealtimeProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["status"] == "unavailable"
        assert response["source"] == "service-fallback"
        assert response["indexes"] == []
        assert response["breadth"] is None
        assert response["market_phase"] in {"trading", "pre_open", "lunch_break", "post_close", "non_trading"}
        assert any("upstream timeout" in item for item in response["diagnostics"])
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_uses_provider_last_success_after_raise(tmp_path):
    class RetainingBrokenRealtimeProvider:
        def __init__(self):
            self._retained = _fake_realtime_snapshot()

        def market_snapshot(self):
            raise RuntimeError("upstream timeout")

        def retained_successful_snapshot(self):
            return self._retained

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.realtime_provider = RetainingBrokenRealtimeProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["status"] == "stale"
        assert response["indexes"][0]["name"] == "上证指数"
        assert response["source"].endswith("+service-retained-last-success")
        assert any("upstream timeout" in item for item in response["diagnostics"])
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_realtime_market_provider_reuses_last_successful_snapshot_on_failure(tmp_path):
    warehouse = Warehouse(tmp_path)
    calls: list[str] = []

    class FakeResponse:
        encoding = "gbk"

        def __init__(self, text: str):
            self.text = text

        def raise_for_status(self) -> None:
            return None

    def requester(url, **kwargs):
        calls.append(url)
        if "hq.sinajs.cn/list=sh000001" in url:
            if len([item for item in calls if "hq.sinajs.cn/list=sh000001" in item]) == 1:
                text = (
                    'var hq_str_sh000001="上证指数,3100.00,3080.00,3100.00,3120.00,3070.00,'
                    '0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-06-05,14:50:00,00";\n'
                    'var hq_str_sz399001="深证成指,9800.00,9700.00,9800.00,9900.00,9650.00,'
                    '0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-06-05,14:50:00,00";\n'
                    'var hq_str_sz399006="创业板指,2100.00,2080.00,2100.00,2120.00,2070.00,'
                    '0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-06-05,14:50:00,00";'
                )
                return FakeResponse(text)
            raise RuntimeError("sina index timeout")
        return FakeResponse("")

    provider = RealtimeMarketProvider(warehouse, requester=requester)
    provider._fetch_live_sectors = lambda: [SectorMover(name="AI应用", change_pct=0.035, source="test")]
    provider._fetch_live_breadth = lambda: MarketBreadth(up=3200, down=1600, flat=100, total=4900, source="test")

    first = provider.market_snapshot()
    second = provider.market_snapshot()

    assert first.status == "live"
    assert second.status == "stale"
    assert second.indexes[0].name == "上证指数"
    assert second.strong_sectors[0].name == "AI应用"
    assert second.source.endswith("+retained-last-success")
    assert any("沿用最近成功行情快照" in item for item in second.diagnostics)


def test_service_realtime_market_snapshot_prefers_ths_concept_page_and_skips_eastmoney_push2(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    requested_requests = []

    def requester(url, **kwargs):
        requested_requests.append((url, kwargs.get("params", {})))
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
            )
        if "q.10jqka.com.cn/gn/" in url:
            return FakeResponse(
                text="""
                <html><body>
                <input type="hidden" id="gnSection"
                  value='{"1":{"platecode":"885001","platename":"电力设备","199112":3.2},
                          "2":{"platecode":"885002","platename":"半导体","199112":2.5}}'>
                </body></html>
                """
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["strong_sectors"][0]["name"] == "电力设备"
        assert response["strong_sectors"][0]["source"] == "ths-concept-section"
        assert "沪市主板" not in [item["name"] for item in response["strong_sectors"]]
        assert not any("eastmoney.com/api/qt/clist/get" in url for url, _ in requested_requests)
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_prefers_cls_breadth_and_hot_plate(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    requested_urls: list[str] = []

    def requester(url, **kwargs):
        requested_urls.append(url)
        if "quote/index/home" in url:
            return FakeResponse(
                {
                    "code": 200,
                    "data": {
                        "index_quote": [
                            {
                                "secu_code": "sh000001",
                                "secu_name": "上证指数",
                                "last_px": 4010.03,
                                "preclose_px": 3959.337,
                                "change_px": 50.69,
                                "change": 0.0128,
                            }
                        ],
                        "up_down_dis": {
                            "rise_num": 3322,
                            "fall_num": 2049,
                            "flat_num": 156,
                            "suspend_num": 12,
                            "up_10": 291,
                            "up_8": 175,
                            "up_6": 353,
                            "up_4": 757,
                            "up_2": 1746,
                            "down_2": 1482,
                            "down_4": 399,
                            "down_6": 112,
                            "down_8": 22,
                            "down_10": 34,
                        },
                    },
                }
            )
        if "web_quote/plate/hot_plate" in url:
            return FakeResponse(
                {
                    "code": 200,
                    "data": {
                        "industry": [
                            {
                                "secu_code": "cls82247",
                                "secu_name": "电子化学品",
                                "change": 0.0706,
                                "up_stock": [{"secu_code": "sz300576", "secu_name": "容大感光", "change": 0.1594}],
                            }
                        ],
                        "concept": [
                            {
                                "secu_code": "cls80537",
                                "secu_name": "功率半导体",
                                "change": 0.0501,
                                "up_stock": [{"secu_code": "sh603290", "secu_name": "斯达半导", "change": 0.1}],
                            }
                        ],
                        "area": [],
                    },
                }
            )
        if "hq.sinajs.cn" in url:
            raise AssertionError("CLS index quote should make Sina index request unnecessary")
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["indexes"][0]["source"] == "cls-quote-index"
        assert response["breadth"]["source"] == "cls-quote-breadth"
        assert response["breadth"]["up"] == 3322
        assert response["breadth"]["down"] == 2049
        assert response["breadth"]["flat"] == 156
        assert response["breadth"]["total"] == 5527
        assert response["breadth"]["distribution"]["suspend"] == 12
        assert response["strong_sectors"][0]["name"] == "电子化学品"
        assert response["strong_sectors"][0]["source"] == "cls-hot-plate"
        assert abs(response["strong_sectors"][0]["change_pct"] - 0.0706) < 0.000001
        assert response["strong_sectors"][0]["leading_symbol"] == "300576"
        assert response["source"] == "cls-quote-index+cls-quote-breadth+cls-hot-plate"
        assert "实时指数来自财联社指数" in response["message"]
        assert "红绿家数来自财联社涨跌分布" in response["message"]
        assert "强势题材来自财联社热门板块" in response["message"]
        assert "暂不可用" not in response["message"]
        assert any("quote/index/home" in url for url in requested_urls)
        assert any("web_quote/plate/hot_plate" in url for url in requested_urls)
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_prefers_ths_concept_quotes_over_industry_quotes(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    def requester(url, **kwargs):
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
            )
        if "q.10jqka.com.cn/gn/" in url:
            return FakeResponse(
                text="""
                <html><body>
                <input type="hidden" id="gnSection"
                  value='{"1":{"platecode":"885001","platename":"AI应用","199112":4.6,"zjjlr":12.3,"zfl":77},
                          "2":{"platecode":"885002","platename":"机器人概念","199112":3.1,"zjjlr":8.5,"zfl":68}}'>
                </body></html>
                """
            )
        if "q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/1/" in url:
            return FakeResponse(
                text="""
                <html><body>
                <table class="m-table m-pager-table">
                  <tbody>
                    <tr>
                      <td>1</td><td>白酒</td><td>3.52</td><td>416.94</td><td>272.51</td><td>27.66</td><td>18</td><td>1</td><td>65.36</td><td>酒鬼酒</td><td>45.74</td><td>10.01</td>
                    </tr>
                  </tbody>
                </table>
                </body></html>
                """
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["strong_sectors"]
        assert response["strong_sectors"][0]["name"] == "AI应用"
        assert response["strong_sectors"][0]["change_pct"] == 0.046
        assert response["strong_sectors"][0]["source"] == "ths-concept-section"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_falls_back_to_eastmoney_sector_api_without_using_it_for_breadth(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    requested_requests = []

    def requester(url, **kwargs):
        requested_requests.append((url, kwargs.get("params", {})))
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-06-06,10:30:00";'
            )
        params = kwargs.get("params", {})
        fs = str(params.get("fs", ""))
        if "m:90+t:3" in fs:
            return FakeResponse(
                {
                    "data": {
                        "diff": [
                            {"f12": "BK1036", "f14": "半导体", "f3": 2.5, "f128": "688001"},
                            {"f12": "BK0985", "f14": "机器人概念", "f3": 1.8, "f128": "300024"},
                            {"f12": "BK1122", "f14": "AI应用", "f3": 1.5, "f128": "300001"},
                        ]
                    }
                }
            )
        if "m:90+t:2" in fs:
            return FakeResponse({"data": {"diff": [{"f12": "BK0473", "f14": "电力行业", "f3": 1.2}]}})
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["strong_sectors"][0]["name"] == "半导体"
        assert response["strong_sectors"][0]["source"] == "eastmoney-sector"
        assert abs(response["strong_sectors"][0]["change_pct"] - 0.025) < 0.000001
        assert response["breadth"] is None
        assert response["message"].endswith("红绿家数暂不可用，未展示全市场宽度。")
        assert any("eastmoney.com" in url for url, _ in requested_requests)
        assert not any(
            "eastmoney.com" in url
            and params.get("fs") == "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            for url, params in requested_requests
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_realtime_provider_retries_eastmoney_sector_hosts_before_giving_up(tmp_path):
    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    requested_urls = []

    def requester(url, **kwargs):
        requested_urls.append(url)
        if "push2.eastmoney.com/api/qt/clist/get" in url and "82.push2" not in url:
            raise OSError("remote disconnected")
        return FakeResponse(
            {
                "data": {
                    "diff": [
                        {"f12": "BK1036", "f14": "半导体", "f3": 2.5, "f128": "688001"},
                        {"f12": "BK0985", "f14": "机器人概念", "f3": 1.8, "f128": "300024"},
                        {"f12": "BK1122", "f14": "AI应用", "f3": 1.5, "f128": "300001"},
                    ]
                }
            }
        )

    provider = RealtimeMarketProvider(Warehouse(tmp_path), requester=requester)

    sectors = provider._parse_sector_rows(
        provider._fetch_eastmoney_sector_rows(["m:90+t:3+f:!50", "m:90+t:3"]),
        "eastmoney-sector",
    )

    assert sectors[0].name == "半导体"
    assert sectors[0].source == "eastmoney-sector"
    assert any(url.startswith("https://push2.eastmoney.com") for url in requested_urls)
    assert any(url.startswith("https://82.push2.eastmoney.com") for url in requested_urls)


def test_realtime_provider_rejects_too_few_eastmoney_sector_rows_with_diagnostics(tmp_path):
    class FakeResponse:
        def raise_for_status(self):
            return

        def json(self):
            return {
                "data": {
                    "diff": [
                        {"f12": "BK1036", "f14": "半导体", "f3": 2.5, "f128": "688001"},
                    ]
                }
            }

    diagnostics: list[str] = []
    provider = RealtimeMarketProvider(Warehouse(tmp_path), requester=lambda url, **kwargs: FakeResponse())

    rows = provider._fetch_eastmoney_sector_rows(["m:90+t:3+f:!50"], diagnostics=diagnostics)

    assert rows == []
    assert any("valid_rows=1" in item and "below_min=3" in item for item in diagnostics)


def test_realtime_provider_reports_eastmoney_sector_request_failures(tmp_path):
    def requester(url, **kwargs):
        raise OSError("remote disconnected")

    diagnostics: list[str] = []
    provider = RealtimeMarketProvider(Warehouse(tmp_path), requester=requester)

    rows = provider._fetch_eastmoney_sector_rows(["m:90+t:3+f:!50"], diagnostics=diagnostics)

    assert rows == []
    assert any("request failed" in item and "remote disconnected" in item for item in diagnostics)
    assert any("m:90+t:3+f:!50" in item for item in diagnostics)


def test_realtime_provider_treats_eastmoney_sub_one_sector_changes_as_percent_units(tmp_path):
    class FakeResponse:
        def raise_for_status(self):
            return

        def json(self):
            return {
                "data": {
                    "diff": [
                        {"f12": "BK1036", "f14": "半导体", "f3": 0.8, "f128": "688001"},
                        {"f12": "BK0985", "f14": "机器人概念", "f3": 0.7, "f128": "300024"},
                        {"f12": "BK1122", "f14": "AI应用", "f3": 0.6, "f128": "300001"},
                    ]
                }
            }

    provider = RealtimeMarketProvider(Warehouse(tmp_path), requester=lambda url, **kwargs: FakeResponse())

    sectors = provider._parse_sector_rows(
        provider._fetch_eastmoney_sector_rows(["m:90+t:3+f:!50"]),
        "eastmoney-sector",
    )

    assert sectors[0].name == "半导体"
    assert abs(sectors[0].change_pct - 0.008) < 0.000001


def test_realtime_provider_bounds_slow_ths_sector_sources_before_eastmoney_fallback(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None):
            self._payload = payload or {}

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    requested_requests = []

    def requester(url, **kwargs):
        params = kwargs.get("params", {})
        requested_requests.append((url, kwargs.get("timeout"), params))
        if "q.10jqka.com.cn/gn/" in url or "q.10jqka.com.cn/thshy/" in url:
            raise TimeoutError("ths sector source timed out quickly")
        if "eastmoney.com/api/qt/clist/get" in url:
            return FakeResponse(
                {
                    "data": {
                        "diff": [
                            {"f12": "BK1036", "f14": "半导体", "f3": 2.5, "f128": "688001"},
                            {"f12": "BK0985", "f14": "机器人概念", "f3": 1.8, "f128": "300024"},
                            {"f12": "BK1122", "f14": "AI应用", "f3": 1.5, "f128": "300001"},
                        ]
                    }
                }
            )
        return FakeResponse({"data": {"diff": []}})

    provider = RealtimeMarketProvider(Warehouse(tmp_path), requester=requester)
    provider.timeout = 4.0
    provider.sector_time_budget = 3.0

    sectors = provider._fetch_live_sectors()

    assert sectors[0].source == "eastmoney-sector"
    ths_timeouts = [
        timeout
        for url, timeout, _ in requested_requests
        if "q.10jqka.com.cn/gn/" in url or "q.10jqka.com.cn/thshy/" in url
    ]
    assert ths_timeouts
    assert max(ths_timeouts) <= 1.0
    assert not any(
        "eastmoney.com" in url
        and params.get("fs") == "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
        for url, _, params in requested_requests
    )


def test_realtime_provider_uses_akshare_sector_fallback_when_eastmoney_sector_unavailable(tmp_path, monkeypatch):
    class FakeAkshare:
        def stock_board_concept_name_em(self):
            return pd.DataFrame(
                [
                    {"板块代码": "BK1036", "板块名称": "半导体", "涨跌幅": 2.5, "领涨股票": "688001"},
                    {"板块代码": "BK0985", "板块名称": "机器人概念", "涨跌幅": 1.8, "领涨股票": "300024"},
                ]
            )

        def stock_board_industry_name_em(self):
            return pd.DataFrame([])

    import sys

    monkeypatch.setitem(sys.modules, "akshare", FakeAkshare())
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    provider._fetch_cls_hot_plate_sectors = lambda diagnostics: []
    provider._fetch_ths_concept_section_rows = lambda: []
    provider._fetch_ths_industry_html_rows = lambda: []
    provider._fetch_eastmoney_sector_rows = lambda fs_values: []
    provider._fetch_sina_sectors = lambda: []

    sectors = provider._fetch_live_sectors()

    assert sectors[0].name == "半导体"
    assert sectors[0].source == "akshare-sector"
    assert abs(sectors[0].change_pct - 0.025) < 0.000001


def test_realtime_provider_times_out_slow_akshare_sector_before_eastmoney_backup(tmp_path):
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    provider.sector_source_timeout = 0.01
    provider._fetch_cls_hot_plate_sectors = lambda diagnostics: []
    provider._fetch_ths_concept_section_rows = lambda: []
    provider._fetch_ths_industry_html_rows = lambda: []
    provider._fetch_sina_sectors = lambda: []

    def slow_akshare_rows(board_type):
        time.sleep(0.2)
        return [{"f12": "BK1036", "f14": "akshare-sector-name", "f3": 9.9}]

    provider._fetch_akshare_sector_rows = slow_akshare_rows
    provider._fetch_eastmoney_sector_rows = lambda fs_values: [
        {"f12": "BK1036", "f14": "eastmoney-sector-name", "f3": 2.5}
    ]
    diagnostics: list[str] = []

    started_at = time.perf_counter()
    sectors = provider._fetch_live_sectors(diagnostics)
    elapsed = time.perf_counter() - started_at

    # 只需排除网络 provider 链；0.15s 在高负载下会误报。
    assert elapsed < 1.0
    assert sectors[0].name == "eastmoney-sector-name"
    assert sectors[0].source == "eastmoney-sector"
    assert any("akshare-sector" in item and "timeout" in item.lower() for item in diagnostics)


def test_realtime_provider_times_out_slow_akshare_breadth_before_heavy_backup(tmp_path):
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    provider.breadth_source_timeout = 0.01
    provider._latest_local_symbol_count = lambda: 5000
    provider._coverage_symbol_count = lambda: 5000
    provider._fetch_cls_breadth = lambda: None
    provider._fetch_ths_market_summary_breadth = lambda: None
    provider._fetch_sina_breadth = lambda: None
    provider._fetch_tencent_breadth = lambda diagnostics: None

    def slow_akshare_breadth(diagnostics):
        time.sleep(0.2)
        return MarketBreadth(up=4900, down=100, flat=0, total=5000, source="akshare-a-share-live")

    provider._fetch_akshare_breadth = slow_akshare_breadth
    provider._fetch_heavy_breadth = lambda diagnostics: MarketBreadth(
        up=3200,
        down=1600,
        flat=200,
        total=5000,
        source="heavy-market-crawler",
    )
    diagnostics: list[str] = []

    started_at = time.perf_counter()
    breadth = provider._fetch_live_breadth(diagnostics)
    elapsed = time.perf_counter() - started_at

    # 只需排除网络 provider 链；0.15s 在高负载下会误报。
    assert elapsed < 1.0
    assert breadth is not None
    assert breadth.source == "heavy-market-crawler"
    assert any("AKShare 实时个股" in item and ("timeout" in item.lower() or "超时" in item) for item in diagnostics)


def test_realtime_provider_keeps_cls_hot_plate_on_request_timeout_only(tmp_path):
    class FakeResponse:
        def __init__(self, payload=None):
            self._payload = payload or {}

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    requested: list[tuple[str, float | None]] = []

    def requester(url, **kwargs):
        requested.append((url, kwargs.get("timeout")))
        time.sleep(0.03)
        return FakeResponse(
            {
                "data": {
                    "industry": [
                        {
                            "secu_code": "cls82247",
                            "secu_name": "cls-sector-name",
                            "change": 0.035,
                            "up_stock": [{"secu_code": "sz300576"}],
                        }
                    ],
                    "concept": [],
                    "area": [],
                }
            }
        )

    provider = RealtimeMarketProvider(Warehouse(tmp_path), requester=requester)
    provider.sector_source_timeout = 0.01

    sectors = provider._fetch_cls_hot_plate_sectors([])

    assert sectors[0].name == "cls-sector-name"
    assert sectors[0].source == "cls-hot-plate"
    assert requested == [("https://x-quote.cls.cn/web_quote/plate/hot_plate", 0.01)]


def test_realtime_provider_prefers_sina_sector_before_akshare_fallback(tmp_path, monkeypatch):
    class FakeAkshare:
        def stock_board_concept_name_em(self):
            return pd.DataFrame([{"板块代码": "BK1036", "板块名称": "半导体", "涨跌幅": 9.9}])

        def stock_board_industry_name_em(self):
            return pd.DataFrame([])

    import sys

    monkeypatch.setitem(sys.modules, "akshare", FakeAkshare())
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    provider._fetch_cls_hot_plate_sectors = lambda diagnostics: []
    provider._fetch_ths_concept_section_rows = lambda: []
    provider._fetch_ths_industry_html_rows = lambda: []
    provider._fetch_eastmoney_sector_rows = lambda fs_values: []
    provider._fetch_sina_sectors = lambda: [SectorMover(name="机器人行业", change_pct=0.03, source="sina-sector")]

    sectors = provider._fetch_live_sectors()

    assert sectors[0].name == "机器人行业"
    assert sectors[0].source == "sina-sector"


def test_realtime_provider_prefers_sina_sector_before_eastmoney_backup(tmp_path):
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    eastmoney_calls = []

    provider._fetch_cls_hot_plate_sectors = lambda diagnostics: []
    provider._fetch_ths_concept_section_rows = lambda: []
    provider._fetch_ths_industry_html_rows = lambda: []
    provider._fetch_sina_sectors = lambda: [SectorMover(name="sina-sector-name", change_pct=0.03, source="sina-sector")]

    def eastmoney_rows(fs_values):
        eastmoney_calls.append(fs_values)
        return [{"f12": "BK1036", "f14": "eastmoney-sector-name", "f3": 5.0}]

    provider._fetch_eastmoney_sector_rows = eastmoney_rows

    sectors = provider._fetch_live_sectors([])

    assert sectors[0].name == "sina-sector-name"
    assert sectors[0].source == "sina-sector"
    assert eastmoney_calls == []


def test_realtime_provider_reports_failed_live_sector_sources(tmp_path):
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    diagnostics: list[str] = []

    provider._fetch_cls_hot_plate_sectors = lambda diagnostics: []
    provider._fetch_ths_concept_section_rows = lambda: []
    provider._fetch_ths_industry_html_rows = lambda: []
    provider._fetch_sina_sectors = lambda: []
    provider._fetch_akshare_sector_rows = lambda board_type: []
    provider._fetch_eastmoney_sector_rows = lambda fs_values: []
    provider._fetch_ths_hot_topic_rows = lambda: []

    sectors = provider._fetch_live_sectors(diagnostics)

    assert sectors == []
    assert any("ths-concept-section" in item for item in diagnostics)
    assert any("ths-industry-html" in item for item in diagnostics)
    assert any("sina-sector" in item for item in diagnostics)
    assert any("akshare-sector" in item for item in diagnostics)
    assert any("eastmoney-sector" in item for item in diagnostics)


def test_service_realtime_market_snapshot_parses_sub_one_ths_concept_pct_as_percent_unit(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    def requester(url, **kwargs):
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
            )
        if "q.10jqka.com.cn/gn/" in url:
            return FakeResponse(
                text="""
                <html><body>
                <input type="hidden" id="gnSection"
                  value='{"1":{"platecode":"885001","platename":"预制菜","199112":0.99,"zjjlr":10.9,"zfl":62}}'>
                </body></html>
                """
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["strong_sectors"][0]["name"] == "预制菜"
        assert abs(response["strong_sectors"][0]["change_pct"] - 0.0099) < 0.000001
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_sorts_ths_concept_percent_units(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    def requester(url, **kwargs):
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
            )
        if "q.10jqka.com.cn/gn/" in url:
            return FakeResponse(
                text="""
                <html><body>
                <input type="hidden" id="gnSection"
                  value='{"1":{"platecode":"885001","platename":"预制菜","199112":0.99,"zjjlr":10.9,"zfl":62},
                          "2":{"platecode":"885002","platename":"AI应用","199112":4.6,"zjjlr":12.3,"zfl":77}}'>
                </body></html>
                """
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert [item["name"] for item in response["strong_sectors"][:2]] == ["AI应用", "预制菜"]
        assert abs(response["strong_sectors"][0]["change_pct"] - 0.046) < 0.000001
        assert abs(response["strong_sectors"][1]["change_pct"] - 0.0099) < 0.000001
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_falls_back_to_ths_industry_page_when_concepts_fail(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    requested_requests = []

    def requester(url, **kwargs):
        requested_requests.append((url, kwargs.get("params", {})))
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
            )
        if "q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/1/" in url:
            return FakeResponse(
                text="""
                <html><body>
                <table class="m-table m-pager-table">
                  <tbody>
                    <tr>
                      <td>1</td><td>小金属</td><td>2.80</td><td>416.94</td><td>272.51</td><td>27.66</td><td>18</td><td>1</td><td>65.36</td><td>洛阳钼业</td><td>45.74</td><td>10.01</td>
                    </tr>
                    <tr>
                      <td>2</td><td>电力</td><td>2.10</td><td>15974.60</td><td>1314.34</td><td>69.90</td><td>83</td><td>24</td><td>8.23</td><td>长江电力</td><td>5.72</td><td>11.28</td>
                    </tr>
                  </tbody>
                </table>
                </body></html>
                """
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["strong_sectors"]
        assert response["strong_sectors"][0]["name"] == "小金属"
        assert response["strong_sectors"][0]["source"] == "ths-industry-html"
        assert "同花顺行业板块总览" in response["message"]
        assert not any(
            "eastmoney.com" in url
            and params.get("fs") == "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            for url, params in requested_requests
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_uses_ths_market_summary_breadth_when_available(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    requested_requests = []

    def requester(url, **kwargs):
        requested_requests.append((url, kwargs.get("params", {})))
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
            )
        if "q.10jqka.com.cn/index/index/board/all/" in url:
            return FakeResponse(text="<html><body>上涨：3210 下跌：1820 平盘：120</body></html>")
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["breadth"] == {"up": 3210, "down": 1820, "flat": 120, "total": 5150, "source": "ths-market-summary"}
        assert "红绿家数来自同花顺市场总览" in response["message"]
        assert not any(
            "eastmoney.com" in url
            and params.get("fs") == "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            for url, params in requested_requests
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_rejects_partial_sina_breadth_before_fallback(tmp_path):
    import pandas as pd

    class FakeResponse:
        text = ""
        encoding = "utf-8"
        content = b""

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    def requester(url, **kwargs):
        if "hq.sinajs.cn" in url:
            if "sh000001" in url:
                return FakeResponse(
                    text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
                )
            return FakeResponse(
                text=(
                    'var hq_str_sz000001="平安银行,0,10.00,10.50,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
                    'var hq_str_sh600000="浦发银行,0,8.00,7.90,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
                    'var hq_str_sz300001="特锐德,0,20.00,20.00,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
                )
            )
        if "10jqka.com.cn" in url:
            return FakeResponse(text="")
        params = kwargs.get("params", {})
        fs = str(params.get("fs", ""))
        fields = str(params.get("fields", ""))
        if "m:90+t:3" in fs:
            return FakeResponse({"data": {"diff": [{"f12": "BK1036", "f14": "半导体", "f3": 2.5, "f128": "688001"}]}})
        if fields == "f12,f14,f3":
            return FakeResponse(
                {
                    "data": {
                        "diff": [
                            {"f12": "000001", "f14": "平安银行", "f3": 1.2},
                            {"f12": "000002", "f14": "万科A", "f3": 1.1},
                        ]
                    }
                }
            )
        return FakeResponse({"data": {"diff": []}})

    bars = pd.DataFrame(
        [
            ("000001", "2026-05-26", 10.0, 10.8, 9.8, 10.0, 1000),
            ("600000", "2026-05-26", 8.0, 8.2, 7.8, 8.0, 1000),
            ("300001", "2026-05-26", 20.0, 20.4, 19.8, 20.0, 1000),
        ],
        columns=["symbol", "trade_date", "open", "high", "low", "close", "volume"],
    )
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(bars)
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["breadth"] is None
        assert response["status"] == "stale"
        assert any("sina-a-share-live" in item and "全市场红绿家数不完整" in item for item in response["diagnostics"])
        assert any("local-latest" in item and "全市场红绿家数不完整" in item for item in response["diagnostics"])
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_realtime_provider_rejects_partial_live_breadth_with_diagnostics(tmp_path):
    warehouse = Warehouse(tmp_path)
    provider = RealtimeMarketProvider(warehouse)
    diagnostics: list[str] = []

    provider._latest_local_symbol_count = lambda: 5100
    provider._fetch_cls_breadth = lambda: None
    provider._fetch_ths_market_summary_breadth = lambda: None
    provider._fetch_sina_breadth = lambda: MarketBreadth(
        up=107,
        down=80,
        flat=5,
        total=192,
        source="sina-a-share-live",
    )
    provider._fetch_tencent_breadth = lambda diagnostics: None
    provider._fetch_akshare_breadth = lambda diagnostics: None
    provider._fetch_heavy_breadth = lambda diagnostics: None
    provider._fetch_eastmoney_breadth = lambda diagnostics: None

    breadth = provider._fetch_live_breadth(diagnostics)

    assert breadth is None
    assert any("sina-a-share-live" in item and "total=192" in item for item in diagnostics)
    assert any("全市场红绿家数不完整" in item for item in diagnostics)


def test_realtime_provider_accepts_cls_quotation_breadth_without_extra_completeness_gate(tmp_path):
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    diagnostics: list[str] = []

    provider._latest_local_symbol_count = lambda: 5500
    provider._coverage_symbol_count = lambda: 5500
    provider._fetch_cls_breadth = lambda: MarketBreadth(
        up=101,
        down=34,
        flat=0,
        total=135,
        source="cls-quote-breadth",
    )
    provider._fetch_ths_market_summary_breadth = lambda: (_ for _ in ()).throw(
        AssertionError("should not query slower breadth providers after CLS returns numbers")
    )

    breadth = provider._fetch_live_breadth(diagnostics)

    assert breadth is not None
    assert breadth.source == "cls-quote-breadth"
    assert breadth.up == 101
    assert breadth.down == 34
    assert diagnostics == []


def test_realtime_provider_returns_cls_breadth_before_slow_local_count_scan(tmp_path):
    provider = RealtimeMarketProvider(Warehouse(tmp_path))
    provider.breadth_time_budget = 0.05
    diagnostics: list[str] = []

    def slow_local_count():
        time.sleep(0.2)
        return 5500

    provider._latest_local_symbol_count = slow_local_count
    provider._coverage_symbol_count = slow_local_count
    provider._fetch_cls_breadth = lambda: MarketBreadth(
        up=101,
        down=34,
        flat=0,
        total=135,
        source="cls-quote-breadth",
    )
    provider._fetch_ths_market_summary_breadth = lambda: (_ for _ in ()).throw(
        AssertionError("CLS breadth should return before local completeness scans")
    )

    started_at = time.perf_counter()
    breadth = provider._fetch_live_breadth_with_budget(diagnostics)
    elapsed = time.perf_counter() - started_at

    # 只需排除网络 provider 链；0.15s 在高负载下会误报。
    assert elapsed < 1.0
    assert breadth is not None
    assert breadth.source == "cls-quote-breadth"
    assert diagnostics == []


def test_realtime_provider_cls_breadth_uses_a_realistic_home_timeout(tmp_path):
    observed_timeouts = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "code": 200,
                "data": {
                    "up_down_dis": {
                        "rise_num": 3200,
                        "fall_num": 1800,
                        "flat_num": 100,
                    }
                },
            }

    def requester(_url, **kwargs):
        observed_timeouts.append(kwargs["timeout"])
        return FakeResponse()

    provider = RealtimeMarketProvider(Warehouse(tmp_path), requester=requester)

    breadth = provider._fetch_cls_breadth([])

    assert breadth is not None
    assert observed_timeouts[0] >= 2.0


def test_realtime_provider_breadth_waits_for_the_shared_cls_home_request(tmp_path):
    request_started = threading.Event()

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "code": 200,
                "data": {
                    "index_quote": [
                        {
                            "secu_code": "sh000001",
                            "secu_name": "上证指数",
                            "last_px": 3100,
                            "change": 0.01,
                        }
                    ],
                    "up_down_dis": {
                        "rise_num": 3200,
                        "fall_num": 1800,
                        "flat_num": 100,
                    },
                },
            }

    def requester(_url, **_kwargs):
        request_started.set()
        time.sleep(0.18)
        return FakeResponse()

    provider = RealtimeMarketProvider(
        Warehouse(tmp_path),
        requester=requester,
        timeout=0.5,
        breadth_time_budget=0.5,
        breadth_source_timeout=0.05,
    )
    owner_result = []
    owner = threading.Thread(
        target=lambda: owner_result.append(provider._fetch_cls_home_payload(timeout=0.3))
    )
    owner.start()
    assert request_started.wait(timeout=0.1)

    breadth = provider._fetch_cls_breadth(
        [],
        deadline=time.monotonic() + 0.4,
    )
    owner.join(timeout=1)

    assert owner_result
    assert breadth is not None
    assert breadth.total == 5100


def test_realtime_provider_rejects_breadth_below_local_pool_ratio(tmp_path):
    warehouse = Warehouse(tmp_path)
    provider = RealtimeMarketProvider(warehouse)
    diagnostics: list[str] = []

    provider._latest_local_symbol_count = lambda: 5100

    accepted = provider._breadth_is_complete(
        MarketBreadth(up=1800, down=1200, flat=100, total=3100, source="sina-a-share-live"),
        5100,
        diagnostics,
    )

    assert accepted is False
    assert any("sina-a-share-live" in item and "total=3100" in item and "比例=60.8%" in item for item in diagnostics)


def test_realtime_provider_rejects_partial_local_breadth_against_warehouse_coverage():
    from datetime import UTC, date, datetime

    bars = pd.DataFrame(
        [
            {
                "symbol": f"{index:06d}",
                "trade_date": pd.Timestamp("2026-06-05"),
                "open": 10.0,
                "high": 10.5,
                "low": 9.8,
                "close": 10.0,
                "volume": 1000,
            }
            for index in range(1, 193)
        ]
    )

    class PartialLatestWarehouse:
        def read_latest_daily_bars(self, days=3):
            return bars

        def cached_symbol_count(self):
            return 5100

        def refresh_symbol_count(self):
            return 5100

        def coverage(self):
            return [
                DatasetCoverage(
                    dataset="daily_bars",
                    symbols=5100,
                    start_date=date(2015, 1, 5),
                    end_date=date(2026, 6, 5),
                    missing_rows=0,
                )
            ]

    provider = RealtimeMarketProvider(PartialLatestWarehouse())

    snapshot = provider._snapshot_from_local(
        datetime(2026, 6, 7, tzinfo=UTC),
        skip_topic_fetch=True,
    )

    assert snapshot.breadth is None
    assert any("local-latest" in item and "total=192" in item for item in snapshot.diagnostics)


def test_realtime_provider_heavy_breadth_uses_browser_provider_after_public_html_failure(tmp_path):
    warehouse = Warehouse(tmp_path)
    provider = RealtimeMarketProvider(warehouse)
    provider._latest_local_symbol_count = lambda: 5100

    def requester(*args, **kwargs):
        raise RuntimeError("public html blocked")

    provider.requester = requester

    class FakeBrowserProvider:
        def fetch_breadth_from_dom(self, url):
            return MarketBreadth(up=3300, down=1500, flat=300, total=5100, source="browser-market-provider")

    provider._heavy_market_provider = HeavyMarketCrawlerProvider(
        requester=requester,
        timeout=0.01,
        browser_provider=FakeBrowserProvider(),
    )

    diagnostics: list[str] = []
    breadth = provider._fetch_live_breadth(diagnostics)

    assert breadth is not None
    assert breadth.source == "browser-market-provider"


def test_heavy_market_crawler_does_not_reuse_cached_breadth_as_current_live_data():
    def requester(*args, **kwargs):
        raise RuntimeError("public market crawler blocked")

    provider = HeavyMarketCrawlerProvider(requester=requester, timeout=0.01)
    provider._last_successful_breadth = MarketBreadth(
        up=3300,
        down=1500,
        flat=300,
        total=5100,
        source="heavy-market-crawler",
    )

    assert provider.fetch_breadth() is None


def test_realtime_provider_live_message_labels_heavy_breadth_source(tmp_path):
    provider = RealtimeMarketProvider(Warehouse(tmp_path))

    message = provider._build_live_message(
        MarketBreadth(up=3300, down=1500, flat=300, total=5100, source="browser-market-provider"),
        [],
    )

    assert "浏览器公开行情爬虫" in message


def test_realtime_provider_live_message_does_not_claim_local_breadth_when_breadth_missing(tmp_path):
    provider = RealtimeMarketProvider(Warehouse(tmp_path))

    message = provider._build_live_message(
        None,
        [SectorMover(name="半导体", change_pct=0.025, source="sina-sector")],
    )

    assert "红绿家数暂不可用" in message
    assert "红绿家数暂回退" not in message


def test_service_market_fupan_keeps_user_mode_candidates_out_of_briefing(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"
        content = b""
        apparent_encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text
            self.content = text.encode("utf-8")

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    requested_requests = []

    def requester(url, **kwargs):
        requested_requests.append((url, kwargs.get("params", {})))
        if "stock.10jqka.com.cn/fupan/" in url:
            return FakeResponse(
                text="""
                <html><body>
                  <div id="fpzj">复盘摘要：机器人和算力活跃。</div>
                  <div class="fp_item_hd"><h2>同花顺解盘</h2></div>
                  <div class="fp_item_cnt"><p>机器人板块午后持续走强，算力方向有承接。</p></div>
                </body></html>
                """
            )
        raise AssertionError(f"unexpected network request: {url}")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.briefing_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/fupan")

        assert response["source"] == "ths-fupan"
        assert [section["title"] for section in response["sections"]] == ["同花顺解盘"]
        assert "当日 user 模式匹配个股" not in json.dumps(response, ensure_ascii=False)
        assert not any(
            "eastmoney.com" in url
            and params.get("fs") == "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            for url, params in requested_requests
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_realtime_provider_ignores_empty_sina_breadth_batches_after_valid_quotes(tmp_path):
    import pandas as pd

    class FakeResponse:
        encoding = "gbk"

        def __init__(self, text: str):
            self.text = text
            self.content = text.encode("gbk", errors="ignore")

        def raise_for_status(self):
            return

    bars = pd.DataFrame(
        [
            ("000001", "2026-05-26", 10.0, 10.8, 9.8, 10.0, 1000),
            ("920002", "2026-05-26", 8.0, 8.2, 7.8, 8.0, 1000),
        ],
        columns=["symbol", "trade_date", "open", "high", "low", "close", "volume"],
    )
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(bars)

    def requester(url, **kwargs):
        if "sz000001" in url:
            return FakeResponse(
                'var hq_str_sz000001="平安银行,0,10.00,10.50,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
            )
        return FakeResponse('var hq_str_sh920002="";')

    provider = RealtimeMarketProvider(warehouse, requester=requester)

    breadth = provider._fetch_sina_breadth()

    assert breadth is not None
    assert breadth.source == "sina-a-share-live"
    assert breadth.up == 1
    assert breadth.down == 0
    assert breadth.flat == 0
    assert breadth.total == 1


def test_service_realtime_market_snapshot_uses_local_breadth_without_eastmoney_when_live_breadth_unavailable(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    requested_requests = []

    def requester(url, **kwargs):
        requested_requests.append((url, kwargs.get("params", {})))
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["breadth"] is None
        assert any("local-latest" in item and "全市场红绿家数不完整" in item for item in response["diagnostics"])
        assert not any(
            "eastmoney.com" in url
            and params.get("fs") == "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            for url, params in requested_requests
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_realtime_provider_sina_breadth_batches_are_bounded(tmp_path):
    import pandas as pd

    class FakeResponse:
        encoding = "gbk"

        def __init__(self, text: str):
            self.text = text
            self.content = text.encode("gbk", errors="ignore")

        def raise_for_status(self):
            return

    bars = pd.DataFrame(
        [
            (f"{index:06d}", "2026-05-26", 10.0, 10.8, 9.8, 10.0, 1000)
            for index in range(1, 406)
        ],
        columns=["symbol", "trade_date", "open", "high", "low", "close", "volume"],
    )
    bars["trade_date"] = pd.to_datetime(bars["trade_date"])
    warehouse = Warehouse(tmp_path)
    warehouse.write_daily_bars(bars)
    requested_symbol_counts = []

    def requester(url, **kwargs):
        symbols = url.split("list=", 1)[1].split(",")
        requested_symbol_counts.append(len(symbols))
        text = "".join(
            f'var hq_str_{symbol}="S,0,10.00,10.50,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
            for symbol in symbols
        )
        return FakeResponse(text)

    provider = RealtimeMarketProvider(warehouse, requester=requester)
    breadth = provider._fetch_sina_breadth()

    assert breadth is not None
    assert breadth.up == 405
    assert requested_symbol_counts == [400, 5]


def test_realtime_provider_returns_breadth_without_waiting_for_slow_sector_sources(tmp_path):
    warehouse = Warehouse(tmp_path)
    provider = RealtimeMarketProvider(warehouse)
    provider.sector_time_budget = 0.02
    provider._fetch_indexes = lambda: [
        MarketIndexQuote(
            symbol="sh000001",
            name="上证指数",
            last=3100.0,
            previous_close=3080.0,
            change=20.0,
            change_pct=0.0064935,
            source="fake-live",
        )
    ]
    provider._fetch_live_breadth = lambda: MarketBreadth(up=3200, down=1700, flat=200, total=5100, source="fast-breadth")

    def slow_sectors():
        time.sleep(0.2)
        return [SectorMover(name="慢板块", change_pct=0.05, source="slow-sector")]

    provider._fetch_live_sectors = slow_sectors

    def local_snapshot(now):
        return RealtimeMarketSnapshot(
            status="stale",
            source="local-latest",
            updated_at=now,
            indexes=[],
            breadth=MarketBreadth(up=1, down=1, flat=0, total=2, source="local-latest"),
            strong_sectors=[SectorMover(name="本地题材", change_pct=0.02, source="local-market-group")],
            yesterday_strong_sectors=[],
            message="local",
        )

    provider._snapshot_from_local = local_snapshot

    started_at = time.perf_counter()
    snapshot = provider.market_snapshot()
    elapsed = time.perf_counter() - started_at

    # 只需排除网络 provider 链；0.15s 在高负载下会误报。
    assert elapsed < 1.0
    assert snapshot.status == "stale"
    assert snapshot.breadth is not None
    assert snapshot.breadth.source == "fast-breadth"
    assert snapshot.strong_sectors[0].source == "local-market-group"
    assert any("实时强势题材接口超时" in item for item in snapshot.diagnostics)


def test_realtime_provider_returns_sectors_without_waiting_for_slow_breadth_sources(tmp_path):
    warehouse = Warehouse(tmp_path)
    provider = RealtimeMarketProvider(warehouse)
    provider.breadth_time_budget = 0.02
    provider.local_snapshot_time_budget = 0.02
    provider._fetch_indexes = lambda: [
        MarketIndexQuote(
            symbol="sh000001",
            name="上证指数",
            last=3100.0,
            previous_close=3080.0,
            change=20.0,
            change_pct=0.0064935,
            source="fake-live",
        )
    ]

    def slow_breadth(diagnostics):
        time.sleep(0.2)
        return MarketBreadth(up=3200, down=1700, flat=200, total=5100, source="slow-breadth")

    provider._fetch_live_breadth = slow_breadth
    provider._fetch_live_sectors = lambda diagnostics: [
        SectorMover(name="快板块", change_pct=0.05, source="fast-sector")
    ]

    def local_snapshot(now):
        return RealtimeMarketSnapshot(
            status="stale",
            source="local-latest",
            updated_at=now,
            indexes=[],
            breadth=None,
            strong_sectors=[],
            yesterday_strong_sectors=[],
            message="local",
        )

    provider._snapshot_from_local = local_snapshot

    started_at = time.perf_counter()
    snapshot = provider.market_snapshot()
    elapsed = time.perf_counter() - started_at

    # 只需排除网络 provider 链；0.15s 在高负载下会误报。
    assert elapsed < 1.0
    assert snapshot.status == "stale"
    assert snapshot.breadth is None
    assert snapshot.strong_sectors[0].source == "fast-sector"
    assert any("实时红绿家数接口超时" in item for item in snapshot.diagnostics)


def test_realtime_provider_does_not_wait_for_slow_local_fallback_after_live_partial(tmp_path):
    warehouse = Warehouse(tmp_path)
    provider = RealtimeMarketProvider(warehouse)
    provider.breadth_time_budget = 0.02
    provider.local_snapshot_time_budget = 0.02
    provider._fetch_indexes = lambda: [
        MarketIndexQuote(
            symbol="sh000001",
            name="上证指数",
            last=3100.0,
            previous_close=3080.0,
            change=20.0,
            change_pct=0.0064935,
            source="fake-live",
        )
    ]
    provider._fetch_live_breadth = lambda diagnostics: None
    provider._fetch_live_sectors = lambda diagnostics: [
        SectorMover(name="快板块", change_pct=0.05, source="fast-sector")
    ]

    def slow_local_snapshot(now):
        time.sleep(0.2)
        return RealtimeMarketSnapshot(
            status="stale",
            source="local-latest",
            updated_at=now,
            indexes=[],
            breadth=MarketBreadth(up=1, down=1, flat=0, total=2, source="local-latest"),
            strong_sectors=[],
            yesterday_strong_sectors=[],
            message="local",
        )

    provider._snapshot_from_local = slow_local_snapshot

    started_at = time.perf_counter()
    snapshot = provider.market_snapshot()
    elapsed = time.perf_counter() - started_at

    # 只需排除网络 provider 链；0.15s 在高负载下会误报。
    assert elapsed < 1.0
    assert snapshot.status == "stale"
    assert snapshot.breadth is None
    assert snapshot.strong_sectors[0].source == "fast-sector"
    assert any("本地兜底行情快照超时" in item for item in snapshot.diagnostics)


def test_realtime_provider_does_not_cache_stale_snapshot_with_local_fallback_sectors(tmp_path):
    warehouse = Warehouse(tmp_path)
    provider = RealtimeMarketProvider(warehouse)
    provider._fetch_indexes = lambda: [
        MarketIndexQuote(
            symbol="sh000001",
            name="涓婅瘉鎸囨暟",
            last=3100.0,
            previous_close=3080.0,
            change=20.0,
            change_pct=0.0064935,
            source="fake-live",
        )
    ]
    provider._fetch_live_breadth = lambda: MarketBreadth(
        up=3200,
        down=1700,
        flat=200,
        total=5100,
        source="fast-breadth",
    )
    provider._fetch_live_sectors = lambda: []

    def local_snapshot(now):
        return RealtimeMarketSnapshot(
            status="stale",
            source="local-latest",
            updated_at=now,
            indexes=[],
            breadth=None,
            strong_sectors=[SectorMover(name="鏈湴棰樻潗", change_pct=0.02, source="local-market-group")],
            yesterday_strong_sectors=[],
            message="local",
        )

    provider._snapshot_from_local = local_snapshot

    snapshot = provider.market_snapshot()

    assert snapshot.status == "stale"
    assert snapshot.breadth is not None
    assert snapshot.breadth.source == "fast-breadth"
    assert snapshot.strong_sectors[0].source == "local-market-group"
    assert provider._last_successful_snapshot is None


def test_realtime_provider_live_snapshot_does_not_build_local_fallback(tmp_path):
    warehouse = Warehouse(tmp_path)
    provider = RealtimeMarketProvider(warehouse)
    provider._fetch_indexes = lambda: [
        MarketIndexQuote(
            symbol="sh000001",
            name="上证指数",
            last=3100.0,
            previous_close=3080.0,
            change=20.0,
            change_pct=0.0064935,
            source="fake-live",
        )
    ]
    provider._fetch_live_breadth = lambda diagnostics: MarketBreadth(
        up=3200,
        down=1700,
        flat=200,
        total=5100,
        source="fast-breadth",
    )
    provider._fetch_live_sectors = lambda diagnostics: [
        SectorMover(name="电力", change_pct=0.024, leading_symbol="600001", source="ths-industry-html")
    ]

    def fail_local_snapshot(now):
        raise AssertionError("live snapshot should not build local fallback")

    provider._snapshot_from_local = fail_local_snapshot

    snapshot = provider.market_snapshot()

    assert snapshot.status == "live"
    assert snapshot.strong_sectors[0].source == "ths-industry-html"
    assert snapshot.yesterday_strong_sectors == []


def test_service_realtime_market_snapshot_tracks_yesterday_strong_sectors_from_local_history(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    requested_requests = []

    def requester(url, **kwargs):
        requested_requests.append((url, kwargs.get("params", {})))
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-27,10:30:00";'
            )
        if "zx.10jqka.com.cn/event/api/getharden" in url:
            return FakeResponse(
                {
                    "errocode": 0,
                    "errormsg": "",
                    "data": [
                        {"code": "300001", "name": "A", "reason": "电力设备", "zhangfu": 9.9, "chengjiaoe": 100000},
                        {"code": "300002", "name": "B", "reason": "电力设备", "zhangfu": 8.2, "chengjiaoe": 90000},
                    ],
                }
            )
        return FakeResponse({"data": {"diff": []}})

    import pandas as pd

    rows = []
    for symbol, _sector_prefix, prev_close, yesterday_close, latest_close in [
        ("300001", "创业板", 10.0, 12.0, 12.2),
        ("300002", "创业板", 20.0, 24.0, 24.1),
        ("600001", "沪市主板", 10.0, 10.4, 10.8),
        ("600002", "沪市主板", 20.0, 20.2, 20.5),
    ]:
        rows.extend(
            [
                {
                    "symbol": symbol,
                    "trade_date": pd.Timestamp("2024-01-03"),
                    "open": prev_close,
                    "high": prev_close,
                    "low": prev_close,
                    "close": prev_close,
                    "volume": 1000,
                    "turnover_rate": 0.02,
                    "float_market_cap": 1_000_000_000,
                    "main_net_inflow": 0.0,
                    "is_st": False,
                    "is_suspended": False,
                    "listing_days": 200,
                },
                {
                    "symbol": symbol,
                    "trade_date": pd.Timestamp("2024-01-04"),
                    "open": yesterday_close,
                    "high": yesterday_close,
                    "low": yesterday_close,
                    "close": yesterday_close,
                    "volume": 1000,
                    "turnover_rate": 0.02,
                    "float_market_cap": 1_000_000_000,
                    "main_net_inflow": 0.0,
                    "is_st": False,
                    "is_suspended": False,
                    "listing_days": 201,
                },
                {
                    "symbol": symbol,
                    "trade_date": pd.Timestamp("2024-01-05"),
                    "open": latest_close,
                    "high": latest_close,
                    "low": latest_close,
                    "close": latest_close,
                    "volume": 1000,
                    "turnover_rate": 0.02,
                    "float_market_cap": 1_000_000_000,
                    "main_net_inflow": 0.0,
                    "is_st": False,
                    "is_suspended": False,
                    "listing_days": 202,
                },
            ]
        )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(pd.DataFrame(rows))
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["strong_sectors"][0]["name"] == "电力设备"
        assert response["strong_sectors"][0]["source"] == "local-market-group"
        assert response["yesterday_strong_sectors"]
        assert response["yesterday_strong_sectors"][0]["name"] == "电力设备"
        assert response["yesterday_strong_sectors"][0]["source"] == "local-yesterday-group"
        assert response["message"].endswith("昨日强势板块追踪来自本地历史。")
        assert not any(
            "eastmoney.com" in url
            and params.get("fs") == "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
            for url, params in requested_requests
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_prefers_ths_market_summary_breadth(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

        @property
        def content(self):
            return self.text.encode("gbk", errors="ignore")

    def requester(url, **kwargs):
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-30,10:30:00";'
            )
        if "q.10jqka.com.cn" in url and "index/index/board/all" in url:
            return FakeResponse(
                text="""
                <html><body>
                <div class="page_info">上涨：3210只 下跌：1820只 平盘：120只</div>
                </body></html>
                """
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["breadth"] == {"up": 3210, "down": 1820, "flat": 120, "total": 5150, "source": "ths-market-summary"}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_prefers_ths_industry_quotes_over_hot_reason_labels(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

        @property
        def content(self):
            return self.text.encode("gbk", errors="ignore")

    def requester(url, **kwargs):
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-30,10:30:00";'
            )
        if "zx.10jqka.com.cn/event/api/getharden" in url:
            return FakeResponse(
                payload={
                    "errocode": 0,
                    "errormsg": "",
                    "data": [
                        {
                            "code": "300001",
                            "name": "A",
                            "reason": "算力租赁+AI政务",
                            "zhangfu": 9.9,
                            "huanshou": 12.0,
                            "chengjiaoe": 100000,
                        },
                        {
                            "code": "300002",
                            "name": "B",
                            "reason": "算力租赁+液冷服务器",
                            "zhangfu": 8.2,
                            "huanshou": 9.0,
                            "chengjiaoe": 90000,
                        },
                        {
                            "code": "300003",
                            "name": "C",
                            "reason": "液冷服务器+AI政务",
                            "zhangfu": 6.8,
                            "huanshou": 7.0,
                            "chengjiaoe": 70000,
                        },
                    ],
                }
            )
        if "q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/1/" in url:
            return FakeResponse(
                text="""
                <html><body>
                <table class="m-table m-pager-table">
                  <tbody>
                    <tr>
                      <td>1</td><td>白酒</td><td>3.52</td><td>416.94</td><td>272.51</td><td>27.66</td><td>18</td><td>1</td><td>65.36</td><td>酒鬼酒</td><td>45.74</td><td>10.01</td>
                    </tr>
                    <tr>
                      <td>2</td><td>电力</td><td>2.40</td><td>15974.60</td><td>1314.34</td><td>69.90</td><td>83</td><td>24</td><td>8.23</td><td>珈伟新能</td><td>5.72</td><td>11.28</td>
                    </tr>
                  </tbody>
                </table>
                </body></html>
                """
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["strong_sectors"]
        assert response["strong_sectors"][0]["name"] == "白酒"
        assert response["strong_sectors"][0]["change_pct"] == 0.0352
        assert response["strong_sectors"][0]["source"] == "ths-industry-html"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_does_not_use_raw_hot_reason_pct_when_quotes_fail(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

        @property
        def content(self):
            return self.text.encode("gbk", errors="ignore")

    def requester(url, **kwargs):
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-30,10:30:00";'
            )
        if "zx.10jqka.com.cn/event/api/getharden" in url:
            return FakeResponse(
                payload={
                    "errocode": 0,
                    "errormsg": "",
                    "data": [
                        {
                            "code": "300001",
                            "name": "A",
                            "reason": "算力租赁+AI政务",
                            "zhangfu": 9.9,
                            "huanshou": 12.0,
                            "chengjiaoe": 100000,
                        },
                        {
                            "code": "300002",
                            "name": "B",
                            "reason": "算力租赁+液冷服务器",
                            "zhangfu": 8.2,
                            "huanshou": 9.0,
                            "chengjiaoe": 90000,
                        },
                        {
                            "code": "300003",
                            "name": "C",
                            "reason": "液冷服务器+AI政务",
                            "zhangfu": 6.8,
                            "huanshou": 7.0,
                            "chengjiaoe": 70000,
                        },
                    ],
                }
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["strong_sectors"] == []
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_falls_back_to_ths_industry_html(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

        @property
        def content(self):
            return self.text.encode("gbk", errors="ignore")

    def requester(url, **kwargs):
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-05-30,10:30:00";'
            )
        if "zx.10jqka.com.cn/event/api/getharden" in url:
            return FakeResponse(payload={"errocode": 1, "errormsg": "unavailable", "data": []})
        if "q.10jqka.com.cn/thshy/index/field/199112/order/desc/page/1/" in url:
            return FakeResponse(
                text="""
                <html><body>
                <table class="m-table m-pager-table">
                  <tbody>
                    <tr>
                      <td>1</td><td>电力</td><td>2.40</td><td>15974.60</td><td>1314.34</td><td>69.90</td><td>83</td><td>24</td><td>8.23</td><td>珈伟新能</td><td>5.72</td><td>11.28</td>
                    </tr>
                    <tr>
                      <td>2</td><td>白酒</td><td>2.10</td><td>1000</td><td>100</td><td>20</td><td>18</td><td>1</td><td>5.0</td><td>酒鬼酒</td><td>45.74</td><td>10.01</td>
                    </tr>
                  </tbody>
                </table>
                </body></html>
                """
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["strong_sectors"]
        assert response["strong_sectors"][0]["name"] == "电力"
        assert response["strong_sectors"][0]["source"] == "ths-industry-html"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_parses_current_sina_sector_payload(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "gbk"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    def requester(url, **kwargs):
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-06-06,10:30:00";'
            )
        if "vip.stock.finance.sina.com.cn/q/view/newSinaHy.php" in url:
            return FakeResponse(
                text=(
                    'var S_Finance_bankuai_sinaindustry = {'
                    '"new_robot":"new_robot,机器人行业,8,20.0,0.6,3.00,100,200,sz300001,9.9,10.0,1.0,领涨股",'
                    '"new_power":"new_power,电力行业,62,10.0,-0.4,-4.00,100,200,sh600001,1.1,10.0,1.0,领跌股"'
                    "};"
                )
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["strong_sectors"][0]["name"] == "机器人行业"
        assert response["strong_sectors"][0]["source"] == "sina-sector"
        assert abs(response["strong_sectors"][0]["change_pct"] - 0.03) < 0.000001
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_realtime_market_snapshot_prefers_sina_sector_over_hot_reason_labels(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

        @property
        def content(self):
            return self.text.encode("gbk", errors="ignore")

    def requester(url, **kwargs):
        if "hq.sinajs.cn" in url:
            return FakeResponse(
                text='var hq_str_sh000001="上证指数,0,100,101,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,2026-06-06,10:30:00";'
            )
        if "vip.stock.finance.sina.com.cn/q/view/newSinaHy.php" in url:
            return FakeResponse(
                text=(
                    'var S_Finance_bankuai_sinaindustry = {'
                    '"new_robot":"new_robot,机器人行业,8,20.0,0.6,3.00,100,200,sz300001,9.9,10.0,1.0,领涨股"'
                    "};"
                )
            )
        if "zx.10jqka.com.cn/event/api/getharden" in url:
            return FakeResponse(
                payload={
                    "errocode": 0,
                    "errormsg": "",
                    "data": [
                        {"code": "300001", "name": "A", "reason": "TopicA", "zhangfu": 20.0, "chengjiaoe": 100000},
                        {"code": "300002", "name": "B", "reason": "TopicA", "zhangfu": 10.0, "chengjiaoe": 90000},
                    ],
                }
            )
        return FakeResponse({"data": {"diff": []}})

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(sample_daily_bars())
    server.state.realtime_provider.requester = requester
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/realtime/market-snapshot")

        assert response["strong_sectors"]
        assert response["strong_sectors"][0]["name"] == "机器人行业"
        assert response["strong_sectors"][0]["source"] == "sina-sector"
        assert abs(response["strong_sectors"][0]["change_pct"] - 0.03) < 0.000001
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_news_uses_configured_provider(tmp_path):
    class FakeNewsProvider:
        def latest_news(self, limit=12):
            from datetime import datetime

            from astock_backtester.models import MarketNewsItem, MarketNewsResponse

            assert limit == 12
            return MarketNewsResponse(
                updated_at=datetime(2026, 5, 27, 10, 30, tzinfo=UTC),
                source="fake-news",
                items=[
                    MarketNewsItem(
                        title="政策利好推动科技板块走强",
                        summary="半导体、AI 应用方向盘中活跃。",
                        source="东方财富",
                        published_at=datetime(2026, 5, 27, 10, 20, tzinfo=UTC),
                        url="https://example.test/news",
                        tags=["科技", "政策"],
                        sentiment="positive",
                    )
                ],
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.news_provider = FakeNewsProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/news")

        assert response["source"] == "fake-news"
        assert response["items"][0]["title"] == "政策利好推动科技板块走强"
        assert response["items"][0]["sentiment"] == "positive"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_commentary_uses_configured_provider(tmp_path):
    class FakeCommentaryProvider:
        def current_commentary(self):
            from datetime import date, datetime

            from astock_backtester.models import MarketCommentaryPoint, MarketCommentaryResponse

            return MarketCommentaryResponse(
                updated_at=datetime(2026, 6, 5, 14, 30, tzinfo=UTC),
                trade_date=date(2026, 6, 5),
                source="fake-commentary",
                stance="positive",
                summary="红盘家数占优，AI应用延续。",
                drivers=[MarketCommentaryPoint(title="强势题材", detail="AI应用+4.20%", weight="high")],
                risks=["成交量不延续时避免追高。"],
                next_watch=["继续观察AI应用承接。"],
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.commentary_provider = FakeCommentaryProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/commentary")

        assert response["source"] == "fake-commentary"
        assert response["stance"] == "positive"
        assert response["drivers"][0]["title"] == "强势题材"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_finance_returns_cls_market_board_payload(tmp_path):
    class FakeResponse:
        text = ""
        encoding = "utf-8"

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    def requester(url, **kwargs):
        if "quote/index/tline" in url:
            return FakeResponse(
                {
                    "code": 200,
                    "data": [
                        {"date": 20260609, "minute": 930, "last_px": 3977.539, "change": 0.0047},
                        {"date": 20260609, "minute": 931, "last_px": 3966.391, "change": -0.0028},
                    ],
                }
            )
        if "v3/transaction/anchor" in url:
            return FakeResponse(
                {
                    "errno": 0,
                    "data": [
                        {
                            "symbol_code": "cls80025",
                            "symbol_name": "PCB",
                            "article_id": 2394344,
                            "c_time": "2026-06-09 09:31:30",
                            "float": "up",
                            "schema": "cailianshe://plate_detail?plate_id=cls80025",
                        }
                    ],
                }
            )
        if "quote/index/basic" in url:
            return FakeResponse({"code": 200, "data": {"preclose_px": 3959.337}})
        if "v2/quote/a/stock/emotion" in url:
            return FakeResponse(
                {
                    "code": 200,
                    "data": {
                        "market_degree": "56",
                        "shsz_balance": "2.64万亿",
                        "shsz_balance_change_px": "-1524亿",
                        "up_ratio_num": "130",
                        "up_open_num": "25",
                        "performance": "1.74%",
                        "up_down_dis": {
                            "rise_num": 3322,
                            "fall_num": 2049,
                            "flat_num": 156,
                            "suspend_num": 12,
                        },
                    },
                }
            )
        if "quote/index/up_down_analysis" in url:
            return FakeResponse(
                {
                    "code": 200,
                    "data": [
                        {
                            "secu_code": "sh601869",
                            "secu_name": "长飞光纤",
                            "change": 0.1,
                            "last_px": 484.33,
                            "time": "2026-06-09 13:34:47",
                            "up_reason": "光纤|全球光纤光缆行业领先企业。",
                            "limit_up_days": 1,
                            "plate": [{"secu_code": "cls81670", "secu_name": "光纤光缆", "change": 0.0393}],
                        }
                    ],
                }
            )
        if "q.10jqka.com.cn/index/index/board/all/" in url:
            return FakeResponse(text="<html><body></body></html>")
        raise AssertionError(f"unexpected url: {url}")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.finance_provider.requester = requester
    server.state.finance_provider.browser_cookie_getter = lambda: None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/finance")

        assert response["source"] == "cls-finance"
        assert response["source_url"] == "https://www.cls.cn/finance"
        assert response["preclose_px"] == 3959.337
        assert response["tline"][0]["minute"] == 930
        assert response["anchors"][0]["name"] == "PCB"
        assert response["emotion"]["market_degree"] == 56.0
        assert response["emotion"]["breadth"]["up"] == 3322
        assert response["up_pool"][0]["symbol"] == "601869"
        assert response["up_pool"][0]["plates"][0]["name"] == "光纤光缆"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_finance_prefers_ths_market_degree_for_score_card(tmp_path):
    class FakeResponse:
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    def requester(url, **kwargs):
        if "quote/index/tline" in url:
            return FakeResponse({"code": 200, "data": []})
        if "v3/transaction/anchor" in url:
            return FakeResponse({"errno": 0, "data": []})
        if "quote/index/basic" in url:
            return FakeResponse({"code": 200, "data": {}})
        if "v2/quote/a/stock/emotion" in url:
            return FakeResponse(
                {
                    "code": 200,
                    "data": {
                        "market_degree": "56",
                        "up_ratio_num": "130",
                        "up_open_num": "25",
                    },
                }
            )
        if "quote/index/up_down_analysis" in url:
            return FakeResponse({"code": 200, "data": []})
        if "q.10jqka.com.cn/api.php?t=indexflash" in url:
            return FakeResponse(text='{"dppj_data": 7.3}')
        raise AssertionError(f"unexpected url: {url}")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.finance_provider.requester = requester
    server.state.finance_provider.browser_cookie_getter = lambda: None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/finance")

        assert response["emotion"]["market_degree"] == 7.3
        assert response["emotion"]["market_degree_source"] == "ths-market-summary"
        assert response["emotion"]["market_degree_label"] == "同花顺大盘评级"
        assert response["emotion"]["up_limit"] == 130
        assert any("同花顺大盘评分" in item for item in response["diagnostics"])
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_finance_uses_browser_cookie_for_ths_indexflash(tmp_path):
    class FakeResponse:
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    seen_cookies = []

    def requester(url, **kwargs):
        if "quote/index/tline" in url:
            return FakeResponse({"code": 200, "data": []})
        if "v3/transaction/anchor" in url:
            return FakeResponse({"errno": 0, "data": []})
        if "quote/index/basic" in url:
            return FakeResponse({"code": 200, "data": {}})
        if "v2/quote/a/stock/emotion" in url:
            return FakeResponse({"code": 200, "data": {}})
        if "quote/index/up_down_analysis" in url:
            return FakeResponse({"code": 200, "data": []})
        if "q.10jqka.com.cn/index/index/board" in url:
            return FakeResponse(text="<html><body>board</body></html>")
        if "q.10jqka.com.cn/api.php?t=indexflash" in url:
            cookie = kwargs.get("headers", {}).get("Cookie") or ""
            seen_cookies.append(cookie)
            if cookie == "v=browser-cookie":
                return FakeResponse(text='{"dppj_data": 7.8}')
            raise AssertionError(f"missing cookie on indexflash request: {cookie!r}")
        raise AssertionError(f"unexpected url: {url}")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.finance_provider.requester = requester
    server.state.finance_provider.browser_cookie_getter = lambda: "v=browser-cookie"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/finance")

        assert response["emotion"]["market_degree"] == 7.8
        assert response["emotion"]["market_degree_source"] == "ths-market-summary"
        assert seen_cookies == ["v=browser-cookie"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_finance_does_not_parse_html_tag_numbers_as_ths_score(tmp_path):
    class FakeResponse:
        encoding = "utf-8"

        def __init__(self, payload=None, text=""):
            self._payload = payload or {}
            self.text = text

        def raise_for_status(self):
            return

        def json(self):
            return self._payload

    class ForbiddenResponse(FakeResponse):
        def raise_for_status(self):
            raise RuntimeError("403 forbidden")

    def requester(url, **kwargs):
        if "quote/index/tline" in url:
            return FakeResponse({"code": 200, "data": []})
        if "v3/transaction/anchor" in url:
            return FakeResponse({"errno": 0, "data": []})
        if "quote/index/basic" in url:
            return FakeResponse({"code": 200, "data": {}})
        if "v2/quote/a/stock/emotion" in url:
            return FakeResponse({"code": 200, "data": {}})
        if "quote/index/up_down_analysis" in url:
            return FakeResponse({"code": 200, "data": []})
        if "q.10jqka.com.cn/api.php?t=indexflash" in url:
            return ForbiddenResponse(text="<h1>Nginx forbidden.</h1>")
        if "q.10jqka.com.cn/index/index/board" in url:
            return FakeResponse(text='<html><body><h3>大盘评级</h3><div id="dppj"></div><p>投资建议</p></body></html>')
        raise AssertionError(f"unexpected url: {url}")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.finance_provider.requester = requester
    server.state.finance_provider.browser_cookie_getter = lambda: None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/finance")

        assert response["emotion"]["market_degree"] is None
        assert response["emotion"]["market_degree_source"] is None
        assert any("403 forbidden" in item for item in response["diagnostics"])
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_finance_returns_empty_board_when_provider_raises(tmp_path):
    class BrokenFinanceProvider:
        def current_board(self):
            raise ValueError("finance payload changed")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.finance_provider = BrokenFinanceProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/finance")

        assert response["source"] == "cls-finance"
        assert response["source_url"] == "https://www.cls.cn/finance"
        assert response["tline"] == []
        assert response["up_pool"] == []
        assert "finance payload changed" in response["diagnostics"][0]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_commentary_returns_backend_brief_fallback_when_provider_raises(tmp_path):
    class BrokenCommentaryProvider:
        def current_commentary(self):
            raise TimeoutError("commentary upstream timeout")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.commentary_provider = BrokenCommentaryProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/commentary")

        assert response["source"] == "local-brief-commentary"
        assert response["mode"] == "local_brief_review"
        assert response["stance"] == "defensive"
        assert "后端简短判断" in response["summary"]
        assert "commentary upstream timeout" in response["diagnostics"][0]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_commentary_does_not_hide_programming_errors(tmp_path):
    class BrokenCommentaryProvider:
        def current_commentary(self):
            raise AttributeError("bad field access")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.commentary_provider = BrokenCommentaryProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        try:
            _request_json("GET", f"http://127.0.0.1:{port}/market/commentary")
        except Exception as exc:
            assert "Remote end closed connection" in str(exc) or "bad field access" in str(exc)
        else:
            raise AssertionError("programming error was hidden behind commentary fallback")
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_news_summary_uses_configured_provider(tmp_path):
    class FakeNewsSummaryProvider:
        def latest_summary(self):
            from datetime import datetime

            from astock_backtester.models import MarketNewsSummaryResponse, MarketNewsTheme

            return MarketNewsSummaryResponse(
                updated_at=datetime(2026, 6, 5, 14, 30, tzinfo=UTC),
                source="fake-summary",
                item_count=3,
                themes=[
                    MarketNewsTheme(
                        title="AI",
                        summary="AI相关消息集中。",
                        sentiment="positive",
                        source_count=2,
                        headlines=["AI应用产业链午后走强"],
                    )
                ],
                highlights=["AI应用产业链午后走强"],
                risks=["退市风险提示增多"],
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.news_summary_provider = FakeNewsSummaryProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/news-summary")

        assert response["source"] == "fake-summary"
        assert response["item_count"] == 3
        assert response["themes"][0]["title"] == "AI"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_fupan_uses_configured_provider(tmp_path):
    class FakeBriefingProvider:
        def latest_fupan(self):
            from datetime import datetime

            from astock_backtester.models import MarketBriefingResponse, MarketBriefingSection

            return MarketBriefingResponse(
                kind="fupan",
                updated_at=datetime(2026, 6, 1, 15, 30, tzinfo=UTC),
                source="fake-fupan",
                source_url="https://stock.10jqka.com.cn/fupan/",
                summary="复盘摘要",
                sections=[MarketBriefingSection(title="指数/概念分析", content="煤炭活跃")],
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.briefing_provider = FakeBriefingProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/fupan")

        assert response["kind"] == "fupan"
        assert response["source"] == "fake-fupan"
        assert response["sections"][0]["title"] == "指数/概念分析"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_market_zaopan_uses_configured_provider(tmp_path):
    class FakeBriefingProvider:
        def latest_zaopan(self):
            from datetime import datetime

            from astock_backtester.models import MarketBriefingResponse, MarketBriefingSection

            return MarketBriefingResponse(
                kind="zaopan",
                updated_at=datetime(2026, 6, 1, 8, 30, tzinfo=UTC),
                source="fake-zaopan",
                source_url="https://stock.10jqka.com.cn/zaopan/",
                summary="早盘摘要",
                sections=[MarketBriefingSection(title="早盘要点", content="关注公司事项")],
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.briefing_provider = FakeBriefingProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/market/zaopan")

        assert response["kind"] == "zaopan"
        assert response["source"] == "fake-zaopan"
        assert response["sections"][0]["title"] == "早盘要点"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_market_news_parses_display_time_as_beijing_time():
    parsed = _parse_time("2026-05-27 10:20:00")

    assert parsed is not None
    assert parsed.isoformat() == "2026-05-27T10:20:00+08:00"


def test_service_risk_alerts_uses_configured_provider(tmp_path):
    class FakeRiskProvider:
        def current_alerts(self):
            from datetime import datetime

            from astock_backtester.models import RiskAlertItem, RiskAlertsResponse

            return RiskAlertsResponse(
                updated_at=datetime(2026, 5, 27, 10, 30, tzinfo=UTC),
                source="fake-risk",
                items=[
                    RiskAlertItem(
                        symbol="000001",
                        name="*ST示例",
                        risk_type="ST风险",
                        reason="股票名称包含 *ST，存在退市风险警示。",
                        severity="high",
                        source="adata",
                        detected_at=datetime(2026, 5, 27, 10, 30, tzinfo=UTC),
                    )
                ],
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.risk_provider = FakeRiskProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/risk/alerts")

        assert response["source"] == "fake-risk"
        assert response["items"][0]["symbol"] == "000001"
        assert response["items"][0]["reason"].startswith("股票名称包含")
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_risk_alerts_reports_diagnostics_when_no_risky_symbols(tmp_path):
    class EmptyRiskProvider:
        def current_alerts(self):
            from datetime import datetime

            from astock_backtester.models import RiskAlertsResponse

            return RiskAlertsResponse(
                updated_at=datetime(2026, 5, 27, 10, 30, tzinfo=UTC),
                source="local",
                items=[],
                diagnostics=["东方财富风险源不可用，已使用本地 ST 字段兜底。"],
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.risk_provider = EmptyRiskProvider()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/risk/alerts")

        assert response["items"] == []
        assert response["diagnostics"] == ["东方财富风险源不可用，已使用本地 ST 字段兜底。"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_validates_user_written_strategy_condition(tmp_path):
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/strategy/conditions/validate",
            {"text": "量比2日介于1.2到2.5"},
        )

        assert response["ok"] is True
        assert response["condition"]["condition_id"] == "volume_ratio_between"
        assert response["condition"]["params"] == {"window": 2, "min": 1.2, "max": 2.5}
        assert "量比2日介于1.2到2.5" in response["examples"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_validates_user_written_exit_condition_with_mode(tmp_path):
    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/strategy/conditions/validate",
            {"text": "突破20日最低", "mode": "exit"},
        )

        assert response["ok"] is True
        assert response["condition"]["condition_id"] == "breakdown_below_n_day_low"
        assert response["condition"]["params"] == {"window": 20}
        assert "跌破20日低点" in response["examples"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_validates_custom_stock_symbols_against_local_warehouse_first(tmp_path):
    class ProviderShouldNotRun:
        def list_symbols(self):
            raise AssertionError("provider list_symbols should not run when local warehouse symbols exist")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.provider = ProviderShouldNotRun()
    server.state.warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["600519", "000001"],
                "trade_date": ["2026-06-18", "2026-06-18"],
                "open": [1.0, 1.0],
                "high": [1.0, 1.0],
                "low": [1.0, 1.0],
                "close": [1.0, 1.0],
                "volume": [1, 1],
                "float_market_cap": [100.0, 100.0],
            }
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/symbols/validate",
            {"symbols": ["SH600519", "000001.SZ", "999999"]},
        )

        assert response == {
            "ok": False,
            "valid_symbols": ["600519", "000001"],
            "invalid_symbols": ["999999"],
            "normalized_symbols": ["600519", "000001", "999999"],
            "source": "local-warehouse",
        }
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_returns_recommended_strategies(tmp_path):
    import pandas as pd

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["AAA", "AAA"],
                "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "open": [10.0, 10.2],
                "high": [10.3, 10.4],
                "low": [9.9, 10.0],
                "close": [10.1, 10.3],
                "volume": [1000, 1200],
                "float_market_cap": [1_000_000_000.0, 1_020_000_000.0],
                "total_market_cap": [1_200_000_000.0, 1_220_000_000.0],
                "main_net_inflow": [float("nan"), float("nan")],
            }
        )
    )
    refresh_finished = server.state.start_coverage_refresh(force=True)
    assert refresh_finished is not None
    assert refresh_finished.wait(timeout=5)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/strategy/recommended")

        names = [item["name"] for item in response["items"]]
        assert "放量突破" in names
        assert "市值量价均衡" in names
        assert "资金趋势跟随" not in names
        assert "极端追高压力测试" not in names
        assert response["items"][0]["strategy"]["entry_groups"][0]["conditions"]
        assert response["items"][0]["example_conditions"]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_recommended_strategies_use_the_cached_coverage_snapshot(tmp_path, monkeypatch):
    from astock_backtester.models import DatasetCoverage

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    with server.state._coverage_lock:
        server.state._coverage_snapshot = [
            DatasetCoverage(dataset="daily_bars", symbols=1, start_date="2024-01-02", end_date="2024-01-03", missing_rows=0),
            DatasetCoverage(dataset="market_cap", symbols=1, start_date="2024-01-02", end_date="2024-01-03", missing_rows=0),
            DatasetCoverage(dataset="capital_flow", symbols=0, start_date=None, end_date=None, missing_rows=0),
        ]

    def coverage_must_not_run():
        raise AssertionError("recommended strategies must use the service coverage snapshot")

    monkeypatch.setattr(server.state.warehouse, "coverage", coverage_must_not_run)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("GET", f"http://127.0.0.1:{port}/strategy/recommended")

        names = [item["name"] for item in response["items"]]
        assert "放量突破" in names
        assert "资金趋势跟随" not in names
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_health_reports_warehouse_market_cap_and_capital_flow_coverage(tmp_path):
    import pandas as pd

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["AAA", "AAA"],
                "trade_date": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                "open": [10.0, 10.2],
                "high": [10.3, 10.4],
                "low": [9.9, 10.0],
                "close": [10.1, 10.3],
                "volume": [1000, 1200],
                "float_market_cap": [1_000_000_000.0, 1_020_000_000.0],
                "total_market_cap": [1_200_000_000.0, 1_220_000_000.0],
                "main_net_inflow": [float("nan"), 2_000_000.0],
            }
        )
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        health = _request_json("GET", f"http://127.0.0.1:{port}/health")

        datasets = {item["dataset"]: item for item in health["coverage"]}
        assert datasets["market_cap"]["symbols"] == 1
        assert datasets["market_cap"]["missing_rows"] == 0
        assert datasets["capital_flow"]["symbols"] == 1
        assert datasets["capital_flow"]["missing_rows"] == 1
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_returns_sync_job_progress_by_id(tmp_path):
    class FakeManager:
        def start_full_market(self, symbols, start_date, end_date):
            from datetime import date

            from astock_backtester.models import SyncJobStatus

            return SyncJobStatus(
                job_id="job-progress",
                mode="full_market_bootstrap",
                status="running",
                total_symbols=len(symbols),
                completed_symbols=1,
                failed_symbols=0,
                imported_rows=100,
                current_symbol="000002",
                start_date=date.fromisoformat(start_date),
                end_date=date.fromisoformat(end_date),
            )

        def get_job(self, job_id):
            from datetime import date

            from astock_backtester.models import SyncJobStatus

            assert job_id == "job-progress"
            return SyncJobStatus(
                job_id=job_id,
                mode="full_market_bootstrap",
                status="completed",
                total_symbols=2,
                completed_symbols=2,
                failed_symbols=0,
                imported_rows=200,
                current_symbol=None,
                start_date=date(2015, 1, 1),
                end_date=date(2015, 1, 5),
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.sync_manager = FakeManager()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        started = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/sync/full-market",
            {"symbols": ["000001", "000002"], "start_date": "2015-01-01", "end_date": "2015-01-05"},
        )
        progress = _request_json("GET", f"http://127.0.0.1:{port}/sync/jobs/{started['job']['job_id']}")

        assert started["job"]["status"] == "running"
        assert started["job"]["current_symbol"] == "000002"
        assert progress["job"]["status"] == "completed"
        assert progress["job"]["imported_rows"] == 200
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_cancels_sync_job_by_id(tmp_path):
    class FakeManager:
        def cancel_job(self, job_id):
            from datetime import date

            from astock_backtester.models import SyncJobStatus

            assert job_id == "job-cancel"
            return SyncJobStatus(
                job_id=job_id,
                mode="capital_flow_backfill",
                status="cancelling",
                total_symbols=3,
                completed_symbols=1,
                processed_symbols=1,
                imported_rows=5,
                returned_rows=8,
                current_symbol="000002",
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 5),
            )

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.sync_manager = FakeManager()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json("POST", f"http://127.0.0.1:{port}/sync/jobs/job-cancel/cancel", {})

        assert response["job"]["status"] == "cancelling"
        assert response["job"]["returned_rows"] == 8
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_fetch_capital_flow_no_gaps_response_reports_filled_missing_rows(tmp_path):
    """空缺口响应必须与其它路径字段同构：filled_missing_rows 不能缺席（前端 types.ts 已声明）。"""

    class FakeSyncManager:
        def start_capital_flow_backfill(self, symbols, start_date, end_date):
            raise AssertionError("窗口内没有资金流缺口时不得启动补齐任务")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.warehouse.write_daily_bars(
        pd.DataFrame(
            {
                "symbol": ["000001"] * 4,
                "trade_date": pd.to_datetime(["2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29"]),
                "open": [10.0] * 4,
                "high": [10.5] * 4,
                "low": [9.8] * 4,
                "close": [10.2] * 4,
                "volume": [1000] * 4,
                "main_net_inflow": [1_000_000.0] * 4,
            }
        )
    )
    server.state.sync_manager = FakeSyncManager()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/fetch/capital-flow",
            {"symbols": [], "start_date": "2026-05-26", "end_date": "2026-05-29"},
        )

        assert response["status"] == "ok"
        assert response["filled_missing_rows"] == 0
        assert "capital_flow_backfill_no_gaps" in {item["code"] for item in response["diagnostics"]}
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_service_missing_only_refreshes_lifecycle_before_building_gap_list(tmp_path):
    """/sync/missing-only 必须先 best-effort 刷新 symbol_lifecycle 再算缺口名单（§9 同款）。

    真退市股先标 delisted 再进完整性快照，否则每轮"只补缺口"都会把停更股
    重复算进名单；刷新本身 never-raises，这里只钉住调用次序。
    """
    order: list[str] = []

    class FakeManager:
        def incomplete_symbols(self, start_date, end_date):
            order.append("incomplete_symbols")
            return []

        def start_full_market(self, symbols, start_date, end_date):
            raise AssertionError("名单为空时不得启动全市场任务")

    server = create_server(host="127.0.0.1", port=0, cache_dir=tmp_path)
    server.state.sync_manager = FakeManager()

    def fake_refresh():
        order.append("refresh_symbol_lifecycle")
        return {"listed_upserts": 0, "delisted_upserts": 0}

    server.state.refresh_symbol_lifecycle_best_effort = fake_refresh
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        response = _request_json(
            "POST",
            f"http://127.0.0.1:{port}/sync/missing-only",
            {"start_date": "2026-05-26", "end_date": "2026-05-29"},
        )

        assert order == ["refresh_symbol_lifecycle", "incomplete_symbols"], (
            "lifecycle 刷新必须发生在缺口名单计算之前，否则真退市股会每轮重复进名单"
        )
        assert response["started"] is False
        assert response["missing_symbols"] == 0
    finally:
        server.shutdown()
        thread.join(timeout=5)
