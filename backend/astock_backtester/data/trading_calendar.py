from __future__ import annotations

import logging
from datetime import date

import pandas as pd

logger = logging.getLogger(__name__)

_A_SHARE_HOLIDAY_RANGES: dict[int, tuple[tuple[str, str], ...]] = {
    2024: (
        ("2024-01-01", "2024-01-01"),
        ("2024-02-09", "2024-02-17"),
        ("2024-04-04", "2024-04-06"),
        ("2024-05-01", "2024-05-05"),
        ("2024-06-10", "2024-06-10"),
        ("2024-09-15", "2024-09-17"),
        ("2024-10-01", "2024-10-07"),
    ),
    2025: (
        ("2025-01-01", "2025-01-01"),
        ("2025-01-28", "2025-02-04"),
        ("2025-04-04", "2025-04-06"),
        ("2025-05-01", "2025-05-05"),
        ("2025-05-31", "2025-06-02"),
        ("2025-10-01", "2025-10-08"),
    ),
    2026: (
        ("2026-01-01", "2026-01-03"),
        ("2026-02-15", "2026-02-23"),
        ("2026-04-04", "2026-04-06"),
        ("2026-05-01", "2026-05-05"),
        ("2026-06-19", "2026-06-21"),
        ("2026-09-25", "2026-09-27"),
        ("2026-10-01", "2026-10-07"),
    ),
    2027: (
        ("2027-01-01", "2027-01-03"),
        ("2027-02-05", "2027-02-13"),
        ("2027-04-03", "2027-04-05"),
        ("2027-05-01", "2027-05-05"),
        ("2027-06-07", "2027-06-09"),
        ("2027-09-13", "2027-09-15"),
        ("2027-10-01", "2027-10-07"),
    ),
    2028: (
        ("2028-01-01", "2028-01-03"),
        ("2028-01-25", "2028-02-02"),
        ("2028-04-03", "2028-04-05"),
        ("2028-05-01", "2028-05-05"),
        ("2028-05-27", "2028-05-29"),
        ("2028-09-02", "2028-09-04"),
        ("2028-10-01", "2028-10-07"),
    ),
}


def _holiday_dates(start_date: pd.Timestamp, end_date: pd.Timestamp) -> set[pd.Timestamp]:
    dates: set[pd.Timestamp] = set()
    for year in range(start_date.year, end_date.year + 1):
        for range_start, range_end in _A_SHARE_HOLIDAY_RANGES.get(year, ()):
            for day in pd.date_range(range_start, range_end, freq="D"):
                dates.add(pd.Timestamp(day).normalize())
    return dates


def holiday_table_last_year() -> int:
    """节假日硬编码表覆盖的最后一年。

    表只到 2028 年；更晚的年份里春节/国庆等休市日会被当作交易日计入
    coverage 缺口（补齐链路永远补不回来）。调用方（coverage/同步）据此
    打日志预警，提醒维护者每年更新该表。
    """
    return max(_A_SHARE_HOLIDAY_RANGES) if _A_SHARE_HOLIDAY_RANGES else 0


_holiday_gap_warned = False


def a_share_trade_dates(start_date: pd.Timestamp | date | str, end_date: pd.Timestamp | date | str) -> set[pd.Timestamp]:
    global _holiday_gap_warned
    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    if end < start:
        return set()
    if not _holiday_gap_warned and end.year > holiday_table_last_year():
        _holiday_gap_warned = True
        logger.warning(
            "交易日历节假日表只覆盖到 %d 年，%d 年及以后的休市日会被当作交易日计入缺口；"
            "请更新 data/trading_calendar.py 的 _A_SHARE_HOLIDAY_RANGES。",
            holiday_table_last_year(),
            end.year,
        )
    weekdays = {pd.Timestamp(day).normalize() for day in pd.date_range(start=start, end=end, freq="B")}
    return weekdays - _holiday_dates(start, end)
