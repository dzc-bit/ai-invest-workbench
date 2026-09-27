"""架构守卫：data / engine / ai 的依赖方向必须单向（AGENTS.md §15-4、§15-8）。

这类约束光写在文档里会腐烂：加一条 import 只要一行代码，而发现反向依赖要花
一次重构。这里用静态 AST 扫描把口径钉住——白名单按**当前事实**登记，新增违规
直接失败；已经存在的历史耦合要解除只能改代码，不能改这张表以外的规则。
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend" / "astock_backtester"
PACKAGE = "astock_backtester"


def _module_name(path: Path) -> str:
    relative = path.relative_to(BACKEND_ROOT.parent)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_relative(module: str, level: int) -> str:
    """把相对 import 解析成绝对模块名。

    ``level`` 是点的个数：``from ..service import x`` 在 ``pkg.data.sync`` 里
    等于 ``pkg.service``。不解析的话相对 import 根本进不了依赖图，整张依赖
    方向检查可以用 ``from ..service import x`` 一句话绕过。
    """
    parts = module.split(".")
    # level=1 表示当前包（``from . import x``），要从自身模块名里去掉最后一段。
    if parts and parts[-1] != "__init__":
        parts = parts[:-1]
    for _ in range(level - 1):
        if parts:
            parts.pop()
    return ".".join(parts)


def _imports(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    module = _module_name(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name.startswith(PACKAGE))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # 相对 import：先解析成绝对路径，再按绝对口径统计。
                base = _resolve_relative(module, node.level)
                target = f"{base}.{node.module}" if node.module else base
                found.add(target)
                for alias in node.names:
                    found.add(f"{target}.{alias.name}")
                continue
            if not node.module or not node.module.startswith(PACKAGE):
                continue
            found.add(node.module)
            # ``from astock_backtester.ai import tools`` 之类也要算到子模块。
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
    return {name for name in found if name != PACKAGE}


def _graph() -> dict[str, set[str]]:
    files = sorted(BACKEND_ROOT.rglob("*.py"))
    module_names = {_module_name(path) for path in files}
    graph: dict[str, set[str]] = defaultdict(set)
    for path in files:
        module = _module_name(path)
        for target in _imports(path):
            # 只统计"确实是一个模块"的边：``from pkg.ai import tools`` 里
            # tools 既是子模块也可能是包内符号。
            if target in module_names:
                graph[module].add(target)
    return graph


ROOT_MODULES = {
    "models",
    "engine",
    "cli",
    "service",
    "backtest_runner",
    "conditions",
    "condition_parser",
    "indicators",
    "recommended_strategies",
    "sample_data",
    "__main__",
}


def _group(module: str) -> str:
    """``astock_backtester.models`` 是根模块，``astock_backtester.ai`` / ``.data.sync`` 属于子包。"""
    parts = module.split(".")
    if len(parts) == 1:
        return "package"
    head = parts[1]
    return "root" if head in ROOT_MODULES else head


@pytest.mark.parametrize("group", ["data", "ai", "root"])
def test_no_layer_below_the_service_imports_it_back_up(group: str):
    """data/ai/根包模块（engine、backtest_runner…）都不得反向 import service。

    service 是最外层的 HTTP 适配：它被任何下层依赖就等于把 HTTP 语义塞进数据层。
    """
    graph = _graph()
    offenders = {
        f"{module} -> {target}"
        for module, targets in graph.items()
        if (_group(module) == group if group != "root" else module.startswith(f"{PACKAGE}.") and _group(module) == "root")
        and not module.endswith(".service")
        for target in targets
        if target == f"{PACKAGE}.service" or target.startswith(f"{PACKAGE}.service.")
    }
    assert not offenders, f"{group} 反向依赖了 service：{sorted(offenders)}"


DATA_SHARED_MODULES = {
    # AGENTS.md §15-4 的 data 内共享模块清单（以 data/ 目录现状为准）。
    "symbols",
    "parsing",
    "http_transport",
    "importer",
    "trading_calendar",
    "cls",
    "cache",
    "warehouse",
    "operations",
    "filelock",
    "astock_adapter",
    "cls_finance",
    "realtime",
    "realtime_parsers",
    "text_cleaning",
    "briefing",
    "news",
    "news_summary",
    "market_commentary",
    "capital_flow_crawler",
    "providers",
    "risk",
    "sync",
}
ROOT_MODULES_ALLOWED_IN_DATA = {"models"}


def test_data_layer_only_touches_models_and_its_own_shared_modules():
    graph = _graph()
    violations: list[str] = []
    for module, targets in graph.items():
        if _group(module) != "data":
            continue
        for target in targets:
            target_group = _group(target)
            if target_group == "data":
                leaf = target.split(".")[-1]
                if leaf not in DATA_SHARED_MODULES:
                    violations.append(f"{module} -> {target}（未知 data 模块）")
                continue
            if target_group == "root" and target.split(".")[-1] in ROOT_MODULES_ALLOWED_IN_DATA:
                continue
            violations.append(f"{module} -> {target}")
    assert not violations, "data/* 越界依赖：\n" + "\n".join(sorted(violations))


def test_ai_boundary_stays_on_public_data_and_models():
    """AI 子包只依赖 models、data 公共接口与根包的回测/条件/指标内核。"""
    allowed_root = {
        "models",
        "backtest_runner",
        "condition_parser",
        "conditions",
        "indicators",
        "recommended_strategies",
    }
    graph = _graph()
    violations = [
        f"{module} -> {target}"
        for module, targets in graph.items()
        if _group(module) == "ai"
        for target in targets
        if _group(target) == "root" and target.split(".")[-1] not in allowed_root
    ]
    assert not violations, "ai/* 越界依赖根包：\n" + "\n".join(sorted(violations))


def test_data_and_engine_do_not_import_the_ai_package():
    """§15-8：ai 是可插拔子包，数据层与撮合引擎不能知道它的存在。

    service 是唯一合法的接线方（它在 ai 之上），因此不在本规则范围内。
    """
    forbidden_sources = {"engine", "cli", "backtest_runner", "conditions", "indicators"}
    graph = _graph()
    offenders = [
        f"{module} -> {target}"
        for module, targets in graph.items()
        for target in targets
        if target.startswith(f"{PACKAGE}.ai")
        and (
            _group(module) == "data"
            or (_group(module) == "root" and module.split(".")[-1] in forbidden_sources)
        )
    ]
    assert not offenders, f"数据层/引擎反向依赖 ai：{sorted(offenders)}"


def test_backend_uses_absolute_imports_only():
    """backend 内一律用绝对 import。

    相对 import（``from ..service import x``）让依赖方向只能靠读代码发现，
    绕得开这张表维护的静态检查；统一用绝对路径后，分组判定才是可靠的。
    """
    offenders: list[str] = []
    for path in sorted(BACKEND_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                offenders.append(f"{path.name}:{node.lineno}: from {'.' * node.level}{node.module or ''}")
    assert not offenders, "backend 出现了相对 import：\n" + "\n".join(offenders)


def test_package_has_no_import_cycles():
    """§15-4 的"全仓 0 个 import 环"是事实，也是这条守卫的存在理由。"""
    graph = _graph()
    state: dict[str, int] = {}
    stack: list[str] = []
    cycles: list[str] = []

    def visit(module: str) -> None:
        marker = state.get(module, 0)
        if marker == 1:
            start = stack.index(module)
            cycles.append(" -> ".join([*stack[start:], module]))
            return
        if marker == 2:
            return
        state[module] = 1
        stack.append(module)
        for target in sorted(graph.get(module, ())):
            visit(target)
        stack.pop()
        state[module] = 2

    for module in sorted(graph):
        visit(module)
    assert not cycles, "出现 import 环：\n" + "\n".join(cycles)
