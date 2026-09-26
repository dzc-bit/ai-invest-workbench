from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from threading import Lock
from threading import local as thread_local
from typing import Any

import pandas as pd
import requests

from astock_backtester.data.http_transport import USER_AGENT as UA
from astock_backtester.data.http_transport import create_scraping_session, curl_verify_kwargs
from astock_backtester.data.parsing import is_blank_numeric, parse_float, parse_money_amount
from astock_backtester.data.symbols import a_share_market_symbol, market_code, normalize_symbol
from astock_backtester.data.trading_calendar import a_share_trade_dates

EASTMONEY_FUND_FLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
EASTMONEY_FUND_FLOW_KLINE_URL = "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get"
# https 与 http 返回同一载荷（实测），且同文件其余端点均为 https：统一走 https，
# 避免明文请求被中间设备改写或投毒。
SINA_FUND_FLOW_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_qsfx_lscjfb"
BAIDU_FUND_FLOW_URL = "https://finance.pae.baidu.com/vapi/v1/fundsortlist"
EASTMONEY_UT = "b2884a393a59ad64002292a3e90d46a5"
SINA_PAGE_SIZE = 5000
SINA_PAGE_RETRIES = 3
SINA_PAGE_BACKOFF_SECONDS = 1.0
SINA_REQUEST_INTERVAL_SECONDS = 0.25
BAIDU_PAGE_RETRIES = 3
BAIDU_PAGE_BACKOFF_SECONDS = 0.5
BAIDU_PAGE_SLEEP_SECONDS = 0.05
DEFAULT_MAX_WORKERS = 8
DEFAULT_CAPITAL_FLOW_BATCH_SIZE = 50
# 单票百度补日的上限（按"最近 N 个缺失日"取）：内层每轮最多 500 页，不封顶时
# 长窗口 + 新浪大面积缺日会退化成 N×500 次请求。
BAIDU_SUPPLEMENT_MAX_MISSING_DATES = 20
# 最近成功行缓存的符号数上限：全市场补齐会留下约 5500 个符号的完整行，
# 无界字典在长驻服务里只增不减（行本身还要按日期窗口裁剪使用）。
RECENT_SUCCESS_CACHE_MAX_SYMBOLS = 512
# 东财变体矩阵的**组合**上限（一个组合 = 一个端点 × 参数变体 × header，其下可跑
# 多个传输）。组合矩阵最坏 2×2×2 = 8 个组合 × 传输数，每次 timeout 秒；不封顶时
# 403/429 这类"连得上但拿不到数据"的失败会让单票卡满全部组合。
# 上限按组合而不是按传输计数：按传输计数时，双传输生产形态会把预算全花在第一个
# 端点，`push2 kline` 备用端点永远轮不到（注入单传输的用例发现不了这个退化）。
EASTMONEY_VARIANT_MAX_COMBINATIONS = 6
FAILED_SYMBOL_RETRY_ROUNDS = 2
FAILED_SYMBOL_RETRY_BACKOFF_SECONDS = 2.0

FIELDS1 = "f1,f2,f3,f7"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"

JsonGetter = Callable[[str, dict[str, str], dict[str, str], int], dict[str, Any]]
JsonGetterVariants = tuple[tuple[str, JsonGetter], ...]

_HEADER_VARIANTS = (
    {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
        "Referer": "https://data.eastmoney.com/zjlx/detail.html",
        "Origin": "https://data.eastmoney.com",
    },
    {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
        "Referer": "https://quote.eastmoney.com/",
        "Origin": "https://quote.eastmoney.com",
    },
)

_PARAM_VARIANTS = (
    {},
    {"ut": EASTMONEY_UT},
)

_ENDPOINT_VARIANTS = (
    {"url": EASTMONEY_FUND_FLOW_URL, "label": "daykline", "params": {}},
    {"url": EASTMONEY_FUND_FLOW_KLINE_URL, "label": "kline", "params": {"klt": "101"}},
)

_BAIDU_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://finance.pae.baidu.com/",
}

_SINA_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://money.finance.sina.com.cn/moneyflow/",
}

_FLOW_COLUMNS = (
    "trade_date",
    "main_net_inflow",
    "small_net_inflow",
    "medium_net_inflow",
    "large_net_inflow",
    "super_large_net_inflow",
    "main_net_inflow_pct",
    "small_net_inflow_pct",
    "medium_net_inflow_pct",
    "large_net_inflow_pct",
    "super_large_net_inflow_pct",
    "close",
    "change_pct",
)

_FLOW_KLINE_COLUMNS = (
    "trade_date",
    "main_net_inflow",
    "small_net_inflow",
    "medium_net_inflow",
    "large_net_inflow",
    "super_large_net_inflow",
)


class CapitalFlowFetchError(RuntimeError):
    def __init__(self, message: str, *, code: str = "network_error") -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _parse_date(value: Any) -> str | None:
    text = str(value or "").strip()
    for fmt, width in (("%Y-%m-%d", 10), ("%Y/%m/%d", 10), ("%Y%m%d", 8)):
        candidate = text[:width]
        try:
            return datetime.strptime(candidate, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _estimate_limit(start_date: str, end_date: str) -> int:
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    days = max((end - start).days + 1, 1)
    return max(5, int(days * 1.3))


_thread_local = thread_local()


def _get_thread_session() -> requests.Session:
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = create_scraping_session()
        _thread_local.session = session
    return session


def _default_json_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    session = _get_thread_session()
    session.trust_env = False
    response = session.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return _loads_eastmoney_json(response.text)


def _default_sina_json_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> Any:
    session = _get_thread_session()
    response = session.get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return json.loads(response.content.decode("gbk", errors="ignore"))


def _curl_cffi_json_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    try:
        from curl_cffi import requests as curl_requests
    except Exception as exc:
        raise CapitalFlowFetchError(f"curl_cffi is not available: {exc}", code="transport_unavailable") from exc

    response = curl_requests.get(
        url,
        params=params,
        headers=headers,
        timeout=timeout,
        impersonate="chrome124",
        # 非 ASCII 用户目录下 libcurl 装不上 certifi 的 CA（curl error 77），
        # 显式传入可加载的 ASCII CA 路径，否则整个备用传输在本机会静默失效。
        **curl_verify_kwargs(),
    )
    response.raise_for_status()
    return _loads_eastmoney_json(response.text)


def _loads_eastmoney_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("(")
        end = stripped.rfind(")")
        if start < 0 or end <= start:
            raise
        payload = json.loads(stripped[start + 1 : end])
    if not isinstance(payload, dict):
        raise CapitalFlowFetchError("Eastmoney response is not a JSON object", code="malformed_payload")
    return payload


class CapitalFlowCrawler:
    """Standalone Eastmoney capital-flow crawler for service-level cache backfills."""

    def __init__(
        self,
        json_get: JsonGetter | None = None,
        baidu_json_get: JsonGetter | None = None,
        sina_json_get: JsonGetter | None = None,
        eastmoney_json_getters: JsonGetterVariants | None = None,
    ) -> None:
        if eastmoney_json_getters is not None:
            self._eastmoney_json_getters = eastmoney_json_getters
        elif json_get is None:
            self._eastmoney_json_getters: JsonGetterVariants = (
                ("requests", _default_json_get),
                ("curl_cffi", _curl_cffi_json_get),
            )
        else:
            self._eastmoney_json_getters = (("injected", json_get),)
        self._baidu_json_get = baidu_json_get or _default_json_get
        self._sina_json_get = sina_json_get or _default_sina_json_get
        self._enable_baidu_fallback = baidu_json_get is not None or json_get is None
        self._enable_sina_fallback = sina_json_get is not None or (
            json_get is None and eastmoney_json_getters is None
        )
        self._recent_success_rows: dict[str, list[dict[str, Any]]] = {}
        self._recent_success_lock = Lock()
        self._sina_request_lock = Lock()
        self._sina_next_allowed_at = 0.0

    def fetch_fund_flow(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        limit: int | None = None,
        timeout: int = 15,
    ) -> list[dict[str, Any]]:
        rows, _diagnostics = self._fetch_fund_flow_with_diagnostics(
            symbol,
            start_date,
            end_date,
            limit=limit,
            timeout=timeout,
        )
        return rows

    def _fetch_fund_flow_with_diagnostics(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
        *,
        limit: int | None = None,
        timeout: int = 15,
        skip_eastmoney: bool = False,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        code = normalize_symbol(symbol)
        request_limit = limit if limit is not None else _estimate_limit(start_date, end_date)
        params = {
            "secid": f"{market_code(code)}.{code}",
            "fields1": FIELDS1,
            "fields2": FIELDS2,
            "klt": "101",
            "lmt": str(request_limit),
        }
        diagnostics: list[dict[str, Any]] = []
        if skip_eastmoney and self._enable_baidu_fallback:
            diagnostics.append(
                {
                    "symbol": code,
                    "code": "provider_attempt_skipped",
                    "provider": "eastmoney",
                    "source": "capital_flow_crawler",
                    "message": "Eastmoney capital-flow provider was skipped after repeated disconnects in this batch.",
                }
            )
            rows, provider_diagnostics, provider = self._fetch_fallback_rows(code, start_date, end_date, timeout)
            diagnostics.extend(provider_diagnostics)
            diagnostics.append(_fallback_used_diagnostic(code, provider, len(rows)))
            self._remember_success_rows(code, rows)
            return rows, diagnostics
        try:
            payload = self._fetch_payload_with_variants(code, params, timeout)
            rows, eastmoney_diagnostics = _parse_payload(code, payload, start_date, end_date)
            diagnostics.extend(eastmoney_diagnostics)
            if self._should_try_baidu_fallback(rows, eastmoney_diagnostics):
                diagnostics.append(
                    {
                        "symbol": code,
                        "code": "provider_attempt_incomplete",
                        "provider": "eastmoney",
                        "source": "capital_flow_crawler",
                        "rows": len(rows),
                        "message": (
                            "Eastmoney capital-flow response did not fully cover the requested date range; "
                            "trying Baidu history fallback."
                        ),
                    }
                )
                fallback_rows, fallback_diagnostics, provider = self._fetch_fallback_rows(code, start_date, end_date, timeout)
                diagnostics.extend(fallback_diagnostics)
                diagnostics.append(_fallback_used_diagnostic(code, provider, len(fallback_rows)))
                self._remember_success_rows(code, fallback_rows)
                return fallback_rows, diagnostics
        except CapitalFlowFetchError as eastmoney_error:
            if not (self._enable_sina_fallback or self._enable_baidu_fallback):
                raise
            diagnostics.append(_provider_failed_diagnostic(code, "eastmoney", eastmoney_error))
            try:
                rows, fallback_diagnostics, provider = self._fetch_fallback_rows(code, start_date, end_date, timeout)
            except CapitalFlowFetchError as fallback_error:
                diagnostics.append(_provider_failed_diagnostic(code, "fallback", fallback_error))
                raise CapitalFlowFetchError(
                    f"Failed to fetch capital flow for {code}: eastmoney={eastmoney_error}; fallback={fallback_error}",
                    code="network_error",
                ) from fallback_error
            diagnostics.extend(fallback_diagnostics)
            diagnostics.append(_fallback_used_diagnostic(code, provider, len(rows)))
        self._remember_success_rows(code, rows)
        return rows, diagnostics

    def _fetch_payload_with_variants(self, code: str, params: dict[str, str], timeout: int) -> dict[str, Any]:
        """Try the endpoint × param × header matrix under a **total** budget.

        矩阵最坏有 ``2 端点 × 2 参数变体 × 2 header × 2 传输 = 16`` 次请求，每次
        ``timeout`` 秒。原有的两条早退守卫只对**网络层**错误生效（远端断连、
        全传输超时），遇到 403/429/空 klines 这类"连得上但拿不到数据"的失败会
        老老实实走满全部组合——全市场补齐时单票最坏 8 组合 × 传输数 × timeout。这里给
        矩阵加一个**组合**上限 :data:`EASTMONEY_VARIANT_MAX_COMBINATIONS`：达到上限立刻
        收手转兜底源，并留下 ``provider_variant_budget_exhausted`` 诊断。

        上限按组合（端点 × 参数 × header）计数而不是按传输计数，这样"备用端点还被
        轮到过"是有保证的：按传输计数时双传输形态会把预算全花在第一个端点，
        `push2 kline` 备用端点永远没机会试（注入单传输的用例发现不了这个退化）。
        用次数而不是墙钟 deadline 是有意的：上限确定、可断言（不依赖时钟行为），
        效果同样是"最坏墙钟有界"。
        """
        errors: list[str] = []
        combinations = 0
        for endpoint in _ENDPOINT_VARIANTS:
            endpoint_params = dict(endpoint["params"])
            for extra_params in _PARAM_VARIANTS:
                request_params = {**params, **endpoint_params, **extra_params}
                variant_label = "base" if not extra_params else ",".join(sorted(extra_params))
                for header_index, headers in enumerate(_HEADER_VARIANTS):
                    if combinations >= EASTMONEY_VARIANT_MAX_COMBINATIONS:
                        errors.append(
                            f"{endpoint['label']} [{variant_label}]: variant combination cap "
                            f"({EASTMONEY_VARIANT_MAX_COMBINATIONS}) reached, skipping remaining combinations"
                        )
                        raise CapitalFlowFetchError(
                            f"Failed to fetch Eastmoney capital flow for {code}: {'; '.join(errors)}",
                            code="network_error",
                        )
                    combinations += 1
                    variant_remote_disconnect = False
                    transport_attempts = 0
                    network_layer_failures = 0
                    for transport_label, json_get in self._eastmoney_json_getters:
                        try:
                            return json_get(str(endpoint["url"]), request_params, headers, timeout)
                        except Exception as exc:
                            transport_attempts += 1
                            label = "" if transport_label == "injected" else f"{transport_label} "
                            errors.append(
                                f"{endpoint['label']} {label}{headers['Referer']} [{variant_label}]: {exc}"
                            )
                            if _is_remote_disconnect(exc):
                                variant_remote_disconnect = True
                            if _is_network_layer_failure(exc):
                                network_layer_failures += 1
                    if variant_remote_disconnect:
                        raise CapitalFlowFetchError(
                            f"Failed to fetch Eastmoney capital flow for {code}: {'; '.join(errors)}"
                        )
                    if (
                        not extra_params
                        and header_index == 0
                        and transport_attempts > 0
                        and network_layer_failures == transport_attempts
                    ):
                        raise CapitalFlowFetchError(
                            f"Failed to fetch Eastmoney capital flow for {code}: endpoint {endpoint['label']} "
                            f"base variant failed with network-layer errors on every transport: {'; '.join(errors)}"
                        )
        raise CapitalFlowFetchError(f"Failed to fetch Eastmoney capital flow for {code}: {'; '.join(errors)}")

    def _should_try_baidu_fallback(self, rows: list[dict[str, Any]], diagnostics: list[dict[str, Any]]) -> bool:
        if not (self._enable_sina_fallback or self._enable_baidu_fallback):
            return False
        if not rows:
            return True
        return any(item.get("code") == "date_coverage_shortfall" for item in diagnostics)

    def _fetch_fallback_rows(
        self,
        code: str,
        start_date: str,
        end_date: str,
        timeout: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        diagnostics: list[dict[str, Any]] = []
        if self._enable_sina_fallback:
            try:
                rows, sina_diagnostics = self._fetch_sina_history_rows(code, start_date, end_date, timeout)
                rows, supplement_diagnostics = self._supplement_missing_rows_with_baidu(
                    code,
                    rows,
                    start_date,
                    end_date,
                    timeout,
                )
                sina_diagnostics.extend(supplement_diagnostics)
                return rows, [*diagnostics, *sina_diagnostics], "sina"
            except CapitalFlowFetchError as exc:
                diagnostics.append(_provider_failed_diagnostic(code, "sina", exc))
        if self._enable_baidu_fallback:
            rows, baidu_diagnostics = self._fetch_baidu_history_rows(code, start_date, end_date, timeout)
            return rows, [*diagnostics, *baidu_diagnostics], "baidu"
        raise CapitalFlowFetchError(f"No enabled fallback provider for {code}", code="provider_unavailable")

    def _fetch_sina_history_rows(
        self,
        code: str,
        start_date: str,
        end_date: str,
        timeout: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        params = {
            "page": "1",
            "num": str(SINA_PAGE_SIZE),
            "sort": "opendate",
            "asc": "0",
            "daima": a_share_market_symbol(code),
        }
        retry_diagnostics: list[dict[str, Any]] = []
        payload = self._fetch_sina_payload_with_retry(code, params, timeout, retry_diagnostics)
        rows, parse_diagnostics = _parse_sina_payload(code, payload, start_date, end_date)
        diagnostics = [*retry_diagnostics, *parse_diagnostics]
        if not rows:
            raise CapitalFlowFetchError(f"Sina payload returned no rows for {code}", code="empty_klines")
        diagnostics.extend(_coverage_diagnostics(code, rows, start_date, end_date, provider="sina"))
        return rows, diagnostics

    def _fetch_sina_payload_with_retry(
        self,
        code: str,
        params: dict[str, str],
        timeout: int,
        diagnostics: list[dict[str, Any]],
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, SINA_PAGE_RETRIES + 1):
            try:
                self._wait_for_sina_request_slot()
                return self._sina_json_get(SINA_FUND_FLOW_URL, params, _SINA_HEADERS, timeout)
            except Exception as exc:
                last_error = exc
                if attempt >= SINA_PAGE_RETRIES:
                    break
                sleep_seconds = SINA_PAGE_BACKOFF_SECONDS * attempt
                diagnostics.append(
                    {
                        "symbol": code,
                        "code": "provider_page_retry",
                        "provider": "sina",
                        "source": "capital_flow_crawler",
                        "attempt": attempt,
                        "sleep_seconds": sleep_seconds,
                        "error": str(exc),
                        "message": f"Sina capital-flow page failed; retrying after {sleep_seconds:g}s.",
                    }
                )
                time.sleep(sleep_seconds)
        raise CapitalFlowFetchError(
            f"Sina page fetch failed for {code}: {last_error}",
            code="network_error" if _is_retryable_provider_error(last_error) else "parse_error",
        ) from last_error

    def _wait_for_sina_request_slot(self) -> None:
        """Reserve this host's next slot, then sleep **outside** the lock.

        持锁睡眠会把并发 worker 全部串行化在 ``SINA_REQUEST_INTERVAL_SECONDS`` 的
        间隔上（8 worker × 5500 只 ≈ 23 分钟纯睡眠）；锁内只做"读—算—写"，
        把下一个可用时刻预留出去，``sleep`` 移到锁外——与
        :class:`~astock_backtester.data.http_transport.HostThrottle` 同一纪律。
        """
        with self._sina_request_lock:
            now = time.monotonic()
            slot = max(now, self._sina_next_allowed_at)
            self._sina_next_allowed_at = slot + SINA_REQUEST_INTERVAL_SECONDS
            delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def _supplement_missing_rows_with_baidu(
        self,
        code: str,
        rows: list[dict[str, Any]],
        start_date: str,
        end_date: str,
        timeout: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not self._enable_baidu_fallback or not rows:
            return rows, []
        existing_dates = {str(row.get("trade_date")) for row in rows if row.get("trade_date")}
        if not existing_dates:
            return rows, []
        first_existing = max(start_date, min(existing_dates))
        last_existing = min(end_date, max(existing_dates))
        expected_dates = {
            item.date().isoformat()
            for item in a_share_trade_dates(pd.Timestamp(first_existing), pd.Timestamp(last_existing))
        }
        missing_dates = expected_dates - existing_dates
        if not missing_dates:
            return rows, []
        # 每个缺失日原本各发一轮百度分页（内层最多 500 页）：长窗口 + 新浪大面积
        # 缺日时退化成 N×500 次请求，单票就能把整批拖死。这里按"最近 N 个缺失日"
        # 封顶，超出的写 ``date_coverage_shortfall``——宁可明确报告欠覆盖，也不
        # 静默把批次跑到天亮。
        ordered_missing = sorted(missing_dates)
        capped_missing = ordered_missing[-BAIDU_SUPPLEMENT_MAX_MISSING_DATES:]
        skipped_missing = len(ordered_missing) - len(capped_missing)
        baidu_rows: list[dict[str, Any]] = []
        baidu_diagnostics: list[dict[str, Any]] = []
        if skipped_missing:
            baidu_diagnostics.append(
                {
                    "symbol": code,
                    "code": "date_coverage_shortfall",
                    "provider": "baidu",
                    "source": "capital_flow_crawler",
                    "missing_dates": skipped_missing,
                    "message": (
                        "Capital-flow crawler capped the Baidu supplement to the most recent "
                        f"{BAIDU_SUPPLEMENT_MAX_MISSING_DATES} missing dates; "
                        f"{skipped_missing} older missing dates stay unfilled."
                    ),
                }
            )
        for missing_date in capped_missing:
            try:
                window_start, window_end = _date_window(missing_date, before_days=2, after_days=2)
                next_rows, next_diagnostics = self._fetch_baidu_history_rows(
                    code,
                    window_start,
                    window_end,
                    timeout,
                )
                baidu_rows.extend(next_rows)
                baidu_diagnostics.extend(next_diagnostics)
            except CapitalFlowFetchError as exc:
                baidu_diagnostics.append(_provider_failed_diagnostic(code, "baidu", exc))
        supplement_rows = [
            row
            for row in baidu_rows
            if str(row.get("trade_date")) in missing_dates
            and row.get("main_net_inflow") is not None
        ]
        if not supplement_rows:
            return rows, baidu_diagnostics
        merged_by_date = {str(row.get("trade_date")): dict(row) for row in rows}
        for row in supplement_rows:
            merged_by_date[str(row.get("trade_date"))] = dict(row)
        diagnostics = [
            *baidu_diagnostics,
            {
                "symbol": code,
                "code": "provider_supplement_used",
                "provider": "baidu",
                "source": "capital_flow_crawler",
                "rows": len(supplement_rows),
                "message": "Capital-flow crawler supplemented missing Sina dates with Baidu history rows.",
            },
        ]
        return list(merged_by_date.values()), diagnostics

    def _fetch_baidu_history_rows(
        self,
        code: str,
        start_date: str,
        end_date: str,
        timeout: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        start = _iso_to_date(start_date)
        cursor = _iso_to_date(end_date) + timedelta(days=3)
        rows: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        seen_trade_dates: set[str] = set()
        seen_cursors: set[str] = set()
        for _ in range(500):
            cursor_text = cursor.strftime("%Y%m%d")
            if cursor_text in seen_cursors:
                break
            seen_cursors.add(cursor_text)
            content = self._fetch_baidu_page_content_with_retry(code, cursor_text, timeout, diagnostics)
            page_rows, page_diagnostics = _parse_baidu_content(code, content, start_date, end_date)
            diagnostics.extend(page_diagnostics)
            for row in page_rows:
                trade_date = str(row["trade_date"])
                if trade_date not in seen_trade_dates:
                    rows.append(row)
                    seen_trade_dates.add(trade_date)
            last_trade_date = _last_baidu_trade_date(content)
            if last_trade_date is None:
                diagnostics.append(
                    {
                        "symbol": code,
                        "code": "malformed_row",
                        "provider": "baidu",
                        "message": "Baidu capital-flow page did not contain a valid trade_date cursor",
                    }
                )
                break
            if last_trade_date <= start:
                break
            cursor = last_trade_date - timedelta(days=1)
            time.sleep(BAIDU_PAGE_SLEEP_SECONDS)
        if not rows:
            raise CapitalFlowFetchError(f"Baidu payload returned no rows for {code}", code="empty_klines")
        diagnostics.extend(_coverage_diagnostics(code, rows, start_date, end_date))
        return rows, diagnostics

    def _fetch_baidu_page_content_with_retry(
        self,
        code: str,
        cursor_text: str,
        timeout: int,
        diagnostics: list[dict[str, Any]],
    ) -> list[Any]:
        params = {
            "code": code,
            "market": "ab",
            "finance_type": "stock",
            "tab": "day",
            "from": "history",
            "date": cursor_text,
            "pn": "0",
            "rn": "20",
            "finClientType": "pc",
        }
        last_error: Exception | None = None
        for attempt in range(1, BAIDU_PAGE_RETRIES + 1):
            try:
                payload = self._baidu_json_get(BAIDU_FUND_FLOW_URL, params, _BAIDU_HEADERS, timeout)
                return _baidu_content(code, payload)
            except Exception as exc:
                last_error = exc
                if attempt >= BAIDU_PAGE_RETRIES:
                    break
                sleep_seconds = BAIDU_PAGE_BACKOFF_SECONDS * attempt
                diagnostics.append(
                    {
                        "symbol": code,
                        "code": "provider_page_retry",
                        "provider": "baidu",
                        "source": "capital_flow_crawler",
                        "attempt": attempt,
                        "cursor": cursor_text,
                        "sleep_seconds": sleep_seconds,
                        "error": str(exc),
                        "message": f"Baidu capital-flow page failed; retrying after {sleep_seconds:g}s.",
                    }
                )
                time.sleep(sleep_seconds)
        if isinstance(last_error, CapitalFlowFetchError):
            raise last_error
        raise CapitalFlowFetchError(
            f"Baidu page fetch failed for {code} at cursor {cursor_text}: {last_error}",
            code="network_error",
        ) from last_error

    def fetch_many_fund_flows(
        self,
        symbols: list[str],
        start_date: str,
        end_date: str,
        *,
        limit: int | None = None,
        timeout: int = 15,
        skip_eastmoney: bool = False,
        max_workers: int = DEFAULT_MAX_WORKERS,
    ) -> dict[str, list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []

        def fetch_one(symbol: str, should_skip_eastmoney: bool) -> dict[str, Any]:
            code = normalize_symbol(symbol)
            try:
                next_rows, next_diagnostics = self._fetch_fund_flow_with_diagnostics(
                    code,
                    start_date,
                    end_date,
                    limit=limit,
                    timeout=timeout,
                    skip_eastmoney=should_skip_eastmoney,
                )
                return {
                    "symbol": code,
                    "rows": next_rows,
                    "failures": [],
                    "diagnostics": next_diagnostics,
                    "skip_eastmoney": diagnostics_should_skip_eastmoney(next_diagnostics),
                }
            except CapitalFlowFetchError as exc:
                failure = {"symbol": code, "code": exc.code, "error": str(exc)}
                next_rows: list[dict[str, Any]] = []
                next_diagnostics = [{**failure, "message": str(exc)}]
                cached_rows = (
                    self._recent_success_rows_for_range(code, start_date, end_date)
                    if exc.code == "network_error"
                    else []
                )
                if cached_rows:
                    next_rows.extend(cached_rows)
                    next_diagnostics.append(
                        {
                            "symbol": code,
                            "code": "recent_success_cache_used",
                            "source": "capital_flow_crawler",
                            "cached_rows": len(cached_rows),
                            "start_date": start_date,
                            "end_date": end_date,
                            "message": "Capital-flow crawler reused recent successful rows after source failure",
                        }
                    )
                return {
                    "symbol": code,
                    "rows": next_rows,
                    "failures": [failure],
                    "diagnostics": next_diagnostics,
                    "skip_eastmoney": exc.code == "network_error",
                }
            except Exception as exc:
                failure = {"symbol": code, "code": "parse_error", "error": str(exc)}
                return {
                    "symbol": code,
                    "rows": [],
                    "failures": [failure],
                    "diagnostics": [{**failure, "message": str(exc)}],
                    "skip_eastmoney": False,
                }

        codes = [normalize_symbol(symbol) for symbol in symbols]
        if not codes:
            return {"rows": rows, "failures": failures, "diagnostics": diagnostics}

        # Serial fetch for first symbol to determine eastmoney skip for the batch
        ordered_results: list[dict[str, Any] | None] = [None] * len(codes)
        first_result = fetch_one(codes[0], skip_eastmoney)
        ordered_results[0] = first_result
        skip_eastmoney_for_rest = bool(skip_eastmoney or first_result["skip_eastmoney"])

        # Parallel fetch for remaining symbols
        remaining = codes[1:]
        worker_count = max(1, min(max_workers, len(remaining)))
        if remaining:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = {
                    executor.submit(fetch_one, code, skip_eastmoney_for_rest): index
                    for index, code in enumerate(remaining, start=1)
                }
                for future in as_completed(futures):
                    ordered_results[futures[future]] = future.result()

        # Collect results and identify failed symbols for retry
        for result in ordered_results:
            if not result:
                continue
            rows.extend(result["rows"])
            failures.extend(result["failures"])
            diagnostics.extend(result["diagnostics"])

        # Retry failed symbols with backoff (skip if cached rows were already used)
        failed_symbols = {str(f["symbol"]) for f in failures if f.get("symbol")}
        symbols_with_cached_rows = {
            str(d["symbol"]) for d in diagnostics
            if d.get("code") == "recent_success_cache_used" and d.get("symbol")
        }
        retryable_failures = failed_symbols - symbols_with_cached_rows
        if retryable_failures and FAILED_SYMBOL_RETRY_ROUNDS > 0:
            for retry_round in range(FAILED_SYMBOL_RETRY_ROUNDS):
                remaining_failed = [s for s in codes if normalize_symbol(s) in retryable_failures]
                if not remaining_failed:
                    break
                time.sleep(FAILED_SYMBOL_RETRY_BACKOFF_SECONDS * (retry_round + 1))
                # Determine skip_eastmoney based on failure pattern
                network_failures = {
                    str(f["symbol"]) for f in failures
                    if f.get("code") == "network_error" and f.get("symbol")
                }
                round_skip = len(network_failures) > len(remaining_failed) / 2
                retry_results: list[dict[str, Any]] = []
                with ThreadPoolExecutor(max_workers=max(1, min(worker_count, len(remaining_failed)))) as executor:
                    futures = {
                        executor.submit(fetch_one, code, round_skip): code
                        for code in remaining_failed
                    }
                    for future in as_completed(futures):
                        retry_results.append(future.result())

                # Remove old failures for retried symbols and add new results
                retried_codes = {normalize_symbol(s) for s in remaining_failed}
                failures = [f for f in failures if str(f.get("symbol")) not in retried_codes]
                diagnostics = [
                    d for d in diagnostics
                    if not (str(d.get("symbol")) in retried_codes and d.get("code") in ("network_error", "provider_attempt_failed"))
                ]
                for result in retry_results:
                    rows.extend(result["rows"])
                    failures.extend(result["failures"])
                    diagnostics.extend(result["diagnostics"])
                    if not result["failures"]:
                        retryable_failures.discard(result["symbol"])

        return {"rows": rows, "failures": failures, "diagnostics": diagnostics}

    def _remember_success_rows(self, code: str, rows: list[dict[str, Any]]) -> None:
        """Remember this symbol's rows for the same-batch fallback.

        缓存只服务"本次请求失败时用最近成功行兜底"，因此按符号数封顶：全市场
        补齐会留下约 5500 个符号的全部行，无界字典在长驻 sidecar 里只增不减。
        超出上限时淘汰最久未更新的符号（``dict`` 保序，等价 FIFO）。
        """
        if not rows:
            return
        with self._recent_success_lock:
            # pop + 重新插入 = 把这个符号挪到"最新使用"，超限时从最旧的一端淘汰
            # （dict 保序，等价 FIFO/LRU）。
            self._recent_success_rows.pop(code, None)
            self._recent_success_rows[code] = [dict(row) for row in rows]
            while len(self._recent_success_rows) > RECENT_SUCCESS_CACHE_MAX_SYMBOLS:
                self._recent_success_rows.pop(next(iter(self._recent_success_rows)))

    def _recent_success_rows_for_range(self, code: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        with self._recent_success_lock:
            cached = [dict(row) for row in self._recent_success_rows.get(code, [])]
        return [
            dict(row)
            for row in cached
            if start_date <= str(row.get("trade_date", "")) <= end_date
        ]


def _iso_to_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def _date_window(value: str, *, before_days: int, after_days: int) -> tuple[str, str]:
    current = _iso_to_date(value)
    return (
        (current - timedelta(days=before_days)).isoformat(),
        (current + timedelta(days=after_days)).isoformat(),
    )


def _provider_failed_diagnostic(code: str, provider: str, exc: CapitalFlowFetchError) -> dict[str, Any]:
    return {
        "symbol": code,
        "code": "provider_attempt_failed",
        "provider": provider,
        "source": "capital_flow_crawler",
        "error_code": exc.code,
        "error": str(exc),
        "message": str(exc),
    }


def _fallback_used_diagnostic(code: str, provider: str, rows: int) -> dict[str, Any]:
    return {
        "symbol": code,
        "code": "provider_fallback_used",
        "provider": provider,
        "source": "capital_flow_crawler",
        "rows": rows,
        "message": f"Capital-flow crawler used {provider} fallback rows after primary provider failed or under-covered.",
    }


def _is_remote_disconnect(exc: Exception) -> bool:
    text = repr(exc).lower()
    return (
        "remotedisconnected" in text
        or "remote end closed connection" in text
        or "connection closed abruptly" in text
    )


def _is_network_layer_failure(exc: Exception | None) -> bool:
    if exc is None:
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, requests.Timeout, requests.ConnectionError)):
        return True
    text = repr(exc).lower()
    return any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "connectionerror",
            "connection refused",
            "connection reset",
            "connection aborted",
            "connection closed abruptly",
            "max retries exceeded",
            "getaddrinfo",
            "name or service not known",
            "temporary failure in name resolution",
            "network is unreachable",
            "no route to host",
        )
    )


def _is_retryable_provider_error(exc: Exception | None) -> bool:
    text = repr(exc).lower()
    return any(
        marker in text
        for marker in (
            "456",
            "429",
            "500",
            "502",
            "503",
            "504",
            "timeout",
            "timed out",
            "connection",
            "remote",
        )
    )


def diagnostics_should_skip_eastmoney(diagnostics: list[dict[str, Any]]) -> bool:
    """本批资金流请求是否应跳过东财、直接走兜底源。

    判定依据是 crawler 自己产出的诊断形状（``provider_attempt_failed`` +
    ``provider=eastmoney`` + ``error_code=network_error``）。这是**唯一**实现：
    ``sync`` 与 ``scripts/run-capital-flow-backfill.py`` 都从这里导入，避免同一
    谓词在三处漂移（判据一改，三处必须同时改的那种 bug）。
    """
    return any(
        isinstance(item, dict)
        and item.get("code") == "provider_attempt_failed"
        and item.get("provider") == "eastmoney"
        and item.get("error_code") == "network_error"
        for item in diagnostics
    )


def _baidu_content(code: str, payload: dict[str, Any]) -> list[Any]:
    result = payload.get("Result") if isinstance(payload, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        raise CapitalFlowFetchError(f"Baidu payload missing content for {code}", code="empty_payload")
    return content


def _parse_baidu_content(
    code: str,
    content: list[Any],
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            diagnostics.append(
                {
                    "symbol": code,
                    "code": "malformed_row",
                    "provider": "baidu",
                    "message": "Baidu capital-flow row is not a JSON object",
                }
            )
            continue
        trade_date = _parse_date(item.get("date") or item.get("showtime") or item.get("time"))
        if trade_date is None:
            diagnostics.append(
                {
                    "symbol": code,
                    "code": "malformed_row",
                    "provider": "baidu",
                    "raw_date": str(item.get("date") or item.get("showtime") or item.get("time")),
                    "message": "Baidu capital-flow row has invalid trade_date",
                }
            )
            continue
        if not (start_date <= trade_date <= end_date):
            continue
        row = {
            "symbol": code,
            "trade_date": trade_date,
            # 单位统一为元：百度带 "万/亿" 后缀，与东财 f52、新浪 netamount 同值。
            "main_net_inflow": parse_money_amount(item.get("extMainIn")),
            "small_net_inflow": parse_money_amount(item.get("littleNetIn")),
            "medium_net_inflow": parse_money_amount(item.get("mediumNetIn")),
            "large_net_inflow": parse_money_amount(item.get("largeNetIn")),
            "super_large_net_inflow": parse_money_amount(item.get("superNetIn")),
            "main_net_inflow_pct": parse_float(item.get("ratio")),
            "close": parse_float(item.get("closepx")),
        }
        rows.append(row)
    return rows, diagnostics


def _parse_sina_payload(
    code: str,
    payload: Any,
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(payload, list):
        raise CapitalFlowFetchError(f"Sina payload is not a list for {code}", code="malformed_payload")
    rows: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            diagnostics.append(
                {
                    "symbol": code,
                    "code": "malformed_row",
                    "provider": "sina",
                    "message": "Sina capital-flow row is not a JSON object",
                }
            )
            continue
        trade_date = _parse_date(item.get("opendate"))
        if trade_date is None:
            diagnostics.append(
                {
                    "symbol": code,
                    "code": "malformed_row",
                    "provider": "sina",
                    "raw_date": str(item.get("opendate")),
                    "message": "Sina capital-flow row has invalid trade_date",
                }
            )
            continue
        if not (start_date <= trade_date <= end_date):
            continue
        row = {
            "symbol": code,
            "trade_date": trade_date,
            "main_net_inflow": parse_float(item.get("netamount")),
            "small_net_inflow": parse_float(item.get("r3_net")),
            "medium_net_inflow": parse_float(item.get("r2_net")),
            "large_net_inflow": parse_float(item.get("r1_net")),
            "super_large_net_inflow": parse_float(item.get("r0_net")),
            "main_net_inflow_pct": _ratio_to_pct(item.get("ratioamount")),
            "close": parse_float(item.get("trade")),
            "change_pct": _ratio_to_pct(item.get("changeratio")),
        }
        rows.append(row)
    return rows, diagnostics


def _ratio_to_pct(value: Any) -> float | None:
    parsed = parse_float(value)
    if parsed is None:
        return None
    return parsed * 100.0


def _last_baidu_trade_date(content: list[Any]) -> date | None:
    for item in reversed(content):
        if not isinstance(item, dict):
            continue
        trade_date = _parse_date(item.get("date") or item.get("showtime") or item.get("time"))
        if trade_date is not None:
            return _iso_to_date(trade_date)
    return None


def _coverage_diagnostics(
    code: str,
    rows: list[dict[str, Any]],
    start_date: str,
    end_date: str,
    provider: str | None = None,
) -> list[dict[str, Any]]:
    if not rows:
        diagnostic = {
            "symbol": code,
            "code": "date_coverage_shortfall",
            "start_date": start_date,
            "end_date": end_date,
            "message": f"Capital-flow crawler returned no rows for requested {start_date} to {end_date}",
        }
        if provider:
            diagnostic["provider"] = provider
        return [diagnostic]
    first = min(str(row["trade_date"]) for row in rows)
    last = max(str(row["trade_date"]) for row in rows)
    if first <= start_date and last >= end_date:
        return []
    diagnostic = {
        "symbol": code,
        "code": "date_coverage_shortfall",
        "start_date": start_date,
        "end_date": end_date,
        "first_trade_date": first,
        "last_trade_date": last,
        "message": f"Capital-flow crawler returned {first} to {last} for requested {start_date} to {end_date}",
    }
    if provider:
        diagnostic["provider"] = provider
    return [diagnostic]


def _parse_payload(
    code: str,
    payload: dict[str, Any],
    start_date: str,
    end_date: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise CapitalFlowFetchError(f"Eastmoney payload missing data for {code}", code="empty_payload")
    klines = data.get("klines", [])
    if not isinstance(klines, list) or not klines:
        raise CapitalFlowFetchError(f"Eastmoney payload has no klines for {code}", code="empty_klines")
    rows = []
    diagnostics: list[dict[str, Any]] = []
    for line in klines:
        row, row_diagnostics = _parse_kline(code, line)
        diagnostics.extend(row_diagnostics)
        if row is None:
            continue
        trade_date = str(row["trade_date"])
        if start_date <= trade_date <= end_date:
            rows.append(row)
    if rows:
        first = min(str(row["trade_date"]) for row in rows)
        last = max(str(row["trade_date"]) for row in rows)
        if first > start_date or last < end_date:
            diagnostics.append(
                {
                    "symbol": code,
                    "code": "date_coverage_shortfall",
                    "start_date": start_date,
                    "end_date": end_date,
                    "first_trade_date": first,
                    "last_trade_date": last,
                    "message": f"Capital-flow crawler returned {first} to {last} for requested {start_date} to {end_date}",
                }
            )
    else:
        diagnostics.append(
            {
                "symbol": code,
                "code": "date_coverage_shortfall",
                "start_date": start_date,
                "end_date": end_date,
                "message": f"Capital-flow crawler returned no rows for requested {start_date} to {end_date}",
            }
        )
    return rows, diagnostics


def _parse_kline(code: str, line: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    parts = str(line).split(",")
    columns = _FLOW_COLUMNS if len(parts) >= len(_FLOW_COLUMNS) else _FLOW_KLINE_COLUMNS
    if len(parts) < len(columns):
        return None, [
            {
                "symbol": code,
                "code": "malformed_row",
                "field_count": len(parts),
                "message": "Capital-flow kline row has too few fields",
            }
        ]
    trade_date = _parse_date(parts[0])
    if trade_date is None:
        return None, [
            {
                "symbol": code,
                "code": "malformed_row",
                "raw_date": str(parts[0]),
                "message": "Capital-flow kline row has invalid trade_date",
            }
        ]
    values: dict[str, Any] = {
        "symbol": code,
        "trade_date": trade_date,
    }
    for column, value in zip(columns[1:], parts[1:], strict=False):
        parsed = parse_float(value)
        if parsed is None and not is_blank_numeric(value):
            diagnostics.append(
                {
                    "symbol": code,
                    "code": "malformed_numeric",
                    "field": column,
                    "trade_date": trade_date,
                    "value": str(value),
                    "message": f"Capital-flow numeric field {column} could not be parsed",
                }
            )
        values[column] = parse_float(value)
    return values, diagnostics
