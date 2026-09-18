from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

from astock_backtester.data.http_transport import BROWSER_USER_AGENT, create_scraping_session
from astock_backtester.data.parsing import parse_float
from astock_backtester.data.symbols import normalize_symbol
from astock_backtester.data.text_cleaning import (
    CN_FIELD_KEYWORDS as _CN_FIELD_KEYWORDS,
)
from astock_backtester.data.text_cleaning import (
    collapse_ws,
)
from astock_backtester.data.text_cleaning import (
    is_noisy_market_line as _is_noisy_content_line,
)
from astock_backtester.data.text_cleaning import (
    is_percent_text as _is_percent_text,
)
from astock_backtester.models import (
    MarketBriefingLink,
    MarketBriefingResponse,
    MarketBriefingSection,
    MarketBriefingTable,
)

THS_FUPAN_URL = "https://stock.10jqka.com.cn/fupan/"
THS_ZAOPAN_URL = "https://stock.10jqka.com.cn/zaopan/"
THS_REFERER = "https://stock.10jqka.com.cn/"
THS_PAGE_TIMEOUT = 3.0
THS_PREWARM_TIMEOUT = 1.0
THS_ARTICLE_DETAIL_TIMEOUT = 1.5
THS_ARTICLE_DETAIL_LIMIT = 1
MARKET_FALLBACK_TIMEOUT = 2.0
EASTMONEY_A_SPOT_URL = "https://82.push2.eastmoney.com/api/qt/clist/get"
SINA_QUOTE_URL = "https://hq.sinajs.cn/list={symbols}"
THS_USER_AGENT = BROWSER_USER_AGENT
INDEX_SYMBOLS = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
]


def _clean_text(value: str | None, max_length: int | None = None) -> str:
    text = collapse_ws(unescape(value or "")).strip()
    if max_length is not None and len(text) > max_length:
        return f"{text[:max_length].rstrip()}..."
    return text


_THS_BOARD_CODE_PATTERN = re.compile(r"(?<!\d)88\d{4}(?!\d)")


def _clean_display_text(value: str | None, max_length: int | None = None) -> str:
    text = _clean_text(value, max_length=max_length)
    if not text:
        return ""
    return _clean_text(_THS_BOARD_CODE_PATTERN.sub("", text))


def _node_text(node: Tag | None, max_length: int | None = None) -> str:
    return _clean_text(node.get_text(" ", strip=True) if node else "", max_length=max_length)


def _is_number_text(value: str) -> bool:
    return bool(re.search(r"[+-]?\d+(?:\.\d+)?", value))


def _is_rank_text(value: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}", value.strip()))


def _is_headerless_market_number_row(row: list[str]) -> bool:
    if len(row) < 4:
        return False
    first = row[0].strip()
    if not first or _is_number_text(first):
        return False
    numeric_cells = [cell for cell in row[1:] if _is_number_text(cell) or _is_percent_text(cell)]
    has_market_shape = any(_is_percent_text(cell) for cell in row[1:]) and len(numeric_cells) >= 3
    return has_market_shape and not any(keyword in first for keyword in _CN_FIELD_KEYWORDS)


def _is_disallowed_section_title(title: str | None) -> bool:
    compact = re.sub(r"\s+", "", title or "")
    return "同比指数盈利" in compact


def _is_ths_board_code_cell(value: str | None) -> bool:
    return bool(re.fullmatch(r"88\d{4}", (value or "").strip()))


def _drop_ths_board_code_columns(rows: list[list[str]]) -> list[list[str]]:
    if len(rows) < 2:
        return rows
    width = max(len(row) for row in rows)
    drop_indexes: set[int] = set()
    for index in range(width):
        cells = [row[index].strip() for row in rows[1:] if index < len(row) and row[index].strip()]
        if cells and all(_is_ths_board_code_cell(cell) for cell in cells):
            drop_indexes.add(index)
    if not drop_indexes:
        return rows
    return [[cell for index, cell in enumerate(row) if index not in drop_indexes] for row in rows]


def _is_stock_gain_price_row(row: list[str], title: str | None) -> bool:
    title_text = title or ""
    return (
        _is_stock_like_title(title_text)
        and (
            (len(row) >= 3 and _is_percent_text(row[1]) and _is_number_text(row[2]))
            or (len(row) == 2 and _is_percent_text(row[1]))
        )
    )


def _is_stock_like_title(title: str | None) -> bool:
    return any(keyword in (title or "") for keyword in ("个股", "股票", "热门个股", "异动个股"))


def _neutral_columns(width: int) -> list[str]:
    labels = ["名称", "数值一", "数值二", "数值三", "数值四", "数值五", "数值六", "数值七"]
    return [labels[index] if index < len(labels) else f"数值{index + 1}" for index in range(width)]


def _infer_table_columns(rows: list[list[str]], title: str | None) -> list[str]:
    width = max(len(row) for row in rows)
    first_data_row = next((row for row in rows if row), [])
    title_text = title or ""
    second_is_percent = len(first_data_row) > 1 and _is_percent_text(first_data_row[1])
    third_is_number = len(first_data_row) > 2 and _is_number_text(first_data_row[2])
    second_is_number = len(first_data_row) > 1 and _is_number_text(first_data_row[1])
    fourth_is_percent = len(first_data_row) > 3 and _is_percent_text(first_data_row[3])
    stock_like_title = _is_stock_like_title(title_text)
    rank_like_title = any(keyword in title_text for keyword in ("涨幅榜", "跌幅榜", "排行榜", "榜单"))

    if width >= 3 and second_is_percent and third_is_number:
        base = ["个股", "涨幅", "现价"] if stock_like_title else ["名称", "涨跌幅", "最新价"]
        return base + _neutral_columns(width)[len(base) :]
    if width >= 4 and second_is_number and third_is_number and fourth_is_percent:
        base = ["名称", "最新值", "涨跌额", "涨跌幅"]
        return base + _neutral_columns(width)[len(base) :]
    if width == 2 and second_is_percent:
        return ["个股", "涨幅"] if stock_like_title else ["名称", "涨跌幅"]
    if rank_like_title and width >= 2:
        base = ["名称", "涨跌幅", "最新价"]
        return base[:width] + _neutral_columns(width)[len(base[:width]) :]
    return _neutral_columns(width)


def _readable_content_from_node(node: Tag | None) -> str:
    if node is None:
        return ""
    blocks = [_node_text(block) for block in node.select("h1,h2,h3,p,li")]
    if not blocks:
        leaf_selectors = "div,section,article,span"
        leaf_nodes = [
            block
            for block in node.select(leaf_selectors)
            if not block.select_one(f"h1,h2,h3,p,li,{leaf_selectors}")
        ]
        blocks = [_node_text(block) for block in leaf_nodes]
    if not blocks:
        blocks = [_node_text(node)]
    filtered = [block for block in blocks if block and not _is_noisy_content_line(block)]
    return "\n\n".join(_clean_display_text(block) for block in filtered)


def _ths_headers(referer: str = THS_REFERER) -> dict[str, str]:
    return {
        "User-Agent": THS_USER_AGENT,
        "Referer": referer,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def _sina_headers() -> dict[str, str]:
    return {
        "User-Agent": THS_USER_AGENT,
        "Referer": "https://finance.sina.com.cn/",
    }


def _eastmoney_headers() -> dict[str, str]:
    return {
        "User-Agent": THS_USER_AGENT,
        "Referer": "https://quote.eastmoney.com/",
        "Origin": "https://quote.eastmoney.com",
    }


def _section_title(node: Tag, fallback: str) -> str:
    title_node = node.select_one("h1, h2, h3, strong, .title, .hd")
    title = _node_text(title_node)
    return title or fallback


def _table_from_node(table: Tag, title: str | None = None) -> MarketBriefingTable | None:
    raw_rows: list[list[str]] = []
    for tr in table.select("tr"):
        cells = [_node_text(cell, max_length=160) for cell in tr.select("th,td")]
        cells = [cell for cell in cells if cell]
        if cells:
            raw_rows.append(cells)
    rows = [
        [_clean_display_text(cell) for cell in row]
        for row in _drop_ths_board_code_columns(raw_rows)
    ]
    if not rows:
        return None

    first_row_has_header = bool(table.select_one("th"))
    if first_row_has_header:
        columns = rows[0]
        data_rows = rows[1:]
    else:
        data_rows = rows
        if not _is_stock_like_title(title) and all(_is_headerless_market_number_row(row) for row in data_rows):
            return None
        if not _is_stock_like_title(title) and all(_is_noisy_content_line(" ".join(row)) for row in data_rows):
            return None
        if data_rows and len(data_rows[0]) >= 3 and _is_rank_text(data_rows[0][0]):
            stripped_rows = [row[1:] if len(row) >= 3 and _is_rank_text(row[0]) else row for row in data_rows]
            if stripped_rows and _is_stock_gain_price_row(stripped_rows[0], title):
                data_rows = stripped_rows
        columns = _infer_table_columns(data_rows, title)

    normalized_rows: list[dict[str, str]] = []
    for row in data_rows[:8]:
        normalized_rows.append(
            {
                columns[index] if index < len(columns) else f"字段{index + 1}": value
                for index, value in enumerate(row)
            }
        )
    if not normalized_rows:
        return None
    return MarketBriefingTable(title=title, columns=columns, rows=normalized_rows)


def _links_from_node(node: Tag, base_url: str, limit: int = 8) -> list[MarketBriefingLink]:
    links: list[MarketBriefingLink] = []
    seen: set[str] = set()
    for anchor in node.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        title = _clean_display_text(str(anchor.get("title") or "") or anchor.get_text(" ", strip=True), max_length=120)
        if not href or not title:
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        links.append(MarketBriefingLink(title=title, url=url))
        seen.add(url)
        if len(links) >= limit:
            break
    return links


def _remove_non_textual_nodes(node: Tag) -> Tag:
    clone = BeautifulSoup(str(node), "html.parser")
    root = clone.find()
    if not isinstance(root, Tag):
        return node
    for child in root.select("script,style,noscript,iframe,textarea,form,button,table,a,img,svg,canvas"):
        child.decompose()
    return root


def _remove_non_body_nodes(node: Tag) -> Tag:
    root = _remove_non_textual_nodes(node)
    for child in root.select("h1,h2,h3,.title,.hd"):
        child.decompose()
    for child in root.select("strong"):
        if child.parent and child.parent.name in {"div", "section", "article"}:
            child.decompose()
        else:
            child.unwrap()
    return root


def _is_ths_article_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.hostname == "stock.10jqka.com.cn" and parsed.path.endswith(".shtml")


def _article_title(soup: BeautifulSoup, fallback: str) -> str:
    title = _node_text(soup.select_one("h1"))
    if title:
        return title
    meta_title = soup.select_one('meta[property="og:title"], meta[name="title"]')
    if meta_title:
        title = _clean_text(str(meta_title.get("content") or ""))
        if title:
            return title
    page_title = _node_text(soup.title)
    return re.sub(r"[_-].*$", "", page_title).strip() or fallback


def _article_body(soup: BeautifulSoup) -> str | None:
    selectors = (
        "article",
        ".main-text",
        ".art_context",
        ".artText",
        ".detail-content",
        ".news-content",
        ".article-content",
        ".post-content",
    )
    container = next((node for selector in selectors for node in soup.select(selector) if isinstance(node, Tag)), None)
    if container is None:
        paragraphs = [_node_text(node) for node in soup.select("p")]
    else:
        for child in container.select("script,style,a,img,svg,canvas,iframe"):
            child.decompose()
        paragraphs = [_node_text(node) for node in container.select("p,li")]
        if not paragraphs:
            paragraphs = [_node_text(container)]
    paragraphs = [paragraph for paragraph in paragraphs if len(paragraph) >= 8 and not _is_noisy_content_line(paragraph)]
    if not paragraphs:
        return None
    return "\n\n".join(_clean_display_text(paragraph) for paragraph in paragraphs)


def _safe_float(value: Any) -> float | None:
    parsed = parse_float(value)
    if parsed is None or parsed != parsed:  # NaN string placeholders count as missing
        return None
    return parsed


def _coerce_float(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _format_decimal(value: float | None, digits: int = 2) -> str:
    number = _coerce_float(value)
    if number is None:
        return "--"
    return f"{number:.{digits}f}"


def _format_pct(value: float | None) -> str:
    pct = _coerce_float(value)
    if pct is None:
        return "--"
    if abs(pct) <= 1:
        pct *= 100
    return f"{pct:.2f}%"


def _format_percent_points(value: float | None) -> str:
    number = _coerce_float(value)
    if number is None:
        return "--"
    return f"{number:.2f}%"


def _decode_sina_quotes(text: str) -> dict[str, list[str]]:
    quotes: dict[str, list[str]] = {}
    for segment in text.split(";"):
        if "hq_str_" not in segment or "=" not in segment:
            continue
        key = segment.split("hq_str_", 1)[1].split("=", 1)[0].strip()
        raw = segment.split("=", 1)[1].strip().strip('"')
        if raw:
            quotes[key] = raw.split(",")
    return quotes


def _section_from_mapping(item: dict[str, Any]) -> MarketBriefingSection | None:
    title = _clean_display_text(str(item.get("title") or "公开行情回顾"))
    if _is_disallowed_section_title(title):
        return None
    content = _clean_display_text(str(item.get("content") or ""))
    raw_links = item.get("links") if isinstance(item.get("links"), list) else []
    links = [
        MarketBriefingLink(title=_clean_display_text(str(link.get("title") or "")), url=str(link.get("url") or "") or None)
        for link in raw_links
        if isinstance(link, dict) and _clean_display_text(str(link.get("title") or ""))
    ]
    raw_tables = item.get("tables") if isinstance(item.get("tables"), list) else []
    tables: list[MarketBriefingTable] = []
    for table in raw_tables:
        if not isinstance(table, dict):
            continue
        rows = table.get("rows") if isinstance(table.get("rows"), list) else []
        normalized_rows = [row for row in rows if isinstance(row, dict)]
        if not normalized_rows:
            continue
        columns = table.get("columns") if isinstance(table.get("columns"), list) else []
        tables.append(
            MarketBriefingTable(
                title=_clean_display_text(str(table.get("title") or "")) or None,
                columns=[_clean_display_text(str(column)) for column in columns if _clean_display_text(str(column))],
                rows=[
                    {
                        _clean_display_text(str(key)): _clean_display_text(str(value))
                        for key, value in row.items()
                    }
                    for row in normalized_rows
                ],
            )
        )
    if not content and not tables and not links:
        return None
    return MarketBriefingSection(title=title, content=content or None, links=links, tables=tables)


def _first_section_link_url(sections: list[MarketBriefingSection]) -> str | None:
    for section in sections:
        for link in section.links:
            url = (link.url or "").strip()
            if url:
                return url
    return None


def _ths_article_source_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed._replace(scheme="https").geturl()


def _first_ths_article_link_url(sections: list[MarketBriefingSection]) -> str | None:
    for section in sections:
        for link in section.links:
            url = (link.url or "").strip()
            if _is_ths_article_url(url):
                return _ths_article_source_url(url)
    return None


@dataclass
class MarketBriefingProvider:
    timeout: float = 8.0
    requester: Callable[..., requests.Response] = field(default_factory=lambda: create_scraping_session().get)
    fallback_provider: Callable[[], list[dict[str, Any]]] | None = None

    def latest_fupan(self) -> MarketBriefingResponse:
        try:
            soup = self._fetch_ths_html(THS_FUPAN_URL)
            return self._parse_fupan(soup)
        except Exception as exc:
            return self._fupan_market_or_local_fallback([f"同花顺复盘读取失败：{exc}"])

    def latest_zaopan(self) -> MarketBriefingResponse:
        try:
            soup = self._fetch_ths_html(THS_ZAOPAN_URL)
            return self._parse_zaopan(soup)
        except Exception as exc:
            return self._zaopan_market_or_local_fallback([f"同花顺早盘读取失败：{exc}"])

    def _fetch_ths_html(self, url: str) -> BeautifulSoup:
        response = self._request_ths(url)
        text = self._response_text(response)
        if not _clean_text(text):
            self._prewarm_ths_session()
            response = self._request_ths(url)
            text = self._response_text(response)
        if not _clean_text(text):
            raise ValueError(f"同花顺返回空页面：{url}")
        return BeautifulSoup(text, "html.parser")

    def _request_ths(self, url: str) -> requests.Response:
        return self.requester(url, timeout=min(self.timeout, THS_PAGE_TIMEOUT), headers=_ths_headers())

    def _fetch_article_section(self, link: MarketBriefingLink, referer: str) -> tuple[MarketBriefingSection | None, str | None]:
        if not _is_ths_article_url(link.url):
            return None, None
        try:
            response = self.requester(
                link.url,
                timeout=min(self.timeout, THS_ARTICLE_DETAIL_TIMEOUT),
                headers=_ths_headers(referer=referer),
            )
            text = self._response_text(response)
            soup = BeautifulSoup(text, "html.parser")
            body = _article_body(soup)
            if not body:
                return None, f"同花顺文章详情未解析到正文：{link.title}"
            title = _article_title(soup, link.title)
            return MarketBriefingSection(
                title=f"全文：{title}",
                content=body,
                links=[link],
                tables=[],
            ), None
        except Exception as exc:
            return None, f"同花顺文章详情抓取失败：{link.title} - {exc}"

    def _expand_article_links(
        self,
        sections: list[MarketBriefingSection],
        referer: str,
        limit: int = THS_ARTICLE_DETAIL_LIMIT,
    ) -> tuple[list[MarketBriefingSection], list[str]]:
        expanded = list(sections)
        diagnostics: list[str] = []
        seen_urls: set[str] = set()
        attempts = 0
        for section in sections:
            for link in section.links:
                if not link.url or link.url in seen_urls:
                    continue
                seen_urls.add(link.url)
                if attempts >= limit:
                    return expanded, diagnostics
                attempts += 1
                detail, diagnostic = self._fetch_article_section(link, referer)
                if detail is not None:
                    expanded.append(detail)
                if diagnostic is not None:
                    diagnostics.append(diagnostic)
        return expanded, diagnostics

    def _response_text(self, response: requests.Response) -> str:
        response.raise_for_status()
        # 声明优先：HTTP 头已带 charset（或非默认 ISO-8859-1）时尊重声明，
        # 只在缺失/失配时才用 apparent_encoding 探测——THS 页面是 GBK 且常
        # 不带 charset，探测是必要兜底；但对已声明的响应强行探测偶发误判
        # （GBK→cp1252），整页变乱文后所有清洗都白做。
        if not response.encoding or response.encoding.lower() == "iso-8859-1":
            response.encoding = getattr(response, "apparent_encoding", None) or response.encoding or "gbk"
        return response.text

    def _prewarm_ths_session(self) -> None:
        try:
            response = self.requester(
                THS_REFERER,
                timeout=min(self.timeout, THS_PREWARM_TIMEOUT),
                headers=_ths_headers(),
            )
            response.raise_for_status()
        except Exception:
            return

    def _parse_fupan(self, soup: BeautifulSoup) -> MarketBriefingResponse:
        summary = _clean_display_text(_node_text(soup.select_one("#fpzj")))
        if _is_noisy_content_line(summary):
            summary = ""
        sections: list[MarketBriefingSection] = []
        diagnostics: list[str] = []
        headers = soup.select(".fp_item_hd")
        contents = soup.select(".fp_item_cnt")
        for header, content in zip(headers, contents, strict=False):
            title = _clean_display_text(_node_text(header.select_one("h1,h2,h3")) or _node_text(header, max_length=40))
            if _is_disallowed_section_title(title):
                continue
            content_without_tables = _remove_non_textual_nodes(content)
            tables = [
                table
                for table in (_table_from_node(node, title=title) for node in content.select("table")[:2])
                if table is not None
            ]
            links = _links_from_node(content, THS_FUPAN_URL)
            body = _readable_content_from_node(content_without_tables)
            if body or tables or links:
                sections.append(MarketBriefingSection(title=title or "复盘板块", content=body or None, links=links, tables=tables))
        expanded_sections, article_diagnostics = self._expand_article_links(sections[:8], THS_FUPAN_URL)
        diagnostics.extend(article_diagnostics)
        source = "ths-fupan"
        source_url: str | None = _first_ths_article_link_url(expanded_sections)
        if not summary and not expanded_sections:
            fallback_sections, fallback_diagnostics = self._market_fallback_sections()
            diagnostics.append("同花顺复盘页未解析到有效章节。")
            diagnostics.extend(fallback_diagnostics)
            if fallback_sections:
                expanded_sections = fallback_sections
                summary = fallback_sections[0].content or "同花顺复盘页暂不可用，已使用公开行情与本地最近交易日生成回顾。"
                source = "ths-fupan+market-fallback"
                source_url = _first_section_link_url(fallback_sections)
            else:
                return self._local_brief_fupan(diagnostics)
        return MarketBriefingResponse(
            kind="fupan",
            updated_at=datetime.now(UTC),
            source=source,
            source_url=source_url,
            summary=summary
            or (
                expanded_sections[0].content
                if expanded_sections and expanded_sections[0].content
                else "同花顺复盘已读取，但页面暂未提供摘要。"
            ),
            sections=expanded_sections,
            diagnostics=diagnostics,
        )

    def _parse_zaopan(self, soup: BeautifulSoup) -> MarketBriefingResponse:
        summary = _node_text(soup.select_one(".yestoday"))
        if _is_noisy_content_line(summary):
            summary = ""
        sections: list[MarketBriefingSection] = []
        main = soup.select_one(".content-main-fl")
        if main:
            main_text_root = _remove_non_textual_nodes(main)
            tables = [
                table
                for table in (_table_from_node(node, title="早盘表格") for node in main.select("table")[:3])
                if table is not None
            ]
            content = _readable_content_from_node(main_text_root)
            if content or tables:
                sections.append(
                    MarketBriefingSection(
                        title="早盘要点",
                        content=content or None,
                        links=_links_from_node(main, THS_ZAOPAN_URL),
                        tables=tables,
                    )
                )

        for part in soup.select(".content-main-fr .table-part")[:4]:
            title = _section_title(part, "早盘侧栏")
            tables = [
                table
                for table in (_table_from_node(node, title=title) for node in part.select("table")[:2])
                if table is not None
            ]
            content_root = _remove_non_body_nodes(part)
            content = _readable_content_from_node(content_root)
            if content or tables:
                sections.append(
                    MarketBriefingSection(
                        title=title,
                        content=content or None,
                        links=_links_from_node(part, THS_ZAOPAN_URL),
                        tables=tables,
                    )
                )
        expanded_sections, diagnostics = self._expand_article_links(sections[:8], THS_ZAOPAN_URL)
        if not summary and not expanded_sections:
            # 页面 200 但选择器全落空（改版/风控空壳）时，不能把“已读取”套话
            # 当真实早盘正文（§7 source 语义），走与 fupan 相同的行情/本地兜底。
            diagnostics.append("同花顺早盘页未解析到有效内容。")
            return self._zaopan_market_or_local_fallback(diagnostics)
        return MarketBriefingResponse(
            kind="zaopan",
            updated_at=datetime.now(UTC),
            source="ths-zaopan",
            source_url=_first_ths_article_link_url(expanded_sections) or THS_ZAOPAN_URL,
            summary=summary or (sections[0].content if sections and sections[0].content else "同花顺早盘已读取，但页面暂未提供摘要。"),
            sections=expanded_sections,
            diagnostics=diagnostics,
        )

    def _fupan_market_or_local_fallback(self, diagnostics: list[str]) -> MarketBriefingResponse:
        fallback_sections, fallback_diagnostics = self._market_fallback_sections()
        diagnostics.extend(fallback_diagnostics)
        if fallback_sections:
            return MarketBriefingResponse(
                kind="fupan",
                updated_at=datetime.now(UTC),
                source="ths-fupan+market-fallback",
                source_url=_first_section_link_url(fallback_sections),
                summary=fallback_sections[0].content or "同花顺复盘页暂不可用，已使用公开行情生成回顾线索。",
                sections=fallback_sections,
                diagnostics=diagnostics,
            )
        return self._local_brief_fupan(diagnostics)

    def _zaopan_market_or_local_fallback(self, diagnostics: list[str]) -> MarketBriefingResponse:
        fallback_sections, fallback_diagnostics = self._market_fallback_sections()
        diagnostics.extend(fallback_diagnostics)
        if fallback_sections:
            return MarketBriefingResponse(
                kind="zaopan",
                updated_at=datetime.now(UTC),
                source="ths-zaopan+market-fallback",
                source_url=_first_section_link_url(fallback_sections),
                summary=fallback_sections[0].content or "同花顺早盘页暂不可用，已使用公开行情生成早盘线索。",
                sections=fallback_sections,
                diagnostics=diagnostics,
            )
        return self._local_brief_zaopan(diagnostics)

    def _local_brief_fupan(self, diagnostics: list[str]) -> MarketBriefingResponse:
        diagnostics = list(diagnostics)
        diagnostics.append("同花顺复盘和公开行情兜底均不可用，已生成本地简短防守复盘。")
        content = (
            "同花顺结构化复盘暂不可用，公开行情兜底也未形成可用结构。"
            "当前只给防守口径：不把新闻、局部行情或空页面包装成确定复盘结论；"
            "等待同花顺正文、指数表或活跃个股表恢复后再确认强势方向。"
        )
        section = MarketBriefingSection(
            title="本地简短复盘",
            content=content,
            links=[],
            tables=[],
        )
        return MarketBriefingResponse(
            kind="fupan",
            updated_at=datetime.now(UTC),
            source="ths-fupan+local-brief",
            source_url=None,
            summary=content,
            sections=[section],
            diagnostics=diagnostics,
        )

    def _local_brief_zaopan(self, diagnostics: list[str]) -> MarketBriefingResponse:
        diagnostics = list(diagnostics)
        diagnostics.append("同花顺早盘和公开行情兜底均不可用，已生成本地简短防守早盘。")
        content = (
            "同花顺结构化早盘暂不可用，公开行情兜底也未形成可用结构。"
            "当前只给防守口径：不把新闻、局部行情或空页面包装成确定早盘结论，"
            "等待同花顺正文、指数表或活跃个股表恢复后再确认盘前线索。"
        )
        section = MarketBriefingSection(
            title="本地简短早盘",
            content=content,
            links=[],
            tables=[],
        )
        return MarketBriefingResponse(
            kind="zaopan",
            updated_at=datetime.now(UTC),
            source="ths-zaopan+local-brief",
            source_url=None,
            summary=content,
            sections=[section],
            diagnostics=diagnostics,
        )

    def _market_fallback_sections(self) -> tuple[list[MarketBriefingSection], list[str]]:
        diagnostics: list[str] = []
        if self.fallback_provider is not None:
            try:
                sections = [
                    section
                    for section in (_section_from_mapping(item) for item in self.fallback_provider())
                    if section is not None
                ]
                if sections:
                    diagnostics.append("已使用注入的公开行情兜底源生成复盘回顾。")
                return sections, diagnostics
            except Exception as exc:
                diagnostics.append(f"公开行情兜底源读取失败：{exc}")

        sections: list[MarketBriefingSection] = []
        index_table = self._fallback_index_table(diagnostics)
        spot_rows = self._fallback_spot_rows(diagnostics)
        content_parts: list[str] = []
        if index_table is not None and index_table.rows:
            lead = index_table.rows[0]
            content_parts.append(
                f"{lead.get('名称', '主要指数')} 最新值 {lead.get('最新值', '--')}，涨跌幅 {lead.get('涨跌幅', '--')}。"
            )
        if spot_rows:
            content_parts.append(
                f"公开行情兜底抓取到 {len(spot_rows)} 只当日涨幅靠前个股，只作为复盘线索，不替代同花顺原文。"
            )
        tables = [table for table in [index_table] if table is not None]
        if spot_rows:
            tables.append(
                MarketBriefingTable(
                    title="公开行情活跃个股",
                    columns=["代码", "名称", "现价", "涨跌额", "涨跌幅"],
                    rows=spot_rows[:8],
                )
            )
        if tables or content_parts:
            sections.append(
                MarketBriefingSection(
                    title="公开行情回顾",
                    content=" ".join(content_parts) or "同花顺复盘页暂不可用，已尝试使用公开行情生成回顾线索。",
                    links=[
                        MarketBriefingLink(
                            title="东方财富行情中心",
                            url="https://quote.eastmoney.com/center/gridlist.html",
                        )
                    ],
                    tables=tables,
                )
            )
        return sections, diagnostics

    def _fallback_index_table(self, diagnostics: list[str]) -> MarketBriefingTable | None:
        symbols = ",".join(symbol for symbol, _ in INDEX_SYMBOLS)
        try:
            response = self.requester(
                SINA_QUOTE_URL.format(symbols=symbols),
                timeout=min(self.timeout, MARKET_FALLBACK_TIMEOUT),
                headers=_sina_headers(),
            )
            response.raise_for_status()
            raw_content = getattr(response, "content", b"")
            text = raw_content.decode("gbk", errors="ignore") if raw_content else response.text
        except Exception as exc:
            diagnostics.append(f"Sina 指数兜底失败：{exc}")
            return None
        decoded = _decode_sina_quotes(text)
        rows: list[dict[str, str]] = []
        for symbol, name in INDEX_SYMBOLS:
            values = decoded.get(symbol, [])
            last = _safe_float(values[3] if len(values) > 3 else None)
            previous = _safe_float(values[2] if len(values) > 2 else None)
            if last is None:
                continue
            change = None if previous is None else last - previous
            change_pct = None if previous in (None, 0) or change is None else change / previous
            rows.append(
                {
                    "名称": name,
                    "最新值": _format_decimal(last),
                    "涨跌额": _format_decimal(change),
                    "涨跌幅": _format_pct(change_pct),
                }
            )
        if not rows:
            diagnostics.append("Sina 指数兜底返回空行情。")
            return None
        return MarketBriefingTable(title="参考指数", columns=["名称", "最新值", "涨跌额", "涨跌幅"], rows=rows)

    def _fallback_spot_rows(self, diagnostics: list[str]) -> list[dict[str, str]]:
        try:
            response = self.requester(
                EASTMONEY_A_SPOT_URL,
                timeout=min(self.timeout, MARKET_FALLBACK_TIMEOUT),
                headers=_eastmoney_headers(),
                params={
                    "pn": "1",
                    "pz": "20",
                    "po": "1",
                    "np": "1",
                    "fltt": "2",
                    "invt": "2",
                    "fid": "f3",
                    "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048",
                    "fields": "f12,f14,f2,f3,f4,f5,f6,f8,f10,f21",
                },
            )
            response.raise_for_status()
            payload = response.json() or {}
        except Exception as exc:
            diagnostics.append(f"东方财富 A 股行情兜底失败：{exc}")
            return []
        rows = payload.get("data", {}).get("diff", []) if isinstance(payload, dict) else []
        if not isinstance(rows, list) or not rows:
            diagnostics.append("东方财富 A 股行情兜底返回空数据。")
            return []
        out: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = normalize_symbol(str(row.get("f12") or ""))
            name = _clean_text(str(row.get("f14") or ""))
            last = _safe_float(row.get("f2"))
            change_pct = _safe_float(row.get("f3"))
            change = _safe_float(row.get("f4"))
            volume = _safe_float(row.get("f5"))
            amount = _safe_float(row.get("f6"))
            turnover = _safe_float(row.get("f8"))
            volume_ratio = _safe_float(row.get("f10"))
            float_market_cap = _safe_float(row.get("f21"))
            if not code or not name or last is None:
                continue
            out.append(
                {
                    "代码": code,
                    "名称": name,
                    "现价": _format_decimal(last),
                    "涨跌额": _format_decimal(change),
                    "涨跌幅": _format_percent_points(change_pct),
                    "成交量": _format_decimal(volume, digits=0),
                    "成交额": _format_decimal(amount, digits=0),
                    "换手率": _format_percent_points(turnover),
                    "量比": _format_decimal(volume_ratio),
                    "流通市值": _format_decimal(float_market_cap, digits=0),
                }
            )
        return out
