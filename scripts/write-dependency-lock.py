#!/usr/bin/env python
"""写出**当前环境**的发布依赖锁：让安装包构建的输入变成一个确定的文件。

``pyproject.toml`` 只有版本下限（``pandas>=2.2``），所以"这次发布的包里有哪个
pandas"此前无人能回答。PyInstaller 打包的是解释器里**实际装着**的东西，因此锁
必须由打包用的那个解释器自己生成，而不是手抄一份期望值——抄来的数字迟早和代码
不一致，这正是 AGENTS.md §17 禁止的漂移。

用法（发布构建前，在将要用于打包的解释器里执行）::

    python scripts/write-dependency-lock.py --out requirements-release.lock.txt

- 只收录 ``pyproject.toml`` 声明的直接依赖及其**可达的传递闭包**，不混入本机
  恰好装着的无关包。
- 任何声明了但环境里装不上的依赖都会让脚本以非零码退出：缺依赖的锁没有意义。
- 头部写明 Python 版本与生成方式，锁本身就是"这个包是在什么环境下打出来的"的记录。
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from importlib import metadata
from pathlib import Path


def _normalize(name: str) -> str:
    text = name.strip()
    for index, character in enumerate(text):
        if character in "<>=!~[;( ":
            text = text[:index]
            break
    return text.strip().lower().replace("_", "-")


def declared_dependencies(pyproject: Path) -> set[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    project = data.get("project", {})
    direct = {_normalize(str(item)) for item in project.get("dependencies", [])}
    for group in (project.get("optional-dependencies") or {}).values():
        direct.update(_normalize(str(item)) for item in group)
    return direct


def closure(seed: set[str]) -> dict[str, str]:
    """seed 依赖 + 其 requires 的传递闭包，键为规范化包名，值为已安装版本。"""
    installed = {
        _normalize(str(dist.metadata["Name"])): dist.version
        for dist in metadata.distributions()
        if dist.metadata and dist.metadata["Name"]
    }
    resolved: dict[str, str] = {}
    queue = list(seed)
    while queue:
        name = queue.pop()
        if name in resolved or name not in installed:
            continue
        resolved[name] = installed[name]
        for requirement in metadata.requires(name) or []:
            text = str(requirement)
            # 只要在当前解释器上真正会被导入的依赖：extras 与环境标记按需忽略，
            # 因为打包环境里装的就是最终集合。
            marker_start = text.find(";")
            marker = text[marker_start + 1 :] if marker_start >= 0 else ""
            if marker and not _marker_matches(marker):
                continue
            child = _normalize(text)
            if child:
                queue.append(child)
    return resolved


def _marker_matches(marker: str) -> bool:
    """保守判定：``extra ==`` 一律不算（发布环境不装 extras），``python_version`` 按当前解释器比。"""
    text = marker.strip()
    if "extra ==" in text:
        return False
    if "python_version" in text:
        import re

        match = re.search(r"python_version\s*(<=|>=|<|>|==|!=)\s*['\"]([0-9.]+)['\"]", text)
        if match:
            operator, wanted = match.group(1), tuple(int(part) for part in match.group(2).split("."))
            current = sys.version_info[:2] + (sys.version_info.micro,)
            padded = wanted + (0,) * (len(current) - len(wanted))
            order = {
                "==": current == padded,
                "!=": current != padded,
                ">=": current >= padded,
                "<=": current <= padded,
                ">": current > padded,
                "<": current < padded,
            }
            return bool(order.get(operator))
    return True


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(repo_root / "requirements-release.lock.txt"))
    parser.add_argument(
        "--pyproject",
        default=str(repo_root / "pyproject.toml"),
        help="声明依赖来源（默认仓库根的 pyproject.toml）",
    )
    args = parser.parse_args(argv)

    declared = declared_dependencies(Path(args.pyproject))
    resolved = closure(declared)
    missing = sorted(declared - set(resolved))
    if missing:
        print(f"缺少已声明依赖，无法生成发布锁：{', '.join(missing)}", file=sys.stderr)
        return 1

    version = ".".join(str(part) for part in sys.version_info[:3])
    lines = [
        "# 自动生成，请勿手改：python scripts/write-dependency-lock.py",
        f"# python {version}",
        "# 生成解释器：" + str(Path(sys.executable).resolve()),
        *[f"{name}=={resolved[name]}" for name in sorted(resolved)],
        "",
    ]
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {args.out}: {len(resolved)} packages, python {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
