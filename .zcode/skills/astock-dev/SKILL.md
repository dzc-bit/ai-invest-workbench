---
name: astock-dev
description: A股策略回测工作台项目开发技能。改后端数据/回测/AI 代码、跑分域测试、调前端组件或预览验证时使用；提供分域测试命令映射、AI 模块注意事项与预览 mock 双轨说明。
---

# A股策略回测工作台 · 开发技能

先读 `AGENTS.md`：§2 与 §15 是红线和架构不变量，§13 是验证命令。本技能只回答"改了什么代码就跑哪个测试"和"怎么预览"。

## 分域测试映射（与 AGENTS.md §13 一致）

| 改动范围 | 测试命令 |
| --- | --- |
| 资金流 crawler / 数据操作 / HTTP 层 | `python -m pytest tests/test_capital_flow_crawler.py tests/test_data_operations.py tests/test_data_service_http.py -q` |
| 行情 / 复盘 | `python -m pytest tests/test_market.py tests/test_data_service_http.py -q` |
| 数据仓 / 覆盖 / 生命周期（symbol_lifecycle） | `python -m pytest tests/test_warehouse.py tests/test_symbol_lifecycle.py tests/test_data_operations.py tests/test_sync_jobs.py -q` |
| AI 子系统（agent/路由/工具） | `python -m pytest tests/test_ai_routes_v150.py tests/test_ai_service_http.py tests/test_ai_agent.py -q` |
| 全量后端门禁 | `python -m pytest tests -q`（≥700 通过） |
| 前端全量 | `.\.tools\node-v20.18.1-win-x64\npm.cmd run test:ui -- --run`（≥250 通过） |
| Rust 壳 | `cargo test --manifest-path src-tauri\Cargo.toml`（先设 CARGO_HOME/RUSTUP_HOME，见 AGENTS.md §13） |

## AI 模块注意事项

目录地图、事件协议与会话历史细节见 `docs/ai-subsystem.md`（本地留存，不入库）；这里只留踩坑清单。

- `ai/` 是独立子包：不新增第二个写工具；LLM 配置只存 `运行产物/AI配置/`；`/ai/config` 只回掩码。
- 提示词模板含字面 JSON 必须用 `{{ }}` 转义（`str.format` 会把 `{"content": ...}` 当占位符，曾踩坑）。
- 测试注入模型：HTTP 级用 `monkeypatch.setattr(ai_service, "_model", FakeModel())`（先 `server.state.ai_service()` 实例化），FakeModel 脚本范式见 `tests/test_ai_routes_v150.py::FakeModel`；禁止网络。
- 新增 AI 路由的错误必须带稳定 code：未配置抛 `AiNotConfigured`、上游失败抛 `AiUpstreamError`（`do_POST` 会自动映射 400 + code）。
- 长期记忆提取等后台线程绝不能阻塞 `/ai/chat/stream` 事件流（哨兵在 finally 里）。

## 预览 mock 双轨

- 前端所有请求函数第一步判断 `isTauriRuntime()`：非 Tauri（浏览器 `npm run dev`，127.0.0.1:1420）走 `apiMocks.ts` / `aiMocks.ts`，Tauri 桌面端走本地 sidecar HTTP。**新增后端端点必须同步补 mock 函数**，否则预览模式白屏/报错。
- 预览模式人工过页面：`.\.tools\node-v20.18.1-win-x64\npm.cmd run dev` 后浏览器打开 127.0.0.1:1420，重点 320/375/768 宽度（设计系统见根目录 `design.md`）。
- 组件测试里 `vi.mock("./api")` 用"spread 真模块 + 覆盖函数"模式，新增导出自动可用；`aiApi` 同理。

## 前端约定

- 样式只用 `frontend/src/styles.css` + `ai-panel.css` 的 token（`var(--paper)`、`var(--rise)`、`var(--text-sm)`…），禁止新增硬编码色值/字号/圆角（`design.md` 是锁）。
- `--rise` 红=涨、`--fall` 绿=跌（A 股语义），不得用绿表达"成功/最优"。
- 中文文案与 aria 标注受测试守卫，改动前先跑对应组件测试。
