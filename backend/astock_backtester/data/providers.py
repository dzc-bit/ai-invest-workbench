from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import pandas as pd

from astock_backtester.data.astock_adapter import AStockDataAdapter
from astock_backtester.data.importer import normalize_daily_bars
from astock_backtester.data.symbols import normalize_symbol

__all__ = [
    "ADataProvider",
    "AkshareProvider",
    "CompositeProvider",
    "DailyDataProvider",
    "HttpAStockProvider",
    "ProviderError",
    "normalize_symbol",
]


class ProviderError(RuntimeError):
    pass


class DailyDataProvider(Protocol):
    name: str

    def list_symbols(self) -> list[str]:
        ...

    def fetch_daily_bars(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        ...

    def fetch_share_history(self, symbol: str) -> pd.DataFrame:
        ...


def _unique_symbols(symbols: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for symbol in symbols:
        code = normalize_symbol(symbol)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def enrich_market_cap_from_share_history(bars: pd.DataFrame, shares: pd.DataFrame) -> pd.DataFrame:
    out = bars.copy()
    if out.empty:
        return out
    if shares.empty:
        out["float_market_cap"] = float("nan")
        out["total_market_cap"] = float("nan")
        return out

    out["trade_date"] = pd.to_datetime(out["trade_date"])
    share_frame = shares.copy()
    share_frame["change_date"] = pd.to_datetime(share_frame["change_date"])
    share_frame = share_frame.sort_values("change_date")
    merged = pd.merge_asof(
        out.sort_values("trade_date"),
        share_frame[["change_date", "total_shares", "list_a_shares"]].sort_values("change_date"),
        left_on="trade_date",
        right_on="change_date",
        direction="backward",
    )
    merged["float_market_cap"] = pd.to_numeric(merged["list_a_shares"], errors="coerce") * merged["close"]
    merged["total_market_cap"] = pd.to_numeric(merged["total_shares"], errors="coerce") * merged["close"]
    return merged.drop(
        columns=[column for column in ["change_date", "total_shares", "list_a_shares"] if column in merged]
    )


@dataclass
class ADataProvider:
    name: str = "adata"

    def _adata(self):
        import adata

        return adata

    def list_symbols(self) -> list[str]:
        adata = self._adata()
        frame = adata.stock.info.all_code()
        if frame is None or frame.empty:
            return []
        code_column = next(
            (column for column in ["stock_code", "code", "symbol"] if column in frame.columns),
            frame.columns[0],
        )
        return _unique_symbols([str(item) for item in frame[code_column].dropna().tolist()])

    def list_symbol_listings(self) -> dict[str, str | None]:
        """Current-market symbol -> listing date (ISO string, possibly ``None``).

        Backed by adata's ``all_code()`` which carries a ``list_date`` column.
        Raises whatever the underlying source raises so callers can fall back.
        """
        adata = self._adata()
        frame = adata.stock.info.all_code()
        if frame is None or frame.empty:
            return {}
        code_column = next(
            (column for column in ["stock_code", "code", "symbol"] if column in frame.columns),
            frame.columns[0],
        )
        has_list_date = "list_date" in frame.columns
        selected = frame[[code_column, "list_date"]] if has_list_date else frame[[code_column]]
        listings: dict[str, str | None] = {}
        # itertuples 替代 iterrows：list_symbol_listings 在每次全市场同步前都会
        # 扫全市场帧，iterrows 的逐行 Series 构造是大头开销。
        for record in selected.itertuples(index=False, name=None):
            code = normalize_symbol(str(record[0]))
            if not code or code in listings:
                continue
            if not has_list_date:
                listings[code] = None
                continue
            raw_date = record[1]
            if raw_date is None or pd.isna(raw_date):
                listings[code] = None
                continue
            listings[code] = str(raw_date)[:10]
        return listings

    def fetch_daily_bars(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        adata = self._adata()
        code = normalize_symbol(symbol)
        frame = adata.stock.market.get_market(stock_code=code, start_date=start_date, end_date=end_date, k_type=1)
        if frame is None or frame.empty:
            return pd.DataFrame()
        frame = frame.rename(columns={"stock_code": "symbol", "turnover_ratio": "turnover_rate"})
        frame["symbol"] = code
        frame["source"] = self.name
        try:
            shares = self.fetch_share_history(code)
        except Exception:
            shares = pd.DataFrame()
        return normalize_daily_bars(enrich_market_cap_from_share_history(frame, shares))

    def fetch_share_history(self, symbol: str) -> pd.DataFrame:
        adata = self._adata()
        code = normalize_symbol(symbol)
        frame = adata.stock.info.get_stock_shares(stock_code=code, is_history=True)
        return pd.DataFrame() if frame is None else frame


@dataclass
class HttpAStockProvider:
    name: str = "http"

    def list_symbols(self) -> list[str]:
        return []

    def fetch_daily_bars(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        frame = AStockDataAdapter.from_http_sources().fetch_daily_bars([symbol], start_date, end_date)
        if frame.empty:
            return frame
        frame["source"] = self.name
        return frame

    def fetch_share_history(self, symbol: str) -> pd.DataFrame:
        return pd.DataFrame()


def _akshare_date(value: str) -> str:
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%Y%m%d")


@dataclass
class AkshareProvider:
    name: str = "akshare"

    def _akshare(self):
        import akshare as ak

        return ak

    def list_symbols(self) -> list[str]:
        ak = self._akshare()
        frame = ak.stock_zh_a_spot_em()
        if frame is None or frame.empty:
            return []
        code_column = next((column for column in ["代码", "股票代码", "symbol", "code"] if column in frame.columns), frame.columns[0])
        return _unique_symbols([str(item) for item in frame[code_column].dropna().tolist()])

    def fetch_realtime_spot_rows(self) -> list[dict[str, object]]:
        ak = self._akshare()
        frame = ak.stock_zh_a_spot_em()
        if frame is None or frame.empty:
            return []
        columns = {
            "code": next((column for column in ["代码", "股票代码", "symbol", "code"] if column in frame.columns), None),
            "name": next((column for column in ["名称", "股票简称", "name"] if column in frame.columns), None),
            "price": next((column for column in ["最新价", "现价", "close", "price"] if column in frame.columns), None),
            "change_pct": next((column for column in ["涨跌幅", "change_pct", "pct_chg"] if column in frame.columns), None),
            "turnover": next((column for column in ["换手率", "turnover_rate", "turnover"] if column in frame.columns), None),
            "volume_ratio": next((column for column in ["量比", "volume_ratio"] if column in frame.columns), None),
            "float_market_cap": next((column for column in ["流通市值", "float_market_cap"] if column in frame.columns), None),
        }
        if not all(columns[key] for key in ("code", "name", "price", "change_pct")):
            return []
        optional_keys = [key for key in ("turnover", "volume_ratio", "float_market_cap") if columns[key]]
        optional_labels = {"turnover": "换手率", "volume_ratio": "量比", "float_market_cap": "流通市值"}
        selected = frame[[columns["code"], columns["name"], columns["price"], columns["change_pct"],
                          *(columns[key] for key in optional_keys)]]
        rows: list[dict[str, object]] = []
        for record in selected.itertuples(index=False, name=None):
            code = normalize_symbol(str(record[0]))
            name = str(record[1]).strip()
            if not code or not name:
                continue
            row: dict[str, object] = {
                "代码": code,
                "名称": name,
                "现价": record[2],
                "涨跌幅": record[3],
            }
            for offset, key in enumerate(optional_keys, start=4):
                row[optional_labels[key]] = record[offset]
            rows.append(row)
        return rows

    def fetch_daily_bars(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        ak = self._akshare()
        code = normalize_symbol(symbol)
        frame = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=_akshare_date(start_date),
            end_date=_akshare_date(end_date),
            adjust="",
        )
        if frame is None or frame.empty:
            return pd.DataFrame()
        normalized = frame.rename(
            columns={
                "日期": "trade_date",
                "股票代码": "symbol",
                "代码": "symbol",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
                "涨跌幅": "change_pct",
                "涨跌额": "change",
                "换手率": "turnover_rate",
            }
        )
        normalized["symbol"] = code
        normalized["source"] = self.name
        return normalize_daily_bars(normalized)

    def fetch_share_history(self, symbol: str) -> pd.DataFrame:
        return pd.DataFrame()


@dataclass
class CompositeProvider:
    providers: list[DailyDataProvider]

    def list_symbols(self) -> list[str]:
        errors: list[str] = []
        for provider in self.providers:
            try:
                symbols = provider.list_symbols()
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            normalized = _unique_symbols(symbols)
            if normalized:
                return normalized
        if errors:
            raise ProviderError("; ".join(errors))
        return []

    def list_symbol_listings(self) -> dict[str, str | None]:
        errors: list[str] = []
        for provider in self.providers:
            listing_reader = getattr(provider, "list_symbol_listings", None)
            if listing_reader is None:
                continue
            try:
                listings = listing_reader()
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            if listings:
                return listings
        if errors:
            raise ProviderError("; ".join(errors))
        return {}

    def fetch_daily_bars(self, symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
        errors: list[str] = []
        for provider in self.providers:
            try:
                frame = provider.fetch_daily_bars(symbol, start_date, end_date)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            if not frame.empty:
                if "source" not in frame.columns:
                    frame["source"] = provider.name
                return normalize_daily_bars(frame)
            errors.append(f"{provider.name}: returned no daily rows")
        if errors:
            raise ProviderError("; ".join(errors))
        return pd.DataFrame()
