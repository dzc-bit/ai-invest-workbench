from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from astock_backtester.data.http_transport import (
    BROWSER_USER_AGENT,
    HostThrottle,
    resilient_get,
    scraping_get,
    scraping_session,
)
from astock_backtester.data.http_transport import (
    USER_AGENT as UA,
)
from astock_backtester.data.importer import normalize_daily_bars
from astock_backtester.data.parsing import parse_float
from astock_backtester.data.symbols import a_share_market_symbol, is_st_name, market_code, normalize_symbol

logger = logging.getLogger(__name__)

# 公开 XHR 日 K（补缺主源）。百度通道对直连 IP 返回 403，且需要完整浏览器 UA；
# 腾讯/新浪这两条公开接口实测直连稳定，且价格口径与仓库一致（不复权）：
#   - 腾讯：volume 单位是手，需要 ×100 换成股；
#   - 新浪：volume 单位是股，且覆盖北交所（腾讯不提供 bj 段）。
TENCENT_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
TENCENT_KLINE_MAX_COUNT = 320
# 分页硬上限：一轮最多拉 320 个交易日，64 轮 ≈ 20480 个交易日（≈84 年），
# 远超任何现实回测窗口；它的作用是兜住"上游每轮都返回不收敛的行"的死循环。
TENCENT_KLINE_MAX_PAGES = 64
# 单票分页回走的**墙钟**预算（秒）：页数上限 64 × 单次 timeout 15s = 最坏 960s/票，
# 异常上游（每轮都慢失败）能把整批补齐拖死。正常回走每轮毫秒级（实测长窗口
# 数次请求即覆盖完成），90 秒足够宽裕；触顶时按欠覆盖记录并让上层 provider 接力。
TENCENT_KLINE_WALK_BUDGET_SECONDS = 90.0
TENCENT_KLINE_HEADERS = {"Referer": "https://gu.qq.com/"}
TENCENT_VOLUME_LOT_SIZE = 100
SINA_KLINE_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
)
SINA_KLINE_MAX_LENGTH = 1000
SINA_KLINE_HEADERS = {"Referer": "https://finance.sina.com.cn"}
# 计算涨跌幅的预热窗口：先取请求起点之前一小段，用来给首行提供前收盘价。
KLINE_WARMUP_DAYS = 10
# 公开 XHR 生产传输的瞬时错误重试次数（resilient_get 内部执行，见 §15-2）。
PUBLIC_XHR_RETRIES = 1
# 东财资金流端点在部分网络下不可达；连续失败达到该次数后本批不再尝试，
# 避免每只股票都白等满超时（资金流有独立的 /fetch/capital-flow 补齐链路）。
EASTMONEY_ENRICHMENT_FAILURE_LIMIT = 3
# 熔断 open 状态的冷却时长：期间调用立即返回，冷却结束后放行一次 half-open 试探。
# 取 60 秒是因为一个试探最坏要等满 2×10s/2×15s 超时——冷却必须显著长于试探成本，
# 否则抖动型故障会退化成"每分钟都送一只票去挨超时"。
EASTMONEY_ENRICHMENT_COOLDOWN_SECONDS = 60.0


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
    calendar span with slack for weekends/holidays and suspensions.  The result
    is deliberately *uncapped*: the caller caps it at
    :data:`SINA_KLINE_MAX_LENGTH` and is responsible for reporting the
    under-coverage that cap implies (see ``_fetch_sina_kline``).
    """
    try:
        span_days = (datetime.strptime(end_date, "%Y-%m-%d") - datetime.strptime(start_date, "%Y-%m-%d")).days
    except ValueError:
        return 120
    # ≈5/7 的日历日是交易日；再留 30% 余量覆盖停牌与节假日。
    return max(30, int(span_days * 0.72 * 1.3) + 30)


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
    # turnover_rate 同理必须显式置空：``normalize_daily_bars`` 的缺列默认值是
    # ``0.0``，不置空就会把"未知"写成"换手率 0%"——假 0 会同时污染
    # ``turnover_between`` 条件与候选打分（0 是合法值，无法与真实 0% 区分）。
    # 真实换手率稍后由 ``_derive_turnover_rate`` 用 volume/流通股 补出来。
    frame["turnover_rate"] = float("nan")
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


def _public_budget(timeout: float) -> float:
    """Total wall-clock budget for one public XHR call (retries + alternate).

    The budget equals the caller's single-attempt ``timeout`` so a blackholed
    upstream still fails as fast as it did before retries existed; the retry and
    the ``curl_cffi`` alternate only consume what a *fast* failure (connection
    reset, anti-bot 403) leaves unused.
    """
    return time.monotonic() + timeout


def _public_default_json_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    """Production transport for public XHR: retry + curl_cffi alternate (§15-2)."""
    response = resilient_get(
        scraping_get,
        url,
        timeout=timeout,
        source="astock-public-json",
        retries=PUBLIC_XHR_RETRIES,
        deadline=_public_budget(timeout),
        allow_alternate=True,
        params=params,
        headers=headers,
    )
    return json.loads(response.text)


def _public_default_text_get(url: str, headers: dict[str, str], timeout: int) -> str:
    """Fetch a GBK text payload (Tencent quote protocol) with the same policy."""
    response = resilient_get(
        scraping_get,
        url,
        timeout=timeout,
        source="astock-public-text",
        retries=PUBLIC_XHR_RETRIES,
        deadline=_public_budget(timeout),
        allow_alternate=True,
        headers=headers,
    )
    return response.content.decode("gbk", errors="replace")


def _unconfigured_json_get(url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> dict[str, Any]:
    raise RuntimeError("eastmoney transport not configured (test construction)")


def _breaker_now() -> float:
    """Clock seam for the enrichment breakers' cooldown math (tests patch this)."""
    return time.monotonic()


class _EnrichmentBreaker:
    """closed → open → half-open circuit breaker for the optional Eastmoney layer.

    状态转移：

    - ``closed``：正常放行；连续 :data:`EASTMONEY_ENRICHMENT_FAILURE_LIMIT` 次失败 → ``open``。
    - ``open``：后续调用**立即返回**（不再等超时），直到冷却
      :data:`EASTMONEY_ENRICHMENT_COOLDOWN_SECONDS` 秒后放行一次试探 → ``half_open``。
    - ``half_open``：同一时刻只放行一个试探；试探成功 → ``closed`` 并清零失败计数，
      试探失败 → 回 ``open`` 且冷却重新计时。

    旧实现是"连续 3 次跳闸、一次成功立即归零"：抖动型故障（失败、成功、失败……）
    下熔断永远不触发，每只票都要白等满超时。info 与 flow 各持一个实例、互相独立，
    但共用 fetcher 的同一把锁（见 ``HttpAStockFetcher.__init__``）。
    """

    def __init__(self, name: str, lock: threading.Lock) -> None:
        self.name = name
        self._lock = lock
        self._state = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._probing = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    def allow(self) -> bool:
        """Whether the next enrichment call may hit the upstream."""
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if _breaker_now() - self._opened_at < EASTMONEY_ENRICHMENT_COOLDOWN_SECONDS:
                    return False
                self._state = "half_open"
            if self._probing:
                return False
            self._probing = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failures = 0
            self._probing = False
            self._opened_at = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._probing = False
            if self._state == "half_open":
                self._open()
                return
            self._failures += 1
            if self._failures >= EASTMONEY_ENRICHMENT_FAILURE_LIMIT:
                self._open()

    def _open(self) -> None:
        self._state = "open"
        self._failures = 0
        self._opened_at = _breaker_now()


class HttpAStockFetcher:
    """HTTP subset learned from simonlin1212/a-stock-data for daily backtest cache fills."""

    # 每 host 出站请求的最小间隔（秒）：≈20 req/s 的保守默认值，兜住 4 个补齐
    # worker 对同一上游的并发突发；注入式构造（密闭测试）不设限速。
    HOST_MIN_REQUEST_INTERVAL = 0.05

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
        # - 生产构造（无注入）默认走真实网络，且经 resilient_get 带重试与
        #   curl_cffi 降级（_public_default_json_get / _public_default_text_get）；
        # - 注入式构造（测试）默认关闭，需要时显式传 public_json_get / public_text_get。
        self._public_json_get = (
            public_json_get
            if public_json_get is not None
            else (_public_default_json_get if real_network else None)
        )
        self._public_text_get = (
            public_text_get
            if public_text_get is not None
            else (_public_default_text_get if real_network else None)
        )
        # 熔断状态是共享可变量：全市场补齐用 4 个 worker 并发跑同一个 adapter，
        # 失败计数与状态转移必须在一把锁里读改写，否则丢更新会让熔断永不跳闸。
        self._breaker_lock = threading.Lock()
        self._flow_breaker = _EnrichmentBreaker("flow", self._breaker_lock)
        self._info_breaker = _EnrichmentBreaker("info", self._breaker_lock)
        # 每 host 出站限速同样是共享的：一个 throttle 覆盖本实例的全部出站路径。
        # 生产构造带保守默认值；注入式构造（密闭测试）为 0，测试可直接改
        # min_request_interval 来断言两次同 host 请求的间隔。
        self.min_request_interval = self.HOST_MIN_REQUEST_INTERVAL if real_network else 0.0
        self._throttle = HostThrottle(lambda: self.min_request_interval)


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
        bars = self._apply_turnover_rate(bars)
        name = str(quote.get("name") or "").strip() or str(info.get("name") or "").strip()
        if name:
            bars["name"] = name
            # is_st 只能由证券简称派生（上游不带该字段）。不派生时 importer 的默认
            # False 会让 ST 股按 10% 判涨跌停（实际 5%），且
            # BacktestSettings.exclude_st（默认 True）静默失效。
            # 语义说明：用的是**当前**简称，因此会整窗标记——某只股票若近期被 ST，
            # 其更早的年份也会标 True。这是保守口径（宁可把风险股整段排除），
            # 与 risk.py 用最新名称识别 ST 的口径一致；不是逐日历史 ST 状态。
            bars["is_st"] = is_st_name(name)
        # 资金流是可选增强：端点不可达时必须快速放弃，否则每只股票都要白等
        # 满超时（实测把全市场补齐从分钟级拖到小时级）。连续失败后熔断。
        flow_by_date = self._fetch_fund_flow_with_breaker(code)
        if flow_by_date:
            bars["main_net_inflow"] = bars["trade_date"].dt.strftime("%Y-%m-%d").map(flow_by_date)
        # 上市日只认东财 ``f189``。腾讯回走的"最早一行"不能当上市日：长期停牌
        # 横跨预热窗口的老股与真次新股在回走上无法区分（实测误报），写错会经
        # ``derive_listing_dates_from_frame`` 污染 symbol_lifecycle，比保持
        # 9999（未知）危险得多。
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
        float shares; earlier rows then use ``float_shares * close``.  A constant
        snapshot is only used when shares cannot be derived.

        Rows that already carry a per-date value (the Baidu path derives
        ``volume / (turnover / 100) * close`` per row) are **kept**: that
        derivation reflects the share count *on that date*, whereas the
        quote-based estimate applies today's share count to every historical
        close and therefore overstates market cap for any stock that has since
        issued or released shares (解禁/增发).  Only gaps are filled.
        """
        if float_market_cap > 0 or _to_float(quote.get("float_mcap_yi"), float("nan")) > 0:
            price = _to_float(quote.get("price"), float("nan"))
            quote_cap = _to_float(quote.get("float_mcap_yi"), float("nan")) * 100_000_000
            float_shares = quote_cap / price if price > 0 else float("nan")
            bars = bars.copy()
            if float_shares > 0:
                derived = float_shares * pd.to_numeric(bars["close"], errors="coerce")
            elif float_market_cap > 0:
                derived = pd.Series(float_market_cap, index=bars.index)
            else:
                return bars
            existing = pd.to_numeric(bars["float_market_cap"], errors="coerce")
            bars["float_market_cap"] = existing.fillna(derived)
        return bars

    @staticmethod
    def _apply_turnover_rate(bars: pd.DataFrame) -> pd.DataFrame:
        """D+换手率（%）＝ volume / 流通股 × 100，只在能推出流通股时填。

        公开 XHR 日 K 不带换手率，而 ``turnover_rate`` 参与
        ``turnover_between`` 条件与候选打分。用报价推出的流通股
        （``float_market_cap / price``）即可逐行还原，量纲与仓库一致是**百分数**
        （实测仓库 median≈0.38）。推不出流通股的行保持 NaN（"未知"），
        绝不写 0。
        """
        if "float_market_cap" not in bars.columns or "close" not in bars.columns:
            return bars
        close = pd.to_numeric(bars["close"], errors="coerce")
        cap = pd.to_numeric(bars["float_market_cap"], errors="coerce")
        shares = cap / close.replace(0, float("nan"))
        volume = pd.to_numeric(bars["volume"], errors="coerce")
        derived = (volume / shares.replace(0, float("nan")) * 100.0).where(lambda value: value >= 0)
        bars = bars.copy()
        existing = pd.to_numeric(bars["turnover_rate"], errors="coerce")
        # 只补空缺：百度通道本身带 turnoverratio（同为百分数量纲），不要去覆盖它。
        bars["turnover_rate"] = existing.fillna(derived)
        return bars

    def _fetch_fund_flow_with_breaker(self, code: str) -> dict[str, Any]:
        """Fetch capital flow behind the ``flow`` circuit breaker.

        Eastmoney's fund-flow endpoint is unreachable from some networks and
        each attempt costs a full timeout, so consecutive failures trip the
        breaker and the rest of the batch skips the call entirely (the dedicated
        ``/fetch/capital-flow`` crawler has its own Sina fallback).  Unlike the
        old "trip after N failures, re-arm on one success" counter, the breaker
        also *recovers*: once the cooldown elapses it lets a single probe through
        and closes again on success — a jittery outage no longer keeps the
        enrichment disabled for the whole run, and a dead endpoint no longer
        collects a timeout on every remaining symbol.
        """
        if not self._flow_breaker.allow():
            return {}
        try:
            rows = self._fetch_eastmoney_fund_flow_120d(code)
        except Exception:
            logger.warning("silent failure in _fetch_eastmoney_fund_flow_120d", exc_info=True)
            self._flow_breaker.record_failure()
            return {}
        self._flow_breaker.record_success()
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
        url = f"https://qt.gtimg.cn/q={market_symbol}"
        self._throttle.wait(url)
        text = self._public_text_get(
            url,
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
        # 腾讯不覆盖北交所（实测 bj920xxx 恒返回空 day）：920/4x/8x 直接走新浪，
        # 省掉一次注定为空的往返（全市场补齐时是每只北交所股票一次）。
        if market_symbol.startswith("bj"):
            fetch_order = (("sina", lambda: self._fetch_sina_kline(code, market_symbol, start_date, end_date)),)
        else:
            fetch_order = (
                ("tencent", lambda: self._fetch_tencent_kline(code, market_symbol, start_date, end_date)),
                ("sina", lambda: self._fetch_sina_kline(code, market_symbol, start_date, end_date)),
            )
        for label, fetch in fetch_order:
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
        """Tencent daily K-line, walked **backwards** from the window end.

        ``count`` only returns the *last* N rows of ``[start, end]`` (实测：
        ``sz000001,day,2015-01-01,2017-12-31,320,`` → 2016-09-07..2017-12-29），
        so a long window can never be walked forwards — the first response is
        the window's *tail*, the forward cursor would jump straight to
        ``end_date`` and everything before it (the 2015-2025 segment) would be
        silently dropped.  The walk therefore rewinds: every round requests
        ``[warmup_start, cursor]`` and moves ``cursor`` to **one day before the
        earliest returned row**, until the warmup start is covered (the
        ``start`` anchor stays at ``_kline_warmup_start`` for every round; the
        server clips to ``[start, end]``, so only ``end`` needs to move).

        Three guards keep the walk finite even against a misbehaving upstream:

        - the earliest returned row must strictly move *backwards* every round
          (rows that do not rewind the window are stale — e.g. an upstream that
          ignores ``end`` and always answers with the newest tail — so they are
          neither accumulated nor re-requested) → ``cursor did not advance``;
        - an empty response ends the walk (the window now precedes the listing
          date, or the upstream has nothing left);
        - :data:`TENCENT_KLINE_MAX_PAGES` bounds the number of rounds outright
          (64 × 320 ≈ 84 years of trading days — far beyond any real backfill),
          and :data:`TENCENT_KLINE_WALK_BUDGET_SECONDS` bounds the round count by
          **wall clock** so a slowly-failing upstream cannot hold one symbol for
          64 × 15s (the page cap alone is not a time bound).

        Rows are de-duplicated by trade date (rewind boundaries can overlap one
        day) and returned sorted ascending, so downstream ``pre_close`` /
        ``change`` derivation sees a monotonic series.

        Rows are de-duplicated by trade date (rewind boundaries can overlap one
        day) and returned sorted ascending, so downstream ``pre_close`` /
        ``change`` derivation sees a monotonic series.
        """
        warmup_start = _kline_warmup_start(start_date)
        rows: list[dict[str, Any]] = []
        seen_dates: set[str] = set()
        cursor = end_date
        earliest_seen: str | None = None
        pages = 0
        walk_deadline = time.monotonic() + TENCENT_KLINE_WALK_BUDGET_SECONDS
        while cursor >= warmup_start:
            if pages >= TENCENT_KLINE_MAX_PAGES:
                logger.warning(
                    "tencent kline stopped at the page cap for %s: cursor=%s end=%s after %s pages (under-coverage)",
                    code,
                    cursor,
                    end_date,
                    pages,
                )
                break
            if time.monotonic() >= walk_deadline:
                logger.warning(
                    "tencent kline stopped at the walk budget for %s: cursor=%s end=%s after %s pages (under-coverage)",
                    code,
                    cursor,
                    end_date,
                    pages,
                )
                break
            pages += 1
            param = f"{market_symbol},day,{warmup_start},{cursor},{TENCENT_KLINE_MAX_COUNT},"
            payload = self._request_public_json(
                TENCENT_KLINE_URL,
                {"param": param},
                {"User-Agent": UA, **TENCENT_KLINE_HEADERS},
                15,
            )
            batch = _tencent_kline_rows(payload, market_symbol)
            if not batch:
                break
            batch_earliest = min(item["trade_date"] for item in batch)
            if earliest_seen is not None and batch_earliest >= earliest_seen:
                # 上游返回的行没有把窗口往回带（重复尾部或忽略 end）：继续循环只会
                # 拉同一份数据，必须立刻收手并留下可诊断的欠覆盖记录。
                logger.warning(
                    "tencent kline cursor did not advance for %s: cursor=%s last_row=%s (under-coverage)",
                    code,
                    cursor,
                    batch_earliest,
                )
                break
            for item in batch:
                if item["trade_date"] in seen_dates:
                    continue
                seen_dates.add(item["trade_date"])
                rows.append(item)
            earliest_seen = batch_earliest
            if batch_earliest <= warmup_start:
                # 已经回走到预热起点（或更早）：覆盖完成，不必再发一次请求。
                break
            cursor = (datetime.strptime(batch_earliest, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        rows.sort(key=lambda item: item["trade_date"])
        return rows

    def _fetch_sina_kline(self, code: str, market_symbol: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """Sina daily K-line.

        ``datalen`` counts trading days *ending at the newest available row*, so
        a fixed value would silently under-cover a long backfill window; the
        length is sized from the requested span (plus slack for suspensions) and
        capped at :data:`SINA_KLINE_MAX_LENGTH`.

        Unlike Tencent's ``param=symbol,day,cursor,end,count`` walk, this
        endpoint has **no date anchor** — it only ever answers "the newest N
        bars" (params are ``symbol``/``scale``/``ma``/``datalen``), so there is
        no cursor to rewind and a second request would return the very same
        tail.  Window segmentation is therefore impossible here, and the
        reviewed fallback applies: whenever the span needs more than one page
        and the response really is the clamped tail, the truncation is logged
        as explicit under-coverage instead of being returned silently (Sina is
        the only public source for Beijing-exchange ``920xxx`` codes, so the
        gap has to stay visible).
        """
        needed = _sina_window_length(start_date, end_date)
        datalen = min(needed, SINA_KLINE_MAX_LENGTH)
        payload = self._request_public_json(
            SINA_KLINE_URL,
            {
                "symbol": market_symbol,
                "scale": "240",
                "ma": "no",
                "datalen": str(datalen),
            },
            {"User-Agent": UA, **SINA_KLINE_HEADERS},
            15,
        )
        # 新浪该端点直接返回 JSON 数组；某些网关会包一层。
        if isinstance(payload, list):
            rows = _sina_kline_rows(payload)
        elif isinstance(payload, dict):
            rows = _sina_kline_rows(payload.get("data") or payload.get("result") or [])
        else:
            rows = []
        self._report_sina_under_coverage(code, start_date, needed, datalen, rows)
        return rows

    @staticmethod
    def _report_sina_under_coverage(
        code: str,
        start_date: str,
        needed: int,
        datalen: int,
        rows: list[dict[str, Any]],
    ) -> None:
        """Log an explicit gap when the datalen cap truncated the window.

        Three conditions must hold together: the span needs more than one page,
        the response filled the whole ``datalen`` request (so the endpoint did
        hold data back), and the oldest returned row is still newer than the
        requested start (so rows are genuinely missing).  A stock listed after
        ``start_date`` simply has fewer rows than ``datalen`` and stays silent.
        """
        if needed <= SINA_KLINE_MAX_LENGTH or len(rows) < datalen or not rows:
            return
        oldest = min(item["trade_date"] for item in rows)
        if oldest > start_date:
            logger.warning(
                "sina kline under-covers %s: window start=%s needs ~%s bars but datalen is capped at %s; oldest returned row=%s",
                code,
                start_date,
                needed,
                datalen,
                oldest,
            )

    def _request_public_json(self, url: str, params: dict[str, str], headers: dict[str, str], timeout: int) -> Any:
        """GET a public XHR, tolerating a bare JSON array (Sina's shape).

        Production construction injects ``_public_default_json_get``, which
        already carries the retry/alternate policy, so this call site must not
        re-implement retry logic; injected getters are called verbatim to keep
        hermetic tests fully offline.
        """
        getter = self._public_json_get
        if getter is None:
            raise RuntimeError("public transport not configured")
        self._throttle.wait(url)
        return getter(url, params, headers, timeout)

    def _try_fetch_eastmoney_stock_info(self, code: str) -> dict[str, Any]:
        """Eastmoney spot info behind the ``info`` circuit breaker (independent of ``flow``)."""
        if not self._info_breaker.allow():
            return {}
        try:
            info = self._fetch_eastmoney_stock_info(code)
        except Exception:
            logger.warning("silent failure in _try_fetch_eastmoney_stock_info", exc_info=True)
            self._info_breaker.record_failure()
            return {}
        self._info_breaker.record_success()
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
        url = "https://finance.pae.baidu.com/selfselect/getstockquotation"
        for label, json_get in self._json_gets:
            for attempt in range(2):
                try:
                    self._throttle.wait(url)
                    payload = json_get(
                        url,
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
                self._throttle.wait(url)
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
