"""One-shot backfill of ``float_market_cap`` for OHLC rows where it is null.

The public daily K-line sources never carry market cap, and rows imported from
older runs can sit in the warehouse with ``float_market_cap = NULL`` forever —
they keep the full-market sync loop re-fetching those symbols every round.

This script fills those rows from adata's share-change history
(``adata.stock.info.get_stock_shares(is_history=True)``): share counts only
change on ``change_date``, so every trading day is aligned with a backward
``merge_asof`` and ``float_market_cap = list_a_shares × close``.  The merge
itself is **not** re-implemented here — it reuses
``providers.enrich_market_cap_from_share_history`` so share/date parsing stays
in one home (AGENTS §15 architecture invariant 1).

Safety notes:

* ``--dry-run`` counts and prints without touching the warehouse;
* written frames carry the **whole original row** (all warehouse columns), not
  just the market-cap column: ``Warehouse.write_daily_bars`` runs
  ``normalize_daily_bars`` first, which fills absent optional columns with
  defaults (``amount=0.0``, ``turnover_rate=0.0``, ...) and those non-null
  defaults would overwrite the real values through ``combine_first``;
* rows that are not ``float_market_cap``-null OHLC rows are never written.

Usage::

    python scripts/backfill-market-cap.py --cache-dir "D:\\New project 6\\运行产物\\本地数据仓" --dry-run
    python scripts/backfill-market-cap.py --cache-dir ... --symbols 000001,000002
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from astock_backtester.data.providers import enrich_market_cap_from_share_history  # noqa: E402
from astock_backtester.data.symbols import normalize_symbol  # noqa: E402
from astock_backtester.data.warehouse import OHLC_COLUMNS, Warehouse  # noqa: E402

# 股本历史里真正参与回填的两列：变动日 + 流通 A 股数。
SHARE_HISTORY_REQUIRED_COLUMNS = ("change_date", "list_a_shares")


def fetch_share_history(stock_code: str) -> pd.DataFrame:
    """adata 股本变动快照（``change_date`` 是股本变动日，不是逐日）。"""
    import adata

    frame = adata.stock.info.get_stock_shares(stock_code=stock_code, is_history=True)
    return pd.DataFrame() if frame is None else pd.DataFrame(frame)


def share_history_usable(shares: pd.DataFrame | None) -> bool:
    """股本历史是否可用于回填：非空、必需列齐全、``list_a_shares`` 至少一个可解析值。"""
    if shares is None or not isinstance(shares, pd.DataFrame) or shares.empty:
        return False
    if not all(column in shares.columns for column in SHARE_HISTORY_REQUIRED_COLUMNS):
        return False
    return bool(pd.to_numeric(shares["list_a_shares"], errors="coerce").notna().any())


def fill_market_cap(rows: pd.DataFrame, shares: pd.DataFrame) -> pd.DataFrame:
    """给一批 null 市值的 OHLC 行回填 ``float_market_cap``，返回**整行**写入帧。

    只带回新算出的流通市值：adata 推导的 ``total_market_cap`` 与其余列一律保留
    仓库原值（写侧 ``combine_first`` 下非空新值会覆盖旧值，无关列不得进写入帧）。
    两个 ``change_date`` 之间股本不变，``merge_asof(direction="backward")`` 把
    变动日快照对齐到每个交易日；早于首个变动日的行保持 null（不写）。
    """
    if rows.empty or not share_history_usable(shares):
        return pd.DataFrame()
    # merge_asof 要求两侧键 dtype 完全相同：分区读出的 trade_date 是
    # datetime64[ns]，而字符串 change_date 在 pandas 3 被解析成 datetime64[us]，
    # 直接合并会抛 MergeError。统一到 ns 再进共享 enrich（它内部的 to_datetime
    # 会保留已给定的单位）。
    rows = rows.copy()
    rows["trade_date"] = pd.to_datetime(rows["trade_date"]).astype("datetime64[ns]")
    share_frame = shares.copy()
    share_frame["change_date"] = pd.to_datetime(share_frame["change_date"], errors="coerce").astype("datetime64[ns]")
    enriched = enrich_market_cap_from_share_history(rows, share_frame)
    enriched = enriched.drop(columns=["total_market_cap"], errors="ignore")
    filled = enriched[enriched["float_market_cap"].notna()]
    if filled.empty:
        return pd.DataFrame()
    return filled


def _partition_candidates(table) -> pd.DataFrame | None:
    """从一个日线分区里挑出「OHLC 完整但 ``float_market_cap`` 为空」的行。

    在 arrow 侧做掩码：整列无 null 时直接跳过分区（不物化成 pandas），
    缺 ``float_market_cap`` 列的旧分区按“全部为空”处理；缺 OHLC 列则不是
    OHLC 行结构，返回 ``None`` 表示无可回填内容。
    """
    names = set(table.column_names)
    if "float_market_cap" in names and table.column("float_market_cap").null_count == 0:
        return None
    mask = None
    if "float_market_cap" in names:
        mask = pc.is_null(table.column("float_market_cap"))
    for column in OHLC_COLUMNS:
        if column not in names:
            return None
        column_mask = pc.is_valid(table.column(column))
        mask = column_mask if mask is None else pc.and_(mask, column_mask)
    if mask is None:
        return None
    candidates = table.filter(mask).to_pandas()
    if candidates.empty:
        return None
    candidates["symbol"] = candidates["symbol"].astype(str)
    return candidates


def backfill_market_cap(
    warehouse: Warehouse,
    *,
    symbols: list[str] | None = None,
    dry_run: bool = False,
    share_fetcher=None,
) -> dict[str, object]:
    """回填仓库中 null ``float_market_cap`` 的 OHLC 行，返回 filled/skipped/failed 计数。

    返回 dict 的 ``filled``/``skipped``/``failed`` 是三个计数（filled 为行数，
    其余为股票数），另附 ``filled_symbols``/``skipped_symbols``/``failed_symbols``
    三个明细列表：

    - ``filled``：实际回填的**行数**；
    - ``skipped``：没有可用股本历史（列缺失/为空）或一个行都没回填的**股票数**；
    - ``failed``：取股本历史时抛异常的**股票数**（单股票异常不中断整体）。

    分区逐个读写，内存上限是“一个分区 + 该分区的写入帧”；``share_fetcher``
    按股票缓存（含失败结果），同一股票跨分区只请求一次。
    """
    if share_fetcher is None:
        # 调用时解析（而不是默认参数）：CLI 测试可以 monkeypatch 模块级实现。
        share_fetcher = fetch_share_history
    selected = {normalize_symbol(symbol) for symbol in symbols if normalize_symbol(symbol)} if symbols else None
    filled_count = 0
    filled_symbols: set[str] = set()
    failed_symbols: set[str] = set()
    seen_symbols: set[str] = set()
    share_cache: dict[str, pd.DataFrame] = {}

    for path in warehouse.daily_bars_parquet_paths():
        try:
            table = pq.read_table(path)
            candidates = _partition_candidates(table)
        except Exception as exc:  # noqa: BLE001 - 单个损坏分区不中断整体回填
            print(f"[backfill-market-cap] unreadable partition {path}: {exc}")
            continue
        if candidates is None or candidates.empty:
            continue
        if selected is not None:
            candidates = candidates[candidates["symbol"].isin(selected)]
        if candidates.empty:
            continue

        write_frames: list[pd.DataFrame] = []
        for symbol, symbol_rows in candidates.groupby("symbol", sort=False):
            symbol_text = str(symbol)
            seen_symbols.add(symbol_text)
            if symbol_text in failed_symbols:
                continue
            if symbol_text not in share_cache:
                try:
                    shares = share_fetcher(symbol_text)
                except Exception as exc:  # noqa: BLE001 - 单股票异常不得中断整体
                    print(f"[backfill-market-cap] {symbol_text}: share history failed: {exc}")
                    failed_symbols.add(symbol_text)
                    continue
                share_cache[symbol_text] = shares
            filled = fill_market_cap(symbol_rows.reset_index(drop=True), share_cache[symbol_text])
            if filled.empty:
                continue
            filled_symbols.add(symbol_text)
            filled_count += int(len(filled))
            write_frames.append(filled)

        if write_frames and not dry_run:
            warehouse.write_daily_bars(pd.concat(write_frames, ignore_index=True))

    skipped_symbols = seen_symbols - filled_symbols - failed_symbols
    return {
        "filled": filled_count,
        "skipped": len(skipped_symbols),
        "failed": len(failed_symbols),
        "filled_symbols": sorted(filled_symbols),
        "skipped_symbols": sorted(skipped_symbols),
        "failed_symbols": sorted(failed_symbols),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill null float_market_cap rows from adata share history")
    parser.add_argument("--cache-dir", required=True, help="warehouse root (运行产物/本地数据仓)")
    parser.add_argument("--symbols", default="", help="comma separated symbol filter, e.g. 000001,000002")
    parser.add_argument("--dry-run", action="store_true", help="count only, never write to the warehouse")
    args = parser.parse_args()

    symbols = [part.strip() for part in args.symbols.split(",") if part.strip()] or None
    warehouse = Warehouse(args.cache_dir)
    result = backfill_market_cap(warehouse, symbols=symbols, dry_run=args.dry_run)

    suffix = " (dry-run, nothing written)" if args.dry_run else ""
    print(
        f"backfill-market-cap{suffix}: filled={result['filled']} rows, "
        f"skipped={result['skipped']} symbols, failed={result['failed']} symbols"
    )
    if result["skipped_symbols"]:
        print(f"  skipped symbols: {', '.join(result['skipped_symbols'])}")
    if result["failed_symbols"]:
        print(f"  failed symbols: {', '.join(result['failed_symbols'])}")


if __name__ == "__main__":
    main()
