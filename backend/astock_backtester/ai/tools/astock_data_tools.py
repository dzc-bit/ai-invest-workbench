"""External research tools adapted from ``simonlin1212/a-stock-data``.

Source: https://github.com/simonlin1212/a-stock-data (Apache-2.0), SKILL.md
V3.8.0 — sections §1.2 (Tencent quote), §2.1 (Eastmoney report API), §3.5
(dragon-tiger board) and §8.1 (limit-up pools).  Adapted to this project's
conventions: symbol normalization via ``data/symbols.py`` (invariant #1),
transport via ``data/http_transport.create_scraping_session`` (invariant #2,
``trust_env=False``), short timeouts, and structured diagnostics on every
failure path.  Public zero-auth endpoints only — no login, cookies or paid
sources, matching the project's crawler boundary.

Eastmoney endpoints share one IP-level rate limiter (``_em_get``) ported from
the upstream ``em_get``: serialised requests with a minimum interval plus
jitter, because push2/datacenter/reportapi throttle >5 req/s per IP.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import requests

from astock_backtester.ai.tools.registry import AiTool
from astock_backtester.data.http_transport import USER_AGENT, create_scraping_session
from astock_backtester.data.symbols import a_share_market_symbol, normalize_symbol

if TYPE_CHECKING:
    from astock_backtester.ai.tools.local_tools import AiBackend

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_API = "https://reportapi.eastmoney.com/report/list"
ZT_POOL_URL = "https://push2ex.eastmoney.com/{endpoint}"
ZTB_UT = "7eea3edcaed734bea9cbfc24409ed989"
EM_MIN_INTERVAL_SECONDS = 1.0

ZT_POOL_ENDPOINTS = {
    "zt": ("getTopicZTPool", "fbt:asc"),
    "zb": ("getTopicZBPool", "fbt:asc"),
    "dt": ("getTopicDTPool", "fund:asc"),
    "yzt": ("getYesterdayZTPool", "zs:desc"),
}
ZT_POOL_NAMES = {"zt": "涨停", "zb": "炸板", "dt": "跌停", "yzt": "昨日涨停"}

_em_lock = threading.Lock()
_em_last_call = 0.0
_em_session: requests.Session | None = None


def _em_session_or_create() -> requests.Session:
    global _em_session
    if _em_session is None:
        session = create_scraping_session()
        session.headers.update({"User-Agent": USER_AGENT})
        _em_session = session
    return _em_session


def _em_get(
    url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: float = 12
) -> requests.Response:
    """Serialised, rate-limited GET for all eastmoney.com endpoints."""
    global _em_last_call
    with _em_lock:
        wait = EM_MIN_INTERVAL_SECONDS - (time.monotonic() - _em_last_call)
        if wait > 0:
            time.sleep(wait + random.uniform(0.05, 0.25))
        try:
            return _em_session_or_create().get(url, params=params, headers=headers, timeout=timeout)
        finally:
            _em_last_call = time.monotonic()


def _eastmoney_datacenter(
    report_name: str,
    *,
    filter_str: str = "",
    page_size: int = 50,
    sort_columns: str = "",
    diagnostics: list[str],
) -> list[dict[str, Any]]:
    params = {
        "reportName": report_name,
        "columns": "ALL",
        "filter": filter_str,
        "pageNumber": "1",
        "pageSize": str(page_size),
        "sortColumns": sort_columns,
        "sortTypes": "-1",
        "source": "WEB",
        "client": "WEB",
    }
    response = _em_get(DATACENTER_URL, params=params)
    response.raise_for_status()
    payload = response.json()
    result = payload.get("result") or {}
    if not result.get("data"):
        diagnostics.append(f"datacenter {report_name} 返回空（可能无记录或接口变动）")
        return []
    return result["data"]


def _wan(amount: Any) -> float | None:
    try:
        return round(float(amount) / 10_000, 1)
    except (TypeError, ValueError):
        return None


def _fmt_zt_time(value: Any) -> str:
    digits = str(value or "").zfill(6)
    return f"{digits[0:2]}:{digits[2:4]}:{digits[4:6]}"


def fetch_tencent_quotes(raw_symbols: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Batch Tencent realtime valuation quotes. Returns (quotes, diagnostics)."""
    prefixed: list[str] = []
    key_to_symbol: dict[str, str] = {}
    diagnostics: list[str] = []
    for raw in raw_symbols[:10]:
        symbol = normalize_symbol(raw)
        market_symbol = a_share_market_symbol(symbol)
        if market_symbol is None:
            diagnostics.append(f"{raw} 不是可识别的 A 股代码，已跳过")
            continue
        prefixed.append(market_symbol)
        key_to_symbol[market_symbol[2:]] = symbol
    if not prefixed:
        return [], diagnostics
    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    try:
        response = create_scraping_session().get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        response.raise_for_status()
        text = response.content.decode("gbk", errors="replace")
    except requests.RequestException as exc:
        raise RuntimeError(f"腾讯行情请求失败：{exc}") from exc

    quotes: list[dict[str, Any]] = []
    for line in text.strip().split(";"):
        if "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        values = line.split('"')[1].split("~")
        if len(values) < 53:
            continue
        code = key_to_symbol.get(key[2:], key[2:])

        def _num(field_values: list[str], index: int) -> float | None:
            try:
                return float(field_values[index]) if field_values[index] else None
            except (ValueError, IndexError):
                return None

        price = _num(values, 3) or 0.0
        last_close = _num(values, 4) or 0.0
        amount_wan = _num(values, 37) or 0.0
        stale = amount_wan == 0 and price == last_close and price > 0
        quotes.append(
            {
                "symbol": code,
                "name": values[1],
                "price": price,
                "change_pct": _num(values, 32),
                "turnover_pct": _num(values, 38),
                "pe_ttm": _num(values, 39),
                "float_mcap_yi": _num(values, 44),
                "total_mcap_yi": _num(values, 45),
                "pb": _num(values, 46),
                "limit_up": _num(values, 47),
                "limit_down": _num(values, 48),
                "vol_ratio": _num(values, 49),
                "pe_static": _num(values, 52),
                "is_stale": stale,
                "stale_reason": "成交量为 0（停牌/未开盘/废码），非当日真实成交" if stale else "",
            }
        )
    return quotes, diagnostics


def fetch_limit_up_rows(pool_type: str, trade_date: str | None = None) -> list[dict[str, Any]]:
    """Fetch one limit-pool as plain rows (shared by the tool and the digest)."""
    if pool_type not in ZT_POOL_ENDPOINTS:
        raise ValueError("pool_type 只能是 zt/zb/dt/yzt")
    endpoint, sort = ZT_POOL_ENDPOINTS[pool_type]
    raw_date = str(trade_date or date.today().strftime("%Y%m%d")).replace("-", "")
    params = {
        "ut": ZTB_UT,
        "dpt": "wz.ztzt",
        "Pageindex": 0,
        "pagesize": 300,
        "sort": sort,
        "date": raw_date,
    }
    response = _em_get(
        ZT_POOL_URL.format(endpoint=endpoint),
        params=params,
        headers={"Referer": "https://quote.eastmoney.com/"},
        timeout=10,
    )
    response.raise_for_status()
    pool = (response.json().get("data") or {}).get("pool") or []
    items = []
    for row in pool:
        zttj = row.get("zttj") or {}
        items.append(
            {
                "symbol": row.get("c"),
                "name": row.get("n"),
                "price": (row.get("p") or 0) / 1000,
                "change_pct": round(row.get("zdp") or 0, 2),
                "turnover": round(row.get("hs") or 0, 2),
                "limit_days": row.get("lbc"),
                "first_seal": _fmt_zt_time(row.get("fbt")),
                "seal_fund": row.get("fund"),
                "break_times": row.get("zbc"),
                "industry": row.get("hybk", ""),
                "zt_stat": f"{zttj.get('days', '?')}天{zttj.get('ct', '?')}板",
            }
        )
    return items


def build_astock_data_tools(backend: AiBackend | None = None) -> list[AiTool]:
    def stock_valuation(args: dict[str, Any]) -> dict[str, Any]:
        raw_symbols = [str(s) for s in args.get("symbols", [])][:10]
        if not raw_symbols:
            return {"ok": False, "error": "symbols 不能为空"}
        try:
            quotes, diagnostics = fetch_tencent_quotes(raw_symbols)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "diagnostics": []}
        return {"ok": bool(quotes), "quotes": quotes, "diagnostics": diagnostics}

    def summarize_valuation(payload: dict[str, Any]) -> str:
        lines = []
        for quote in payload.get("quotes", []):
            stale = "（停牌/疑似废码）" if quote.get("is_stale") else ""
            lines.append(
                f"- {quote.get('symbol')} {quote.get('name')}{stale}：¥{quote.get('price')} "
                f"{(quote.get('change_pct') or 0):+.2f}%，PE(TTM) {quote.get('pe_ttm')}，PB {quote.get('pb')}，"
                f"总市值 {quote.get('total_mcap_yi')} 亿，换手 {quote.get('turnover_pct')}%"
            )
        return "\n".join(lines) or "无有效报价"

    def stock_research_reports(args: dict[str, Any]) -> dict[str, Any]:
        symbol = normalize_symbol(str(args.get("symbol", "")))
        if not symbol.isdigit():
            return {"ok": False, "error": f"无法识别股票代码：{args.get('symbol')!r}"}
        max_items = max(1, min(int(args.get("max_items", 8)), 20))
        params = {
            "industryCode": "*",
            "pageSize": "100",
            "industry": "*",
            "rating": "*",
            "ratingChange": "*",
            "beginTime": "2020-01-01",
            "endTime": "2030-01-01",
            "pageNo": "1",
            "fields": "",
            "qType": "0",
            "orgCode": "",
            "code": symbol,
            "rcode": "",
            "p": "1",
            "pageNum": "1",
            "pageNumber": "1",
        }
        diagnostics: list[str] = []
        try:
            response = _em_get(REPORT_API, params=params, headers={"Referer": "https://data.eastmoney.com/"}, timeout=15)
            response.raise_for_status()
            rows = response.json().get("data") or []
        except (requests.RequestException, ValueError) as exc:
            return {"ok": False, "error": f"东财研报接口失败：{exc}", "diagnostics": diagnostics}
        if not rows:
            return {"ok": True, "reports": [], "note": "东财无该标的研报覆盖或接口返回为空", "diagnostics": diagnostics}
        reports = [
            {
                "date": str(row.get("publishDate", ""))[:10],
                "org": row.get("orgSName", ""),
                "rating": row.get("emRatingName", ""),
                "title": row.get("title", ""),
                "eps_this_year": row.get("predictThisYearEps"),
                "industry": row.get("indvInduName", ""),
            }
            for row in rows[:max_items]
        ]
        return {"ok": True, "symbol": symbol, "count": len(rows), "reports": reports, "diagnostics": diagnostics}

    def summarize_reports(payload: dict[str, Any]) -> str:
        if not payload.get("reports"):
            return str(payload.get("note", "无研报记录"))
        lines = [f"共 {payload.get('count')} 篇，最新 {len(payload['reports'])} 篇："]
        for report in payload["reports"]:
            lines.append(f"- {report.get('date')} {report.get('org')}【{report.get('rating')}】{report.get('title')}")
        return "\n".join(lines)

    def dragon_tiger_board(args: dict[str, Any]) -> dict[str, Any]:
        symbol = normalize_symbol(str(args.get("symbol", "")))
        if not symbol.isdigit():
            return {"ok": False, "error": f"无法识别股票代码：{args.get('symbol')!r}"}
        look_back = max(7, min(int(args.get("look_back", 30)), 90))
        trade_date = str(args.get("trade_date") or date.today().isoformat())
        start = datetime.strptime(trade_date, "%Y-%m-%d") - timedelta(days=look_back)
        diagnostics: list[str] = []
        filter_records = (
            f"(TRADE_DATE>='{start:%Y-%m-%d}')(TRADE_DATE<='{trade_date}')(SECURITY_CODE=\"{symbol}\")"
        )
        try:
            rows = _eastmoney_datacenter(
                "RPT_DAILYBILLBOARD_DETAILSNEW",
                filter_str=filter_records,
                page_size=20,
                sort_columns="TRADE_DATE",
                diagnostics=diagnostics,
            )
        except requests.RequestException as exc:
            return {"ok": False, "error": f"龙虎榜接口失败：{exc}", "diagnostics": diagnostics}
        records = [
            {
                "date": str(row.get("TRADE_DATE", ""))[:10],
                "reason": row.get("EXPLANATION", ""),
                "net_buy_wan": _wan(row.get("BILLBOARD_NET_AMT")),
                "turnover_rate": row.get("TURNOVERRATE"),
            }
            for row in rows[:10]
        ]
        return {"ok": True, "symbol": symbol, "records": records, "diagnostics": diagnostics}

    def summarize_dragon_tiger(payload: dict[str, Any]) -> str:
        records = payload.get("records", [])
        if not records:
            return "回看窗口内无龙虎榜上榜记录"
        lines = [f"共 {len(records)} 次上榜："]
        for record in records:
            lines.append(f"- {record.get('date')} {record.get('reason')} 净买 {record.get('net_buy_wan')} 万")
        return "\n".join(lines)

    def limit_up_pool(args: dict[str, Any]) -> dict[str, Any]:
        pool_type = str(args.get("pool_type", "zt")).strip().lower()
        if pool_type not in ZT_POOL_ENDPOINTS:
            return {"ok": False, "error": "pool_type 只能是 zt/zb/dt/yzt"}
        raw_date = str(args.get("trade_date") or date.today().strftime("%Y%m%d")).replace("-", "")
        try:
            rows = fetch_limit_up_rows(pool_type, raw_date)
        except (requests.RequestException, ValueError) as exc:
            return {"ok": False, "error": f"涨停板接口失败：{exc}"}
        if not rows:
            return {"ok": True, "pool": [], "note": "该日期无数据（非交易日或尚未生成）"}
        return {
            "ok": True,
            "pool_type": ZT_POOL_NAMES[pool_type],
            "trade_date": raw_date,
            "count": len(rows),
            "items": rows[:20],
        }

    def summarize_limit_up(payload: dict[str, Any]) -> str:
        if not payload.get("items"):
            return str(payload.get("note", "无数据"))
        lines = [f"{payload.get('pool_type')}共 {payload.get('count')} 只，前 {len(payload['items'])} 只："]
        for item in payload["items"]:
            lines.append(
                f"- {item.get('name')}（{item.get('symbol')}）{item.get('zt_stat')} 连板{item.get('limit_days')} "
                f"炸板{item.get('break_times')}次 {item.get('industry')}"
            )
        return "\n".join(lines)

    def compare_stocks(args: dict[str, Any]) -> dict[str, Any]:
        raw_symbols = [str(s) for s in args.get("symbols", [])][:6]
        if len(raw_symbols) < 2:
            return {"ok": False, "error": "至少需要 2 只股票代码（最多 6 只）"}
        try:
            quotes, diagnostics = fetch_tencent_quotes(raw_symbols)
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc)}
        rows: list[dict[str, Any]] = []
        for quote in quotes:
            row = dict(quote)
            if backend is not None:
                window = max(20, min(int(args.get("window", 60)), 250))
                from datetime import timedelta

                end = date.today()
                start = end - timedelta(days=window * 3)
                frame = backend.warehouse.read_daily_bars(
                    symbols=[quote["symbol"]], start_date=start.isoformat(), end_date=end.isoformat(), require_ohlc=True
                )
                if not frame.empty:
                    closes = frame.sort_values("trade_date")["close"].astype(float)
                    returns = closes.pct_change().dropna()
                    row["window_return_pct"] = round(float(closes.iloc[-1] / closes.iloc[0] - 1.0), 4)
                    row["annualized_volatility_pct"] = (
                        round(float(returns.std() * (242**0.5)), 4) if len(returns) > 2 else None
                    )
                    row["max_drawdown_pct"] = round(float((closes / closes.cummax() - 1.0).min()), 4)
            rows.append(row)
        return {"ok": bool(rows), "rows": rows, "diagnostics": diagnostics}

    def summarize_compare(payload: dict[str, Any]) -> str:
        lines = ["多股对比（估值 + 区间统计）："]
        for row in payload.get("rows", []):
            lines.append(
                f"- {row.get('symbol')} {row.get('name')}：¥{row.get('price')} PE {row.get('pe_ttm')} / PB {row.get('pb')}，"
                f"总市值 {row.get('total_mcap_yi')} 亿"
                + (
                    f"，近段收益 {row.get('window_return_pct', 0):+.2%}，回撤 {row.get('max_drawdown_pct', 0):.2%}"
                    if row.get("window_return_pct") is not None
                    else ""
                )
            )
        return "\n".join(lines)

    return [
        AiTool(
            name="stock_valuation",
            description="批量查询 A 股实时估值：现价、涨跌幅、PE(TTM)/静态、PB、总市值/流通市值、换手率、涨跌停价（腾讯财经公开接口）。",
            parameters={
                "type": "object",
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"}, "description": "最多 10 个 6 位代码"},
                },
                "required": ["symbols"],
            },
            executor=stock_valuation,
            summarizer=summarize_valuation,
            digest_chars=2_600,
        ),
        AiTool(
            name="stock_research_reports",
            description="查询东财机构研报列表：日期、机构、评级、标题与 EPS 预测，用于基本面/机构观点分析。",
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位股票代码"},
                    "max_items": {"type": "integer", "description": "返回篇数，默认 8，最大 20"},
                },
                "required": ["symbol"],
            },
            executor=stock_research_reports,
            summarizer=summarize_reports,
            digest_chars=2_600,
        ),
        AiTool(
            name="dragon_tiger_board",
            description="查询个股龙虎榜上榜记录（日期、原因、龙虎榜净买额），用于资金面分析。",
            parameters={
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "6 位股票代码"},
                    "trade_date": {"type": "string", "description": "YYYY-MM-DD，默认今天"},
                    "look_back": {"type": "integer", "description": "回看天数，默认 30，最大 90"},
                },
                "required": ["symbol"],
            },
            executor=dragon_tiger_board,
            summarizer=summarize_dragon_tiger,
            digest_chars=2_600,
        ),
        AiTool(
            name="limit_up_pool",
            description="查询涨停/炸板/跌停/昨日涨停池，含连板梯队与行业，用于情绪面与题材分析。",
            parameters={
                "type": "object",
                "properties": {
                    "pool_type": {"type": "string", "enum": ["zt", "zb", "dt", "yzt"], "description": "默认 zt"},
                    "trade_date": {"type": "string", "description": "YYYYMMDD，默认今天"},
                },
            },
            executor=limit_up_pool,
            summarizer=summarize_limit_up,
            digest_chars=3_000,
        ),
        AiTool(
            name="compare_stocks",
            description=(
                "多股横向对比（2-6 只）：并排列出估值（PE/PB/市值）与本地区间统计（收益/波动/回撤），"
                "用于选股比较场景，一次调用代替逐只查询。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "symbols": {"type": "array", "items": {"type": "string"}, "description": "2-6 个 6 位代码"},
                    "window": {"type": "integer", "description": "本地统计窗口，默认 60 个交易日"},
                },
                "required": ["symbols"],
            },
            executor=compare_stocks,
            summarizer=summarize_compare,
        ),
    ]
