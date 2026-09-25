from threading import Event

import pytest
from astock_backtester.data.capital_flow_crawler import (
    EASTMONEY_FUND_FLOW_KLINE_URL,
    SINA_FUND_FLOW_URL,
    CapitalFlowCrawler,
    CapitalFlowFetchError,
)
from astock_backtester.data.symbols import normalize_symbol


def test_fetch_fund_flow_builds_eastmoney_request_and_filters_dates():
    calls = []

    def fake_json_get(url, params, headers, timeout):
        calls.append((url, params, headers, timeout))
        return {
            "data": {
                "klines": [
                    "2024-01-02,2000000,-1500000,-500000,1200000,800000,4.2,-3.1,-1.1,2.5,1.7,1688.0,1.5",
                    "2024-01-03,-3000000,2500000,500000,-2000000,-1000000,-6.0,5.0,1.0,-4.0,-2.0,1690.0,-0.5",
                    "2024-01-04,6000000,-5000000,-1000000,4000000,2000000,8.1,-6.7,-1.4,5.4,2.7,1700.0,0.8",
                ]
            }
        }

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    rows = crawler.fetch_fund_flow("SH600519", "2024-01-03", "2024-01-04", limit=20)

    assert rows == [
        {
            "symbol": "600519",
            "trade_date": "2024-01-03",
            "close": 1690.0,
            "change_pct": -0.5,
            "main_net_inflow": -3000000.0,
            "main_net_inflow_pct": -6.0,
            "small_net_inflow": 2500000.0,
            "small_net_inflow_pct": 5.0,
            "medium_net_inflow": 500000.0,
            "medium_net_inflow_pct": 1.0,
            "large_net_inflow": -2000000.0,
            "large_net_inflow_pct": -4.0,
            "super_large_net_inflow": -1000000.0,
            "super_large_net_inflow_pct": -2.0,
        },
        {
            "symbol": "600519",
            "trade_date": "2024-01-04",
            "close": 1700.0,
            "change_pct": 0.8,
            "main_net_inflow": 6000000.0,
            "main_net_inflow_pct": 8.1,
            "small_net_inflow": -5000000.0,
            "small_net_inflow_pct": -6.7,
            "medium_net_inflow": -1000000.0,
            "medium_net_inflow_pct": -1.4,
            "large_net_inflow": 4000000.0,
            "large_net_inflow_pct": 5.4,
            "super_large_net_inflow": 2000000.0,
            "super_large_net_inflow_pct": 2.7,
        },
    ]

    url, params, headers, timeout = calls[0]
    assert url == "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    assert params["secid"] == "1.600519"
    assert params["lmt"] == "20"
    assert params["klt"] == "101"
    assert params["fields2"] == "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"
    assert headers["Referer"] == "https://data.eastmoney.com/zjlx/detail.html"
    assert timeout == 15


def test_fetch_fund_flow_returns_empty_rows_for_missing_payload():
    crawler = CapitalFlowCrawler(json_get=lambda url, params, headers, timeout: {"data": None})

    with pytest.raises(CapitalFlowFetchError, match="empty_payload"):
        crawler.fetch_fund_flow("600519", "2024-01-01", "2024-01-31")


def test_fetch_many_fund_flows_reports_empty_payload_and_empty_klines_as_failures(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("astock_backtester.data.capital_flow_crawler.time.sleep", sleeps.append)

    def fake_json_get(url, params, headers, timeout):
        if params["secid"] == "1.600519":
            return {"data": None}
        return {"data": {"klines": []}}

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    result = crawler.fetch_many_fund_flows(["600519", "000001"], "2024-01-01", "2024-01-31")

    assert result["rows"] == []
    assert result["failures"] == [
        {"symbol": "600519", "code": "empty_payload", "error": "empty_payload: Eastmoney payload missing data for 600519"},
        {"symbol": "000001", "code": "empty_klines", "error": "empty_klines: Eastmoney payload has no klines for 000001"},
    ]
    # 退避序列被显式断言（此前放过真实 sleep，三个用例各白等 6 秒）
    assert sleeps == [2.0, 4.0]


def test_fetch_many_fund_flows_fetches_remaining_symbols_with_bounded_parallelism(monkeypatch):
    crawler = CapitalFlowCrawler(json_get=lambda url, params, headers, timeout: {"data": {"klines": []}})
    started = {"000002": Event(), "000003": Event()}
    calls: list[str] = []

    def fake_fetch(code, start_date, end_date, *, limit=None, timeout=15, skip_eastmoney=False):
        calls.append(code)
        if code in started:
            started[code].set()
            assert started["000002"].wait(timeout=1)
            assert started["000003"].wait(timeout=1)
        return [
            {
                "symbol": code,
                "trade_date": "2024-01-02",
                "main_net_inflow": 1000000.0,
            }
        ], []

    monkeypatch.setattr(crawler, "_fetch_fund_flow_with_diagnostics", fake_fetch)

    result = crawler.fetch_many_fund_flows(
        ["000001", "000002", "000003"],
        "2024-01-02",
        "2024-01-02",
        max_workers=2,
    )

    assert calls[0] == "000001"
    assert {row["symbol"] for row in result["rows"]} == {"000001", "000002", "000003"}
    assert result["failures"] == []


def test_fetch_many_fund_flows_parallel_success_rows_are_available_for_later_cache_fallback(monkeypatch):
    crawler = CapitalFlowCrawler(json_get=lambda url, params, headers, timeout: {"data": {"klines": []}})

    def successful_fetch(code, start_date, end_date, *, limit=None, timeout=15, skip_eastmoney=False):
        rows = [
            {
                "symbol": code,
                "trade_date": "2024-01-02",
                "main_net_inflow": 1000000.0,
            }
        ]
        crawler._remember_success_rows(code, rows)
        return rows, []

    monkeypatch.setattr(crawler, "_fetch_fund_flow_with_diagnostics", successful_fetch)
    crawler.fetch_many_fund_flows(["000001", "000002", "000003"], "2024-01-02", "2024-01-02", max_workers=3)

    def failing_fetch(code, start_date, end_date, *, limit=None, timeout=15, skip_eastmoney=False):
        raise CapitalFlowFetchError("remote disconnected", code="network_error")

    monkeypatch.setattr(crawler, "_fetch_fund_flow_with_diagnostics", failing_fetch)
    result = crawler.fetch_many_fund_flows(["000003"], "2024-01-02", "2024-01-02")

    assert result["rows"] == [{"symbol": "000003", "trade_date": "2024-01-02", "main_net_inflow": 1000000.0}]
    assert result["failures"] == [{"symbol": "000003", "code": "network_error", "error": "network_error: remote disconnected"}]
    assert any(item["code"] == "recent_success_cache_used" for item in result["diagnostics"])


def test_fetch_many_fund_flows_keeps_bad_numeric_rows_and_reports_diagnostics():
    def fake_json_get(url, params, headers, timeout):
        return {
            "data": {
                "klines": [
                    "2024-01-02,bad-number,-1500000,-500000,1200000,800000,4.2,-3.1,-1.1,2.5,1.7,1688.0,1.5",
                    "2024-01-03,2000000,-1500000,-500000,1200000,800000,4.2,-3.1,-1.1,2.5,1.7,1688.0,1.5",
                ]
            }
        }

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    result = crawler.fetch_many_fund_flows(["600519"], "2024-01-01", "2024-01-31")

    assert len(result["rows"]) == 2
    assert result["rows"][0]["main_net_inflow"] is None
    assert result["rows"][1]["main_net_inflow"] == 2000000.0
    assert result["failures"] == []
    assert any(
        item["symbol"] == "600519"
        and item["code"] == "malformed_numeric"
        and item["field"] == "main_net_inflow"
        and item["trade_date"] == "2024-01-02"
        for item in result["diagnostics"]
    )


def test_fetch_many_fund_flows_reports_date_coverage_shortfall():
    def fake_json_get(url, params, headers, timeout):
        return {
            "data": {
                "klines": [
                    "2024-01-03,2000000,-1500000,-500000,1200000,800000,4.2,-3.1,-1.1,2.5,1.7,1688.0,1.5",
                ]
            }
        }

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    result = crawler.fetch_many_fund_flows(["600519"], "2024-01-02", "2024-01-05")

    assert len(result["rows"]) == 1
    assert result["failures"] == []
    assert any(
        item["symbol"] == "600519"
        and item["code"] == "date_coverage_shortfall"
        and item["first_trade_date"] == "2024-01-03"
        and item["last_trade_date"] == "2024-01-03"
        for item in result["diagnostics"]
    )


def test_fetch_fund_flow_estimates_limit_from_date_span():
    calls = []

    def fake_json_get(url, params, headers, timeout):
        calls.append(params)
        return {"data": {"klines": []}}

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    with pytest.raises(CapitalFlowFetchError, match="empty_klines"):
        crawler.fetch_fund_flow("600519", "2024-01-01", "2024-03-31")

    assert calls[0]["lmt"] == "118"


def test_fetch_fund_flow_skips_malformed_and_blank_numeric_values():
    def fake_json_get(url, params, headers, timeout):
        return {
            "data": {
                "klines": [
                    "not-a-date,2000000,-1500000,-500000,1200000,800000,4.2,-3.1,-1.1,2.5,1.7,1688.0,1.5",
                    "2024-01-02,--,-1500000,-500000,1200000,800000,4.2,-3.1,-1.1,2.5,1.7,1688.0,1.5",
                    "2024-01-03,-3000000,2500000,500000,-2000000,-1000000,-6.0,5.0,1.0,-4.0,-2.0,1690.0,-0.5",
                    "2024-01-04,too-short",
                ]
            }
        }

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    rows = crawler.fetch_fund_flow("600519", "2024-01-01", "2024-01-31")

    assert len(rows) == 2
    assert rows[0]["trade_date"] == "2024-01-02"
    assert rows[0]["main_net_inflow"] is None
    assert rows[1]["trade_date"] == "2024-01-03"
    assert rows[1]["main_net_inflow"] == -3000000.0


def test_fetch_fund_flow_raises_clear_error_when_network_fails():
    def fake_json_get(url, params, headers, timeout):
        raise OSError("remote disconnected")

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    with pytest.raises(CapitalFlowFetchError, match="600519"):
        crawler.fetch_fund_flow("600519", "2024-01-01", "2024-01-31")


def test_default_json_get_ignores_system_proxy_settings(monkeypatch):
    from astock_backtester.data import capital_flow_crawler

    class FakeResponse:
        text = '{"data":{"klines":[]}}'

        def raise_for_status(self):
            return None

    class FakeSession:
        def __init__(self):
            self.trust_env = True
            self.closed = False

        def get(self, url, params, headers, timeout):
            assert self.trust_env is False
            assert url == "https://example.test"
            assert params == {"a": "1"}
            assert headers == {"User-Agent": "test"}
            assert timeout == 3
            return FakeResponse()

        def close(self):
            self.closed = True

    sessions = []

    def fake_get_thread_session():
        session = FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(capital_flow_crawler, "_get_thread_session", fake_get_thread_session)

    result = capital_flow_crawler._default_json_get(
        "https://example.test",
        {"a": "1"},
        {"User-Agent": "test"},
        3,
    )

    assert result == {"data": {"klines": []}}
    assert len(sessions) == 1
    assert sessions[0].trust_env is False


def test_fetch_fund_flow_retries_with_header_variants_after_remote_disconnect():
    calls = []

    def fake_json_get(url, params, headers, timeout):
        calls.append((url, headers["Referer"], timeout))
        if len(calls) == 1:
            raise OSError("remote disconnected")
        return {
            "data": {
                "klines": [
                    "2024-01-02,2000000,-1500000,-500000,1200000,800000,4.2,-3.1,-1.1,2.5,1.7,1688.0,1.5",
                ]
            }
        }

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    rows = crawler.fetch_fund_flow("600519", "2024-01-01", "2024-01-31", timeout=9)

    assert rows[0]["main_net_inflow"] == 2000000.0
    assert len(calls) == 2
    assert calls[0][1] == "https://data.eastmoney.com/zjlx/detail.html"
    assert calls[1][1] == "https://quote.eastmoney.com/"
    assert calls[0][2] == 9
    assert calls[1][2] == 9


def test_fetch_fund_flow_tries_eastmoney_ut_param_variant_after_basic_requests_fail():
    calls = []

    def fake_json_get(url, params, headers, timeout):
        calls.append((dict(params), headers["Referer"]))
        if "ut" not in params:
            raise OSError("remote disconnected")
        return {
            "data": {
                "klines": [
                    "2024-01-02,2000000,-1500000,-500000,1200000,800000,4.2,-3.1,-1.1,2.5,1.7,1688.0,1.5",
                ]
            }
        }

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    rows = crawler.fetch_fund_flow("600519", "2024-01-01", "2024-01-31")

    assert rows[0]["main_net_inflow"] == 2000000.0
    assert "ut" not in calls[0][0]
    assert "ut" not in calls[1][0]
    assert calls[0][1] == "https://data.eastmoney.com/zjlx/detail.html"
    assert calls[1][1] == "https://quote.eastmoney.com/"
    assert calls[2][0]["ut"]


def test_fetch_fund_flow_uses_push2_kline_fallback_when_daykline_fails():
    calls = []

    def fake_json_get(url, params, headers, timeout):
        calls.append((url, dict(params), headers["Referer"]))
        if url != EASTMONEY_FUND_FLOW_KLINE_URL:
            raise OSError("daykline disconnected")
        return {
            "data": {
                "klines": [
                    "2024-01-02,2000000,-1500000,-500000,1200000,800000",
                    "2024-01-03,-3000000,2500000,500000,-2000000,-1000000",
                ]
            }
        }

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    rows = crawler.fetch_fund_flow("000001", "2024-01-01", "2024-01-31")

    assert rows[0]["main_net_inflow"] == 2000000.0
    assert rows[1]["main_net_inflow"] == -3000000.0
    assert calls[-1][0] == EASTMONEY_FUND_FLOW_KLINE_URL
    assert calls[-1][1]["klt"] == "101"
    assert calls[-1][1]["lmt"] == "40"


def test_fetch_many_fund_flows_keeps_successful_rows_and_reports_failures(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("astock_backtester.data.capital_flow_crawler.time.sleep", sleeps.append)

    def fake_json_get(url, params, headers, timeout):
        if params["secid"] == "1.600519":
            return {
                "data": {
                    "klines": [
                        "2024-01-02,2000000,-1500000,-500000,1200000,800000,4.2,-3.1,-1.1,2.5,1.7,1688.0,1.5",
                    ]
                }
            }
        raise OSError("remote disconnected")

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    result = crawler.fetch_many_fund_flows(["600519", "000001"], "2024-01-01", "2024-01-31")

    assert len(result["rows"]) == 1
    assert result["rows"][0]["symbol"] == "600519"
    assert result["failures"][0]["symbol"] == "000001"
    assert "Failed to fetch Eastmoney capital flow for 000001" in result["failures"][0]["error"]
    assert "https://data.eastmoney.com/zjlx/detail.html [base]: remote disconnected" in result["failures"][0]["error"]
    assert "https://quote.eastmoney.com/ [base]: remote disconnected" in result["failures"][0]["error"]
    assert "https://data.eastmoney.com/zjlx/detail.html [ut]: remote disconnected" in result["failures"][0]["error"]
    assert sleeps == [2.0, 4.0]


def test_fetch_many_fund_flows_uses_baidu_history_fallback_without_terminal_failure():
    eastmoney_calls = []
    baidu_calls = []

    def fake_eastmoney_json_get(url, params, headers, timeout):
        eastmoney_calls.append((url, dict(params), headers["Referer"], timeout))
        raise OSError("remote disconnected")

    def fake_baidu_json_get(url, params, headers, timeout):
        baidu_calls.append((url, dict(params), headers["Referer"], timeout))
        return {
            "Result": {
                "content": [
                    {
                        "date": "2026/06/05",
                        "closepx": "10.98",
                        "extMainIn": "+3298.68\u4e07",
                        "littleNetIn": "+1786.39\u4e07",
                        "mediumNetIn": "-5085.07\u4e07",
                        "largeNetIn": "+1.32\u4ebf",
                        "superNetIn": "-9886.01\u4e07",
                        "ratio": "+1.48%",
                    },
                    {
                        "date": "2026/06/04",
                        "closepx": "10.95",
                        "extMainIn": "-1.81\u4ebf",
                        "littleNetIn": "+9737.23\u4e07",
                        "mediumNetIn": "+8390.03\u4e07",
                        "largeNetIn": "-873.31\u4e07",
                        "superNetIn": "-1.73\u4ebf",
                        "ratio": "-8.14%",
                    },
                ]
            }
        }

    crawler = CapitalFlowCrawler(
        baidu_json_get=fake_baidu_json_get,
        eastmoney_json_getters=(
            ("requests", fake_eastmoney_json_get),
            ("curl_cffi", fake_eastmoney_json_get),
        ),
    )

    result = crawler.fetch_many_fund_flows(["000001"], "2026-06-04", "2026-06-05")

    assert result["failures"] == []
    assert len(result["rows"]) == 2
    assert result["rows"][0]["symbol"] == "000001"
    assert result["rows"][0]["trade_date"] == "2026-06-05"
    assert result["rows"][0]["main_net_inflow"] == 32986800.0
    assert result["rows"][0]["large_net_inflow"] == 132000000.0
    assert result["rows"][0]["super_large_net_inflow"] == -98860100.0
    assert result["rows"][0]["main_net_inflow_pct"] == 1.48
    assert result["rows"][1]["main_net_inflow"] == -181000000.0
    assert eastmoney_calls
    assert baidu_calls == [
        (
            "https://finance.pae.baidu.com/vapi/v1/fundsortlist",
            {
                "code": "000001",
                "market": "ab",
                "finance_type": "stock",
                "tab": "day",
                "from": "history",
                "date": "20260608",
                "pn": "0",
                "rn": "20",
                "finClientType": "pc",
            },
            "https://finance.pae.baidu.com/",
            15,
        )
    ]
    assert any(
        item["symbol"] == "000001"
        and item["code"] == "provider_attempt_failed"
        and item["provider"] == "eastmoney"
        for item in result["diagnostics"]
    )
    assert any(
        item["symbol"] == "000001"
        and item["code"] == "provider_fallback_used"
        and item["provider"] == "baidu"
        and item["rows"] == 2
        for item in result["diagnostics"]
    )


def test_fetch_many_fund_flows_uses_sina_fallback_before_baidu_when_eastmoney_fails():
    eastmoney_calls = []
    sina_calls = []
    baidu_calls = []

    def fake_eastmoney_json_get(url, params, headers, timeout):
        eastmoney_calls.append(params["secid"])
        raise OSError("remote disconnected")

    def fake_sina_json_get(url, params, headers, timeout):
        sina_calls.append((url, dict(params), headers["Referer"], timeout))
        return [
            {
                "opendate": "2026-06-05",
                "trade": "10.98",
                "changeratio": "0.0148",
                "netamount": "32986800.0",
                "ratioamount": "0.0148",
                "r0_net": "-98860100.0",
                "r1_net": "132000000.0",
                "r2_net": "-50850700.0",
                "r3_net": "17863900.0",
            },
            {
                "opendate": "2026-06-04",
                "trade": "10.95",
                "changeratio": "-0.0814",
                "netamount": "-181000000.0",
                "ratioamount": "-0.0814",
                "r0_net": "-173000000.0",
                "r1_net": "-8733100.0",
                "r2_net": "83900300.0",
                "r3_net": "97372300.0",
            },
        ]

    def fake_baidu_json_get(url, params, headers, timeout):
        baidu_calls.append(params["code"])
        return {"Result": {"content": [{"date": "2026/06/05", "extMainIn": "100\u4e07"}]}}

    crawler = CapitalFlowCrawler(
        baidu_json_get=fake_baidu_json_get,
        sina_json_get=fake_sina_json_get,
        eastmoney_json_getters=(
            ("requests", fake_eastmoney_json_get),
            ("curl_cffi", fake_eastmoney_json_get),
        ),
    )

    result = crawler.fetch_many_fund_flows(["000001"], "2026-06-04", "2026-06-05")

    assert result["failures"] == []
    assert len(result["rows"]) == 2
    assert result["rows"][0]["symbol"] == "000001"
    assert result["rows"][0]["trade_date"] == "2026-06-05"
    assert result["rows"][0]["main_net_inflow"] == 32986800.0
    assert result["rows"][0]["large_net_inflow"] == 132000000.0
    assert result["rows"][0]["super_large_net_inflow"] == -98860100.0
    assert result["rows"][0]["main_net_inflow_pct"] == pytest.approx(1.48)
    assert eastmoney_calls
    assert sina_calls == [
        (
            SINA_FUND_FLOW_URL,
            {
                "page": "1",
                "num": "5000",
                "sort": "opendate",
                "asc": "0",
                "daima": "sz000001",
            },
            "https://money.finance.sina.com.cn/moneyflow/",
            15,
        )
    ]
    assert baidu_calls == []
    assert any(
        item["symbol"] == "000001"
        and item["code"] == "provider_fallback_used"
        and item["provider"] == "sina"
        and item["rows"] == 2
        for item in result["diagnostics"]
    )


def test_sina_history_fallback_retries_transient_http_errors_before_baidu(monkeypatch):
    eastmoney_calls = []
    sina_calls = []
    baidu_calls = []
    sleeps = []

    def fake_eastmoney_json_get(url, params, headers, timeout):
        eastmoney_calls.append(params["secid"])
        raise OSError("remote disconnected")

    def fake_sina_json_get(url, params, headers, timeout):
        sina_calls.append(params["daima"])
        if len(sina_calls) == 1:
            raise RuntimeError("456 Client Error")
        return [
            {
                "opendate": "2026-06-05",
                "trade": "10.98",
                "changeratio": "0.0148",
                "netamount": "32986800.0",
                "ratioamount": "0.0148",
            }
        ]

    def fake_baidu_json_get(url, params, headers, timeout):
        baidu_calls.append(params["code"])
        return {"Result": {"content": [{"date": "2026/06/05", "extMainIn": "100\u4e07"}]}}

    monkeypatch.setattr("astock_backtester.data.capital_flow_crawler.time.sleep", lambda value: sleeps.append(value))
    crawler = CapitalFlowCrawler(
        baidu_json_get=fake_baidu_json_get,
        sina_json_get=fake_sina_json_get,
        eastmoney_json_getters=(("requests", fake_eastmoney_json_get),),
    )

    result = crawler.fetch_many_fund_flows(["000001"], "2026-06-05", "2026-06-05")

    assert result["failures"] == []
    assert result["rows"][0]["main_net_inflow"] == 32986800.0
    assert sina_calls == ["sz000001", "sz000001"]
    assert sleeps
    assert baidu_calls == []
    assert any(
        item["code"] == "provider_page_retry" and item["provider"] == "sina"
        for item in result["diagnostics"]
    )


def test_fetch_many_fund_flows_rate_limits_parallel_sina_fallback(monkeypatch):
    eastmoney_calls = []
    sina_calls = []
    sleeps = []

    def fake_eastmoney_json_get(url, params, headers, timeout):
        eastmoney_calls.append(params["secid"])
        raise OSError("remote disconnected")

    def fake_sina_json_get(url, params, headers, timeout):
        sina_calls.append(params["daima"])
        return [
            {
                "opendate": "2026-06-05",
                "netamount": "32986800.0",
            }
        ]

    monotonic_values = iter([0.0, 0.0, 0.0, 0.0, 10.0])
    monkeypatch.setattr("astock_backtester.data.capital_flow_crawler.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("astock_backtester.data.capital_flow_crawler.time.sleep", lambda value: sleeps.append(value))
    crawler = CapitalFlowCrawler(
        sina_json_get=fake_sina_json_get,
        eastmoney_json_getters=(("requests", fake_eastmoney_json_get),),
    )

    result = crawler.fetch_many_fund_flows(
        ["000001", "000002"],
        "2026-06-05",
        "2026-06-05",
        max_workers=2,
    )

    assert result["failures"] == []
    assert sorted(sina_calls) == ["sz000001", "sz000002"]
    assert sleeps == [0.25]


def test_sina_fallback_merges_baidu_rows_for_missing_dates():
    def fake_eastmoney_json_get(url, params, headers, timeout):
        raise OSError("remote disconnected")

    def fake_sina_json_get(url, params, headers, timeout):
        return [
            {"opendate": "2024-07-17", "netamount": "300.0"},
            {"opendate": "2024-07-15", "netamount": "100.0"},
        ]

    def fake_baidu_json_get(url, params, headers, timeout):
        return {
            "Result": {
                "content": [
                    {"date": "2024/07/17", "extMainIn": "300"},
                    {"date": "2024/07/16", "extMainIn": "200"},
                    {"date": "2024/07/15", "extMainIn": "100"},
                ]
            }
        }

    crawler = CapitalFlowCrawler(
        baidu_json_get=fake_baidu_json_get,
        sina_json_get=fake_sina_json_get,
        eastmoney_json_getters=(("requests", fake_eastmoney_json_get),),
    )

    result = crawler.fetch_many_fund_flows(["000001"], "2024-07-15", "2024-07-17")

    rows = sorted(result["rows"], key=lambda row: row["trade_date"])
    assert [(row["trade_date"], row["main_net_inflow"]) for row in rows] == [
        ("2024-07-15", 100.0),
        ("2024-07-16", 200.0),
        ("2024-07-17", 300.0),
    ]
    assert any(
        item["code"] == "provider_supplement_used"
        and item["provider"] == "baidu"
        and item["rows"] == 1
        for item in result["diagnostics"]
    )


def test_baidu_history_fallback_paginates_until_requested_start_date():
    requested_dates = []

    def fake_eastmoney_json_get(url, params, headers, timeout):
        raise OSError("remote disconnected")

    def fake_baidu_json_get(url, params, headers, timeout):
        requested_dates.append(params["date"])
        if params["date"] == "20260608":
            return {
                "Result": {
                    "content": [
                        {"date": "2026/06/05", "extMainIn": "100\u4e07"},
                        {"date": "2026/06/04", "extMainIn": "200\u4e07"},
                    ]
                }
            }
        return {
            "Result": {
                "content": [
                    {"date": "2026/06/03", "extMainIn": "300\u4e07"},
                    {"date": "2026/06/02", "extMainIn": "400\u4e07"},
                    {"date": "2026/06/01", "extMainIn": "500\u4e07"},
                ]
            }
        }

    crawler = CapitalFlowCrawler(
        baidu_json_get=fake_baidu_json_get,
        eastmoney_json_getters=(
            ("requests", fake_eastmoney_json_get),
            ("curl_cffi", fake_eastmoney_json_get),
        ),
    )

    rows = crawler.fetch_fund_flow("000001", "2026-06-02", "2026-06-05")

    assert [row["trade_date"] for row in rows] == [
        "2026-06-05",
        "2026-06-04",
        "2026-06-03",
        "2026-06-02",
    ]
    assert [row["main_net_inflow"] for row in rows] == [1000000.0, 2000000.0, 3000000.0, 4000000.0]
    assert requested_dates == ["20260608", "20260603"]


def test_baidu_history_fallback_retries_page_failures_with_backoff(monkeypatch):
    requested_dates = []
    sleeps = []

    def fake_eastmoney_json_get(url, params, headers, timeout):
        raise OSError("remote disconnected")

    def fake_baidu_json_get(url, params, headers, timeout):
        requested_dates.append(params["date"])
        if len(requested_dates) == 1:
            raise OSError("temporary baidu disconnect")
        return {"Result": {"content": [{"date": "2026/06/05", "extMainIn": "100\u4e07"}]}}

    monkeypatch.setattr("astock_backtester.data.capital_flow_crawler.time.sleep", sleeps.append)
    crawler = CapitalFlowCrawler(
        baidu_json_get=fake_baidu_json_get,
        eastmoney_json_getters=(
            ("requests", fake_eastmoney_json_get),
            ("curl_cffi", fake_eastmoney_json_get),
        ),
    )

    result = crawler.fetch_many_fund_flows(["000001"], "2026-06-05", "2026-06-05")

    assert result["failures"] == []
    assert result["rows"][0]["main_net_inflow"] == 1000000.0
    assert requested_dates == ["20260608", "20260608"]
    assert sleeps == [0.5]
    assert any(
        item["symbol"] == "000001"
        and item["code"] == "provider_page_retry"
        and item["provider"] == "baidu"
        and item["attempt"] == 1
        for item in result["diagnostics"]
    )


def test_baidu_history_fallback_rate_limits_between_pages(monkeypatch):
    requested_dates = []
    sleeps = []

    def fake_eastmoney_json_get(url, params, headers, timeout):
        raise OSError("remote disconnected")

    def fake_baidu_json_get(url, params, headers, timeout):
        requested_dates.append(params["date"])
        if params["date"] == "20260608":
            return {
                "Result": {
                    "content": [
                        {"date": "2026/06/05", "extMainIn": "100\u4e07"},
                        {"date": "2026/06/04", "extMainIn": "200\u4e07"},
                    ]
                }
            }
        return {"Result": {"content": [{"date": "2026/06/03", "extMainIn": "300\u4e07"}]}}

    monkeypatch.setattr("astock_backtester.data.capital_flow_crawler.time.sleep", sleeps.append)
    crawler = CapitalFlowCrawler(
        baidu_json_get=fake_baidu_json_get,
        eastmoney_json_getters=(
            ("requests", fake_eastmoney_json_get),
            ("curl_cffi", fake_eastmoney_json_get),
        ),
    )

    rows = crawler.fetch_fund_flow("000001", "2026-06-03", "2026-06-05")

    assert [row["trade_date"] for row in rows] == ["2026-06-05", "2026-06-04", "2026-06-03"]
    assert requested_dates == ["20260608", "20260603"]
    assert sleeps == [0.05]


def test_baidu_history_fallback_suspends_repeated_eastmoney_disconnects_for_batch_speed():
    eastmoney_calls = []
    baidu_codes = []

    def fake_eastmoney_json_get(url, params, headers, timeout):
        eastmoney_calls.append(params["secid"])
        raise OSError("remote disconnected")

    def fake_baidu_json_get(url, params, headers, timeout):
        baidu_codes.append(params["code"])
        return {"Result": {"content": [{"date": "2026/06/05", "extMainIn": "100\u4e07"}]}}

    crawler = CapitalFlowCrawler(
        baidu_json_get=fake_baidu_json_get,
        eastmoney_json_getters=(
            ("requests", fake_eastmoney_json_get),
            ("curl_cffi", fake_eastmoney_json_get),
        ),
    )

    result = crawler.fetch_many_fund_flows(["000001", "000002"], "2026-06-05", "2026-06-05")

    assert result["failures"] == []
    assert [row["symbol"] for row in result["rows"]] == ["000001", "000002"]
    assert {item.split(".", 1)[1] for item in eastmoney_calls} == {"000001"}
    assert baidu_codes == ["000001", "000002"]
    assert any(
        item["symbol"] == "000002"
        and item["code"] == "provider_attempt_skipped"
        and item["provider"] == "eastmoney"
        for item in result["diagnostics"]
    )


def test_eastmoney_remote_disconnect_tries_all_transports_before_baidu_fallback():
    eastmoney_calls = []

    def fake_eastmoney_json_get(url, params, headers, timeout):
        eastmoney_calls.append((url, headers["Referer"]))
        raise OSError("RemoteDisconnected: remote end closed connection without response")

    def fake_baidu_json_get(url, params, headers, timeout):
        return {"Result": {"content": [{"date": "2026/06/05", "extMainIn": "100\u4e07"}]}}

    crawler = CapitalFlowCrawler(
        baidu_json_get=fake_baidu_json_get,
        eastmoney_json_getters=(
            ("requests", fake_eastmoney_json_get),
            ("curl_cffi", fake_eastmoney_json_get),
        ),
    )

    result = crawler.fetch_many_fund_flows(["000001"], "2026-06-05", "2026-06-05")

    assert result["failures"] == []
    assert result["rows"][0]["main_net_inflow"] == 1000000.0
    assert len(eastmoney_calls) == 2
    assert any(
        item["symbol"] == "000001"
        and item["code"] == "provider_attempt_failed"
        and item["provider"] == "eastmoney"
        for item in result["diagnostics"]
    )


def test_eastmoney_skip_is_limited_to_one_fetch_many_batch():
    eastmoney_calls = []
    baidu_codes = []

    def fake_eastmoney_json_get(url, params, headers, timeout):
        eastmoney_calls.append(params["secid"])
        raise OSError("remote disconnected")

    def fake_baidu_json_get(url, params, headers, timeout):
        baidu_codes.append(params["code"])
        return {"Result": {"content": [{"date": "2026/06/05", "extMainIn": "100\u4e07"}]}}

    crawler = CapitalFlowCrawler(json_get=fake_eastmoney_json_get, baidu_json_get=fake_baidu_json_get)

    first = crawler.fetch_many_fund_flows(["000001", "000002"], "2026-06-05", "2026-06-05")
    second = crawler.fetch_many_fund_flows(["000003"], "2026-06-05", "2026-06-05")

    assert first["failures"] == []
    assert second["failures"] == []
    assert any(call.endswith(".000003") for call in eastmoney_calls)
    assert not any(
        item["symbol"] == "000003" and item["code"] == "provider_attempt_skipped"
        for item in second["diagnostics"]
    )


def test_fetch_many_fund_flows_uses_recent_success_cache_after_disconnect():
    calls = 0

    def fake_json_get(url, params, headers, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "data": {
                    "klines": [
                        "2024-01-02,2000000,-1500000,-500000,1200000,800000,4.2,-3.1,-1.1,2.5,1.7,1688.0,1.5",
                    ]
                }
            }
        raise OSError("remote disconnected")

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    first = crawler.fetch_many_fund_flows(["600519"], "2024-01-02", "2024-01-02")
    second = crawler.fetch_many_fund_flows(["600519"], "2024-01-02", "2024-01-02")

    assert first["rows"][0]["main_net_inflow"] == 2000000.0
    assert second["rows"][0]["main_net_inflow"] == 2000000.0
    assert second["failures"][0]["symbol"] == "600519"
    assert second["failures"][0]["code"] == "network_error"
    assert any(
        item["symbol"] == "600519" and item["code"] == "recent_success_cache_used"
        for item in second["diagnostics"]
    )


def test_fetch_many_fund_flows_does_not_reuse_recent_success_cache_after_empty_klines(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("astock_backtester.data.capital_flow_crawler.time.sleep", sleeps.append)
    calls = 0

    def fake_json_get(url, params, headers, timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "data": {
                    "klines": [
                        "2024-01-02,2000000,-1500000,-500000,1200000,800000,4.2,-3.1,-1.1,2.5,1.7,1688.0,1.5",
                    ]
                }
            }
        return {"data": {"klines": []}}

    crawler = CapitalFlowCrawler(json_get=fake_json_get)

    first = crawler.fetch_many_fund_flows(["600519"], "2024-01-02", "2024-01-02")
    second = crawler.fetch_many_fund_flows(["600519"], "2024-01-02", "2024-01-02")

    assert first["rows"][0]["main_net_inflow"] == 2000000.0
    assert second["rows"] == []
    assert second["failures"][0]["symbol"] == "600519"
    assert second["failures"][0]["code"] == "empty_klines"
    assert not any(item["code"] == "recent_success_cache_used" for item in second["diagnostics"])
    # 同一符号连续重试：退避从 2.0 起步翻倍（第二次重试 4.0）
    assert sleeps == [2.0, 4.0]


def test_normalize_code_accepts_common_a_share_symbol_forms():
    assert normalize_symbol("SH600519") == "600519"
    assert normalize_symbol("600519.SH") == "600519"
    assert normalize_symbol(" sz000001 ") == "000001"
