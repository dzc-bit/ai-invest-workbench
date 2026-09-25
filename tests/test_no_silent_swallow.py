"""静默失败棘轮（借 deepseek-harness 的"配置/失败必须响亮"纪律）。

P1-① 的教训：`item.summary` 的 AttributeError 被紧邻的
``except Exception:\\n pass`` 吞掉，功能静默失效两个版本无人发现。
本守卫把"静默吞异常"圈定在一份**显式豁免名单**里：新代码再写
``except Exception:`` + ``pass``（且 try 体内无日志/计数）必须先到这里
登记理由，否则测试红——既不放任增量，也不逼着一次性清历史。
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"

# 格式：相对路径 -> 豁免条数。每条豁免都必须有理由（见对应代码注释）。
# 理由失效（代码变更后该处已能响亮失败）就把数字减到 0 并删行（§17.3）。
# 基线冻结于 2026-09-25（共 17 处，均为 best-effort 定时引擎/收尾路径）：
# 本名单**只许减不许增**——新增静默吞异常会让本测试变红。
ALLOWED_SILENT_SWALLOWS: dict[str, int] = {
    "astock_backtester/ai/digest.py": 6,
    "astock_backtester/ai/facade.py": 2,
    "astock_backtester/ai/insights.py": 1,
    "astock_backtester/ai/reports.py": 6,
    "astock_backtester/ai/tools/query_tools.py": 1,
    "astock_backtester/data/realtime.py": 1,
}

# except 行尾允许带注释（# noqa: BLE001 等标记）
_PATTERN = re.compile(
    r"except (?:Exception|BaseException)(?: as \w+)?:\s*(?:#[^\n]*)?\n\s*pass\b"
)


def _iter_silent_swallows() -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in BACKEND.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        hits = len(_PATTERN.findall(text))
        if hits:
            counts[path.relative_to(BACKEND).as_posix()] = hits
    return counts


def test_silent_exception_swallowing_is_capped_by_an_explicit_allowlist():
    actual = _iter_silent_swallows()
    unexpected = {path: count for path, count in actual.items() if count > ALLOWED_SILENT_SWALLOWS.get(path, 0)}
    assert not unexpected, (
        "发现新的静默吞异常点（except Exception: pass）——静默失效最难被发现的 bug 形态。"
        "改为 log/计数/diagnostics，或确属'失败必须无声'时在 tests/test_no_silent_swallow.py"
        f" 的 ALLOWED_SILENT_SWALLOWS 登记理由：{unexpected}"
    )
    stale = [path for path in ALLOWED_SILENT_SWALLOWS if path not in actual]
    assert not stale, f"豁免名单里有条目已不再匹配任何代码，请删掉：{stale}"
