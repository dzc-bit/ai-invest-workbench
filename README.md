# AI 投研工作台

> 曾用名「A 股策略回测工作台」。它早已不止于回测——回测只是众多入口之一。

面向 A 股的本地优先 AI 投研终端：本地数据仓 + 实时行情 + 财联社资讯聚合 + 风险清单监控 + LLM AI 投研 Agent（评股对话 / 本地知识检索 / 参数寻优 / 资讯摘要）+ 策略回测。前端 React + TypeScript，桌面容器 Tauri，本地数据服务与回测执行由 Python 承担。

当前版本：`1.6.0`

本轮与历轮发布内容统一记在 [`CHANGELOG.md`](CHANGELOG.md)；本文件只描述“现在能做什么”，不堆发布流水账。

## 面向用户

- 策略条件一句话生成：在"策略配置 → AI 条件理解"里用口语描述买卖规则，AI 解析成可勾选的条件清单（含近似说明），确认后写入策略；需要精细控制时展开"高级模式"手工编辑。
- **AI 实时盘面优先**：本地数据仓是历史数据，AI 回答任何"当前/今天/盘中"的行情问题会先走实时通道——`realtime_stock_detail` 提供个股实时价、涨跌幅、量比、换手与涨停/跌停/炸板判定（腾讯公开行情，不读仓），配合实时大盘快照（指数、红绿家数、强势板块）与涨停/炸板/跌停池。引用本地历史数据时 AI 会自动标注数据截止日期并声明"非实时"，实时不可用时明确说明，不拿旧数据冒充当下。
- **三种研究风格，输出真的不一样**：设置里可选保守（防御型）/ 均衡（默认）/ 激进（进攻型），三者的取证清单、结论骨架与决策口径彼此独立——激进风格按龙头选手视角作答（情绪周期定位、连板梯队与断层、龙头辨识度、打板/低吸/半路参与语义、断板预案与仓位纪律），保守风格则先算下行风险与安全边际。风格同时作用于对话、回测点评与定时复盘报告。
- AI 助手：右上角"AI 助手"唤起右侧抽屉。抽屉顶部按"对话 / 历史 / 快讯 / 报告"切换，主区一次只显示一个面板，消息区始终占满剩余高度；支持个股诊断、大盘快评、自然语言生成策略并一键回测、回测结果解读；工具调用过程与数据来源全部可见。
- AI 对话历史：关掉抽屉或重启应用后再打开，会自动续上最近一条对话；"历史"面板列出全部会话，可切换或新建一条。追问仍在同一会话里，更早的内容（含压缩后的会话纪要）会继续被引用。旧会话由后端按容量与保留期自动回收，无需手工清理。
- AI 参数寻优：策略配置页底部"AI 参数寻优"面板，选 2–4 个参数给候选值（最多 48 组合），一键网格回测出对比表 + AI 解读最优区间与过拟合警告。
- 回测报告导出：回测完成后"导出报告"一键下载单文件 HTML（权益曲线、指标、交易明细、AI 解读），可直接存档或分享。
- 数据中心：维护 A 股日线、资金流、市值和覆盖信息，支持导入、全市场同步、「只补缺口」（按缺口名单发起抓取，不做全量扫描）、指定股票补齐和资金流补齐；"AI 诊断缺失"按钮分析覆盖缺口并指路补齐操作；"数据源健康监控"折叠卡实时展示各数据源最近成功状态。
- 覆盖口径可信：新上市股票上市前、已退市股票退市后不再算"缺失"，停牌类缺行单列（公开渠道天然没有停牌 K 线，不可补也不该算作数据问题）；覆盖表与同步进度反映真实**可行动**缺口（逐股明细带"未上市/已退市"徽标）。
- 策略回测：支持入场/离场条件、仓位参数、止盈止损、涨跌停约束和流式回测结果。
- 行情看板：展示指数、红绿家数、强势板块、行情评价、新闻摘要、资讯事件、同花顺复盘/早盘和风险提示；后端有新数据或出现 AI 快讯时通过事件流即时提醒。
- 候选股票：回测结果通过 `latest_strategy_matches.matches` 展示符合当前策略的个股。
- 桌面更新：通过 GitHub Releases 发布 Windows 安装包，并由应用内更新入口检查新版本。

最新安装包见 [GitHub Releases](https://github.com/dzc-bit/ai-invest-workbench/releases)。

## 数据源概览

- 历史行情：腾讯公开日 K、新浪日 K（覆盖北交所）、AData、AKShare、百度股市通 / PAE、东方财富公开接口。
- 实时行情：财联社、同花顺、Sina、Tencent、AKShare 及后端公开行情爬虫；AI 个股实时快照走腾讯公开行情（涨停/跌停/炸板判定直接比对交易所公布的涨跌停价）。
- 强势板块：同花顺概念/行业、Sina 行业、AKShare、东方财富板块接口。
- 新闻资讯：东方财富栏目资讯、东方财富要闻、财联社电报。
- 复盘早盘：同花顺复盘/早盘页面，失败时使用公开行情或本地简短判断兜底。
- 资金流：东方财富公开资金流接口，缺口和失败原因会通过 diagnostics/failures 暴露给上层服务。
- 传输策略：所有爬虫统一 `trust_env=False`（不读系统代理，防止代理劫持国内行情站）与 `http_transport.py` 集中重试；`curl_cffi` 备用传输自带 ASCII CA 解析，中文用户名的 Windows 不再整条失效。

## 技术架构

| 层级 | 目录 | 说明 |
| --- | --- | --- |
| 前端 | `frontend/src` | React + TypeScript，页面、状态、图表和结构化接口消费；API 层统一在 `api.ts`（含浏览器预览 mock 双轨），AI 对话流在 `aiApi.ts`，轮询类逻辑收敛在 `hooks/`；视觉系统由根目录 `design.md` 锁定（`:root` token 块 + A 股红涨绿跌语义） |
| 桌面容器 | `src-tauri/src` | Tauri + Rust，负责桌面命令、本地服务启动（含 sidecar 五重身份校验）、策略保存和更新器 |
| 本地后端 | `backend/astock_backtester` | Python，自写 HTTP 服务 + 数据 provider + 仓库 + 回测引擎；回测条件在 `conditions.py` 注册表统一维护（行级求值与向量化预过滤成对注册）；`symbol_lifecycle` 表驱动覆盖/同步的上市–退市窗口口径 |
| AI 子系统 | `backend/astock_backtester/ai` | 独立子包：openai SDK 薄封装、工具注册表（本地数据 + a-stock-data 裁剪端点）、Agent 循环、上下文预算、分层记忆、会话存储与自动回收、RAG 检索、快讯引擎、条件解析/单段点评/参数寻优轻路由；对数据仓只读 |
| 共享数据设施 | `backend/astock_backtester/data` | `symbols.py`/`parsing.py` 收敛符号与数值解析，`http_transport.py` 统一 UA、代理策略和重试传输 |
| 测试 | `tests`、`frontend/src/*.test.*` | 三层测试：后端（行为级，含 AI 子系统）、前端（≥270 断言）、Rust；回环 HTTP 测试自带代理隔离 |

依赖方向保持单向（`service → data/* → models`，data 层不反向依赖根包），全仓零 import 环。错误响应携带稳定 `code`（`no_local_data` / `validation_error` / `payload_error` / `request_failed`；AI 另有 `ai_not_configured` / `ai_upstream_error` / `ai_session_busy` / `ai_session_not_found`），前端按错误码翻译文案。

## 质量门禁

CI（`.github/workflows/ci.yml`，windows-latest）在 push/PR 时运行三组检查：pytest + ruff、eslint + tsc + vitest、cargo test。本地等价命令：

```powershell
python -m ruff check backend tests scripts
npm run lint
npm run typecheck
npm run test:coverage
cargo test --manifest-path src-tauri/Cargo.toml
```

覆盖率（pytest-cov + vitest v8）当前只出报告不设阈值。

## 本地 HTTP 接口

桌面端启动后会拉起本地 sidecar，前端访问 `http://127.0.0.1:<port>`。

| 能力 | 接口 |
| --- | --- |
| 健康检查 | `GET /ping`、`GET /health`、`GET /logs/recent`、`GET /diagnostics/sources`、`GET /diagnostics/data-gaps` |
| 覆盖查询 | `POST /coverage/daily-bars` |
| 数据同步 | `POST /sync/full-market`、`GET /sync/jobs/{job_id}` |
| 数据导入与补齐 | `POST /import/daily-bars`、`POST /fetch/daily-bars`、`POST /fetch/capital-flow` |
| 行情与资讯 | `GET /realtime/market-snapshot`、`GET /market/commentary`、`GET /market/news-summary`、`GET /market/news` |
| 复盘/早盘 | `GET /market/fupan`、`GET /market/zaopan` |
| 风险与策略 | `GET /risk/alerts`、`GET /strategy/recommended`、`POST /strategy/conditions/validate` |
| 回测 | `POST /run/backtest/stream` |
| AI 助手 | `GET /ai/status`、`GET /ai/news`、`GET /ai/config`、`POST /ai/config`、`GET /ai/config/reveal`（仅限本机桌面端，带 Host/Origin 校验）、`POST /ai/chat/stream`、`GET /ai/events/stream` |
| AI 会话历史 | `GET /ai/sessions`（会话列表）、`GET /ai/session?session_id=`（回读展示转录）、`POST /ai/session/delete`（显式删除；日常回收由写侧 `SessionStore.prune` 自动完成） |
| AI 轻路由 | `POST /ai/conditions/parse`（自然语言→条件 DSL，自愈校验）、`POST /ai/insight/oneshot`（场景化单段点评）、`POST /ai/optimize`（参数网格寻优，NDJSON 流式） |
| AI 报告与过拟合 | `GET /ai/reports`、`GET /ai/report/file?name=`、`POST /ai/overfit/check` |

`/run/backtest/stream` 与 `/ai/chat/stream` 返回 NDJSON，需要逐行解析；`/ai/chat/stream` 的最后一个事件为 `{"type":"result", ...}`（错误时为 `error`）。`/ai/events/stream` 为长连接（insight / data_fresh / heartbeat）。

## 开发环境

建议环境：

- Node.js 20+
- Python 3.11+
- Rust stable toolchain
- Windows 桌面构建需要 Tauri 支持的 MSVC 构建工具

安装依赖：

```powershell
npm install
python -m pip install -e .
```

常用命令：

```powershell
npm run test:ui -- --run
npm run lint
npm run typecheck
npm run build
npm run build:data-service
python -m pytest tests -q
python -m ruff check backend tests scripts
cargo test --manifest-path src-tauri/Cargo.toml
```

开发模式：

```powershell
npm run tauri -- dev
```

生产构建：

```powershell
npm run tauri -- build --ci
```

## 文档

| 文件 | 内容 |
| --- | --- |
| [`AGENTS.md`](AGENTS.md) | 贡献者与 AI agent 的红线、架构不变量、门禁命令 |
| [`design.md`](design.md) | 视觉系统的锁（命名 token 表） |
| [`CHANGELOG.md`](CHANGELOG.md) | 各版本发布记录 |

更细的模块手册（AI 子系统目录与事件协议、Windows 构建签名流程、前端 token 纪律）放在维护者本地的 `docs/` 目录；该目录整体被 `.gitignore` 排除、不入库，需要时按 `AGENTS.md` 对应章节的摘要与红线行事。

Windows 发布使用项目内固定的 Node、Python、Rust、MSVC 和 NSIS 工具。发布前确认生成的安装包、签名文件、临时更新清单、日志和运行数据没有提交到 Git 仓库。
