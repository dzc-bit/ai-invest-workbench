---
description: 按顺序跑完全部质量门禁并汇总结果
---

按顺序执行以下门禁命令（工作目录：仓库根 `D:\New project 6`）。每一步记录"通过 / 失败（含失败摘要）"，全部跑完后给一张汇总表；任何一步失败都继续跑完剩余步骤，最后统一给出修复建议。

1. Python lint：

```powershell
python -m ruff check backend tests scripts
```

2. 后端测试：

```powershell
python -m pytest tests -q
```

3. 前端 lint：

```powershell
.\.tools\node-v20.18.1-win-x64\npm.cmd run lint
```

4. 前端类型检查：

```powershell
.\.tools\node-v20.18.1-win-x64\npm.cmd run typecheck
```

5. 前端测试：

```powershell
.\.tools\node-v20.18.1-win-x64\npm.cmd run test:ui -- --run
```

6. Rust 测试（必须先设置工具链环境变量，见 `AGENTS.md` §13）：

```powershell
$env:CARGO_HOME='D:\New project 6\.tools\cargo-home'
$env:RUSTUP_HOME='D:\New project 6\.tools\rustup-home'
$env:PATH='D:\New project 6\.tools\rustup-home\toolchains\stable-x86_64-pc-windows-msvc\bin;' + $env:PATH
cargo test --manifest-path src-tauri\Cargo.toml
```

期望基线：pytest ≥700 通过、vitest ≥250 通过、其余零失败。全绿后提醒：`git status --short --untracked-files=all` 应无临时探针/日志/安装包残留。
