#!/usr/bin/env python
"""对**已经打出来的** sidecar 做启动冒烟验证：文件齐全不等于能启动。

CI 此前只检查 ``src-tauri/bin`` 里 4 个文件是否存在，于是一个"缺 DLL / 入口
import 失败 / 打包漏了 data 文件"的包可以一路绿灯进发布。这里真的把它跑起来，
只问一个最小问题：它能不能绑定端口并回答 ``GET /ping``。

隔离口径（AGENTS.md §12/§11）：
- 用 ``tempfile`` 下的空缓存目录，绝不指向 ``运行产物\\本地数据仓``；
- 只打回环 ``/ping``，不碰任何外部行情源；
- 结束时无条件终止**整棵进程树**并删除临时目录（含超时被杀的情况）——PyInstaller
  ``--onefile`` 是引导进程 + 内层进程两段结构，只终止父进程会留下内层进程占着
  端口与缓存目录。
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _configure_stdio() -> None:
    """让中文提示在非 UTF-8 控制台上也不会把失败路径自己弄崩。

    CI 的 Windows runner 把 stdout 接到 cp1252 管道，`print("找不到 sidecar：…")`
    会先抛 ``UnicodeEncodeError``（实测 2026-09-27 首次跑该门禁即红）——那正是
    这套脚本最该说清楚话的失败分支。改成 UTF-8 后管道里是 UTF-8 字节，日志
    按 UTF-8 解码；本机旧代码页顶多显示乱码，但绝不吞掉失败原因。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _ping(base_url: str) -> dict[str, object] | None:
    try:
        with NO_PROXY_OPENER.open(f"{base_url}/ping", timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def smoke(launch: list[str], *, timeout_seconds: float) -> tuple[bool, str]:
    cache_dir = Path(tempfile.mkdtemp(prefix="astock-sidecar-smoke-"))
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    process: subprocess.Popen[bytes] | None = None
    try:
        try:
            process = subprocess.Popen(
                [*launch, "--host", "127.0.0.1", "--port", str(port), "--cache-dir", str(cache_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
            )
        except OSError as exc:
            # "文件在但起不来"正是这套门禁要拦的东西（缺 DLL、不是可执行文件……）。
            return False, f"无法启动 {launch[0]}：{exc}"
        # stdout/stderr 必须有人读：Windows 管道缓冲只有几 KB，启动期任何输出
        # （PyInstaller 引导警告、pandas 的 stderr 告警）都会把子进程阻塞在写端，
        # /ping 于是永远不回答 —— 90 秒后误报"起不来"。排空线程顺带保留尾部
        # 输出，供"启动后即退出"的诊断使用。
        captured: list[bytes] = []

        def _drain() -> None:
            if process is None or process.stdout is None:
                return
            try:
                for chunk in iter(lambda: process.stdout.read(4096), b""):
                    captured.append(chunk)
            except (ValueError, OSError):
                pass

        drain_thread = threading.Thread(target=_drain, daemon=True)
        drain_thread.start()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                drain_thread.join(timeout=2)
                output = b"".join(captured).decode("utf-8", "replace")
                return False, f"sidecar 启动后即退出（exit={process.returncode}）：{output[-800:]}"
            if _ping(base_url) == {"ok": True}:
                return True, f"{base_url}/ping -> ok"
            time.sleep(0.25)
        return False, f"{timeout_seconds:.0f} 秒内 /ping 没有回答（端口 {port} 未就绪）"
    finally:
        _terminate_process_tree(process)
        shutil.rmtree(cache_dir, ignore_errors=True)


def _terminate_process_tree(process: subprocess.Popen[bytes] | None) -> None:
    """终止 sidecar 及其子进程。

    PyInstaller ``--onefile`` 在 Windows 上是"引导进程 + 解包后真正干活的内层进程"
    两段结构：只 ``terminate()`` 父进程会留下内层进程继续占着端口与缓存目录
    （实测：冒烟后 ``astock-data-service.exe`` 仍在监听，缓存目录删不掉）。因此
    必须按进程树结束；taskkill 不可用时退回单进程终止。
    """
    if process is None or process.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
        )
        try:
            process.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            pass
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # 只在冒烟验证的子进程上用 kill 兜底
        process.kill()
        process.wait(timeout=10)


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", help="打包出来的 astock-data-service.exe 路径（发布构建用这个）")
    parser.add_argument(
        "--module",
        action="store_true",
        help="改跑源码入口 python -m astock_backtester.service（用于在本地/CI 验证本脚本自身的判定）",
    )
    parser.add_argument("--timeout", type=float, default=60.0, help="等待 /ping 就绪的秒数")
    args = parser.parse_args(argv)

    if bool(args.exe) == args.module:
        print("--exe 与 --module 必须二选一", file=sys.stderr)
        return 2
    if args.module:
        launch = [sys.executable, "-m", "astock_backtester.service"]
    else:
        exe = Path(args.exe)
        if not exe.is_file():
            print(f"找不到 sidecar：{exe}", file=sys.stderr)
            return 1
        launch = [str(exe)]
    ok, detail = smoke(launch, timeout_seconds=args.timeout)
    print(("sidecar 冒烟验证通过：" if ok else "sidecar 冒烟验证失败：") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
