from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any

import requests
from bs4 import BeautifulSoup

from astock_backtester.data.cls import CLS_QUOTE_BASE_URL, CLS_SITE_BASE_URL, cls_request_json
from astock_backtester.data.http_transport import resilient_get, scraping_get, should_allow_alternate_transport
from astock_backtester.data.realtime_parsers import (
    THS_HEADERS,
    THS_MARKET_SUMMARY_URL,
)
from astock_backtester.data.realtime_parsers import (
    breadth_from_cls_distribution as _breadth_from_cls_distribution,
)
from astock_backtester.data.realtime_parsers import (
    breadth_from_cls_home_data as _breadth_from_cls_home_data,
)
from astock_backtester.data.realtime_parsers import (
    normalize_change_pct as _normalize_change_pct,
)
from astock_backtester.data.realtime_parsers import (
    parse_float as _parse_float,
)
from astock_backtester.data.realtime_parsers import (
    parse_int as _parse_int,
)
from astock_backtester.data.symbols import normalize_symbol
from astock_backtester.models import (
    ClsFinanceAnchor,
    ClsFinanceEmotion,
    ClsFinancePlate,
    ClsFinancePoolItem,
    ClsFinanceResponse,
    ClsFinanceTlinePoint,
)

CLS_FINANCE_URL = "https://www.cls.cn/finance"
CLS_TLINE_URL = f"{CLS_QUOTE_BASE_URL}/quote/index/tline"
CLS_HOME_URL = f"{CLS_QUOTE_BASE_URL}/quote/index/home"
CLS_INDEX_BASIC_URL = f"{CLS_QUOTE_BASE_URL}/quote/index/basic"
CLS_STOCK_EMOTION_URL = f"{CLS_QUOTE_BASE_URL}/v2/quote/a/stock/emotion"
CLS_UP_DOWN_ANALYSIS_URL = f"{CLS_QUOTE_BASE_URL}/quote/index/up_down_analysis"
CLS_TRANSACTION_ANCHOR_URL = f"{CLS_SITE_BASE_URL}/v3/transaction/anchor"
THS_MARKET_DEGREE_SOURCE = "ths-market-summary"
THS_MARKET_DEGREE_LABEL = "同花顺大盘评级"
CLS_MARKET_DEGREE_SOURCE = "cls-finance-emotion"
CLS_MARKET_DEGREE_LABEL = "财联社市场热度"
THS_MARKET_BOARD_URL = "http://q.10jqka.com.cn/index/index/board"
THS_CHAMELEON_URL = "https://s.thsi.cn/js/chameleon/chameleon.1.7.min.1781803.js"
THS_MARKET_INDEXFLASH_URLS = (
    "http://q.10jqka.com.cn/api.php?t=indexflash&",
    "https://q.10jqka.com.cn/api.php?t=indexflash&",
)
THS_MARKET_DEGREE_URLS = (*THS_MARKET_INDEXFLASH_URLS, THS_MARKET_BOARD_URL, THS_MARKET_SUMMARY_URL)
THS_MARKET_SCORE_HEADERS = {
    **THS_HEADERS,
    "Accept": "*/*",
    "Referer": THS_MARKET_BOARD_URL,
}
THS_MARKET_DPPJ_RE = re.compile(
    r"""["']?dppj_data["']?\s*[:=]\s*["']?([0-9]{1,3}(?:\.[0-9]+)?)""",
    re.IGNORECASE,
)
THS_MARKET_DEGREE_RE = re.compile(
    r"(?:大盘评分|大盘评级|市场评分|market[_-]?degree|market[_-]?score|score)"
    r"[^0-9]{0,24}([0-9]{1,3}(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


_ths_cookie_cache_lock = Lock()
_ths_cookie_cache_value: str | None = None
_ths_cookie_cache_until: float = 0.0
_THS_COOKIE_CACHE_TTL: float = 90.0


logger = logging.getLogger(__name__)


def _cls_payload_data(payload: Any, diagnostics: list[str], source: str) -> Any | None:
    if not isinstance(payload, dict):
        diagnostics.append(f"{source}读取失败：响应不是对象。")
        return None
    for key, success_codes in (("code", {"0", "200"}), ("errno", {"0"})):
        value = payload.get(key)
        if value is not None and str(value).strip() not in success_codes:
            detail = str(payload.get("message") or payload.get("msg") or payload.get("error") or "").strip()
            suffix = f"：{detail}" if detail else ""
            diagnostics.append(f"{source}读取失败：CLS 业务错误 {key}={value}{suffix}")
            return None
    if "data" not in payload:
        diagnostics.append(f"{source}读取失败：响应缺少 data 字段。")
        return None
    return payload["data"]


@dataclass
class ClsFinanceProvider:
    requester: Callable[..., requests.Response] = scraping_get
    timeout: float = 5.0
    browser_cookie_getter: Callable[[], str | None] | None = None
    alternate_requester: Callable[..., Any] | None = None
    allow_alternate_transport: bool | None = None
    cache_ttl: float = 3.0
    recent_success_ttl: float = 15 * 60.0
    _refresh_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _cached_response: ClsFinanceResponse | None = field(default=None, init=False, repr=False)
    _cached_until: float = field(default=0.0, init=False, repr=False)
    _last_successful_response: ClsFinanceResponse | None = field(default=None, init=False, repr=False)
    _last_successful_at: float = field(default=0.0, init=False, repr=False)

    def current_board(self) -> ClsFinanceResponse:
        with self._refresh_lock:
            now = monotonic()
            if (
                self._cached_response is not None
                and self.cache_ttl > 0
                and now < self._cached_until
            ):
                return self._cached_response.model_copy(deep=True)

            response, failed_list_fields = self._fetch_current_board()
            fetched_at = monotonic()
            response, used_recent_success = self._merge_recent_success(
                response,
                fetched_at,
                failed_list_fields=failed_list_fields,
            )
            successful = self._is_successful_response(response)
            if successful and not used_recent_success:
                self._last_successful_response = response.model_copy(deep=True)
                self._last_successful_at = fetched_at
            if successful or used_recent_success:
                self._cached_response = response.model_copy(deep=True)
                self._cached_until = min(
                    fetched_at + max(0.0, self.cache_ttl),
                    self._last_successful_at + self.recent_success_ttl
                    if self._last_successful_at and self.recent_success_ttl > 0
                    else fetched_at + max(0.0, self.cache_ttl),
                )
            else:
                self._cached_response = None
                self._cached_until = 0.0
            return response.model_copy(deep=True)

    def recent_success(self) -> dict[str, object]:
        """Public health snapshot for ``/diagnostics/sources``; never fetches."""
        response = self._last_successful_response
        if response is None or self._last_successful_at <= 0.0:
            return {"ok": False, "seconds_since_success": None, "diagnostics": ["尚未有成功拉取记录。"]}
        return {
            "ok": True,
            "seconds_since_success": max(0.0, monotonic() - self._last_successful_at),
            "source": response.source,
            "diagnostics": list(response.diagnostics),
        }

    def _merge_recent_success(
        self,
        response: ClsFinanceResponse,
        fetched_at: float,
        *,
        failed_list_fields: set[str],
    ) -> tuple[ClsFinanceResponse, bool]:
        previous = self._last_successful_response
        if (
            previous is None
            or self.recent_success_ttl <= 0
            or fetched_at - self._last_successful_at > self.recent_success_ttl
        ):
            return response, False

        retained = response.model_copy(deep=True)
        previous = previous.model_copy(deep=True)
        reused_fields: list[str] = []
        for field_name in ("tline", "anchors", "up_pool"):
            if (
                field_name in failed_list_fields
                and not getattr(retained, field_name)
                and getattr(previous, field_name)
            ):
                setattr(retained, field_name, getattr(previous, field_name))
                reused_fields.append(field_name)
        if retained.preclose_px is None and previous.preclose_px is not None:
            retained.preclose_px = previous.preclose_px
            reused_fields.append("preclose_px")

        current_emotion = retained.emotion
        previous_emotion = previous.emotion
        if current_emotion is None and previous_emotion is not None:
            retained.emotion = previous_emotion
            reused_fields.append("emotion")
        elif current_emotion is not None and previous_emotion is not None:
            for field_name in (
                "market_degree",
                "market_degree_source",
                "market_degree_label",
                "shsz_balance",
                "shsz_balance_change",
                "breadth",
                "up_limit",
                "open_limit",
                "performance",
            ):
                if getattr(current_emotion, field_name) is None and getattr(previous_emotion, field_name) is not None:
                    setattr(current_emotion, field_name, getattr(previous_emotion, field_name))
                    reused_fields.append(f"emotion.{field_name}")

        if not reused_fields:
            return response, False
        retained.source = f"{retained.source}+recent-success-cache"
        retained.diagnostics = [
            *retained.diagnostics,
            "recent_success_cache_used for missing CLS finance fields: "
            + ", ".join(reused_fields),
        ]
        return retained, True

    def _is_successful_response(self, response: ClsFinanceResponse) -> bool:
        emotion = response.emotion
        has_emotion_data = emotion is not None and any(
            (
                emotion.market_degree is not None,
                bool(emotion.shsz_balance),
                bool(emotion.shsz_balance_change),
                emotion.breadth is not None,
                emotion.up_limit is not None,
                emotion.open_limit is not None,
                bool(emotion.performance),
            )
        )
        return bool(
            response.tline
            or response.anchors
            or response.preclose_px is not None
            or has_emotion_data
            or response.up_pool
        )

    def _fetch_current_board(self) -> tuple[ClsFinanceResponse, set[str]]:
        diagnostics: list[str] = []
        today = date.today().isoformat()
        tline_diagnostic_count = len(diagnostics)
        tline = self._read_tline(diagnostics)
        tline_failed = len(diagnostics) > tline_diagnostic_count
        anchors_diagnostic_count = len(diagnostics)
        anchors = self._read_anchors(today, diagnostics)
        anchors_failed = len(diagnostics) > anchors_diagnostic_count
        preclose_px = self._read_preclose(diagnostics)
        emotion = self._read_emotion(diagnostics)
        ths_market_degree = self._read_ths_market_degree(diagnostics)
        emotion = self._with_market_degree_source(emotion, ths_market_degree)
        up_pool_diagnostic_count = len(diagnostics)
        up_pool = self._read_up_pool(diagnostics)
        up_pool_failed = len(diagnostics) > up_pool_diagnostic_count
        failed_list_fields = {
            field_name
            for field_name, failed in (
                ("tline", tline_failed),
                ("anchors", anchors_failed),
                ("up_pool", up_pool_failed),
            )
            if failed
        }
        return (
            ClsFinanceResponse(
                updated_at=datetime.now(UTC),
                source="cls-finance",
                source_url=CLS_FINANCE_URL,
                preclose_px=preclose_px,
                tline=tline,
                anchors=anchors,
                emotion=emotion,
                up_pool=up_pool,
                diagnostics=diagnostics,
            ),
            failed_list_fields,
        )

    def _read_tline(self, diagnostics: list[str]) -> list[ClsFinanceTlinePoint]:
        try:
            payload = cls_request_json(
                self.requester,
                CLS_TLINE_URL,
                timeout=self.timeout,
            )
        except Exception as exc:
            diagnostics.append(f"财联社分时线读取失败：{exc}")
            return []
        rows = _cls_payload_data(payload, diagnostics, "财联社分时线")
        if not isinstance(rows, list):
            if rows is not None:
                diagnostics.append("财联社分时线读取失败：data 不是列表。")
            return []
        points: list[ClsFinanceTlinePoint] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            minute = _parse_int(row.get("minute"))
            last_px = _parse_float(row.get("last_px"))
            if minute is None or last_px is None:
                continue
            points.append(
                ClsFinanceTlinePoint(
                    date=_parse_int(row.get("date")),
                    minute=minute,
                    last_px=last_px,
                    change=_normalize_change_pct(row.get("change")),
                )
            )
        if rows and not points:
            diagnostics.append("CLS tline response contained no parseable rows.")
        return sorted(points, key=lambda point: (point.date if point.date is not None else 0, point.minute))

    def _read_anchors(self, cdate: str, diagnostics: list[str]) -> list[ClsFinanceAnchor]:
        try:
            payload = cls_request_json(
                self.requester,
                CLS_TRANSACTION_ANCHOR_URL,
                params={"cdate": cdate},
                timeout=self.timeout,
            )
        except Exception as exc:
            diagnostics.append(f"财联社盘面锚点读取失败：{exc}")
            return []
        rows = _cls_payload_data(payload, diagnostics, "财联社盘面锚点")
        if not isinstance(rows, list):
            if rows is not None:
                diagnostics.append("财联社盘面锚点读取失败：data 不是列表。")
            return []
        anchors: list[ClsFinanceAnchor] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("symbol_code") or "").strip()
            name = str(row.get("symbol_name") or "").strip()
            if not code or not name:
                continue
            direction = str(row.get("float") or "flat").strip()
            if direction not in {"up", "down"}:
                direction = "flat"
            anchors.append(
                ClsFinanceAnchor(
                    code=code,
                    name=name,
                    article_id=_parse_int(row.get("article_id")),
                    c_time=str(row.get("c_time") or "").strip() or None,
                    direction=direction,
                    url=f"https://www.cls.cn/plate?code={code}" if code.startswith("cls") else None,
                )
            )
        if rows and not anchors:
            diagnostics.append("CLS anchors response contained no parseable rows.")
        return anchors

    def _read_preclose(self, diagnostics: list[str]) -> float | None:
        try:
            payload = cls_request_json(
                self.requester,
                CLS_INDEX_BASIC_URL,
                params={"secu_code": "sh000001", "fields": "preclose_px"},
                timeout=self.timeout,
            )
        except Exception as exc:
            diagnostics.append(f"财联社指数昨收读取失败：{exc}")
            return None
        data = _cls_payload_data(payload, diagnostics, "财联社指数昨收")
        if not isinstance(data, dict):
            if data is not None:
                diagnostics.append("财联社指数昨收读取失败：data 不是对象。")
            return None
        return _parse_float(data.get("preclose_px"))

    def _read_emotion(self, diagnostics: list[str]) -> ClsFinanceEmotion | None:
        try:
            payload = cls_request_json(self.requester, CLS_STOCK_EMOTION_URL, timeout=self.timeout)
        except Exception as exc:
            diagnostics.append(f"财联社市场热度读取失败：{exc}")
            breadth, up_limit, open_limit = self._read_home_distribution(diagnostics)
            if breadth is None and up_limit is None and open_limit is None:
                return None
            return ClsFinanceEmotion(
                breadth=breadth,
                up_limit=up_limit,
                open_limit=open_limit,
            )
        data = _cls_payload_data(payload, diagnostics, "财联社市场热度")
        if not isinstance(data, dict):
            if data is not None:
                diagnostics.append("财联社市场热度读取失败：data 不是对象。")
            breadth, up_limit, open_limit = self._read_home_distribution(diagnostics)
            if breadth is None and up_limit is None and open_limit is None:
                return None
            return ClsFinanceEmotion(
                breadth=breadth,
                up_limit=up_limit,
                open_limit=open_limit,
            )
        breadth = _breadth_from_cls_distribution(data.get("up_down_dis", {}))
        if breadth is not None:
            breadth.source = "cls-finance-emotion"
        home_up_limit: int | None = None
        home_open_limit: int | None = None
        if breadth is None:
            breadth, home_up_limit, home_open_limit = self._read_home_distribution(diagnostics)
        market_degree = _parse_float(data.get("market_degree"))
        up_ratio_num = data.get("up_ratio_num")
        up_limit = _parse_int(str(up_ratio_num)) if up_ratio_num is not None else None
        up_open_num = data.get("up_open_num")
        open_limit = _parse_int(str(up_open_num)) if up_open_num is not None else None
        return ClsFinanceEmotion(
            market_degree=market_degree,
            market_degree_source=CLS_MARKET_DEGREE_SOURCE if market_degree is not None else None,
            market_degree_label=CLS_MARKET_DEGREE_LABEL if market_degree is not None else None,
            shsz_balance=str(data.get("shsz_balance") or "").strip() or None,
            shsz_balance_change=str(data.get("shsz_balance_change_px") or "").strip() or None,
            breadth=breadth,
            up_limit=up_limit if up_limit is not None else home_up_limit,
            open_limit=open_limit if open_limit is not None else home_open_limit,
            performance=str(data.get("performance") or "").strip() or None,
        )

    def _read_home_distribution(
        self,
        diagnostics: list[str],
    ) -> tuple[Any, int | None, int | None]:
        try:
            payload = cls_request_json(self.requester, CLS_HOME_URL, timeout=self.timeout)
        except Exception as exc:
            diagnostics.append(f"CLS homepage distribution fallback failed: {exc}")
            return None, None, None
        data = _cls_payload_data(payload, diagnostics, "CLS 首页涨跌分布兜底")
        if not isinstance(data, dict):
            if data is not None:
                diagnostics.append("CLS 首页涨跌分布兜底失败：data 不是对象。")
            return None, None, None
        breadth = _breadth_from_cls_home_data(data)
        if breadth is not None:
            breadth.source = "cls-finance-home"
            diagnostics.append("CLS homepage distribution fallback used after emotion endpoint failure.")
        distribution = data.get("up_down_dis")
        if not isinstance(distribution, dict):
            return breadth, None, None
        return (
            breadth,
            _parse_int(distribution.get("up_num")),
            _parse_int(distribution.get("up_open_num")),
        )

    def _read_up_pool(self, diagnostics: list[str]) -> list[ClsFinancePoolItem]:
        try:
            payload = cls_request_json(
                self.requester,
                CLS_UP_DOWN_ANALYSIS_URL,
                params={"type": "up_pool", "way": "last_px", "rever": 1},
                timeout=self.timeout,
            )
        except Exception as exc:
            diagnostics.append(f"财联社涨停池读取失败：{exc}")
            return []
        rows = _cls_payload_data(payload, diagnostics, "财联社涨停池")
        if not isinstance(rows, list):
            if rows is not None:
                diagnostics.append("财联社涨停池读取失败：data 不是列表。")
            return []
        items: list[ClsFinancePoolItem] = []
        for row in rows[:20]:
            if not isinstance(row, dict):
                continue
            symbol = normalize_symbol(str(row.get("secu_code") or ""))
            name = str(row.get("secu_name") or "").strip()
            if not symbol or not name:
                continue
            plates: list[ClsFinancePlate] = []
            for plate in row.get("plate") or []:
                if not isinstance(plate, dict):
                    continue
                plate_code = str(plate.get("secu_code") or "").strip()
                plate_name = str(plate.get("secu_name") or "").strip()
                if plate_code and plate_name:
                    plates.append(
                        ClsFinancePlate(
                            code=plate_code,
                            name=plate_name,
                            change_pct=_normalize_change_pct(plate.get("change")),
                        )
                    )
            items.append(
                ClsFinancePoolItem(
                    symbol=symbol,
                    name=name,
                    change_pct=_normalize_change_pct(row.get("change")),
                    last=_parse_float(row.get("last_px")),
                    time=str(row.get("time") or "").strip() or None,
                    reason=str(row.get("up_reason") or "").strip() or None,
                    limit_up_days=_parse_int(row.get("limit_up_days")),
                    plates=plates,
                )
            )
        if rows and not items:
            diagnostics.append("CLS up-pool response contained no parseable rows.")
        return items

    def _read_ths_market_degree(self, diagnostics: list[str]) -> float | None:
        errors: list[str] = []
        browser_cookie: str | None = None
        browser_cookie_read = False
        for url in THS_MARKET_DEGREE_URLS:
            request_diagnostics: list[str] = []
            headers = dict(THS_MARKET_SCORE_HEADERS)
            if url in THS_MARKET_INDEXFLASH_URLS:
                if not browser_cookie_read:
                    browser_cookie_read = True
                    try:
                        if self.browser_cookie_getter is not None:
                            browser_cookie = self.browser_cookie_getter()
                        else:
                            browser_cookie = read_ths_browser_cookie(min(self.timeout, 3.0))
                    except Exception as exc:
                        errors.append(f"同花顺浏览器校验读取失败：{exc}")
                if browser_cookie:
                    headers["Cookie"] = browser_cookie
            try:
                if url in THS_MARKET_INDEXFLASH_URLS:
                    response = resilient_get(
                        self.requester,
                        url,
                        timeout=min(self.timeout, 3.0),
                        source="ths-market-degree",
                        diagnostics=request_diagnostics,
                        retries=0,
                        alternate_requester=self.alternate_requester,
                        allow_alternate=should_allow_alternate_transport(
                            self.requester,
                            self.allow_alternate_transport,
                        ),
                        headers=headers,
                    )
                else:
                    response = self.requester(
                        url,
                        timeout=min(self.timeout, 3.0),
                        headers=headers,
                    )
                    response.raise_for_status()
            except Exception as exc:
                errors.extend(request_diagnostics)
                errors.append(f"{url}: {exc}")
                continue
            response.encoding = response.encoding or "gbk"
            text = response.text
            visible_text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
            degree = _parse_ths_market_degree(text)
            if degree is None:
                degree = _parse_ths_market_degree_visible_text(visible_text)
            if degree is None:
                errors.extend(request_diagnostics)
                errors.append(f"{url}: 页面未包含可解析的大盘评分")
                continue
            diagnostics.extend(request_diagnostics)
            diagnostics.append(f"同花顺大盘评分读取成功：{degree:.1f}")
            return degree
        diagnostics.append(f"同花顺大盘评分读取失败：{'; '.join(errors[:4])}")
        return None

    def _with_market_degree_source(
        self,
        emotion: ClsFinanceEmotion | None,
        ths_market_degree: float | None,
    ) -> ClsFinanceEmotion | None:
        if emotion is None and ths_market_degree is None:
            return None
        if emotion is None:
            emotion = ClsFinanceEmotion()
        if ths_market_degree is not None:
            emotion.market_degree = ths_market_degree
            emotion.market_degree_source = THS_MARKET_DEGREE_SOURCE
            emotion.market_degree_label = THS_MARKET_DEGREE_LABEL
        elif emotion.market_degree is not None and not emotion.market_degree_source:
            emotion.market_degree_source = CLS_MARKET_DEGREE_SOURCE
            emotion.market_degree_label = CLS_MARKET_DEGREE_LABEL
        return emotion


_SCRIPT_BLOCK_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)


def _parse_ths_market_degree(text: str) -> float | None:
    if not text:
        return None
    payload_degree = _parse_ths_market_degree_payload(text)
    if payload_degree is not None:
        return payload_degree
    # 只要是 HTML（含标签字符）就只在 <script> 块内找 dppj_data：§5 明令禁止
    # 把标签属性（如 data-dppj_data="85"）里的数字当评分；indexflash 载荷不含
    # HTML 标签，仍在全文匹配。
    if "<" in text:
        haystack = "\n".join(match.group(1) for match in _SCRIPT_BLOCK_RE.finditer(text))
    else:
        haystack = text
    for match in THS_MARKET_DPPJ_RE.finditer(haystack):
        value = _normalize_ths_market_degree(match.group(1))
        if value is not None:
            return value
    return None


def _parse_ths_market_degree_visible_text(text: str) -> float | None:
    if not text:
        return None
    for match in THS_MARKET_DEGREE_RE.finditer(text):
        value = _normalize_ths_market_degree(match.group(1))
        if value is not None:
            return value
    return None


def _parse_ths_market_degree_payload(text: str) -> float | None:
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1].strip()
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError):
        return None
    return _market_degree_from_payload(payload)


def _market_degree_from_payload(payload: Any) -> float | None:
    if isinstance(payload, dict):
        for key in ("dppj_data", "market_degree", "market_score", "score"):
            if key in payload:
                value = _normalize_ths_market_degree(payload.get(key))
                if value is not None:
                    return value
        for value in payload.values():
            nested = _market_degree_from_payload(value)
            if nested is not None:
                return nested
    if isinstance(payload, list):
        for item in payload:
            nested = _market_degree_from_payload(item)
            if nested is not None:
                return nested
    return None


def _normalize_ths_market_degree(value: object) -> float | None:
    parsed = _parse_float(value)
    # 0 分是上游的占位/缺数值，不是真实评级；与“没解析到”同等对待。
    if parsed is None or parsed <= 0:
        return None
    if parsed <= 10:
        return parsed
    if parsed <= 100:
        return parsed / 10
    return None


def read_ths_browser_cookie(timeout_s: float = 1.2) -> str | None:
    global _ths_cookie_cache_value, _ths_cookie_cache_until
    now = monotonic()
    with _ths_cookie_cache_lock:
        if _ths_cookie_cache_value is not None and now < _ths_cookie_cache_until:
            return _ths_cookie_cache_value

    node = _resolve_node_executable()
    if node is None:
        return None
    worker = _resolve_ths_cookie_worker()
    startup_kwargs = _subprocess_startup_kwargs()
    timeout_s = min(max(float(timeout_s), 0.2), 5.0)
    environment = {**os.environ, "THS_COOKIE_TIMEOUT_MS": str(round(timeout_s * 1000))}
    try:
        if worker is not None:
            completed = subprocess.run(
                [node, str(worker)],
                cwd=worker.parent,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_s + 1.2,
                env=environment,
                **startup_kwargs,
            )
        else:
            script = _ths_browser_cookie_script()
            completed = subprocess.run(
                [node, "-e", script],
                cwd=_project_root(),
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_s + 1.2,
                env=environment,
                **startup_kwargs,
            )
    except Exception:
        logger.warning("silent failure in read_ths_browser_cookie", exc_info=True)
        return None
    cookie = completed.stdout.strip()
    if completed.returncode != 0 or "v=" not in cookie:
        return None
    with _ths_cookie_cache_lock:
        _ths_cookie_cache_value = cookie
        _ths_cookie_cache_until = monotonic() + _THS_COOKIE_CACHE_TTL
    return cookie


def _read_ths_browser_cookie() -> str | None:
    """Compatibility wrapper for callers that have not adopted a budget yet."""
    return read_ths_browser_cookie()


def _subprocess_startup_kwargs() -> dict[str, object]:
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": startupinfo,
    }


def _sidecar_runtime_dir() -> Path | None:
    if getattr(sys, "frozen", False):
        try:
            return Path(sys.executable).resolve().parent
        except OSError:
            return None
    return None


def _resolve_ths_cookie_worker() -> Path | None:
    candidates: list[Path] = []
    sidecar_dir = _sidecar_runtime_dir()
    if sidecar_dir is not None:
        candidates.append(sidecar_dir / "ths-cookie-worker.cjs")
    candidates.append(_project_root() / "src-tauri" / "bin" / "ths-cookie-worker.cjs")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _resolve_node_executable() -> str | None:
    candidates: list[Path] = []
    sidecar_dir = _sidecar_runtime_dir()
    if sidecar_dir is not None:
        candidates.append(sidecar_dir / "node.exe")
    candidates.extend(
        [
            _project_root() / "src-tauri" / "bin" / "node.exe",
            _project_root() / ".tools" / "node-v20.18.1-win-x64" / "node.exe",
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("node")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _ths_browser_cookie_script() -> str:
    return f"""
const {{ JSDOM, VirtualConsole }} = require("jsdom");

(async () => {{
  const timeoutMs = Math.max(200, Number(process.env.THS_COOKIE_TIMEOUT_MS || "1200"));
  const virtualConsole = new VirtualConsole();
  virtualConsole.on("error", () => {{}});
  virtualConsole.on("warn", () => {{}});
  const dom = new JSDOM(
    "<!doctype html><html><head><script src=\\"{THS_CHAMELEON_URL}\\"></script></head><body></body></html>",
    {{
      url: "{THS_MARKET_BOARD_URL}",
      resources: "usable",
      runScripts: "dangerously",
      pretendToBeVisual: true,
      virtualConsole,
      userAgent: "Mozilla/5.0"
    }}
  );
  const deadline = Date.now() + timeoutMs;
  let cookie = "";
  try {{
    while (Date.now() < deadline) {{
      cookie = dom.window.document.cookie || "";
      if (/(?:^|;\\s*)v=/.test(cookie)) {{
        process.stdout.write(cookie);
        return;
      }}
      await new Promise((resolve) => setTimeout(resolve, 25));
    }}
  }} finally {{
    dom.window.close();
  }}
  process.exit(1);
}})().catch(() => process.exit(1));
"""
