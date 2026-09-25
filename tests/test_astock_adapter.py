import pandas as pd
from astock_backtester.cli import handle_command
from astock_backtester.data.astock_adapter import (
    EASTMONEY_ENRICHMENT_FAILURE_LIMIT,
    AStockDataAdapter,
    AStockDataUnavailable,
    HttpAStockFetcher,
)


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
    """腾讯不覆盖北交所（返回空 day），新浪必须接管且 volume 单位是股。"""
    calls: list[str] = []

    def fake_public_json_get(url, params, headers, timeout):
        calls.append(url)
        if "fqkline/get" in url:
            return {"data": {}}
        assert "getKLineData" in url
        assert params["symbol"] == "bj920171"
        return [
            {"day": "2026-09-22", "open": "16.60", "high": "16.67", "low": "16.07", "close": "16.26", "volume": "1529165"},
            {"day": "2026-09-23", "open": "16.27", "high": "16.50", "low": "15.98", "close": "16.01", "volume": "1034412"},
        ]

    fetcher = HttpAStockFetcher(public_json_get=fake_public_json_get)
    result = fetcher.fetch_daily_bars(["920171"], "2026-09-22", "2026-09-23")

    assert len(result) == 2
    assert result.loc[0, "volume"] == 1_529_165  # 新浪 volume 单位是股，直接入库
    assert any("fqkline" in url for url in calls) and any("getKLineData" in url for url in calls)


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
