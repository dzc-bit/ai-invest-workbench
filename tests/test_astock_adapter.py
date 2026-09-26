import logging
import threading
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse

import pandas as pd
from astock_backtester.cli import handle_command
from astock_backtester.data import astock_adapter, http_transport
from astock_backtester.data.astock_adapter import (
    EASTMONEY_ENRICHMENT_FAILURE_LIMIT,
    AStockDataAdapter,
    AStockDataUnavailable,
    HttpAStockFetcher,
)


class _FakeResponse:
    """Minimal ``requests.Response`` stand-in for injected transports."""

    def __init__(self, text, content=None):
        self.text = text
        self.content = content if content is not None else text.encode("gbk", errors="replace")

    def raise_for_status(self):
        return None


class _FakeSession:
    """Session facade for the legacy ``scraping_session().get(...)`` path."""

    def __init__(self, getter):
        self._getter = getter

    def get(self, url, **kwargs):
        return self._getter(url, **kwargs)


def _forbid_alternate(url, **kwargs):
    raise AssertionError("alternate transport must not run in this test")


def test_adapter_reports_unconfigured_fetcher():
    adapter = AStockDataAdapter(fetcher=None)

    try:
        adapter.fetch_daily_bars(["AAA"], "2024-01-02", "2024-01-08")
    except AStockDataUnavailable as exc:
        assert "a-stock-data fetcher is not configured" in str(exc)
    else:
        raise AssertionError("expected AStockDataUnavailable")


def test_adapter_normalizes_fetcher_output():
    def fake_fetcher(symbols, start_date, end_date):
        return pd.DataFrame(
            {
                "symbol": ["AAA"],
                "date": ["2024-01-02"],
                "open": [10.0],
                "high": [11.0],
                "low": [9.8],
                "close": [10.5],
                "volume": [1000],
                "float_market_cap": [8_000_000_000],
                "main_net_inflow": [2_000_000],
            }
        )

    adapter = AStockDataAdapter(fetcher=fake_fetcher)
    result = adapter.fetch_daily_bars(["AAA"], "2024-01-02", "2024-01-08")

    assert result.loc[0, "symbol"] == "AAA"
    assert result.loc[0, "main_net_inflow"] == 2_000_000


def test_fetch_status_is_explicit():
    response = handle_command({"command": "fetch_status"})

    assert response["ok"] is True
    assert response["status"]["configured"] is True


def test_http_fetcher_maps_astock_data_sources_to_daily_bars():
    def fake_json_get(url, params, headers, timeout):
        if "getstockquotation" in url:
            return {
                "Result": {
                    "newMarketData": {
                        "keys": ["time", "open", "close", "high", "low", "volume", "amount"],
                        "marketData": (
                            "2024-01-02,10,10.5,11,9.8,1000,100000;"
                            "2024-01-03,10.5,11,11.2,10.1,1500,165000"
                        ),
                    }
                }
            }
        if "fflow/daykline/get" in url:
            return {"data": {"klines": ["2024-01-02,2000,1,2,3,4", "2024-01-03,3000,1,2,3,4"]}}
        if "api/qt/stock/get" in url:
            return {"data": {"f58": "贵州茅台", "f117": 8_800_000_000, "f189": "20200101"}}
        raise AssertionError(f"unexpected url: {url}")

    fetcher = HttpAStockFetcher(json_get=fake_json_get)

    result = fetcher.fetch_daily_bars(["SH600519"], "2024-01-02", "2024-01-03")

    assert result["symbol"].tolist() == ["600519", "600519"]
    assert result["trade_date"].dt.strftime("%Y-%m-%d").tolist() == ["2024-01-02", "2024-01-03"]
    assert result["open"].tolist() == [10.0, 10.5]
    assert result["high"].tolist() == [11.0, 11.2]
    assert result["low"].tolist() == [9.8, 10.1]
    assert result["close"].tolist() == [10.5, 11.0]
    assert result["volume"].tolist() == [1000, 1500]
    assert result["main_net_inflow"].tolist() == [2000.0, 3000.0]
    assert result["float_market_cap"].tolist() == [8_800_000_000, 8_800_000_000]
    assert result["name"].tolist() == ["贵州茅台", "贵州茅台"]
    assert result["listing_days"].min() > 1000


def test_http_fetcher_maps_baidu_amount_turnover_and_estimated_float_market_cap():
    def fake_json_get(url, params, headers, timeout):
        if "getstockquotation" in url:
            return {
                "Result": {
                    "newMarketData": {
                        "keys": [
                            "time",
                            "open",
                            "close",
                            "high",
                            "low",
                            "volume",
                            "amount",
                            "range",
                            "ratio",
                            "turnoverratio",
                            "preClose",
                        ],
                        "marketData": "2024-01-02,10,10.5,11,9.8,1000,10500,+0.5,+5.0,2.0,10",
                    }
                }
            }
        return {}

    fetcher = HttpAStockFetcher(json_get=fake_json_get)

    result = fetcher.fetch_daily_bars(["600519"], "2024-01-02", "2024-01-02")

    assert result.loc[0, "amount"] == 10500
    assert result.loc[0, "change"] == 0.5
    assert result.loc[0, "change_pct"] == 5.0
    assert result.loc[0, "turnover_rate"] == 2.0
    assert result.loc[0, "pre_close"] == 10
    assert result.loc[0, "float_market_cap"] == 525000.0


def test_http_fetcher_keeps_missing_flow_as_missing_value():
    def fake_json_get(url, params, headers, timeout):
        if "getstockquotation" in url:
            return {
                "Result": {
                    "newMarketData": {
                        "keys": ["time", "open", "close", "high", "low", "volume"],
                        "marketData": "2024-01-02,10,10.5,11,9.8,1000",
                    }
                }
            }
        if "fflow/daykline/get" in url:
            return {"data": {"klines": ["2026-05-20,2000,1,2,3,4"]}}
        if "api/qt/stock/get" in url:
            return {"data": {"f117": 8_800_000_000, "f189": "20200101"}}
        raise AssertionError(f"unexpected url: {url}")

    fetcher = HttpAStockFetcher(json_get=fake_json_get)

    result = fetcher.fetch_daily_bars(["600519"], "2024-01-02", "2024-01-02")

    assert pd.isna(result.loc[0, "main_net_inflow"])


def test_http_fetcher_keeps_daily_bars_when_optional_sources_fail():
    def fake_json_get(url, params, headers, timeout):
        if "getstockquotation" in url:
            return {
                "Result": {
                    "newMarketData": {
                        "keys": ["time", "open", "close", "high", "low", "volume"],
                        "marketData": "2024-01-02,10,10.5,11,9.8,1000",
                    }
                }
            }
        raise OSError("optional source unavailable")

    fetcher = HttpAStockFetcher(json_get=fake_json_get)

    result = fetcher.fetch_daily_bars(["600519"], "2024-01-02", "2024-01-02")

    assert len(result) == 1
    assert result.loc[0, "close"] == 10.5
    assert pd.isna(result.loc[0, "main_net_inflow"])
    assert pd.isna(result.loc[0, "float_market_cap"])


def test_http_fetcher_keeps_daily_bars_when_optional_sources_return_unexpected_shapes():
    def fake_json_get(url, params, headers, timeout):
        if "getstockquotation" in url:
            return {
                "Result": {
                    "newMarketData": {
                        "keys": ["time", "open", "close", "high", "low", "volume"],
                        "marketData": "2024-01-02,10,10.5,11,9.8,1000",
                    }
                }
            }
        return []

    fetcher = HttpAStockFetcher(json_get=fake_json_get)

    result = fetcher.fetch_daily_bars(["600519"], "2024-01-02", "2024-01-02")

    assert len(result) == 1
    assert result.loc[0, "close"] == 10.5
    assert pd.isna(result.loc[0, "main_net_inflow"])
    assert pd.isna(result.loc[0, "float_market_cap"])


def test_http_fetcher_retries_baidu_kline_when_result_shape_is_throttled():
    calls = 0

    def fake_json_get(url, params, headers, timeout):
        nonlocal calls
        if "getstockquotation" in url:
            calls += 1
            if calls == 1:
                return {"ResultCode": "403", "Result": []}
            return {
                "ResultCode": "0",
                "Result": {
                    "newMarketData": {
                        "keys": ["time", "open", "close", "high", "low", "volume"],
                        "marketData": "2024-01-02,10,10.5,11,9.8,1000",
                    }
                },
            }
        return {}

    fetcher = HttpAStockFetcher(json_get=fake_json_get)

    result = fetcher.fetch_daily_bars(["600519"], "2024-01-02", "2024-01-02")

    assert calls == 2
    assert len(result) == 1
    assert result.loc[0, "close"] == 10.5


def test_http_fetcher_tries_browser_transport_when_requests_gets_baidu_403():
    calls = []

    def requests_json_get(url, params, headers, timeout):
        calls.append(("requests", url))
        if "getstockquotation" in url:
            return {"QueryID": "0", "ResultCode": "403", "Result": []}
        raise AssertionError(f"requests transport should not reach optional source: {url}")

    def browser_json_get(url, params, headers, timeout):
        calls.append(("curl_cffi", url))
        if "getstockquotation" in url:
            return {
                "ResultCode": "0",
                "Result": {
                    "newMarketData": {
                        "keys": ["time", "open", "close", "high", "low", "volume"],
                        "marketData": "2026-06-12,10,10.5,11,9.8,1000",
                    }
                },
            }
        if "fflow/daykline/get" in url:
            return {"data": {"klines": []}}
        if "api/qt/stock/get" in url:
            return {"data": {"f117": 8_800_000_000, "f189": "20200101"}}
        raise AssertionError(f"unexpected url: {url}")

    fetcher = HttpAStockFetcher(json_gets=(("requests", requests_json_get), ("curl_cffi", browser_json_get)))

    result = fetcher.fetch_daily_bars(["600519"], "2026-06-12", "2026-06-18")

    assert result["symbol"].tolist() == ["600519"]
    assert result.loc[0, "close"] == 10.5
    assert [label for label, _ in calls[:2]] == ["requests", "curl_cffi"]


def test_http_fetcher_does_not_retry_plain_empty_baidu_daily_rows():
    calls = 0

    def fake_json_get(url, params, headers, timeout):
        nonlocal calls
        if "getstockquotation" in url:
            calls += 1
            return {"Result": {"newMarketData": {"keys": ["time", "open"], "marketData": ""}}}
        raise AssertionError(f"unexpected optional source call after empty daily rows: {url}")

    fetcher = HttpAStockFetcher(json_get=fake_json_get)

    result = fetcher.fetch_daily_bars(["000050"], "2026-06-12", "2026-06-18")

    assert result.empty
    assert calls == 1


def test_http_fetcher_public_kline_is_primary_and_hermetic_by_default():
    """注入式构造必须完全离线：公开 XHR 主源默认关闭，只用注入的百度链路。"""
    calls: list[str] = []

    def fake_json_get(url, params, headers, timeout):
        calls.append(url)
        if "getstockquotation" in url:
            return {
                "Result": {
                    "newMarketData": {
                        "keys": ["time", "open", "close", "high", "low", "volume"],
                        "marketData": "2026-09-21,10,10.5,11,9.8,1000",
                    }
                }
            }
        return {}

    fetcher = HttpAStockFetcher(json_get=fake_json_get)
    result = fetcher.fetch_daily_bars(["600519"], "2026-09-21", "2026-09-21")

    assert len(result) == 1
    assert result.loc[0, "close"] == 10.5
    # 公开源（腾讯/新浪）在注入模式下绝不能被打到
    assert not any("gtimg" in url or "sina" in url for url in calls)


def test_http_fetcher_prefers_public_kline_over_baidu():
    """生产构造下公开 XHR 主源优先：腾讯日线返回数据时不触碰百度。"""
    calls: list[str] = []

    def fake_public_json_get(url, params, headers, timeout):
        calls.append(url)
        market_symbol = str(params.get("param", "")).split(",")[0]
        return {
            "data": {
                market_symbol: {
                    "day": [
                        ["2026-09-18", "10.00", "10.40", "10.60", "9.90", "1000"],
                        ["2026-09-21", "10.50", "10.90", "11.20", "10.30", "1200"],
                        ["2026-09-22", "10.95", "11.30", "11.40", "10.80", "1500"],
                        ["2026-09-23", "11.35", "11.10", "11.60", "11.00", "1300"],
                        ["2026-09-24", "11.15", "10.90", "11.25", "10.70", "1100"],
                    ]
                }
            }
        }

    def fail_baidu(url, params, headers, timeout):
        raise AssertionError("baidu must not be reached when public kline returns rows")

    fetcher = HttpAStockFetcher(json_get=fail_baidu, public_json_get=fake_public_json_get)
    result = fetcher.fetch_daily_bars(["600519"], "2026-09-21", "2026-09-24")

    assert len(result) == 4
    # 预热行（09-18）只用于给首行算前收盘，必须被窗口裁掉
    assert result["trade_date"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-09-21",
        "2026-09-22",
        "2026-09-23",
        "2026-09-24",
    ]
    # 腾讯 volume 是手，入库前必须 ×100 换成股
    assert result.loc[0, "volume"] == 120_000
    # pre_close 来自预热行 10.40；change_pct 为百分比口径
    assert result.loc[0, "pre_close"] == 10.40
    assert result.loc[0, "change_pct"] == round((10.90 / 10.40 - 1) * 100, 4)
    assert not any("getstockquotation" in url for url in calls)


def test_http_fetcher_falls_back_to_sina_for_beijing_codes():
    """腾讯不覆盖北交所：直接走新浪（省掉一次注定为空的往返），volume 单位是股。"""
    calls: list[str] = []

    def fake_public_json_get(url, params, headers, timeout):
        calls.append(url)
        assert "getKLineData" in url, f"bj codes must go straight to Sina, got {url}"
        assert params["symbol"] == "bj920171"
        return [
            {"day": "2026-09-22", "open": "16.60", "high": "16.67", "low": "16.07", "close": "16.26", "volume": "1529165"},
            {"day": "2026-09-23", "open": "16.27", "high": "16.50", "low": "15.98", "close": "16.01", "volume": "1034412"},
        ]

    fetcher = HttpAStockFetcher(public_json_get=fake_public_json_get)
    result = fetcher.fetch_daily_bars(["920171"], "2026-09-22", "2026-09-23")

    assert len(result) == 2
    assert result.loc[0, "volume"] == 1_529_165  # 新浪 volume 单位是股，直接入库
    assert all("fqkline" not in url for url in calls), "tencent must not be queried for bj codes"


def test_http_fetcher_tencent_quote_tops_up_float_market_cap():
    """公开日 K 不带市值：用腾讯报价推导 float_shares×close 填充。"""
    fields = ["0"] * 53
    fields[1] = "贵州茅台"
    fields[3] = "1500.00"  # 现价
    fields[44] = "15463.51"  # 流通市值（亿）
    fields[45] = "15463.51"  # 总市值（亿）
    quote_line = 'v_sh600519="' + "~".join(fields) + '"'

    def fake_public_json_get(url, params, headers, timeout):
        return {"data": {"sh600519": {"day": [["2026-09-24", "10.00", "10.50", "11.00", "9.90", "1000"]]}}}

    def fake_public_text_get(url, headers, timeout):
        assert "qt.gtimg.cn" in url
        return quote_line

    fetcher = HttpAStockFetcher(public_json_get=fake_public_json_get, public_text_get=fake_public_text_get)
    result = fetcher.fetch_daily_bars(["600519"], "2026-09-24", "2026-09-24")

    assert len(result) == 1
    cap = result.loc[0, "float_market_cap"]
    assert cap > 0
    # float_shares = 15463.51亿 / 1500；float_market_cap = shares × close(10.50)
    expected = 15_463.51e8 / 1500.0 * 10.5
    assert abs(cap - expected) < 1.0


def test_http_fetcher_market_cap_breaker_stops_eastmoney_after_failures():
    """东财增强端点连续失败后必须熔断，避免全市场补齐时每只票都等满超时。"""

    def slow_failure(url, params, headers, timeout):
        raise TimeoutError("unreachable")

    def kline(url, params, headers, timeout):
        return {"data": {"sz000001": {"day": [["2026-09-24", "10.00", "10.50", "11.00", "9.90", "1000"]]}}}

    def quote(url, headers, timeout):
        return ""

    fetcher = HttpAStockFetcher(json_gets=(("requests", slow_failure),), public_json_get=kline, public_text_get=quote)
    for _ in range(EASTMONEY_ENRICHMENT_FAILURE_LIMIT):
        fetcher.fetch_daily_bars(["000001"], "2026-09-24", "2026-09-24")
    calls_after_breaker = {"n": 0}

    def counting_failure(url, params, headers, timeout):
        calls_after_breaker["n"] += 1
        raise TimeoutError("unreachable")

    fetcher._json_gets = (("requests", counting_failure),)
    fetcher.fetch_daily_bars(["000001"], "2026-09-24", "2026-09-24")
    # 熔断后：公开 K 线与报价都不走东财， 增强 RPC 一次都不该被调用
    assert calls_after_breaker["n"] == 0


def test_cli_fetch_daily_bars_writes_cache(monkeypatch, tmp_path):
    class FakeAdapter:
        def fetch_daily_bars(self, symbols, start_date, end_date):
            assert symbols == ["600519"]
            assert start_date == "2024-01-02"
            assert end_date == "2024-01-08"
            return pd.DataFrame(
                {
                    "symbol": ["600519"],
                    "trade_date": ["2024-01-02"],
                    "open": [10.0],
                    "high": [11.0],
                    "low": [9.8],
                    "close": [10.5],
                    "volume": [1000],
                }
            )

    monkeypatch.setattr(
        "astock_backtester.cli.AStockDataAdapter.from_http_sources",
        lambda: FakeAdapter(),
    )

    response = handle_command(
        {
            "command": "fetch_daily_bars",
            "symbols": ["600519"],
            "start_date": "2024-01-02",
            "end_date": "2024-01-08",
            "cache_dir": str(tmp_path),
        }
    )

    assert response["ok"] is True
    assert response["imported_rows"] == 1
    assert response["coverage"][0]["symbols"] == 1


def test_public_json_transport_retries_transient_failures(monkeypatch):
    """公开 XHR 生产传输必须走 resilient_get：瞬时失败要重试，不能一次就放弃。"""
    attempts = []

    def flaky_get(url, **kwargs):
        attempts.append(url)
        if len(attempts) == 1:
            raise ConnectionError("connection reset by peer")
        return _FakeResponse('{"rows": 1}')

    monkeypatch.setattr(astock_adapter, "scraping_get", flaky_get, raising=False)
    monkeypatch.setattr(astock_adapter, "scraping_session", lambda: _FakeSession(flaky_get))
    monkeypatch.setattr(http_transport, "_curl_get", _forbid_alternate)

    fetcher = HttpAStockFetcher()
    payload = fetcher._request_public_json(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        {"symbol": "sh600519"},
        {"User-Agent": "ua"},
        5,
    )

    assert payload == {"rows": 1}
    assert len(attempts) == 2, "transient failure must be retried exactly once"


def test_public_text_transport_degrades_to_alternate_transport(monkeypatch):
    """腾讯报价的生产传输必须能在主传输持续失败时降级到 curl_cffi 备用传输。"""
    primary_calls = []
    alternate_calls = []
    fields = ["0"] * 46
    fields[1] = "贵州茅台"
    fields[3] = "1500.00"
    fields[38] = "0.5"
    fields[44] = "15463.51"
    fields[45] = "15463.51"
    quote_line = 'v_sh600519="' + "~".join(fields) + '"'

    def primary(url, **kwargs):
        primary_calls.append(url)
        raise OSError("network unreachable")

    def alternate(url, **kwargs):
        alternate_calls.append(url)
        return _FakeResponse(quote_line)

    monkeypatch.setattr(astock_adapter, "scraping_get", primary, raising=False)
    monkeypatch.setattr(astock_adapter, "create_scraping_session", lambda: _FakeSession(primary), raising=False)
    monkeypatch.setattr(http_transport, "_curl_get", alternate)

    fetcher = HttpAStockFetcher()
    quote = fetcher._try_fetch_tencent_quote("600519")

    assert primary_calls, "the primary public text transport must be attempted first"
    assert alternate_calls and "qt.gtimg.cn" in alternate_calls[0]
    assert quote["name"] == "贵州茅台"


def test_injected_public_transports_bypass_resilient_transport(monkeypatch):
    """注入式构造必须完全离线：注入的公开传输直连调用，不经过 resilient_get。"""
    injected_calls = []

    def fake_public_json_get(url, params, headers, timeout):
        injected_calls.append(url)
        return {"data": {}}

    def fail_json_get(url, params, headers, timeout):
        raise AssertionError("eastmoney transport must stay disabled when public_json_get is injected")

    monkeypatch.setattr(
        http_transport,
        "_curl_get",
        lambda url, **kwargs: (_ for _ in ()).throw(AssertionError("no real transport in injected construction")),
    )

    fetcher = HttpAStockFetcher(json_get=fail_json_get, public_json_get=fake_public_json_get)
    payload = fetcher._request_public_json("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get", {}, {}, 5)

    assert payload == {"data": {}}
    assert injected_calls == ["https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"]


def test_tencent_kline_stops_when_upstream_rows_do_not_advance():
    """上游返回不推进 cursor 的行时必须 break，不能每轮等一次超时地死循环。"""
    calls = {"n": 0}

    def frozen_kline(url, params, headers, timeout):
        calls["n"] += 1
        if calls["n"] > 2:
            # 旧实现靠空批收尾：第三次才返回空，让死循环在可断言的次数内停下。
            return {"data": {"sh600519": {"day": []}}}
        return {"data": {"sh600519": {"day": [["2026-09-24", "10.00", "10.50", "11.00", "9.80", "1000"]]}}}

    fetcher = HttpAStockFetcher(public_json_get=frozen_kline)
    rows = fetcher._fetch_tencent_kline("600519", "sh600519", "2026-09-01", "2026-09-30")

    assert calls["n"] == 2, "the second round sees a non-advancing cursor and must stop"
    assert [item["trade_date"] for item in rows] == ["2026-09-24"]


def _tencent_tail_series(start: str, end: str) -> list[list[str]]:
    """按 ``[date, open, close, high, low, volume_lots]`` 造一段工作日序列。"""
    return [
        [day.strftime("%Y-%m-%d"), "10.00", "10.50", "11.00", "9.80", "1000"]
        for day in pd.bdate_range(start, end)
    ]


def _tencent_tail_endpoint(series: list[list[str]], market_symbol: str = "sz000001"):
    """实测语义的 fake 上游：返回 ``[start, end]`` 内**最后** count 行（不是前 count 行）。"""
    calls: list[str] = []

    def endpoint(url, params, headers, timeout):
        param = str(params["param"])
        calls.append(param)
        parts = param.split(",")
        start, end, count = parts[2], parts[3], int(parts[4])
        window = [row for row in series if start <= row[0] <= end][-count:]
        return {"data": {market_symbol: {"day": window}}}

    return endpoint, calls


def test_tencent_kline_rewinds_from_the_window_end_to_cover_the_whole_range():
    """腾讯 count 是“窗口内最后 N 行”：长窗口必须按窗口**回走**，不能向前推进。

    向前推进时第一轮就拿到窗口尾部、直接触发 ``last >= end`` 收尾，2015-2025
    整段丢失（2017 年前后的 float_market_cap null 永远补不上的根因）。
    """
    series = _tencent_tail_series("2014-12-01", "2026-09-30")
    endpoint, calls = _tencent_tail_endpoint(series)
    fetcher = HttpAStockFetcher(public_json_get=endpoint)

    rows = fetcher._fetch_tencent_kline("000001", "sz000001", "2015-01-01", "2026-09-30")

    warmup = astock_adapter._kline_warmup_start("2015-01-01")
    expected = [row[0] for row in series if warmup <= row[0] <= "2026-09-30"]
    assert [item["trade_date"] for item in rows] == expected  # 完整覆盖 + 已排序 + 已去重
    assert len(calls) > 1, "a multi-year window must page backwards instead of one-shot"
    starts = [call.split(",")[2] for call in calls]
    ends = [call.split(",")[3] for call in calls]
    assert set(starts) == {warmup}, "every round keeps the warmup start anchor"
    assert ends == sorted(ends, reverse=True), "each round must rewind the window end"


def test_http_fetcher_tencent_walk_covers_the_requested_window():
    """端到端回归守卫：整窗抓取必须覆盖 [start, end]，而不是只返回最后 320 行。"""
    series = _tencent_tail_series("2014-12-01", "2026-09-30")
    endpoint, _calls = _tencent_tail_endpoint(series)

    def fail_baidu(url, params, headers, timeout):
        raise AssertionError("baidu must not be reached when public kline returns rows")

    fetcher = HttpAStockFetcher(json_get=fail_baidu, public_json_get=endpoint)
    result = fetcher.fetch_daily_bars(["000001"], "2015-01-01", "2026-09-30")

    dates = result["trade_date"].dt.strftime("%Y-%m-%d").tolist()
    expected = [row[0] for row in series if "2015-01-01" <= row[0] <= "2026-09-30"]
    assert dates == expected
    assert len(dates) > 1000, "the old forward walk only ever returned the final 320 rows"


def test_tencent_kline_stops_when_upstream_ignores_the_window_end(caplog):
    """无视 end、永远回最新尾部的上游：第二轮最早日不回退，必须立刻收手。"""
    series = _tencent_tail_series("2026-06-01", "2026-09-30")
    calls = {"n": 0}

    def stubborn_kline(url, params, headers, timeout):
        calls["n"] += 1
        return {"data": {"sz000001": {"day": series[:]}}}

    fetcher = HttpAStockFetcher(public_json_get=stubborn_kline)
    with caplog.at_level(logging.WARNING, logger="astock_backtester.data.astock_adapter"):
        rows = fetcher._fetch_tencent_kline("000001", "sz000001", "2015-01-01", "2026-09-30")

    assert calls["n"] == 2, "the second round returns the very same tail and must stop"
    assert len({item["trade_date"] for item in rows}) == len(rows), "rewind boundaries must not duplicate rows"
    assert any("cursor did not advance" in record.getMessage() for record in caplog.records)


def test_tencent_kline_paging_stops_at_page_cap():
    """分页必须有迭代上限：上游每轮只往回带一天也不能把循环跑成无界。"""
    calls = {"n": 0}

    def one_day_per_call(url, params, headers, timeout):
        calls["n"] += 1
        if calls["n"] > 500:
            return {"data": {"sh600519": {"day": []}}}
        # 实测语义：返回窗口内最后一行 → 用请求窗口的 end（param 第 4 段）造行。
        window_end = str(params["param"]).split(",")[3]
        return {"data": {"sh600519": {"day": [[window_end, "10.00", "10.50", "11.00", "9.80", "1000"]]}}}

    fetcher = HttpAStockFetcher(public_json_get=one_day_per_call)
    rows = fetcher._fetch_tencent_kline("600519", "sh600519", "2015-01-05", "2026-09-30")

    cap = getattr(astock_adapter, "TENCENT_KLINE_MAX_PAGES", None)
    assert isinstance(cap, int), "tencent kline paging must declare an explicit page cap"
    assert calls["n"] == cap, "paging must stop at the declared cap"
    assert len(rows) == cap
    assert len({item["trade_date"] for item in rows}) == cap, "each rewound page must contribute a fresh day"


def test_tencent_kline_paging_stops_at_walk_budget(monkeypatch, caplog):
    """页数上限不是时间上限：64 页 × 15s = 最坏 960s/票，必须有墙钟预算。"""
    budget = getattr(astock_adapter, "TENCENT_KLINE_WALK_BUDGET_SECONDS", None)
    assert isinstance(budget, (int, float)) and budget > 0

    calls = {"n": 0}

    def slow_one_day(url, params, headers, timeout):
        calls["n"] += 1
        # 每次请求都"耗时"半天预算，让回走在两三页内触顶。
        clock[0] += budget / 2
        window_end = str(params["param"]).split(",")[3]
        return {"data": {"sh600519": {"day": [[window_end, "10.00", "10.50", "11.00", "9.80", "1000"]]}}}

    clock = [0.0]
    monkeypatch.setattr(astock_adapter.time, "monotonic", lambda: clock[0])
    fetcher = HttpAStockFetcher(public_json_get=slow_one_day)

    with caplog.at_level(logging.WARNING, logger="astock_backtester.data.astock_adapter"):
        rows = fetcher._fetch_tencent_kline("600519", "sh600519", "2015-01-05", "2026-09-30")

    assert calls["n"] < astock_adapter.TENCENT_KLINE_MAX_PAGES, "the wall-clock budget must stop the walk early"
    assert rows, "pages fetched before the budget ran out must still be returned"
    assert any("walk budget" in record.getMessage() for record in caplog.records)


def _sina_tail(datalen: int, last_day: str) -> list[dict[str, str]]:
    """A clamped newest-N tail: ``datalen`` rows ending at ``last_day``."""
    end = datetime.strptime(last_day, "%Y-%m-%d").date()
    rows = []
    for offset in range(datalen):
        day = (end - timedelta(days=offset)).strftime("%Y-%m-%d")
        rows.append(
            {"day": day, "open": "10.00", "high": "11.00", "low": "9.50", "close": "10.50", "volume": "1000"}
        )
    return rows


def test_sina_long_window_reports_under_coverage(monkeypatch, caplog):
    """新浪跨度需求超过 datalen 封顶时必须显式报欠覆盖，绝不允许静默返回截断尾部。"""
    from astock_backtester.data.astock_adapter import SINA_KLINE_MAX_LENGTH

    requested_datalen = []

    def fake_public_json_get(url, params, headers, timeout):
        if "fqkline/get" in url:
            return {"data": {}}
        assert "getKLineData" in url
        datalen = int(params["datalen"])
        requested_datalen.append(datalen)
        return _sina_tail(datalen, "2026-09-24")

    fetcher = HttpAStockFetcher(public_json_get=fake_public_json_get)
    with caplog.at_level(logging.WARNING, logger="astock_backtester.data.astock_adapter"):
        result = fetcher.fetch_daily_bars(["920171"], "2015-01-05", "2026-09-24")

    assert not result.empty
    assert requested_datalen and max(requested_datalen) <= SINA_KLINE_MAX_LENGTH, "datalen must stay within the cap"
    under_coverage = [record for record in caplog.records if "under-cover" in record.getMessage()]
    assert under_coverage, "a span beyond one page must be reported as under-coverage"


def test_sina_short_window_stays_silent(caplog):
    """单页能覆盖的窗口不需要任何欠覆盖告警（避免日志噪音）。"""
    def fake_public_json_get(url, params, headers, timeout):
        if "fqkline/get" in url:
            return {"data": {}}
        return _sina_tail(int(params["datalen"]), "2026-09-24")

    fetcher = HttpAStockFetcher(public_json_get=fake_public_json_get)
    with caplog.at_level(logging.WARNING, logger="astock_backtester.data.astock_adapter"):
        result = fetcher.fetch_daily_bars(["920171"], "2026-09-22", "2026-09-23")

    assert len(result) == 2
    assert not [record for record in caplog.records if "under-cover" in record.getMessage()]


def test_flow_breaker_recovers_through_half_open_probe(monkeypatch):
    """熔断必须是 closed→open→half-open 状态机：冷却后放行一次试探，成功即恢复。"""
    now = [0.0]
    state = {"fail": True, "calls": 0}

    def transport(url, params, headers, timeout):
        state["calls"] += 1
        if state["fail"]:
            raise TimeoutError("unreachable")
        return {"data": {"klines": ["2026-09-24,2000,1,2"]}}

    monkeypatch.setattr(astock_adapter, "_breaker_now", lambda: now[0])
    fetcher = HttpAStockFetcher(json_get=transport)

    for _ in range(EASTMONEY_ENRICHMENT_FAILURE_LIMIT):
        assert fetcher._fetch_fund_flow_with_breaker("600519") == {}
    assert state["calls"] == EASTMONEY_ENRICHMENT_FAILURE_LIMIT

    # open：后续调用立即返回，既不打上游也不等超时
    assert fetcher._fetch_fund_flow_with_breaker("600519") == {}
    assert state["calls"] == EASTMONEY_ENRICHMENT_FAILURE_LIMIT

    cooldown = getattr(astock_adapter, "EASTMONEY_ENRICHMENT_COOLDOWN_SECONDS", 60.0)
    assert isinstance(cooldown, (int, float)) and cooldown > 0, "open must cool down for a bounded period"

    # 冷却结束 → half-open 放行一次试探；试探成功 → closed 并清零
    state["fail"] = False
    now[0] += cooldown + 1
    assert fetcher._fetch_fund_flow_with_breaker("600519") == {"2026-09-24": 2000.0}
    assert state["calls"] == EASTMONEY_ENRICHMENT_FAILURE_LIMIT + 1

    # closed：恢复正常放行
    assert fetcher._fetch_fund_flow_with_breaker("600519") == {"2026-09-24": 2000.0}
    assert state["calls"] == EASTMONEY_ENRICHMENT_FAILURE_LIMIT + 2


def test_flow_breaker_probe_failure_reopens_for_another_cooldown(monkeypatch):
    """half-open 试探失败必须回到 open，且冷却重新计时（不能立刻再打一次超时）。"""
    now = [0.0]
    calls = {"n": 0}

    def failing_transport(url, params, headers, timeout):
        calls["n"] += 1
        raise TimeoutError("unreachable")

    monkeypatch.setattr(astock_adapter, "_breaker_now", lambda: now[0])
    fetcher = HttpAStockFetcher(json_get=failing_transport)

    for _ in range(EASTMONEY_ENRICHMENT_FAILURE_LIMIT):
        fetcher._fetch_fund_flow_with_breaker("600519")
    cooldown = getattr(astock_adapter, "EASTMONEY_ENRICHMENT_COOLDOWN_SECONDS", 60.0)

    fetcher._fetch_fund_flow_with_breaker("600519")
    assert calls["n"] == EASTMONEY_ENRICHMENT_FAILURE_LIMIT

    now[0] += cooldown + 1
    fetcher._fetch_fund_flow_with_breaker("600519")
    assert calls["n"] == EASTMONEY_ENRICHMENT_FAILURE_LIMIT + 1

    fetcher._fetch_fund_flow_with_breaker("600519")
    assert calls["n"] == EASTMONEY_ENRICHMENT_FAILURE_LIMIT + 1, "a failed probe must return to open"

    now[0] += cooldown + 1
    fetcher._fetch_fund_flow_with_breaker("600519")
    assert calls["n"] == EASTMONEY_ENRICHMENT_FAILURE_LIMIT + 2


def test_flow_and_info_breakers_open_independently():
    """info 与 flow 是两条独立熔断：资金流跳闸不能连带停掉市值补齐。"""
    counts = {"flow": 0, "info": 0}

    def transport(url, params, headers, timeout):
        if "fflow/daykline/get" in url:
            counts["flow"] += 1
            raise TimeoutError("unreachable")
        counts["info"] += 1
        return {"data": {"f58": "股票", "f117": 8_800_000_000, "f189": "20200101"}}

    fetcher = HttpAStockFetcher(json_get=transport)

    for _ in range(EASTMONEY_ENRICHMENT_FAILURE_LIMIT):
        fetcher._fetch_fund_flow_with_breaker("600519")
    fetcher._fetch_fund_flow_with_breaker("600519")
    assert counts["flow"] == EASTMONEY_ENRICHMENT_FAILURE_LIMIT

    info = fetcher._try_fetch_eastmoney_stock_info("600519")

    assert info["name"] == "股票"
    assert counts["info"] == 1


def test_flow_and_info_breakers_share_one_lock():
    """info 与 flow 的熔断状态必须在同一把锁下读改写（共用一个 fetcher 的锁）。"""
    fetcher = HttpAStockFetcher(json_get=lambda url, params, headers, timeout: {})
    flow = getattr(fetcher, "_flow_breaker", None)
    info = getattr(fetcher, "_info_breaker", None)

    assert flow is not None and info is not None, "fetcher must own one breaker per endpoint"
    assert getattr(flow, "_lock", None) is getattr(info, "_lock", None), "flow 与 info 熔断必须共用同一把锁"


def test_enrichment_breaker_counts_failures_exactly_under_concurrency(monkeypatch):
    """失败计数的读改写必须在同一把锁里：并发丢更新会让熔断永不跳闸。"""
    monkeypatch.setattr(astock_adapter, "EASTMONEY_ENRICHMENT_FAILURE_LIMIT", 10_000)
    fetcher = HttpAStockFetcher(json_get=lambda url, params, headers, timeout: {})
    breaker = getattr(fetcher, "_flow_breaker", None)
    assert breaker is not None, "the breaker must own the failure counter"

    workers, per_worker = 4, 250

    def hammer():
        for _ in range(per_worker):
            breaker.record_failure()

    threads = [threading.Thread(target=hammer) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert breaker.failures == workers * per_worker
    assert breaker.state == "closed", "below the limit the breaker must stay closed"


def test_injected_transports_skip_throttling():
    """注入式构造（hermetic 测试）不能被限速拖慢：默认节流阈值必须是 0。"""
    fetcher = HttpAStockFetcher(json_get=lambda url, params, headers, timeout: {})

    assert getattr(fetcher, "min_request_interval", None) == 0.0, "injected transports must bypass throttling"


def test_production_construction_throttles_per_host():
    """生产构造必须带每 host 限速：全市场补齐的 4 个 worker 不能裸打上游。"""
    default_interval = getattr(HttpAStockFetcher, "HOST_MIN_REQUEST_INTERVAL", None)
    assert isinstance(default_interval, (int, float)) and default_interval > 0, "production must throttle per host"

    fetcher = HttpAStockFetcher()

    assert fetcher.min_request_interval == default_interval


def test_every_outbound_request_path_is_throttled():
    """四条出站路径（公开 K 线、腾讯报价、东财资金流/信息、百度兜底）都要过同一个每 host 限速器。"""
    interval = 0.05
    stamps: dict[str, list[float]] = {}

    def stamp(url, payload):
        stamps.setdefault(urlparse(url).netloc, []).append(time.monotonic())
        return payload

    def public_json(url, params, headers, timeout):
        return stamp(url, {"data": {"sh600519": {"day": [["2026-09-24", "10.00", "10.50", "11.00", "9.80", "1000"]]}}})

    def public_text(url, headers, timeout):
        return stamp(url, "")

    def json_get(url, params, headers, timeout):
        if "fflow/daykline/get" in url:
            return stamp(url, {"data": {"klines": []}})
        return stamp(url, {"data": {"f58": "股票"}})

    fetcher = HttpAStockFetcher(json_get=json_get, public_json_get=public_json, public_text_get=public_text)
    fetcher.min_request_interval = interval

    kline_url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    fetcher._request_public_json(kline_url, {}, {}, 5)
    fetcher._request_public_json(kline_url, {}, {}, 5)

    fetcher._fetch_tencent_quote("600519")
    fetcher._fetch_tencent_quote("600519")

    fetcher._fetch_fund_flow_with_breaker("600519")
    fetcher._fetch_fund_flow_with_breaker("600519")

    fetcher._try_fetch_eastmoney_stock_info("600519")
    fetcher._try_fetch_eastmoney_stock_info("600519")

    fetcher._fetch_baidu_market_data("600519", "2026-09-24")
    fetcher._fetch_baidu_market_data("600519", "2026-09-24")

    assert len(stamps) == 5, f"expected five distinct hosts, got {sorted(stamps)}"
    for host, times in stamps.items():
        assert len(times) == 2, f"{host}: expected two same-host requests"
        assert times[1] - times[0] >= interval, f"{host}: same-host requests must be >= {interval}s apart"


def test_public_kline_path_never_writes_fake_zero_turnover_rate():
    """公开 XHR 主路径不带换手率：拿不到流通股时必须是 NaN，不能是假 0。

    ``normalize_daily_bars`` 给缺列填 ``0.0``，若 ``_with_derived_columns`` 不显式
    置空，新行会带着"换手率 0%"入库——0 是合法值，无法与真实 0% 区分，
    ``turnover_between`` 条件与候选打分都会被污染。
    """

    def fake_public_json_get(url, params, headers, timeout):
        return {"data": {"sh600519": {"day": [["2026-09-24", "10.00", "10.50", "11.00", "9.80", "1000"]]}}}

    def fake_public_text_get(url, headers, timeout):
        # 报价里市值字段为空 → 推不出流通股。
        return 'v_sh600519="' + "~".join(["0"] * 46) + '"'

    fetcher = HttpAStockFetcher(
        public_json_get=fake_public_json_get,
        public_text_get=fake_public_text_get,
    )
    result = fetcher.fetch_daily_bars(["600519"], "2026-09-24", "2026-09-24")

    assert len(result) == 1
    assert pd.isna(result.loc[0, "turnover_rate"]), "unknown turnover must stay NaN, not 0.0"
    assert pd.isna(result.loc[0, "amount"])


def test_public_kline_path_derives_turnover_rate_from_float_shares():
    """能推出流通股时逐行补换手率，量纲是**百分数**（与仓库 229 万行实测一致）。"""
    fields = ["0"] * 53
    fields[1] = "示例股份"
    fields[3] = "10.00"  # 现价
    fields[44] = "1.00"  # 流通市值 1 亿元 → 流通股 1000 万股
    quote_line = 'v_sh600519="' + "~".join(fields) + '"'

    def fake_public_json_get(url, params, headers, timeout):
        # volume 单位是手，入库 ×100 = 100_000 股 = 1000_0000 股的 1% → 换手率 1.0(%)
        return {"data": {"sh600519": {"day": [["2026-09-24", "10.00", "10.50", "11.00", "9.80", "1000"]]}}}

    def fake_public_text_get(url, headers, timeout):
        return quote_line

    fetcher = HttpAStockFetcher(
        public_json_get=fake_public_json_get,
        public_text_get=fake_public_text_get,
    )
    result = fetcher.fetch_daily_bars(["600519"], "2026-09-24", "2026-09-24")

    assert result.loc[0, "turnover_rate"] == 1.0


def test_public_kline_path_marks_st_from_security_name():
    """ST/*ST 只能由证券简称派生：不派生时 ST 股会按 10% 判涨跌停（实际 5%）。"""
    fields = ["0"] * 53
    fields[1] = "*ST示例"
    fields[3] = "10.00"
    fields[44] = "1.00"
    quote_line = 'v_sh600519="' + "~".join(fields) + '"'

    def fake_public_json_get(url, params, headers, timeout):
        return {"data": {"sh600519": {"day": [["2026-09-24", "10.00", "10.50", "11.00", "9.80", "1000"]]}}}

    def fake_public_text_get(url, headers, timeout):
        return quote_line

    fetcher = HttpAStockFetcher(
        public_json_get=fake_public_json_get,
        public_text_get=fake_public_text_get,
    )
    result = fetcher.fetch_daily_bars(["600519"], "2026-09-24", "2026-09-24")

    assert bool(result.loc[0, "is_st"]) is True
    assert result.loc[0, "name"] == "*ST示例"


def test_public_kline_path_keeps_normal_name_out_of_st():
    """普通简称不得被误标 ST（"ST" 子串判定要防退市整理期等非 ST 名称）。"""
    fields = ["0"] * 53
    fields[1] = "国华退"
    fields[3] = "10.00"
    fields[44] = "1.00"
    quote_line = 'v_sh600519="' + "~".join(fields) + '"'

    def fake_public_json_get(url, params, headers, timeout):
        return {"data": {"sh600519": {"day": [["2026-09-24", "10.00", "10.50", "11.00", "9.80", "1000"]]}}}

    def fake_public_text_get(url, headers, timeout):
        return quote_line

    fetcher = HttpAStockFetcher(
        public_json_get=fake_public_json_get,
        public_text_get=fake_public_text_get,
    )
    result = fetcher.fetch_daily_bars(["600519"], "2026-09-24", "2026-09-24")

    # 退市整理期涨跌幅仍是 10%，标成 is_st 会给出错误的 5% 涨跌停口径。
    assert bool(result.loc[0, "is_st"]) is False
