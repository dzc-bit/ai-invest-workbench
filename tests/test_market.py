from __future__ import annotations

import sys
import time
from datetime import UTC, datetime

import pytest
from astock_backtester.data.briefing import (
    THS_FUPAN_URL,
    THS_REFERER,
    THS_ZAOPAN_URL,
    MarketBriefingProvider,
    _is_noisy_content_line,
    _table_from_node,
)
from astock_backtester.data.cls_finance import (
    ClsFinanceProvider,
    _parse_ths_market_degree,
    _resolve_node_executable,
    _resolve_ths_cookie_worker,
    _subprocess_startup_kwargs,
)
from astock_backtester.data.market_commentary import MarketCommentaryProvider
from astock_backtester.models import (
    MarketBreadth,
    MarketBriefingResponse,
    MarketBriefingSection,
    MarketIndexQuote,
    MarketNewsItem,
    MarketNewsResponse,
    RealtimeMarketSnapshot,
    SectorMover,
)
from bs4 import BeautifulSoup

# Merged from: test_cls_finance.py, test_market_briefing.py, test_market_commentary.py


# ---------------------------------------------------------------------------
# test_cls_finance.py
# ---------------------------------------------------------------------------


def test_resolves_ths_cookie_runtime_next_to_frozen_sidecar(tmp_path, monkeypatch):
    sidecar = tmp_path / "astock-data-service.exe"
    node = tmp_path / "node.exe"
    worker = tmp_path / "ths-cookie-worker.cjs"
    sidecar.write_text("", encoding="utf-8")
    node.write_text("", encoding="utf-8")
    worker.write_text("", encoding="utf-8")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(sidecar))

    assert _resolve_node_executable() == str(node)
    assert _resolve_ths_cookie_worker() == worker


def test_ths_cookie_worker_hides_console_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    kwargs = _subprocess_startup_kwargs()

    assert kwargs["creationflags"] == 0x08000000
    assert kwargs["startupinfo"].dwFlags & 1
    assert kwargs["startupinfo"].wShowWindow == 0


class _FakeResponse:
    encoding = "utf-8"

    def __init__(self, payload=None, text=""):
        self._payload = payload or {}
        self.text = text

    def raise_for_status(self):
        return

    def json(self):
        return self._payload


def test_cls_finance_uses_browser_transport_when_ths_score_rejects_requests():
    primary_headers = []
    alternate_headers = []

    class ForbiddenResponse(_FakeResponse):
        def raise_for_status(self):
            raise RuntimeError("403 forbidden")

    def requester(_url, **kwargs):
        primary_headers.append(kwargs["headers"])
        return ForbiddenResponse()

    def alternate_requester(_url, **kwargs):
        alternate_headers.append(kwargs["headers"])
        return _FakeResponse(text='{"dppj_data": 7.1}')

    diagnostics = []
    provider = ClsFinanceProvider(
        requester=requester,
        alternate_requester=alternate_requester,
        allow_alternate_transport=True,
        browser_cookie_getter=lambda: "v=browser-cookie",
    )

    score = provider._read_ths_market_degree(diagnostics)

    assert score == 7.1
    assert primary_headers[0]["Cookie"] == "v=browser-cookie"
    assert alternate_headers[0]["Cookie"] == "v=browser-cookie"
    assert any("alternate transport used" in item for item in diagnostics)


def _finance_payloads():
    return {
        "tline": {"code": 200, "data": [{"date": 20260714, "minute": 930, "last_px": 3960, "change": 0.01}]},
        "anchor": {"errno": 0, "data": []},
        "basic": {"code": 200, "data": {"preclose_px": 3950}},
        "emotion": {
            "code": 200,
            "data": {
                "market_degree": "56",
                "up_ratio_num": "85",
                "up_open_num": "12",
                "up_down_dis": {"rise_num": 3919, "fall_num": 1215, "flat_num": 67},
            },
        },
        "up_pool": {"code": 200, "data": []},
        "home": {
            "code": 200,
            "data": {
                "up_down_dis": {"rise_num": 3919, "fall_num": 1215, "flat_num": 67, "up_num": 85},
            },
        },
    }


def test_cls_finance_tline_matches_the_cls_finance_page_request_params():
    requested_params = {}

    def requester(_url, **kwargs):
        requested_params.update(kwargs["params"])
        return _FakeResponse(
            {"code": 200, "data": [{"date": 20260714, "minute": 930, "last_px": 3960}]}
        )

    points = ClsFinanceProvider(requester=requester)._read_tline([])

    assert points
    assert "secu_code" not in requested_params


def test_cls_finance_tline_sorts_points_by_date_and_minute():
    def requester(_url, **_kwargs):
        return _FakeResponse(
            {
                "code": 200,
                "data": [
                    {"date": 20260715, "minute": 1300, "last_px": 3970},
                    {"date": 20260714, "minute": 1500, "last_px": 3960},
                    {"date": 20260715, "minute": 930, "last_px": 3950},
                    {"date": 20260714, "minute": 930, "last_px": 3940},
                    {"date": 20260715, "minute": 930, "last_px": 3951},
                ],
            }
        )

    points = ClsFinanceProvider(requester=requester)._read_tline([])

    assert [(point.date, point.minute, point.last_px) for point in points] == [
        (20260714, 930, 3940),
        (20260714, 1500, 3960),
        (20260715, 930, 3950),
        (20260715, 930, 3951),
        (20260715, 1300, 3970),
    ]


def test_cls_finance_reuses_recent_success_after_transient_source_failure():
    payloads = _finance_payloads()
    should_fail = False

    def requester(url, **kwargs):
        if should_fail:
            raise TimeoutError("CLS temporary timeout")
        if "quote/index/tline" in url:
            return _FakeResponse(payloads["tline"])
        if "v3/transaction/anchor" in url:
            return _FakeResponse(payloads["anchor"])
        if "quote/index/basic" in url:
            return _FakeResponse(payloads["basic"])
        if "v2/quote/a/stock/emotion" in url:
            return _FakeResponse(payloads["emotion"])
        if "quote/index/up_down_analysis" in url:
            return _FakeResponse(payloads["up_pool"])
        if "q.10jqka.com.cn" in url:
            return _FakeResponse(text="<html><body></body></html>")
        raise AssertionError(url)

    provider = ClsFinanceProvider(requester=requester, cache_ttl=0, recent_success_ttl=60)
    provider.browser_cookie_getter = lambda: None

    first = provider.current_board()
    should_fail = True
    second = provider.current_board()

    assert first.tline
    assert second.source.endswith("+recent-success-cache")
    assert second.tline == first.tline
    assert any("recent_success_cache_used" in item for item in second.diagnostics)


def test_cls_finance_preserves_recent_complete_fields_when_refresh_is_partial():
    payloads = _finance_payloads()
    partial = False

    def requester(url, **kwargs):
        if partial:
            if "quote/index/tline" in url:
                return _FakeResponse(payloads["tline"])
            if "v3/transaction/anchor" in url:
                return _FakeResponse(payloads["anchor"])
            if "q.10jqka.com.cn" in url:
                return _FakeResponse(text="<html><body></body></html>")
            raise TimeoutError("CLS partial refresh timeout")
        if "quote/index/tline" in url:
            return _FakeResponse(payloads["tline"])
        if "v3/transaction/anchor" in url:
            return _FakeResponse(payloads["anchor"])
        if "quote/index/basic" in url:
            return _FakeResponse(payloads["basic"])
        if "v2/quote/a/stock/emotion" in url:
            return _FakeResponse(payloads["emotion"])
        if "quote/index/up_down_analysis" in url:
            return _FakeResponse(payloads["up_pool"])
        if "q.10jqka.com.cn" in url:
            return _FakeResponse(text="<html><body></body></html>")
        raise AssertionError(url)

    provider = ClsFinanceProvider(requester=requester, cache_ttl=0, recent_success_ttl=60)
    provider.browser_cookie_getter = lambda: None

    first = provider.current_board()
    partial = True
    second = provider.current_board()

    assert first.emotion is not None
    assert first.emotion.breadth is not None
    assert second.emotion is not None
    assert second.emotion.breadth == first.emotion.breadth
    assert second.source.endswith("+recent-success-cache")
    assert any("recent_success_cache_used" in item for item in second.diagnostics)


def test_cls_finance_preserves_a_valid_empty_up_pool_over_recent_success():
    payloads = _finance_payloads()
    payloads["up_pool"] = {
        "code": 200,
        "data": [{"secu_code": "sh600001", "secu_name": "Prior limit-up"}],
    }
    use_empty_pool = False

    def requester(url, **_kwargs):
        if "quote/index/tline" in url:
            return _FakeResponse(payloads["tline"])
        if "v3/transaction/anchor" in url:
            return _FakeResponse(payloads["anchor"])
        if "quote/index/basic" in url:
            return _FakeResponse(payloads["basic"])
        if "v2/quote/a/stock/emotion" in url:
            return _FakeResponse(payloads["emotion"])
        if "quote/index/up_down_analysis" in url:
            if use_empty_pool:
                return _FakeResponse({"code": 200, "data": []})
            return _FakeResponse(payloads["up_pool"])
        if "q.10jqka.com.cn" in url:
            return _FakeResponse(text="<html><body></body></html>")
        raise AssertionError(url)

    provider = ClsFinanceProvider(requester=requester, cache_ttl=0, recent_success_ttl=60)
    provider.browser_cookie_getter = lambda: None

    first = provider.current_board()
    use_empty_pool = True
    second = provider.current_board()

    assert first.up_pool
    assert second.up_pool == []
    assert not second.source.endswith("+recent-success-cache")


@pytest.mark.parametrize(
    ("field_name", "url_fragment"),
    [
        ("tline", "quote/index/tline"),
        ("anchors", "v3/transaction/anchor"),
        ("up_pool", "quote/index/up_down_analysis"),
    ],
)
def test_cls_finance_reuses_recent_list_field_when_nonempty_rows_are_unparseable(
    field_name,
    url_fragment,
):
    payloads = _finance_payloads()
    payloads["anchor"] = {
        "errno": 0,
        "data": [{"symbol_code": "cls123", "symbol_name": "Prior anchor"}],
    }
    payloads["up_pool"] = {
        "code": 200,
        "data": [{"secu_code": "sh600001", "secu_name": "Prior limit-up"}],
    }
    malformed = False

    def requester(url, **_kwargs):
        if url_fragment in url and malformed:
            return _FakeResponse({"code": 200, "data": [{}]})
        if "quote/index/tline" in url:
            return _FakeResponse(payloads["tline"])
        if "v3/transaction/anchor" in url:
            return _FakeResponse(payloads["anchor"])
        if "quote/index/basic" in url:
            return _FakeResponse(payloads["basic"])
        if "v2/quote/a/stock/emotion" in url:
            return _FakeResponse(payloads["emotion"])
        if "quote/index/up_down_analysis" in url:
            return _FakeResponse(payloads["up_pool"])
        if "q.10jqka.com.cn" in url:
            return _FakeResponse(text="<html><body></body></html>")
        raise AssertionError(url)

    provider = ClsFinanceProvider(requester=requester, cache_ttl=0, recent_success_ttl=60)
    provider.browser_cookie_getter = lambda: None

    first = provider.current_board()
    malformed = True
    second = provider.current_board()

    assert getattr(first, field_name)
    assert getattr(second, field_name) == getattr(first, field_name)
    assert second.source.endswith("+recent-success-cache")
    assert any("no parseable rows" in item for item in second.diagnostics)


def test_cls_finance_uses_home_distribution_when_emotion_endpoint_fails():
    payloads = _finance_payloads()

    def requester(url, **kwargs):
        if "quote/index/tline" in url:
            return _FakeResponse(payloads["tline"])
        if "v3/transaction/anchor" in url:
            return _FakeResponse(payloads["anchor"])
        if "quote/index/basic" in url:
            return _FakeResponse(payloads["basic"])
        if "v2/quote/a/stock/emotion" in url:
            raise TimeoutError("emotion timeout")
        if "quote/index/home" in url:
            return _FakeResponse(payloads["home"])
        if "quote/index/up_down_analysis" in url:
            return _FakeResponse(payloads["up_pool"])
        if "q.10jqka.com.cn" in url:
            return _FakeResponse(text="<html><body></body></html>")
        raise AssertionError(url)

    provider = ClsFinanceProvider(requester=requester, cache_ttl=0)
    provider.browser_cookie_getter = lambda: None

    response = provider.current_board()

    assert response.emotion is not None
    assert response.emotion.breadth is not None
    assert response.emotion.breadth.total == 5201
    assert response.emotion.up_limit == 85
    assert any("CLS homepage distribution fallback used" in item for item in response.diagnostics)


def test_cls_finance_emotion_keeps_primary_zero_counts_over_home_fallback():
    def requester(url, **_kwargs):
        if "v2/quote/a/stock/emotion" in url:
            return _FakeResponse(
                {
                    "code": 200,
                    "data": {
                        "up_ratio_num": 0,
                        "up_open_num": 0,
                        "up_down_dis": {},
                    },
                }
            )
        if "quote/index/home" in url:
            return _FakeResponse(
                {
                    "code": 200,
                    "data": {
                        "up_down_dis": {
                            "rise_num": 3919,
                            "fall_num": 1215,
                            "flat_num": 67,
                            "up_num": 85,
                            "up_open_num": 12,
                        }
                    },
                }
            )
        raise AssertionError(url)

    emotion = ClsFinanceProvider(requester=requester)._read_emotion([])

    assert emotion is not None
    assert emotion.up_limit == 0
    assert emotion.open_limit == 0


def test_cls_finance_does_not_cache_logical_error_responses():
    calls = 0

    def requester(_url, **_kwargs):
        nonlocal calls
        calls += 1
        return _FakeResponse({"code": 500, "message": "signature invalid", "data": {}})

    provider = ClsFinanceProvider(requester=requester, cache_ttl=60, recent_success_ttl=60)
    provider._read_ths_market_degree = lambda _diagnostics: None

    first = provider.current_board()
    calls_after_first = calls
    second = provider.current_board()

    assert calls_after_first > 0
    assert calls > calls_after_first
    assert first.emotion is None
    assert second.emotion is None
    assert provider._cached_response is None
    assert provider._last_successful_response is None
    assert any("code=500" in item for item in first.diagnostics)


# ---------------------------------------------------------------------------
# test_market_briefing.py
# ---------------------------------------------------------------------------


class FakeHtmlResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.content = text.encode("gbk", errors="ignore")
        self.encoding = "gbk"
        self.apparent_encoding = "gbk"

    def raise_for_status(self) -> None:
        return


def test_is_noisy_content_line_filters_ambiguous_profit_and_cn_label_numeric_soup():
    assert _is_noisy_content_line("同比指数盈利")
    assert _is_noisy_content_line(
        "板块名称 最新涨幅 涨跌幅% 股票数（只） 1293.69 +14.46 +1.13% 363.54亿 2026-06-05 15:00:00"
    )
    assert not _is_noisy_content_line("机器人板块午后持续走强，资金围绕题材龙头博弈。")


def test_table_from_node_rejects_headerless_mystery_market_number_rows():
    soup = BeautifulSoup(
        """
        <table>
          <tr><td>半导体</td><td>1293.69</td><td>+14.46</td><td>+1.13%</td><td>363.54亿</td></tr>
          <tr><td>机器人</td><td>1102.18</td><td>+22.40</td><td>+2.07%</td><td>251.11亿</td></tr>
        </table>
        """,
        "html.parser",
    )

    assert _table_from_node(soup.table, title="指数表现") is None


def test_table_from_node_keeps_readable_table_with_explicit_headers():
    soup = BeautifulSoup(
        """
        <table>
          <tr><th>板块名称</th><th>涨跌幅</th><th>解读</th></tr>
          <tr><td>机器人</td><td>+2.07%</td><td>午后持续走强</td></tr>
        </table>
        """,
        "html.parser",
    )

    table = _table_from_node(soup.table, title="板块表现")

    assert table is not None
    assert table.columns == ["板块名称", "涨跌幅", "解读"]
    assert table.rows == [{"板块名称": "机器人", "涨跌幅": "+2.07%", "解读": "午后持续走强"}]


def test_market_briefing_provider_keeps_full_ths_fupan_summary_and_section_body():
    summary_sentinel = "复盘摘要尾部哨兵"
    body_sentinel = "复盘正文尾部哨兵"
    long_summary = ("复盘长摘要" * 70) + summary_sentinel
    long_body = ("复盘长章节正文" * 70) + body_sentinel
    html = f"""
    <html><body>
      <div id="fpzj">{long_summary}</div>
      <div class="fp_item_hd"><h2>长章节</h2></div>
      <div class="fp_item_cnt"><p>{long_body}</p></div>
    </body></html>
    """
    detail_html = """
    <html><body>
      <h1>A股收评：科技股回调</h1>
      <article><p>A股三大指数集体下跌后，科技股尾盘仍有分化。</p></article>
    </body></html>
    """

    def requester(url: str, **kwargs):
        if url.endswith("test.shtml"):
            return FakeHtmlResponse(detail_html)
        return FakeHtmlResponse(html)

    provider = MarketBriefingProvider(requester=requester)

    response = provider.latest_fupan()

    assert response.summary == long_summary
    assert summary_sentinel in response.summary
    assert not response.summary.endswith("...")
    assert response.sections[0].content == long_body
    assert body_sentinel in (response.sections[0].content or "")
    assert not (response.sections[0].content or "").endswith("...")


def test_market_briefing_provider_keeps_full_ths_zaopan_summary_main_and_sidebar_body():
    summary_sentinel = "早盘摘要尾部哨兵"
    main_sentinel = "早盘主栏尾部哨兵"
    sidebar_sentinel = "早盘侧栏尾部哨兵"
    long_summary = ("早盘长摘要" * 60) + summary_sentinel
    long_main_body = ("早盘主栏正文" * 90) + main_sentinel
    long_sidebar_body = ("早盘侧栏正文" * 60) + sidebar_sentinel
    html = f"""
    <html><body>
      <div class="yestoday">{long_summary}</div>
      <div class="content-main-fl"><p>{long_main_body}</p></div>
      <div class="content-main-fr">
        <div class="table-part">
          <h2>侧栏观点</h2>
          <p>{long_sidebar_body}</p>
        </div>
      </div>
    </body></html>
    """

    provider = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html))

    response = provider.latest_zaopan()

    assert response.summary == long_summary
    assert summary_sentinel in response.summary
    assert not response.summary.endswith("...")
    assert response.sections[0].content == long_main_body
    assert main_sentinel in (response.sections[0].content or "")
    assert not (response.sections[0].content or "").endswith("...")
    assert response.sections[1].content == long_sidebar_body
    assert sidebar_sentinel in (response.sections[1].content or "")
    assert not (response.sections[1].content or "").endswith("...")


def test_market_briefing_provider_keeps_full_ths_zaopan_summary_when_derived_from_main_body():
    main_sentinel = "早盘派生摘要尾部哨兵"
    long_main_body = ("早盘无摘要主栏正文" * 80) + main_sentinel
    html = f"""
    <html><body>
      <div class="content-main-fl"><p>{long_main_body}</p></div>
    </body></html>
    """

    detail_html = """
    <html><body>
      <h1>A股收评：科技股回调</h1>
      <article><p>A股三大指数集体下跌后，科技股尾盘仍有分化。</p></article>
    </body></html>
    """

    def requester(url: str, **kwargs):
        if url.endswith("test.shtml"):
            return FakeHtmlResponse(detail_html)
        return FakeHtmlResponse(html)

    provider = MarketBriefingProvider(requester=requester)

    response = provider.latest_zaopan()

    assert response.summary == long_main_body
    assert main_sentinel in response.summary
    assert not response.summary.endswith("...")


def test_market_briefing_provider_prewarms_and_retries_when_ths_returns_empty_body():
    valid_html = """
    <html><body>
      <div id="fpzj">重试后拿到复盘摘要</div>
      <div class="fp_item_hd"><h2>重试章节</h2></div>
      <div class="fp_item_cnt"><p>重试后拿到复盘正文</p></div>
    </body></html>
    """
    calls: list[tuple[str, dict]] = []

    def requester(url: str, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            return FakeHtmlResponse("")
        if url == THS_REFERER:
            return FakeHtmlResponse("<html><body>同花顺入口预热</body></html>")
        return FakeHtmlResponse(valid_html)

    provider = MarketBriefingProvider(requester=requester)

    response = provider.latest_fupan()

    assert response.source == "ths-fupan"
    assert response.summary == "重试后拿到复盘摘要"
    assert [url for url, _ in calls] == [THS_FUPAN_URL, THS_REFERER, THS_FUPAN_URL]
    request_headers = calls[0][1]["headers"]
    assert request_headers["Referer"] == THS_REFERER
    assert "Windows NT" in request_headers["User-Agent"]
    assert "zh-CN" in request_headers["Accept-Language"]


def test_market_briefing_provider_parses_ths_fupan_sections_tables_and_links():
    html = """
    <html><body data-case="fupan-table-link-expansion">
      <div id="fpzj">A股三大指数集体下跌，煤炭、养鸡、AI应用活跃。</div>
      <div class="fp_item_hd"><em>01</em><h2>指数/概念分析</h2></div>
      <div class="fp_item_cnt">
        <p>ERP概念 +4.78%，财税数字化 +4.46%。</p>
        <table>
          <tr><th>个股</th><th>涨幅</th></tr>
          <tr><td>软通动力</td><td>20.00%</td></tr>
        </table>
      </div>
      <div class="fp_item_hd"><em>02</em><h2>同花顺解盘</h2></div>
      <div class="fp_item_cnt">
        <ul><li><a href="http://stock.10jqka.com.cn/test.shtml" title="A股收评：科技股回调">A股收评</a></li></ul>
      </div>
    </body></html>
    """
    detail_html = """
    <html><body>
      <h1>A股收评：科技股回调</h1>
      <article><p>A股三大指数集体下跌后，科技股尾盘仍有分化。</p></article>
    </body></html>
    """

    def requester(url: str, **kwargs):
        if url.endswith("test.shtml"):
            return FakeHtmlResponse(detail_html)
        return FakeHtmlResponse(html)

    provider = MarketBriefingProvider(requester=requester)

    response = provider.latest_fupan()

    assert response.kind == "fupan"
    assert response.source == "ths-fupan"
    assert response.summary == "A股三大指数集体下跌，煤炭、养鸡、AI应用活跃。"
    assert [section.title for section in response.sections] == ["指数/概念分析", "同花顺解盘", "全文：A股收评：科技股回调"]
    assert response.sections[0].tables[0].columns == ["个股", "涨幅"]
    assert response.sections[0].tables[0].rows == [{"个股": "软通动力", "涨幅": "20.00%"}]
    assert response.sections[1].links[0].title == "A股收评：科技股回调"
    assert "A股三大指数集体下跌" in (response.sections[2].content or "")


def test_market_briefing_provider_expands_article_links_into_full_text_sections():
    index_html = """
    <html><body>
      <div id="fpzj">A股复盘摘要</div>
      <div class="fp_item_hd"><h2>同花顺解盘</h2></div>
      <div class="fp_item_cnt">
        <ul>
          <li><a href="http://stock.10jqka.com.cn/20260605/c677247169.shtml" title="A股收评：机器人走强">A股收评</a></li>
          <li><a href="http://q.10jqka.com.cn/gn/detail/code/309000/">减速器</a></li>
        </ul>
      </div>
    </body></html>
    """
    detail_html = """
    <html><body>
      <h1>A股收评：机器人走强</h1>
      <article>
        <p>今日机器人板块午后持续冲高，减速器方向多只个股涨停。</p>
        <p>尾盘资金仍围绕题材龙头博弈，明日重点观察成交额能否继续放大。</p>
      </article>
    </body></html>
    """
    calls: list[str] = []

    def requester(url: str, **kwargs):
        calls.append(url)
        if url.endswith("c677247169.shtml"):
            return FakeHtmlResponse(detail_html)
        return FakeHtmlResponse(index_html)

    response = MarketBriefingProvider(requester=requester).latest_fupan()

    assert calls == [THS_FUPAN_URL, "http://stock.10jqka.com.cn/20260605/c677247169.shtml"]
    assert [section.title for section in response.sections] == ["同花顺解盘", "全文：A股收评：机器人走强"]
    assert "今日机器人板块午后持续冲高" in (response.sections[1].content or "")
    assert "明日重点观察成交额能否继续放大" in (response.sections[1].content or "")
    assert response.sections[1].links[0].url == "http://stock.10jqka.com.cn/20260605/c677247169.shtml"
    assert response.source_url == "https://stock.10jqka.com.cn/20260605/c677247169.shtml"


def test_market_briefing_provider_uses_article_detail_url_as_original_source():
    index_html = """
    <html><body>
      <div id="fpzj">A股复盘摘要</div>
      <div class="fp_item_hd"><h2>同花顺解盘</h2></div>
      <div class="fp_item_cnt">
        <a href="/20260605/c677247169.shtml" title="A股收评：机器人走强">A股收评</a>
      </div>
    </body></html>
    """
    detail_html = """
    <html><body>
      <h1>A股收评：机器人走强</h1>
      <article><p>今日机器人板块午后持续冲高。</p></article>
    </body></html>
    """

    def requester(url: str, **kwargs):
        if url.endswith("c677247169.shtml"):
            return FakeHtmlResponse(detail_html)
        return FakeHtmlResponse(index_html)

    response = MarketBriefingProvider(requester=requester).latest_fupan()

    assert response.source_url == "https://stock.10jqka.com.cn/20260605/c677247169.shtml"


def test_market_briefing_provider_reports_article_expansion_failures_in_diagnostics():
    index_html = """
    <html><body>
      <div id="fpzj">A股复盘摘要</div>
      <div class="fp_item_hd"><h2>同花顺解盘</h2></div>
      <div class="fp_item_cnt">
        <a href="http://stock.10jqka.com.cn/20260605/c677247169.shtml" title="A股收评：机器人走强">A股收评</a>
      </div>
    </body></html>
    """

    def requester(url: str, **kwargs):
        if url.endswith("c677247169.shtml"):
            raise RuntimeError("anti crawler")
        return FakeHtmlResponse(index_html)

    response = MarketBriefingProvider(requester=requester).latest_fupan()

    assert [section.title for section in response.sections] == ["同花顺解盘"]
    assert response.diagnostics == ["同花顺文章详情抓取失败：A股收评：机器人走强 - anti crawler"]


def test_market_briefing_provider_limits_slow_article_expansion_attempts():
    index_html = """
    <html><body>
      <div id="fpzj">A股复盘摘要</div>
      <div class="fp_item_hd"><h2>同花顺解盘</h2></div>
      <div class="fp_item_cnt">
        <a href="http://stock.10jqka.com.cn/20260605/c677247169.shtml" title="第一篇">第一篇</a>
        <a href="http://stock.10jqka.com.cn/20260605/c677247170.shtml" title="第二篇">第二篇</a>
        <a href="http://stock.10jqka.com.cn/20260605/c677247171.shtml" title="第三篇">第三篇</a>
        <a href="http://stock.10jqka.com.cn/20260605/c677247172.shtml" title="第四篇">第四篇</a>
      </div>
    </body></html>
    """
    calls: list[tuple[str, float]] = []

    def requester(url: str, **kwargs):
        calls.append((url, kwargs["timeout"]))
        if url == THS_FUPAN_URL:
            return FakeHtmlResponse(index_html)
        raise RuntimeError("slow article")

    response = MarketBriefingProvider(timeout=8.0, requester=requester).latest_fupan()

    article_calls = [call for call in calls if call[0] != THS_FUPAN_URL]
    assert len(article_calls) == 1
    assert article_calls[0][1] == 1.5
    assert response.diagnostics == ["同花顺文章详情抓取失败：第一篇 - slow article"]


def test_market_briefing_provider_bounds_empty_page_and_market_fallback_timeouts():
    calls: list[tuple[str, float]] = []

    def requester(url: str, **kwargs):
        calls.append((url, kwargs["timeout"]))
        if url in {THS_FUPAN_URL, THS_REFERER}:
            return FakeHtmlResponse("")
        raise RuntimeError("fallback unavailable")

    response = MarketBriefingProvider(timeout=8.0, requester=requester).latest_fupan()

    assert response.source == "ths-fupan+local-brief"
    assert calls == [
        (THS_FUPAN_URL, 3.0),
        (THS_REFERER, 1.0),
        (THS_FUPAN_URL, 3.0),
        ("https://hq.sinajs.cn/list=sh000001,sz399001,sz399006", 2.0),
        ("https://82.push2.eastmoney.com/api/qt/clist/get", 2.0),
    ]


def test_market_briefing_provider_parses_ths_zaopan_summary_and_tables():
    html = """
    <html><body>
      <div class="yestoday">昨日收盘指数 上证指数：4068.57 -0.734%</div>
      <div class="content-main-fl">
        <p>【昨日国内行情回顾】A股三大指数集体下跌，白酒、煤炭涨幅居前。</p>
        <table>
          <tr><th>公司名称</th><th>事项</th></tr>
          <tr><td>利通电子</td><td>股票交易严重异常波动</td></tr>
        </table>
      </div>
      <div class="content-main-fr">
        <div class="table-part">
          <h2>今日停复牌</h2>
          <table>
            <tr><th>简称</th><th>事项</th></tr>
            <tr><td>*ST天择</td><td>停牌</td></tr>
          </table>
        </div>
      </div>
    </body></html>
    """

    provider = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html))

    response = provider.latest_zaopan()

    assert response.kind == "zaopan"
    assert response.source == "ths-zaopan"
    assert response.summary.startswith("昨日收盘指数")
    assert [section.title for section in response.sections] == ["早盘要点", "今日停复牌"]
    assert response.sections[0].tables[0].rows[0]["公司名称"] == "利通电子"
    assert response.sections[1].tables[0].rows[0]["简称"] == "*ST天择"


def test_market_briefing_provider_uses_zaopan_page_as_source_when_no_article_detail_exists():
    html = """
    <html><body>
      <div class="yestoday">昨日收盘指数 上证指数：4068.57 -0.734%</div>
      <div class="content-main-fl">
        <p>【昨日国内行情回顾】A股三大指数集体下跌，白酒、煤炭涨幅居前。</p>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_zaopan()

    assert response.source == "ths-zaopan"
    assert response.source_url == THS_ZAOPAN_URL


def test_market_briefing_provider_labels_stock_gain_price_tables_without_misusing_theme_columns():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：热门个股活跃。</div>
      <div class="fp_item_hd"><h2>热门个股</h2></div>
      <div class="fp_item_cnt">
        <table>
          <tr><td>软通动力</td><td>20.00%</td><td>58.63</td></tr>
          <tr><td>新易盛</td><td>13.24%</td><td>118.20</td></tr>
        </table>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    table = response.sections[0].tables[0]
    assert table.columns == ["个股", "涨幅", "现价"]
    assert table.rows[0] == {"个股": "软通动力", "涨幅": "20.00%", "现价": "58.63"}
    assert "异动原因" not in table.columns
    assert "影响" not in table.columns


def test_market_briefing_provider_preserves_index_page_paragraphs():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：题材轮动。</div>
      <div class="fp_item_hd"><h2>同花顺解盘</h2></div>
      <div class="fp_item_cnt">
        <p>机器人板块午后持续走强。</p>
        <p>明日重点观察成交额能否继续放大。</p>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    assert response.sections[0].content == "机器人板块午后持续走强。\n\n明日重点观察成交额能否继续放大。"


def test_market_briefing_provider_removes_sidebar_title_from_zaopan_content():
    html = """
    <html><body>
      <div class="yestoday">昨日收盘指数 上证指数：4068.57 -0.734%</div>
      <div class="content-main-fr">
        <div class="table-part">
          <h2>机构观点</h2>
          <p>机构认为低空经济仍需观察成交承接。</p>
        </div>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_zaopan()

    assert response.sections[0].title == "机构观点"
    assert response.sections[0].content == "机构认为低空经济仍需观察成交承接。"
    assert not response.sections[0].content.startswith("机构观点")


def test_market_briefing_provider_removes_leading_rank_from_stock_gain_price_table():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：热门个股活跃。</div>
      <div class="fp_item_hd"><h2>热门个股涨幅榜</h2></div>
      <div class="fp_item_cnt">
        <table>
          <tr><td>1</td><td>软通动力</td><td>20.00%</td><td>58.63</td></tr>
          <tr><td>2</td><td>新易盛</td><td>13.24%</td><td>118.20</td></tr>
        </table>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    table = response.sections[0].tables[0]
    assert table.columns == ["个股", "涨幅", "现价"]
    assert table.rows[0] == {"个股": "软通动力", "涨幅": "20.00%", "现价": "58.63"}
    assert "数值一" not in table.columns


def test_market_briefing_provider_removes_leading_rank_from_three_column_stock_gain_table():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：热门个股活跃。</div>
      <div class="fp_item_hd"><h2>热门个股涨幅榜</h2></div>
      <div class="fp_item_cnt">
        <table>
          <tr><td>1</td><td>软通动力</td><td>20.00%</td></tr>
          <tr><td>2</td><td>新易盛</td><td>13.24%</td></tr>
        </table>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    table = response.sections[0].tables[0]
    assert table.columns == ["个股", "涨幅"]
    assert table.rows[0] == {"个股": "软通动力", "涨幅": "20.00%"}


def test_market_briefing_provider_keeps_inline_strong_body_text():
    html = """
    <html><body>
      <div class="yestoday">昨日收盘指数 上证指数：4068.57 -0.734%</div>
      <div class="content-main-fr">
        <div class="table-part">
          <h2>机构观点</h2>
          <p>资金关注<strong>机器人主线</strong>的成交承接。</p>
        </div>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_zaopan()

    assert "机器人主线" in (response.sections[0].content or "")
    assert response.sections[0].content == "资金关注 机器人主线 的成交承接。"


def test_market_briefing_provider_filters_all_numeric_tables():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：指数小幅反弹。</div>
      <div class="fp_item_hd"><h2>神秘数字</h2></div>
      <div class="fp_item_cnt">
        <table>
          <tr><td>1293.69</td><td>+14.46</td><td>+1.13%</td><td>363.54亿</td></tr>
          <tr><td>2026-06-05 15:00:00</td><td>2026-06-05 15:00:00</td><td>1</td><td>2</td></tr>
        </table>
        <p>机器人板块午后持续走强，资金围绕题材龙头博弈。</p>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    assert response.sections[0].tables == []
    assert response.sections[0].content == "机器人板块午后持续走强，资金围绕题材龙头博弈。"


def test_market_briefing_provider_filters_numeric_soup_and_repeated_timestamps_from_section_content():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：指数小幅反弹。</div>
      <div class="fp_item_hd"><h2>指数表现</h2></div>
      <div class="fp_item_cnt">
        <p>权重 1293.69 +14.46 +1.13% 363.54亿 2026-06-05 15:00:00 2026-06-05 15:00:00 2026-06-05 15:00:00</p>
        <p>2026-06-05 15:00:00 2026-06-05 15:00:00 2026-06-05 15:00:00</p>
        <p>机器人板块午后持续走强，资金围绕题材龙头博弈。</p>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    assert response.sections[0].content == "机器人板块午后持续走强，资金围绕题材龙头博弈。"


def test_market_briefing_provider_filters_cn_label_numeric_soup_and_percent_symbols():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：指数小幅反弹。</div>
      <div class="fp_item_hd"><h2>指数表现</h2></div>
      <div class="fp_item_cnt">
        <p>同比指数盈利 2700 0 股票数（只） 同比指数盈利% 计算方式 1293.69 +14.46 +1.13% 363.54亿</p>
        <p>%</p>
        <p>% %</p>
        <p>机器人板块午后持续走强，资金围绕题材龙头博弈。</p>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    assert response.sections[0].content == "机器人板块午后持续走强，资金围绕题材龙头博弈。"
    assert "同比指数盈利" not in (response.sections[0].content or "")
    assert "% %" not in (response.sections[0].content or "")


def test_market_briefing_provider_drops_ambiguous_profit_section_title_entirely():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：指数小幅反弹。</div>
      <div class="fp_item_hd"><h2>同比指数盈利</h2></div>
      <div class="fp_item_cnt">
        <p>2700 0 股票数（只） 同比指数盈利% 计算方式: (个股收盘价-开盘价)/开盘价*100%-对应指数涨幅; (比率+数量)</p>
      </div>
      <div class="fp_item_hd"><h2>同花顺解盘</h2></div>
      <div class="fp_item_cnt">
        <p>机器人板块午后持续走强，资金围绕题材龙头博弈。</p>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    dumped = str(response.model_dump(mode="json"))
    assert [section.title for section in response.sections] == ["同花顺解盘"]
    assert "同比指数盈利" not in dumped
    assert "计算方式" not in dumped


def test_market_briefing_provider_drops_ambiguous_profit_summary_text():
    html = """
    <html><body>
      <div id="fpzj">同比指数盈利</div>
      <div class="fp_item_hd"><h2>同花顺解盘</h2></div>
      <div class="fp_item_cnt">
        <p>机器人板块午后持续走强，资金围绕题材龙头博弈。</p>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    assert response.summary == "机器人板块午后持续走强，资金围绕题材龙头博弈。"
    assert "同比指数盈利" not in str(response.model_dump(mode="json"))


def test_market_briefing_provider_removes_ths_board_codes_from_key_sector_text():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：权重方向轮动。</div>
      <div class="fp_item_hd"><h2>重点板块</h2></div>
      <div class="fp_item_cnt">
        <p>银行 881155 保险 881156 钢铁 881112 房地产 881153 活跃。</p>
        <table>
          <tr><th>板块</th><th>解读</th></tr>
          <tr><td>银行 881155</td><td>低估值方向修复</td></tr>
          <tr><td>保险 881156</td><td>权重承接改善</td></tr>
        </table>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    dumped = str(response.model_dump(mode="json"))
    assert "881155" not in dumped
    assert "881156" not in dumped
    assert "银行  保险" not in dumped
    assert response.sections[0].content == "银行 保险 钢铁 房地产 活跃。"
    assert response.sections[0].tables[0].rows[0]["板块"] == "银行"


def test_market_briefing_provider_removes_separate_ths_board_code_column():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：权重方向轮动。</div>
      <div class="fp_item_hd"><h2>重点板块</h2></div>
      <div class="fp_item_cnt">
        <table>
          <tr><th>板块</th><th>代码</th><th>解读</th></tr>
          <tr><td>银行</td><td>881155</td><td>低估值方向修复</td></tr>
          <tr><td>保险</td><td>881156</td><td>权重承接改善</td></tr>
        </table>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    table = response.sections[0].tables[0]
    assert table.columns == ["板块", "解读"]
    assert table.rows == [{"板块": "银行", "解读": "低估值方向修复"}, {"板块": "保险", "解读": "权重承接改善"}]
    assert "881155" not in str(response.model_dump(mode="json"))


def test_market_briefing_provider_filters_div_numeric_soup_and_keeps_readable_sentences():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：指数小幅反弹。</div>
      <div class="fp_item_hd"><h2>行业表现</h2></div>
      <div class="fp_item_cnt">
        <div>
          半导体 1293.69 +14.46 +1.13% 363.54亿 2026-06-05 15:00:00
          机器人 1102.18 +22.40 +2.07% 251.11亿 2026-06-05 15:00:00
          % %
        </div>
        <div>机器人板块午后持续走强，资金围绕题材龙头博弈。</div>
        <span>算力方向尾盘承接改善，仍需观察成交额能否延续。</span>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_fupan()

    content = response.sections[0].content or ""
    assert "1293.69" not in content
    assert "2026-06-05" not in content
    assert "% %" not in content
    assert "机器人板块午后持续走强，资金围绕题材龙头博弈。" in content
    assert "算力方向尾盘承接改善，仍需观察成交额能否延续。" in content


def test_market_briefing_provider_uses_market_fallback_when_ths_page_has_no_sections():
    html = "<html><body><div id='fpzj'></div></body></html>"

    def fallback_provider():
        return [
            {
                "title": "公开行情回顾",
                "content": "上证指数小幅回升，机器人与算力方向保持活跃。",
                "links": [{"title": "东方财富行情", "url": "https://quote.eastmoney.com/center/gridlist.html"}],
                "tables": [
                    {
                        "title": "参考指数",
                        "columns": ["名称", "最新值", "涨跌额", "涨跌幅"],
                        "rows": [{"名称": "上证指数", "最新值": "3120.00", "涨跌额": "12.50", "涨跌幅": "0.40%"}],
                    }
                ],
            }
        ]

    response = MarketBriefingProvider(
        requester=lambda *args, **kwargs: FakeHtmlResponse(html),
        fallback_provider=fallback_provider,
    ).latest_fupan()

    assert response.source == "ths-fupan+market-fallback"
    assert response.source_url == "https://quote.eastmoney.com/center/gridlist.html"
    assert response.summary == "上证指数小幅回升，机器人与算力方向保持活跃。"
    assert response.sections[0].title == "公开行情回顾"
    assert response.sections[0].tables[0].columns == ["名称", "最新值", "涨跌额", "涨跌幅"]
    assert any("同花顺复盘页未解析到有效章节" in item for item in response.diagnostics)


def test_market_briefing_provider_uses_market_fallback_when_ths_request_fails():
    def requester(*args, **kwargs):
        raise RuntimeError("ths blocked")

    def fallback_provider():
        return [
            {
                "title": "公开行情回顾",
                "content": "同花顺复盘页暂不可用，公开行情显示指数震荡，强势方向仅作为线索。",
                "links": [{"title": "东方财富行情", "url": "https://quote.eastmoney.com/center/gridlist.html"}],
                "tables": [
                    {
                        "title": "参考指数",
                        "columns": ["名称", "最新值", "涨跌额", "涨跌幅"],
                        "rows": [{"名称": "上证指数", "最新值": "3120.00", "涨跌额": "12.50", "涨跌幅": "0.40%"}],
                    }
                ],
            }
        ]

    response = MarketBriefingProvider(
        requester=requester,
        fallback_provider=fallback_provider,
    ).latest_fupan()

    assert response.source == "ths-fupan+market-fallback"
    assert response.source_url == "https://quote.eastmoney.com/center/gridlist.html"
    assert response.summary == "同花顺复盘页暂不可用，公开行情显示指数震荡，强势方向仅作为线索。"
    assert response.sections[0].title == "公开行情回顾"
    assert response.sections[0].links[0].url == "https://quote.eastmoney.com/center/gridlist.html"
    assert response.sections[0].tables[0].columns == ["名称", "最新值", "涨跌额", "涨跌幅"]
    assert response.diagnostics[0] == "同花顺复盘读取失败：ths blocked"
    assert any("已使用注入的公开行情兜底源生成复盘回顾" in item for item in response.diagnostics)


def test_market_briefing_provider_returns_local_brief_section_when_fupan_and_market_fallback_fail():
    def requester(*args, **kwargs):
        raise RuntimeError("all sources blocked")

    response = MarketBriefingProvider(requester=requester).latest_fupan()

    assert response.source == "ths-fupan+local-brief"
    assert response.source_url is None
    assert response.sections
    assert response.sections[0].title == "本地简短复盘"
    assert "只给防守口径" in (response.sections[0].content or "")
    assert "同花顺复盘读取失败：all sources blocked" in response.diagnostics[0]
    assert any("Sina 指数兜底失败" in item for item in response.diagnostics)
    assert any("东方财富 A 股行情兜底失败" in item for item in response.diagnostics)


def test_market_briefing_provider_does_not_mix_user_mode_candidates_from_legacy_latest_bars_hook():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：机器人和算力活跃。</div>
      <div class="fp_item_hd"><h2>同花顺解盘</h2></div>
      <div class="fp_item_cnt"><p>机器人板块午后持续走强，算力方向有承接。</p></div>
    </body></html>
    """
    provider = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html))
    provider.latest_bars_provider = lambda: [{"symbol": "300001", "name": "机器人A", "close": 10.8}]

    response = provider.latest_fupan()

    assert [section.title for section in response.sections] == ["同花顺解盘"]
    assert response.sections[0].content == "机器人板块午后持续走强，算力方向有承接。"
    assert "当日 user 模式匹配个股" not in str(response.model_dump(mode="json"))


def test_market_briefing_provider_does_not_mix_user_mode_candidates_from_legacy_realtime_hook():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：机器人和算力活跃。</div>
      <div class="fp_item_hd"><h2>同花顺解盘</h2></div>
      <div class="fp_item_cnt"><p>机器人板块午后持续走强，算力方向有承接。</p></div>
    </body></html>
    """

    provider = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html))
    provider.realtime_spot_provider = lambda: [
        {"代码": "300001", "名称": "机器人A", "现价": "10.88", "涨跌额": "0.88", "涨跌幅": "8.80%"},
        {"代码": "600002", "名称": "低量B", "现价": "20.10", "涨跌额": "0.10", "涨跌幅": "0.50%"},
    ]

    response = provider.latest_fupan()

    assert [section.title for section in response.sections] == ["同花顺解盘"]
    assert "当日 user 模式匹配个股" not in str(response.model_dump(mode="json"))


def test_market_briefing_provider_does_not_mix_akshare_realtime_spot_candidates_from_legacy_hook():
    html = """
    <html><body>
      <div id="fpzj">复盘摘要：机器人和算力活跃。</div>
      <div class="fp_item_hd"><h2>同花顺解盘</h2></div>
      <div class="fp_item_cnt"><p>机器人板块午后持续走强，算力方向有承接。</p></div>
    </body></html>
    """

    provider = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html))
    provider.realtime_spot_provider = lambda: [
        {"代码": "300001", "名称": "机器人A", "最新价": 10.88, "涨跌幅": 8.8, "换手率": 4.2, "量比": 1.8, "流通市值": 85.0},
        {"代码": "600002", "名称": "低涨幅B", "最新价": 20.10, "涨跌幅": 0.5, "换手率": 2.0, "量比": 1.4, "流通市值": 120.0},
    ]

    response = provider.latest_fupan()

    assert [section.title for section in response.sections] == ["同花顺解盘"]
    assert "当日 user 模式匹配个股" not in str(response.model_dump(mode="json"))


def test_market_briefing_provider_returns_diagnostics_when_ths_unavailable():
    def requester(*args, **kwargs):
        raise RuntimeError("network closed")

    response = MarketBriefingProvider(requester=requester).latest_fupan()

    assert response.kind == "fupan"
    assert response.source == "ths-fupan+local-brief"
    assert "只给防守口径" in response.summary
    assert response.sections[0].title == "本地简短复盘"
    assert response.diagnostics[0] == "同花顺复盘读取失败：network closed"
    assert any("本地简短防守复盘" in item for item in response.diagnostics)


def test_market_briefing_provider_labels_zaopan_fallback_source_when_ths_unavailable():
    def requester(*args, **kwargs):
        raise RuntimeError("zaopan blocked")

    response = MarketBriefingProvider(requester=requester).latest_zaopan()

    assert response.kind == "zaopan"
    assert response.source == "ths-zaopan+local-brief"
    assert response.source_url is None
    assert response.sections
    assert response.diagnostics[0] == "同花顺早盘读取失败：zaopan blocked"
    assert any("本地简短防守早盘" in item for item in response.diagnostics)


def test_market_briefing_provider_zaopan_empty_parse_falls_back_instead_of_claiming_success():
    """页面 200 但选择器全落空（改版/风控空壳）时不能标 ths-zaopan 输出“已读取”套话。

    否则“同花顺早盘已读取，重点关注……”会被 AI 复盘报告当早盘事实素材。
    """
    html = """
    <html><body>
      <div class="layout"><p>同花顺早盘页改版后的空壳，没有任何已知结构。</p></div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_zaopan()

    assert response.kind == "zaopan"
    assert response.source in ("ths-zaopan+market-fallback", "ths-zaopan+local-brief")
    assert "已读取，重点关注" not in response.summary
    assert any("未解析到有效内容" in item for item in response.diagnostics)


def test_market_briefing_provider_zaopan_summary_noise_line_is_dropped():
    """`.yestoday` 里的时间戳串/数字汤不算真实早盘摘要——被过滤后摘要用正文兜底。"""
    html = """
    <html><body>
      <div class="yestoday">2026-09-18 09:31 2026-09-18 15:00 板块名称 最新涨幅 1293.69</div>
      <div class="content-main-fl">
        <p>【盘前要点】A股三大指数昨夜美股收跌，今日关注量能变化。</p>
      </div>
    </body></html>
    """

    response = MarketBriefingProvider(requester=lambda *args, **kwargs: FakeHtmlResponse(html)).latest_zaopan()

    assert response.source == "ths-zaopan"
    assert response.summary.startswith("【盘前要点】")
    assert "1293.69" not in response.summary


def test_parse_ths_market_degree_ignores_attribute_hits_and_zero_placeholder():
    """§5：标签属性里的数字不是评分；0 分是占位值，与“没解析到”同等对待。"""
    attribute_page = '<html><body><div data-dppj_data="85">大盘评分 6.5</div></body></html>'
    assert _parse_ths_market_degree(attribute_page) is None

    inline_script = '<html><head><script>var dppj_data = 7.2;</script></head><body></body></html>'
    assert _parse_ths_market_degree(inline_script) == 7.2

    assert _parse_ths_market_degree('{"dppj_data": 0}') is None
    assert _parse_ths_market_degree('{"dppj_data": 7.1}') == 7.1


# ---------------------------------------------------------------------------
# test_market_commentary.py
# ---------------------------------------------------------------------------


class FakeRealtimeProvider:
    def market_snapshot(self) -> RealtimeMarketSnapshot:
        return RealtimeMarketSnapshot(
            status="live",
            source="fake-live",
            updated_at=datetime(2026, 6, 5, 14, 50, tzinfo=UTC),
            indexes=[
                MarketIndexQuote(
                    symbol="sh000001",
                    name="上证指数",
                    last=3100.0,
                    previous_close=3080.0,
                    change=20.0,
                    change_pct=0.0065,
                    source="fake-live",
                    updated_at=datetime(2026, 6, 5, 14, 50, tzinfo=UTC),
                )
            ],
            breadth=MarketBreadth(up=3600, down=1300, flat=180, total=5080, source="fake-live"),
            strong_sectors=[
                SectorMover(name="AI应用", change_pct=0.042, leading_symbol="300001", source="ths-concept"),
                SectorMover(name="算力租赁", change_pct=0.031, leading_symbol="300002", source="ths-concept"),
            ],
            yesterday_strong_sectors=[
                SectorMover(name="AI应用", change_pct=0.038, leading_symbol="300001", source="local-yesterday"),
                SectorMover(name="机器人", change_pct=0.026, leading_symbol="300024", source="local-yesterday"),
            ],
            message="ok",
        )


class FakeNewsProvider:
    def latest_news(self, limit: int = 12) -> MarketNewsResponse:
        return MarketNewsResponse(
            updated_at=datetime(2026, 6, 5, 14, 55, tzinfo=UTC),
            source="fake-news",
            items=[
                MarketNewsItem(
                    title="政策利好推动AI应用和算力方向走强",
                    summary="盘中AI应用、算力租赁、半导体方向成交活跃。",
                    source="示例财经",
                    published_at=datetime(2026, 6, 5, 14, 30, tzinfo=UTC),
                    tags=["AI", "算力", "政策"],
                    sentiment="positive",
                )
            ],
        )


class FakeFupanProvider:
    def latest_fupan(self) -> MarketBriefingResponse:
        return MarketBriefingResponse(
            kind="fupan",
            updated_at=datetime(2026, 6, 5, 15, 35, tzinfo=UTC),
            source="ths-fupan",
            source_url="https://stock.10jqka.com.cn/fupan/",
            summary="收盘后复盘：机器人、算力和AI应用方向活跃，指数震荡但题材承接尚可。",
            sections=[
                MarketBriefingSection(
                    title="同花顺解盘",
                    content="机器人板块午后继续走强，多只个股涨停。算力方向保持资金关注，明日观察成交额能否继续放大。",
                    links=[],
                    tables=[],
                )
            ],
            diagnostics=[],
        )


def test_market_commentary_builds_specific_same_day_view_from_live_snapshot_and_news():
    response = MarketCommentaryProvider(FakeRealtimeProvider(), FakeNewsProvider()).current_commentary()

    assert response.mode == "intraday"
    assert response.stance == "positive"
    assert response.trade_date.isoformat() == "2026-06-05"
    assert "上证指数+0.65%" in response.summary
    assert "AI应用" in response.summary
    assert "3600" in response.summary
    assert response.drivers[0].title == "强势题材"
    assert "算力租赁" in response.drivers[0].detail
    assert any(point.title == "市场宽度" and "上涨占比 70.9%" in point.detail for point in response.drivers)
    assert any(point.title == "新闻催化" and "候选" in point.detail for point in response.drivers)
    assert any("AI应用" in item for item in response.next_watch)
    assert response.diagnostics == ["实时盘面数据完整：已使用指数、红绿家数、强势题材和昨日强势追踪生成评价。"]


def test_market_commentary_accepts_retained_post_close_snapshot_as_review():
    class RetainedPostCloseProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            snapshot = FakeRealtimeProvider().market_snapshot()
            snapshot.status = "stale"
            snapshot.market_phase = "post_close"
            snapshot.source = "ashare-sina+retained-last-success"
            snapshot.message = "收盘后使用最近成功行情快照。"
            snapshot.diagnostics = ["收盘后降低刷新频率，沿用 2026-06-05 14:50 的成功快照。"]
            return snapshot

    response = MarketCommentaryProvider(RetainedPostCloseProvider(), FakeNewsProvider()).current_commentary()

    assert response.mode == "post_close"
    assert response.source == "ashare-sina+retained-last-success+commentary"
    assert "收盘后复盘" in response.summary
    assert "AI应用" in response.summary
    assert any("收盘后降低刷新频率" in item for item in response.diagnostics)
    assert not response.summary.startswith("实时盘面暂不可用")


def test_market_commentary_labels_lunch_break_snapshot_as_review():
    class LunchBreakProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            snapshot = FakeRealtimeProvider().market_snapshot()
            snapshot.status = "stale"
            snapshot.market_phase = "lunch_break"
            snapshot.source = "ashare-sina+retained-last-success"
            snapshot.message = "午间休市使用最近成功行情快照。"
            snapshot.diagnostics = ["午间休市，使用最近成功行情快照生成回顾。"]
            return snapshot

    response = MarketCommentaryProvider(LunchBreakProvider(), FakeNewsProvider()).current_commentary()

    assert response.mode == "lunch_break_review"
    assert "午间盘面回顾" in response.summary
    assert any("午间休市" in item for item in response.diagnostics)


def test_market_commentary_accepts_weekend_snapshot_as_recent_trading_day_review():
    class WeekendRetainedProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            snapshot = FakeRealtimeProvider().market_snapshot()
            snapshot.status = "stale"
            snapshot.market_phase = "non_trading"
            snapshot.source = "local-latest+retained-last-success"
            snapshot.message = "非交易日使用最近交易日快照。"
            snapshot.diagnostics = ["周末非交易日，使用最近交易日快照生成回顾。"]
            return snapshot

    response = MarketCommentaryProvider(WeekendRetainedProvider(), FakeNewsProvider()).current_commentary()

    assert response.mode == "non_trading_review"
    assert "非交易日最近交易日回顾" in response.summary
    assert response.trade_date.isoformat() == "2026-06-05"
    assert any("周末非交易日" in item for item in response.diagnostics)
    assert response.drivers[0].title == "强势题材"


def test_market_commentary_explains_unavailable_realtime_source_without_crashing():
    class BrokenRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            raise RuntimeError("network closed")

    response = MarketCommentaryProvider(BrokenRealtimeProvider(), FakeNewsProvider()).current_commentary()

    assert response.mode == "local_brief_review"
    assert response.stance == "defensive"
    assert response.drivers
    assert response.drivers[0].title == "后端防守判断"
    assert "后端简短判断" in response.summary
    assert response.risks
    assert any("不能把新闻或局部数据包装成确定结论" in item for item in response.risks)
    assert any("完整红绿家数" in item for item in response.next_watch)
    assert response.diagnostics == [
        "行情评价读取实时快照失败：network closed",
        "后端已生成简短防守判断，避免前端 fallback 或局部数据被包装成确定行情结论。",
    ]


def test_market_commentary_uses_news_tags_when_realtime_snapshot_fails():
    class BrokenRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            raise TimeoutError("snapshot timeout")

    response = MarketCommentaryProvider(BrokenRealtimeProvider(), FakeNewsProvider()).current_commentary()

    assert response.mode == "local_brief_review"
    assert response.source == "local-brief-commentary"
    assert response.stance == "defensive"
    assert "AI" in response.summary
    assert "算力" in response.summary
    assert len(response.drivers) == 1
    assert response.drivers[0].title == "后端防守判断"
    assert "AI" in response.drivers[0].detail
    assert any("不能把新闻或局部数据包装成确定结论" in item for item in response.risks)
    assert any("完整红绿家数" in item for item in response.next_watch)
    assert response.diagnostics == [
        "行情评价读取实时快照失败：snapshot timeout",
        "后端已生成简短防守判断，避免前端 fallback 或局部数据被包装成确定行情结论。",
    ]


def test_market_commentary_uses_news_tags_when_realtime_snapshot_is_slow():
    class SlowRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            time.sleep(0.05)
            return FakeRealtimeProvider().market_snapshot()

    response = MarketCommentaryProvider(
        SlowRealtimeProvider(),
        FakeNewsProvider(),
        snapshot_timeout=0.001,
    ).current_commentary()

    assert response.source == "local-brief-commentary"
    assert "AI" in response.summary
    assert response.drivers[0].title == "后端防守判断"
    assert response.diagnostics == [
        "行情评价读取实时快照超时：0.001秒",
        "后端已生成简短防守判断，避免前端 fallback 或局部数据被包装成确定行情结论。",
    ]


def test_market_commentary_default_snapshot_timeout_allows_longer_realtime_reads():
    provider = MarketCommentaryProvider(FakeRealtimeProvider(), FakeNewsProvider())

    assert provider.snapshot_timeout == 30.0


def test_market_commentary_uses_fupan_when_realtime_snapshot_is_slow():
    class SlowRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            time.sleep(0.05)
            return FakeRealtimeProvider().market_snapshot()

    response = MarketCommentaryProvider(
        SlowRealtimeProvider(),
        FakeNewsProvider(),
        briefing_provider=FakeFupanProvider(),
        snapshot_timeout=0.001,
    ).current_commentary()

    assert response.mode == "post_close"
    assert response.source == "ths-fupan+briefing-commentary"
    assert response.stance == "neutral"
    assert "收盘后复盘" in response.summary
    assert "机器人" in response.summary
    assert "算力" in response.drivers[0].detail
    assert response.drivers[0].title == "同花顺复盘"
    assert any("实时盘面读取失败，已用同花顺复盘" in item for item in response.diagnostics)
    assert not response.summary.startswith("实时盘面暂不可用")


def test_market_commentary_ignores_noisy_briefing_fallback_text():
    class BrokenRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            raise TimeoutError("snapshot timeout")

    class NoisyFupanProvider:
        def latest_fupan(self) -> MarketBriefingResponse:
            return MarketBriefingResponse(
                kind="fupan",
                updated_at=datetime(2026, 6, 5, 15, 35, tzinfo=UTC),
                source="ths-fupan",
                source_url="https://stock.10jqka.com.cn/fupan/",
                summary="同比指数盈利",
                sections=[
                    MarketBriefingSection(
                        title="指数表现",
                        content="板块名称 最新涨幅 涨跌幅% 股票数（只） 1293.69 +14.46 +1.13% 363.54亿 2026-06-05 15:00:00",
                        links=[],
                        tables=[],
                    )
                ],
                diagnostics=[],
            )

    response = MarketCommentaryProvider(
        BrokenRealtimeProvider(),
        FakeNewsProvider(),
        briefing_provider=NoisyFupanProvider(),
        snapshot_timeout=None,
    ).current_commentary()

    dumped = str(response.model_dump(mode="json"))
    assert response.source == "local-brief-commentary"
    assert "同比指数盈利" not in dumped
    assert "1293.69" not in dumped
    assert "363.54亿" not in dumped


def test_market_commentary_labels_market_fallback_briefing_as_public_market_fallback():
    class BrokenRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            raise TimeoutError("snapshot timeout")

    class MarketFallbackFupanProvider:
        def latest_fupan(self) -> MarketBriefingResponse:
            return MarketBriefingResponse(
                kind="fupan",
                updated_at=datetime(2026, 6, 5, 15, 35, tzinfo=UTC),
                source="ths-fupan+market-fallback",
                source_url="https://stock.10jqka.com.cn/fupan/",
                summary="公开行情回顾：指数震荡，半导体方向保持活跃。",
                sections=[MarketBriefingSection(title="公开行情回顾", content="半导体涨幅靠前，只作为复盘线索。")],
                diagnostics=["同花顺复盘读取失败：network closed"],
            )

    response = MarketCommentaryProvider(
        BrokenRealtimeProvider(),
        FakeNewsProvider(),
        briefing_provider=MarketFallbackFupanProvider(),
        snapshot_timeout=None,
    ).current_commentary()

    assert response.source == "ths-fupan+market-fallback+briefing-commentary"
    assert response.drivers[0].title == "公开行情复盘兜底"
    assert "公开行情复盘兜底" in response.summary
    assert any("公开行情复盘兜底" in item for item in response.risks)
    assert not any("同花顺复盘公开页面" in item for item in response.risks)


def test_market_commentary_labels_local_briefing_as_local_brief_review():
    class BrokenRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            raise TimeoutError("snapshot timeout")

    class LocalBriefFupanProvider:
        def latest_fupan(self) -> MarketBriefingResponse:
            return MarketBriefingResponse(
                kind="fupan",
                updated_at=datetime(2026, 6, 5, 15, 35, tzinfo=UTC),
                source="ths-fupan+local-brief",
                source_url="https://stock.10jqka.com.cn/fupan/",
                summary="本地简短复盘：当前只给防守口径。",
                sections=[MarketBriefingSection(title="本地简短复盘", content="不包装成确定行情结论。")],
                diagnostics=["同花顺复盘和公开行情兜底均不可用"],
            )

    response = MarketCommentaryProvider(
        BrokenRealtimeProvider(),
        FakeNewsProvider(),
        briefing_provider=LocalBriefFupanProvider(),
        snapshot_timeout=None,
    ).current_commentary()

    assert response.source == "ths-fupan+local-brief+briefing-commentary"
    assert response.drivers[0].title == "本地简短复盘"
    assert "本地简短复盘" in response.summary
    assert any("本地简短复盘" in item for item in response.risks)
    assert not any("同花顺复盘公开页面" in item for item in response.risks)


def test_market_commentary_builds_backend_brief_review_when_realtime_and_fupan_are_unavailable():
    class EmptyLiveRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            return RealtimeMarketSnapshot(
                status="unavailable",
                source="fake-live-empty",
                updated_at=datetime(2026, 6, 5, 14, 50, tzinfo=UTC),
                indexes=[],
                breadth=None,
                strong_sectors=[],
                yesterday_strong_sectors=[],
                message="实时源返回空快照",
            )

    class EmptyFupanProvider:
        def latest_fupan(self) -> MarketBriefingResponse:
            return MarketBriefingResponse(
                kind="fupan",
                updated_at=datetime(2026, 6, 5, 15, 35, tzinfo=UTC),
                source="fallback",
                source_url=None,
                summary="",
                sections=[],
                diagnostics=["同花顺复盘页面没有可解析正文"],
            )

    response = MarketCommentaryProvider(
        EmptyLiveRealtimeProvider(),
        FakeNewsProvider(),
        briefing_provider=EmptyFupanProvider(),
    ).current_commentary()

    assert response.source == "local-brief-commentary"
    assert response.mode == "local_brief_review"
    assert response.stance == "defensive"
    assert "后端简短判断" in response.summary
    assert "实时盘面和同花顺复盘暂不可用" in response.summary
    assert response.drivers[0].title == "后端防守判断"
    assert any("不能把新闻或局部数据包装成确定结论" in item for item in response.risks)
    assert any("后端已生成简短防守判断" in item for item in response.diagnostics)


def test_market_commentary_reports_diagnostic_when_realtime_source_returns_unavailable():
    class UnavailableRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            return RealtimeMarketSnapshot(
                status="unavailable",
                source="fake-live",
                updated_at=datetime(2026, 6, 5, 14, 50, tzinfo=UTC),
                indexes=[],
                breadth=None,
                strong_sectors=[],
                yesterday_strong_sectors=[],
                message="同花顺和东方财富均无可用实时数据",
            )

    response = MarketCommentaryProvider(UnavailableRealtimeProvider(), FakeNewsProvider()).current_commentary()

    assert response.source == "local-brief-commentary"
    assert response.stance == "defensive"
    assert "后端简短判断" in response.summary
    assert "AI" in response.summary
    assert response.drivers[0].title == "后端防守判断"
    assert response.diagnostics == [
        "行情评价实时快照不可用：同花顺和东方财富均无可用实时数据",
        "实时盘面不完整：快照状态为 unavailable、缺少指数、缺少红绿家数、缺少强势题材，未生成确定盘面评价。",
        "后端已生成简短防守判断，避免前端 fallback 或局部数据被包装成确定行情结论。",
    ]


def test_market_commentary_does_not_build_definite_view_from_unavailable_snapshot_with_local_data():
    class UnavailableWithLocalDataProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            return RealtimeMarketSnapshot(
                status="unavailable",
                source="local-latest",
                updated_at=datetime(2026, 6, 5, 14, 50, tzinfo=UTC),
                indexes=[
                    MarketIndexQuote(
                        symbol="local-market",
                        name="本地全市场",
                        last=10.3,
                        previous_close=10.0,
                        change=0.3,
                        change_pct=0.03,
                        source="local-latest",
                        updated_at=datetime(2026, 6, 5, 14, 50, tzinfo=UTC),
                    )
                ],
                breadth=MarketBreadth(up=4, down=1, flat=0, total=5, source="local-latest"),
                strong_sectors=[
                    SectorMover(name="AI应用", change_pct=0.052, leading_symbol="300001", source="local-market-group"),
                    SectorMover(name="机器人", change_pct=0.031, leading_symbol="300024", source="local-market-group"),
                ],
                yesterday_strong_sectors=[
                    SectorMover(name="AI应用", change_pct=0.038, leading_symbol="300001", source="local-yesterday"),
                ],
                message="实时行情不可用，已使用本地最近交易日数据。",
            )

    response = MarketCommentaryProvider(UnavailableWithLocalDataProvider(), FakeNewsProvider()).current_commentary()

    assert response.source == "local-brief-commentary"
    assert response.trade_date.isoformat() == "2026-06-05"
    assert response.stance == "defensive"
    assert "后端简短判断" in response.summary
    assert "AI应用" not in response.summary
    assert "红盘 4" not in response.summary
    assert response.drivers[0].title == "后端防守判断"
    assert not any(point.title == "强势题材" for point in response.drivers)
    assert any("不能把新闻或局部数据包装成确定结论" in item for item in response.risks)
    assert any("完整红绿家数" in item for item in response.next_watch)
    assert response.diagnostics[0] == "行情评价实时快照不可用：实时行情不可用，已使用本地最近交易日数据。"
    assert "快照状态为 unavailable" in response.diagnostics[1]
    assert "红绿家数不完整" in response.diagnostics[1]


def test_market_commentary_does_not_build_definite_view_from_empty_live_snapshot():
    class EmptyLiveRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            return RealtimeMarketSnapshot(
                status="live",
                source="fake-live-empty",
                updated_at=datetime(2026, 6, 5, 14, 50, tzinfo=UTC),
                indexes=[],
                breadth=None,
                strong_sectors=[],
                yesterday_strong_sectors=[],
                message="实时源返回空快照",
            )

    response = MarketCommentaryProvider(EmptyLiveRealtimeProvider(), FakeNewsProvider()).current_commentary()

    assert response.source == "local-brief-commentary"
    assert response.stance == "defensive"
    assert "后端简短判断" in response.summary
    assert "今日强势题材" not in response.summary
    assert response.drivers[0].title == "后端防守判断"
    assert any("不能把新闻或局部数据包装成确定结论" in item for item in response.risks)
    assert response.diagnostics == [
        "实时盘面不完整：缺少指数、缺少红绿家数、缺少强势题材，未生成确定盘面评价。",
        "后端已生成简短防守判断，避免前端 fallback 或局部数据被包装成确定行情结论。",
    ]


def test_market_commentary_rejects_live_snapshot_with_partial_breadth_total():
    class PartialBreadthRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            return RealtimeMarketSnapshot(
                status="live",
                source="ashare-sina+sina-a-share-live+ths-concept",
                updated_at=datetime(2026, 6, 5, 14, 50, tzinfo=UTC),
                indexes=[
                    MarketIndexQuote(
                        symbol="sh000001",
                        name="上证指数",
                        last=3100.0,
                        previous_close=3080.0,
                        change=20.0,
                        change_pct=0.0065,
                        source="ashare-sina",
                    )
                ],
                breadth=MarketBreadth(up=107, down=80, flat=5, total=192, source="sina-a-share-live"),
                strong_sectors=[
                    SectorMover(name="AI应用", change_pct=0.042, leading_symbol="300001", source="ths-concept")
                ],
                yesterday_strong_sectors=[],
                message="局部实时宽度",
                diagnostics=["红绿家数来源 sina-a-share-live 不完整：total=192，低于全市场阈值。"],
            )

    response = MarketCommentaryProvider(PartialBreadthRealtimeProvider(), FakeNewsProvider()).current_commentary()

    assert response.source == "local-brief-commentary"
    assert response.mode == "local_brief_review"
    assert "后端简短判断" in response.summary
    assert not any("实时盘面数据完整" in item for item in response.diagnostics)
    assert any("红绿家数不完整" in item for item in response.diagnostics)


def test_market_commentary_rejects_live_snapshot_with_local_fallback_sectors():
    class LocalSectorRealtimeProvider:
        def market_snapshot(self) -> RealtimeMarketSnapshot:
            return RealtimeMarketSnapshot(
                status="live",
                source="ashare-sina+sina-a-share-live+local-market-group",
                updated_at=datetime(2026, 6, 5, 14, 50, tzinfo=UTC),
                indexes=[
                    MarketIndexQuote(
                        symbol="sh000001",
                        name="上证指数",
                        last=3100.0,
                        previous_close=3080.0,
                        change=20.0,
                        change_pct=0.0065,
                        source="ashare-sina",
                    )
                ],
                breadth=MarketBreadth(up=3300, down=1500, flat=300, total=5100, source="sina-a-share-live"),
                strong_sectors=[
                    SectorMover(name="AI应用", change_pct=0.042, leading_symbol="300001", source="local-market-group")
                ],
                yesterday_strong_sectors=[],
                message="live breadth with local sectors",
                diagnostics=["实时强势题材接口暂不可用，已回退到本地最近交易日题材聚合。"],
            )

    response = MarketCommentaryProvider(LocalSectorRealtimeProvider(), FakeNewsProvider()).current_commentary()

    assert response.source == "local-brief-commentary"
    assert response.mode == "local_brief_review"
    assert not any("实时盘面数据完整" in item for item in response.diagnostics)
    assert any("local fallback" in item for item in response.diagnostics)
