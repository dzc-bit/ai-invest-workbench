from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from astock_backtester.data.http_transport import (
    BROWSER_USER_AGENT,
    create_scraping_session,
    scraping_session,
)
from astock_backtester.data.http_transport import (
    USER_AGENT as UA,
)
from astock_backtester.data.importer import normalize_daily_bars
from astock_backtester.data.parsing import parse_float
from astock_backtester.data.symbols import a_share_market_symbol, market_code, normalize_symbol

logger = logging.getLogger(__name__)

# 公开 XHR 日 K（补缺主源）。百度通道对直连 IP 返回 403，且需要完整浏览器 UA；
# 腾讯/新浪这两条公开接口实测直连稳定，且价格口径与仓库一致（不复权）：
#   - 腾讯：volume 单位是手，需要 ×100 换成股；
#   - 新浪：volume 单位是股，且覆盖北交所（腾讯不提供 bj 段）。
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_KLINE_MAX_COUNT = 320
TENCENT_KLINE_HEADERS = {"Referer": "https://gu.qq.com/"}
TENCENT_VOLUME_LOT_SIZE = 100
SINA_KLINE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
)
SINA_KLINE_MAX_LENGTH = 1000
SINA_KLINE_HEADERS = {"Referer": "https://finance.sina.com.cn"}
# 计算涨跌幅的预热窗口：先取请求起点之前一小段，用来给首行提供前收盘价。
KLINE_WARMUP_DAYS = 10
# 东财资金流端点在部分网络下不可达；连续失败达到该次数后本批不再尝试，
# 避免每只股票都白等满超时（资金流有独立的 /fetch/capital-flow 补齐链路）。
EASTMONEY_ENRICHMENT_FAILURE_LIMIT = 3


class AStockDataUnavailable(RuntimeError):
    pass


DailyBarsFetcher = Callable[[Sequence[str], str, str], pd.DataFrame]
JsonGetter = Callable[[str, dict[str, str], dict[str, str], int], dict[str, Any]]
JsonGetterVariants = Sequence[tuple[str, JsonGetter]]


def _to_float(value: Any, default: float = 0.0) -> float:
    parsed = parse_float(value)
    if parsed is None:
        if value in (None, "", "-", "--"):
            return default
        raise ValueError(f"unparseable numeric value: {value!r}")
    return parsed


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text[:8], fmt).date()
        except ValueError:
            continue
    return None


def _kline_warmup_start(start_date: str) -> str:
    """Request a little extra history so the first requested row has a pre-close."""
    try:
        return (datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=KLINE_WARMUP_DAYS)).strftime("%Y-%m-%d")
    except ValueError:
        return start_date


def _sina_window_length(start_date: str, end_date: str) -> int:
    """Trading-day count needed to cover ``start_date..end_date``.

    ``datalen`` selects the newest N bars, so the window is sized from the
    calendar span with slack for weekends/holidays and suspensions, then capped.
    """
    try:
        span_days = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days
    except ValueError:
        return 120
    # ≈5/7 的日历日是交易日；再留 30% 余量覆盖停牌与节假日。
    return max(30, min(SINA_KLINE_MAX_LENGTH, int(span_days * 0.72 * 1.3) + 30))


def _sina_kline_rows(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        trade_date = _parse_date(item.get("day"))
        if trade_date is None:
            continue
        rows.append(
            {
                "trade_date": trade_date.isoformat(),
                "open": _to_float(item.get("open")),
                "high": _to_float(item.get("high")),
                "low": _to_float(item.get("low")),
                "close": _to_float(item.get("close")),
                # 新浪 volume 单位是股，与仓库口径一致，不需要换算。
                "volume": _to_float(item.get("volume")),
            }
        )
    return rows


def _tencent_kline_rows(payload: Any, market_symbol: str) -> list[dict[str, Any]]:
    """Parse Tencent's ``fqkline`` payload into unadjusted daily rows.

    ``day`` is the unadjusted series (``qfqday`` would rewrite historical prices,
    which the warehouse must not mix in). Each row is
    ``[date, open, close, high, low, volume_lots, ...]``; volume is converted from
    lots to shares so both HTTP sources agree with the warehouse convention.
    """
    if not isinstance(payload, dict):
        return []
    node = (payload.get("data") or {}).get(market_symbol)
    if not isinstance(node, dict):
        return []
    raw_rows = node.get("day")
    if not isinstance(raw_rows, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in raw_rows:
        if not isinstance(item, list) or len(item) < 6:
            continue
        head = item[:6]
        if not all(isinstance(value, str) for value in head):
            # 除权/除息提示是以 dict 形式追加在第 7 位；前 6 位不是字符串就不是行情行。
            continue
        trade_date = _parse_date(head[0])
        if trade_date is None:
            continue
        rows.append(
            {
                "trade_date": trade_date.isoformat(),
                "open": _to_float(head[1]),
                "close": _to_float(head[2]),
                "high": _to_float(head[3]),
                "low": _to_float(head[4]),
                "volume": _to_float(head[5]) * TENCENT_VOLUME_LOT_SIZE,
            }
        )
    return rows


def _with_derived_columns(rows: list[dict[str, Any]], code: str) -> pd.DataFrame:
    """Finish a source-agnostic row set: symbol, prev close, change, market cap."""
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows).sort_values("trade_date").drop_duplicates("trade_date").reset_index(drop=True)
    frame["symbol"] = code
    closes = frame["close"]
    previous = closes.shift(1)
    frame["pre_close"] = previous
    frame["change"] = (closes - previous).round(3)
    # 与仓库既有口径一致：change_pct 是百分数（-0.44 = -0.44%）。
    frame["change_pct"] = (frame["change"] / previous * 100).round(4)
    # 成交额单位是元；公开 XHR 不提供成交额，用均价估算会引入误差，故留空。
    frame["amount"] = float("nan")
    return frame


def _should_retry_baidu_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    result_code = str(payload.get("ResultCode") or "")
    result = payload.get("Result")
    return result_code in {"403", "429"} or result == []


def _default_json_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    response = scraping_session().get(url, params=params, headers=headers, timeout=timeout)
    response.raise_for_status()
    return json.loads(response.text)


def _curl_cffi_json_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    from curl_cffi import requests as curl_requests

    from astock_backtester.data.http_transport import curl_verify_kwargs

    response = curl_requests.get(
        url,
        params=params,
        headers=headers,
        timeout=timeout,
        impersonate="chrome124",
        **curl_verify_kwargs(),
    )
    response.raise_for_status()
    return json.loads(response.text)


def _default_text_get(url: str, headers: dict[str, str], timeout: int) -> str:
    """Fetch a GBK text payload (Tencent quote protocol) on the scraping session."""
    response = create_scraping_session().get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.content.decode("gbk", errors="replace")


def _unconfigured_json_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    raise RuntimeError("eastmoney transport not configured (test construction)")


class HttpAStockFetcher:
    """HTTP subset learned from simonlin1212/a-stock-data for daily backtest cache fills."""

    def __init__(
        self,
        json_get: JsonGetter | None = None,
        json_gets: JsonGetterVariants | None = None,
        public_json_get: JsonGetter | None = None,
        public_text_get: Callable[[str, dict[str, str], int], str] | None = None,
    ) -> None:
        # 传输注入规则（测试密闭性）：任何一条传输被注入，未注入的传输一律
        # 关闭，绝不允许"公开源恰好可达"时用例静默打到真实网络。
        real_network = json_get is None and json_gets is None and public_json_get is None and public_text_get is None
        if json_gets is not None:
            self._json_gets = tuple(json_gets)
        elif json_get is not None:
            self._json_gets = (("injected", json_get),)
        else:
            self._json_gets = (
                (("requests", _default_json_get), ("curl_cffi", _curl_cffi_json_get))
                if real_network
                else (("unconfigured", _unconfigured_json_get),)
            )
        # 公开 XHR 主源（腾讯/新浪日 K、腾讯报价）用独立传输：
        # - 生产构造（无注入）默认走真实网络；
        # - 注入式构造（测试）默认关闭，需要时显式传 public_json_get / public_text_get。
        self._public_json_get = (
            public_json_get
            if public_json_get is not None
            else (_default_json_get if real_network else None)
        )
        self._public_text_get = (
            public_text_get
            if public_text_get is not None
            else (_default_text_get if real_network else None)
        )
        self._flow_failures = 0
        self._info_failures = 0


    def fetch_daily_bars(self, symbols: Sequence[str], start_date: str, end_date: str) -> pd.DataFrame:
        frames = [self._fetch_one(symbol, start_date, end_date) for symbol in symbols]
        rows = [frame for frame in frames if not frame.empty]
        if not rows:
            return pd.DataFrame()
        return pd.concat(rows, ignore_index=True)

    def _fetch_one(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        code = normalize_symbol(symbol)
        bars = self._fetch_public_kline(code, start_date, end_date)
        if bars.empty:
            # 公开 XHR 主源都不可用时才回落百度：百度对直连 IP 常返回 403，
            # 且需要完整浏览器 UA（短 UA 会被拒），所以它只能是兜底而不是主源。
            bars = self._fetch_baidu_kline(code, start_date, end_date)
        if bars.empty:
            return bars

        # 名称与市值优先走腾讯报价（与日 K 同一条公开链路、毫秒级）；东财 spot
        # 只在报价拿不到时兜底——它在部分网络下不可达，而每次尝试要等满超时，
        # 全市场补齐会被它拖成数小时。
        quote = self._try_fetch_tencent_quote(code)
        info: dict[str, Any] = {}
        float_market_cap = _to_float(quote.get("float_mcap_yi"), float("nan")) * 100_000_000
        if not (float_market_cap > 0) or not str(quote.get("name") or "").strip():
            info = self._try_fetch_eastmoney_stock_info(code)
            if not (float_market_cap > 0):
                float_market_cap = _to_float(info.get("float_mcap"), float("nan"))

        bars = self._apply_market_cap(bars, quote, float_market_cap)
        name = str(quote.get("name") or "").strip() or str(info.get("name") or "").strip()
        if name:
            bars["name"] = name
        # 资金流是可选增强：端点不可达时必须快速放弃，否则每只股票都要白等
        # 满超时（实测把全市场补齐从分钟级拖到小时级）。连续失败后熔断。
        flow_by_date = self._fetch_fund_flow_with_breaker(code)
        if flow_by_date:
            bars["main_net_inflow"] = bars["trade_date"].dt.strftime("%Y-%m-%d").map(flow_by_date)
        list_date = _parse_date(info.get("list_date"))
        if list_date is None:
            bars["listing_days"] = 9999
        else:
            bars["listing_days"] = (bars["trade_date"].dt.date - list_date).map(lambda delta: delta.days)
        return bars

    @staticmethod
    def _apply_market_cap(bars: pd.DataFrame, quote: dict[str, Any], float_market_cap: float) -> pd.DataFrame:
        """Fill ``float_market_cap`` across the window.

        A quote gives the *current* price and current float market cap, hence
        float shares; earlier rows then use ``float_shares * close``.  That is
        the same derivation the Baidu path already used
        (``volume / turnover * close``), so market-cap values stay consistent
        when the source changes.  A constant snapshot is only used when shares
        cannot be derived.
        """
        if float_market_cap > 0 or _to_float(quote.get("float_mcap_yi"), float("nan")) > 0:
            price = _to_float(quote.get("price"), float("nan"))
            quote_cap = _to_float(quote.get("float_mcap_yi"), float("nan")) * 100_000_000
            float_shares = quote_cap / price if price > 0 else float("nan")
            bars = bars.copy()
            if float_shares > 0:
                bars["float_market_cap"] = float_shares * pd.to_numeric(bars["close"], errors="coerce")
            elif float_market_cap > 0:
                bars["float_market_cap"] = float_market_cap
        return bars

    def _fetch_fund_flow_with_breaker(self, code: str) -> dict[str, Any]:
        """Fetch capital flow, skipping the call after repeated failures.

        Eastmoney's fund-flow endpoint is unreachable from some networks and
        each attempt costs a full timeout.  After a few consecutive failures the
        rest of the batch skips it entirely (the dedicated
        ``/fetch/capital-flow`` crawler has its own Sina fallback); one success
        re-arms it, so a transient outage cannot disable enrichment for a whole
        backfill run.
        """
        if self._flow_failures >= EASTMONEY_ENRICHMENT_FAILURE_LIMIT:
            return {}
        try:
            rows = self._fetch_eastmoney_fund_flow_120d(code)
        except Exception:
            logger.warning("silent failure in _fetch_eastmoney_fund_flow_120d", exc_info=True)
            self._flow_failures += 1
            return {}
        self._flow_failures = 0
        return {item["date"]: item["main_net"] for item in rows}

    def _try_fetch_tencent_quote(self, code: str) -> dict[str, Any]:
        try:
            return self._fetch_tencent_quote(code)
        except Exception:
            logger.warning("silent failure in _fetch_tencent_quote", exc_info=True)
            return {}

    def _fetch_tencent_quote(self, code: str) -> dict[str, Any]:
        """Realtime snapshot from ``qt.gtimg.cn`` (total/float market cap only).

        The daily K-line endpoints do not carry market cap, and the Eastmoney
        spot endpoint is unreliable from some networks, so this quote is the
        market-cap top-up used when the Eastmoney info call returns nothing.
        """
        market_symbol = a_share_market_symbol(code)
        if market_symbol is None or self._public_text_get is None:
            return {}
        text = self._public_text_get(
            f"https://qt.gtimg.cn/q={market_symbol}",
            {"User-Agent": BROWSER_USER_AGENT},
            10,
        )
        if '"' not in text:
            return {}
        values = text.split('"')[1].split("~")
        if len(values) < 46:
            return {}
        return {
            "name": values[1],
            "price": _to_float(values[3], float("nan")),
            "change_pct": _to_float(values[32], float("nan")),
            "float_mcap_yi": _to_float(values[44], float("nan")),
            "total_mcap_yi": _to_float(values[45], float("nan")),
            "turnover_pct": _to_float(values[38], float("nan")),
        }

    def _fetch_public_kline(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """Tencent → Sina public daily K-line, clipped to the requested window.

        Both endpoints are unadjusted and agree with the warehouse convention;
        they stay on the ``trust_env=False`` scraping path so a system proxy can
        never hijack domestic market hosts. Tencent carries no Beijing-exchange
        rows, so Sina is the fallback that covers ``4xx/8xx/920xxx`` codes.
        """
        if self._public_json_get is None:
            return pd.DataFrame()
        market_symbol = a_share_market_symbol(code)
        if market_symbol is None:
            return pd.DataFrame()
        for label, fetch in (
            ("tencent", lambda: self._fetch_tencent_kline(code, market_symbol, start_date, end_date)),
            ("sina", lambda: self._fetch_sina_kline(code, market_symbol, start_date, end_date)),
        ):
            try:
                rows = fetch()
            except Exception as exc:
                logger.warning("silent failure in public %s kline for %s: %s", label, code, exc)
                continue
            if not rows:
                continue
            frame = _with_derived_columns(rows, code)
            clipped = frame.loc[
                (frame["trade_date"] >= start_date) & (frame["trade_date"] <= end_date)
            ].reset_index(drop=True)
            if not clipped.empty:
                return normalize_daily_bars(clipped)
        return pd.DataFrame()

    def _fetch_tencent_kline(self, code: str, market_symbol: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """Tencent daily K-line.

        ``count`` only returns the *last* N rows, so the range is walked in
        windows of :data:`TENCENT_KLINE_MAX_COUNT` trading days (≈15 months);
        larger counts silently return an empty payload.
        """
        rows: list[dict[str, Any]] = []
        cursor = _kline_warmup_start(start_date)
        while cursor <= end_date:
            param = f"{market_symbol},day,{cursor},{end_date},{TENCENT_KLINE_MAX_COUNT},"
            payload = self._request_public_json(
                TENCENT_KLINE_URL,
                {"param": param},
                {"User-Agent": UA, **TENCENT_KLINE_HEADERS},
                15,
            )
            batch = _tencent_kline_rows(payload, market_symbol)
            if not batch:
                break
            rows.extend(batch)
            last_date = max(item["trade_date"] for item in batch)
            if last_date >= end_date:
                break
            # 窗口是"最后 N 行"，下一轮从已取到的最后一天之后继续，避免重复拉取。
            cursor = (datetime.strptime(last_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        return rows

    def _fetch_sina_kline(self, code: str, market_symbol: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """Sina daily K-line.

        ``datalen`` counts trading days *ending at the newest available row*, so
        a fixed value would silently under-cover a long backfill window.  The
        length is sized from the requested span (plus slack for suspensions) and
        capped at :data:`SINA_KLINE_MAX_LENGTH`.
        """
        params = {
            "symbol": market_symbol,
            "scale": "240",
            "ma": "no",
            "datalen": str(_sina_window_length(start_date, end_date)),
        }
        payload = self._request_public_json(
            SINA_KLINE_URL,
            params,
            {"User-Agent": UA, **SINA_KLINE_HEADERS},
            15,
        )
        # 新浪该端点直接返回 JSON 数组；某些网关会包一层。
        if isinstance(payload, list):
            return _sina_kline_rows(payload)
        if isinstance(payload, dict):
            return _sina_kline_rows(payload.get("data") or payload.get("result") or [])
        return []

    def _request_public_json(self, url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> Any:
        """GET a public XHR, tolerating a bare JSON array (Sina's shape)."""
        getter = self._public_json_get
        if getter is None:
            raise RuntimeError("public transport not configured")
        if getter is _default_json_get:
            response = scraping_session().get(url, params=params, headers=headers, timeout=timeout)
            response.raise_for_status()
            return json.loads(response.text)
        return getter(url, params, headers, timeout)

    def _try_fetch_eastmoney_stock_info(self, code: str) -> dict[str, Any]:
        if self._info_failures >= EASTMONEY_ENRICHMENT_FAILURE_LIMIT:
            return {}
        try:
            info = self._fetch_eastmoney_stock_info(code)
        except Exception:
            logger.warning("silent failure in _try_fetch_eastmoney_stock_info", exc_info=True)
            self._info_failures += 1
            return {}
        self._info_failures = 0
        return info

    def _fetch_baidu_kline(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        market_data = self._fetch_baidu_market_data(code, start_date)
        if not market_data:
            return pd.DataFrame()
        keys = list(market_data.get("keys") or [])
        rows = []
        for raw_row in str(market_data.get("marketData") or "").split(";"):
            if not raw_row.strip():
                continue
            values = raw_row.split(",")
            item = dict(zip(keys, values, strict=False))
            trade_date = _parse_date(item.get("time"))
            if trade_date is None or not (start_date <= trade_date.isoformat() <= end_date):
                continue
            rows.append(
                {
                    "symbol": code,
                    "trade_date": trade_date.isoformat(),
                    "open": _to_float(item.get("open")),
                    "high": _to_float(item.get("high")),
                    "low": _to_float(item.get("low")),
                    "close": _to_float(item.get("close")),
                    "volume": _to_float(item.get("volume")),
                    "amount": _to_float(item.get("amount")),
                    "change": _to_float(item.get("range")),
                    "change_pct": _to_float(item.get("ratio")),
                    "turnover_rate": _to_float(item.get("turnoverratio")),
                    "pre_close": _to_float(item.get("preClose"), float("nan")),
                }
            )
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        turnover = pd.to_numeric(frame["turnover_rate"], errors="coerce")
        volume = pd.to_numeric(frame["volume"], errors="coerce")
        close = pd.to_numeric(frame["close"], errors="coerce")
        frame["float_market_cap"] = (volume / (turnover / 100.0)) * close
        frame.loc[turnover <= 0, "float_market_cap"] = float("nan")
        return normalize_daily_bars(frame)

    def _fetch_baidu_market_data(self, code: str, start_date: str) -> dict[str, Any]:
        params = {
            "all": "1",
            "isIndex": "false",
            "isBk": "false",
            "isBlock": "false",
            "isFutures": "false",
            "isStock": "true",
            "newFormat": "1",
            "group": "quotation_kline_ab",
            "finClientType": "pc",
            "code": code,
            "start_time": start_date,
            "ktype": "1",
        }
        headers = {
            # 百度只对完整浏览器 UA 放行：短 UA（"Mozilla/5.0 (Windows NT 10.0;
            # Win64; x64) AppleWebKit/537.36"）会稳定拿到 403，实测换完整 Chrome
            # UA 后同一请求 200。百度在本项目只是公开 XHR 主源之后的兜底。
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "application/vnd.finance-web.v1+json",
            "Origin": "https://gushitong.baidu.com",
            "Referer": "https://gushitong.baidu.com/",
        }
        for label, json_get in self._json_gets:
            for attempt in range(2):
                try:
                    payload = json_get(
                        "https://finance.pae.baidu.com/selfselect/getstockquotation",
                        params,
                        headers,
                        8,
                    )
                except Exception:
                    logger.warning("silent failure in _fetch_baidu_market_data", exc_info=True)
                    break
                result = payload.get("Result") if isinstance(payload, dict) else None
                if isinstance(result, dict):
                    return result.get("newMarketData", {}) or {}
                if not _should_retry_baidu_payload(payload):
                    break
                if label != "injected":
                    break
                if attempt == 0 and label == "injected":
                    time.sleep(0.2)
            if label != "injected":
                continue
        return {}

    def _request_json(self, url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> dict[str, Any]:
        last_error: Exception | None = None
        for _label, json_get in self._json_gets:
            try:
                return json_get(url, params, headers, timeout)
            except Exception as exc:
                last_error = exc
                continue
        if last_error is not None:
            raise last_error
        return {}

    def _fetch_eastmoney_fund_flow_120d(self, code: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get",
            {
                "secid": f"{market_code(code)}.{code}",
                "fields1": "f1,f2,f3,f7",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
                "lmt": "120",
            },
            {
                "User-Agent": UA,
                "Referer": "https://quote.eastmoney.com/",
                "Origin": "https://quote.eastmoney.com",
            },
            15,
        )
        rows = []
        for line in payload.get("data", {}).get("klines", []) or []:
            parts = str(line).split(",")
            if len(parts) >= 2:
                rows.append({"date": parts[0], "main_net": _to_float(parts[1])})
        return rows

    def _fetch_eastmoney_stock_info(self, code: str) -> dict[str, Any]:
        payload = self._request_json(
            "https://push2.eastmoney.com/api/qt/stock/get",
            {
                "fltt": "2",
                "invt": "2",
                "fields": "f57,f58,f84,f85,f127,f116,f117,f189,f43",
                "secid": f"{market_code(code)}.{code}",
            },
            {"User-Agent": UA},
            10,
        )
        data = payload.get("data", {}) or {}
        return {
            "code": data.get("f57", ""),
            "name": data.get("f58", ""),
            "industry": data.get("f127", ""),
            "total_shares": data.get("f84", 0),
            "float_shares": data.get("f85", 0),
            "mcap": data.get("f116", 0),
            "float_mcap": data.get("f117", 0),
            "list_date": data.get("f189", ""),
            "price": data.get("f43", 0),
        }


class AStockDataAdapter:
    def __init__(self, fetcher: DailyBarsFetcher | None = None) -> None:
        self.fetcher = fetcher

    @classmethod
    def from_http_sources(cls) -> AStockDataAdapter:
        return cls(fetcher=HttpAStockFetcher().fetch_daily_bars)

    def fetch_daily_bars(self, symbols: Sequence[str], start_date: str, end_date: str) -> pd.DataFrame:
        if self.fetcher is None:
            raise AStockDataUnavailable(
                "a-stock-data fetcher is not configured. Configure a fetcher that returns daily OHLCV, "
                "market cap, turnover, and capital-flow columns."
            )
        frame = self.fetcher(symbols, start_date, end_date)
        return frame if frame.empty else normalize_daily_bars(frame)
