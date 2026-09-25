"""Shared A-share symbol normalization helpers.

Consolidates implementations that were previously duplicated across
``providers``, ``capital_flow_crawler``, ``astock_adapter``, ``briefing``
and ``risk``.  All helpers are pure string functions with no I/O.
"""

from __future__ import annotations


def normalize_symbol(symbol: str) -> str:
    """Return the bare 6-digit A-share code for ``symbol``.

    Accepts ``SH/SZ/BJ`` prefixes and ``.XX`` suffixes; digit-only results are
    zero-padded to six digits.
    """
    code = str(symbol).strip().upper()
    if code.startswith(("SH", "SZ", "BJ")):
        code = code[2:]
    if "." in code:
        code = code.split(".", 1)[0]
    return code.zfill(6) if code.isdigit() else code


def a_share_market_symbol(symbol: str) -> str | None:
    """Convert an A-share code to the ``sh``/``sz``/``bj`` prefix form used by
    Sina and Tencent quote APIs.

    ``900xxx`` Shanghai B-shares map to ``sh``; ``920xxx`` is the *Beijing*
    exchange segment and must map to ``bj`` — Sina and Tencent both answer
    ``none_match``/empty for ``sh920xxx``, so the old blanket ``9`` -> ``sh``
    rule silently dropped every 920 code from quotes and capital-flow
    backfills.  ``None`` is returned for codes that do not match a known
    A-share digit prefix.
    """
    code = normalize_symbol(symbol)
    if not code or not code.isdigit():
        return None
    if code.startswith("92"):
        return f"bj{code}"
    if code.startswith(("6", "9")):
        return f"sh{code}"
    if code.startswith(("0", "2", "3")):
        return f"sz{code}"
    if code.startswith(("4", "8")):
        return f"bj{code}"
    return None


def sina_summary_symbol(symbol: str) -> str | None:
    """``s_``-prefixed Sina symbol form used by the market summary quote API."""
    code = a_share_market_symbol(symbol)
    return f"s_{code}" if code else None


def market_code(code: str) -> int:
    """Eastmoney numeric market prefix: 1 for Shanghai (6/9), 0 otherwise.

    ``920xxx`` Beijing codes resolve through :func:`a_share_market_symbol`'s
    ``bj`` branch but Eastmoney's ``push2`` family still addresses them under
    the ``0`` market bucket (verified against ``push2`` spot: ``secid=0.920171``
    returns the quote), so only ``6``/``9`` Shanghai-listed codes take prefix 1.
    """
    normalized = normalize_symbol(code)
    if normalized.startswith("92"):
        return 0
    return 1 if normalized.startswith(("6", "9")) else 0
