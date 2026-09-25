# AGENT 必读

这份文档只放**花很久才踩明白的坑换来的红线与不变量**：路径与 git 边界、实时行情完整性、爬虫与资金流边界、A 股交易日历与覆盖口径、错误码契约、AI 子系统硬约束、门禁命令。能从代码直接读出来的目录清单、事件协议细节和一次性发布记录不放在这里。

分层规则（新增内容先问该放哪一层）：

`docs/` 整个目录被 `.gitignore` 排除（内部文档与截图仅本机留存），所以下列 `docs/*` 只在维护者本机存在；克隆仓库后找不到它们时，按本文对应章节的摘要行事即可。

| 文件 | 放什么 | 什么时候读 |
| --- | --- | --- |
| `AGENTS.md`（本文） | 红线、不变量、门禁命令 | 每次接手都读，改代码前对照 §2 与 §15 |
| `docs/ai-subsystem.md`（本地） | AI 子包目录地图、事件协议、会话历史、记忆引擎、测试分层 | 改 `backend/astock_backtester/ai/` 或 AI 抽屉前端时 |
| `docs/release-build.md`（本地） | Windows 构建、签名、覆盖安装与安装后 sidecar 验证 | 只在发布安装包时 |
| `docs/frontend-style.md`（本地） | 设计 token 纪律与响应式检查 | 改前端样式时 |
| `design.md` | 视觉系统的锁（token 表本身） | 新增色值/字号/圆角前 |
| `README.md` / `CHANGELOG.md` | 面向用户的能力说明与历史发布记录 | 需要"这个产品有什么功能"时，不是接手前置条件 |

`项目说明书.md`、`应用创新类项目报告.md` 和 `演示文档操作提醒.md` 是本地人工材料，不上传 GitHub，也不作为 agent 接手依据。

## 1. 工作区和 git

真实业务根目录是：

```text
D:\New project 6
```

`C:\Users\大帝之资\Documents\New project 6` 是 Junction。所有命令、测试、构建、探针和 git 操作都必须在 `D:\New project 6` 执行。

接手后先跑：

```powershell
git status --short --untracked-files=all
git diff
git remote -v
git branch --show-current
```

期望 remote（仓库已由 `Astock-backtester` 改名为 `ai-invest-workbench`，旧地址会被 GitHub 301 重定向）：

```text
https://github.com/dzc-bit/ai-invest-workbench.git
```

保护已有未提交修改。不要覆盖无关文件，不要清理、删除、迁移 `D:\New project 6\运行产物`。版本号统一跟随桌面端当前版本（以 `package.json` 的 `version` 为准，各处清单由 `tests/test_scripts.py::test_release_manifests_use_one_version` 把关），除非用户明确要求改版本就不要动它。前端视觉系统由仓库根 `design.md` 锁定（token 纪律见 [`docs/frontend-style.md`](docs/frontend-style.md)）。

## 2. 绝对不要碰错边界

- 不修改 token、CORS、开放 API 安全边界。
- 不改无关模块，不做顺手重构。
- 不清理整个 `运行产物`。
- 不提交私钥、安装包、`.sig`、临时 `latest.json`、探针脚本、日志或运行数据。
- 不提交本地人工说明和演示材料：`项目说明书.md`、`应用创新类项目报告.md`、`演示文档操作提醒.md`。
- 前端只消费后端结构化响应，不写上游 URL、爬虫逻辑或字段清洗规则。
- 清理临时文件时不要直接执行 `git clean -fdX` 或等价的一把梭命令，因为它会把 `.tools`、`node_modules`、`src-tauri\bin`、`src-tauri\target` 和 `运行产物` 这类仍需保留的本地工具、构建产物或用户数据也列入删除范围；只点名删除 `.pytest_cache`、`.ruff_cache`、`.tmp`、`.pyinstaller`、`__pycache__` 等明确临时缓存。

## 3. JSON 文件别误判

仓库根目录没有很多业务 JSON 是正常的。源码 JSON 主要是：

- `package.json`
- `package-lock.json`
- `tsconfig.json`
- `src-tauri/tauri.conf.json`
- `src-tauri/capabilities/main.json`

这些 JSON 是运行或发布产物，不应作为源码提交：

- `运行产物\策略配置\saved-strategies.json`
- `release-assets\latest.json`
- 临时探针 JSON
- 临时日志 JSON

`latest.json` 只有在发布流程中由本次真实 `.sig` 生成才可信；验证后本地临时文件通常要删除，不能手写、伪造或复用旧文件。

## 4. 模块必须独立

不要把这些模块混成一个：

| 模块 | 接口或来源 |
| --- | --- |
| 今日实时行情 | `GET /realtime/market-snapshot` |
| 行情评价 | `GET /market/commentary` |
| 新闻汇总 | `GET /market/news-summary` |
| 资讯与事件 | `GET /market/news` |
| 同花顺复盘 | `GET /market/fupan` |
| 同花顺早盘 | `GET /market/zaopan` |
| 数据源健康 | `GET /diagnostics/sources`（聚合 realtime/news/finance 最近成功状态，只读不触发抓取） |
| user 模式候选 | `/run/backtest/stream` 最终 `result.latest_strategy_matches.matches` |
| 资金流补齐 | `POST /fetch/daily-bars`、`POST /fetch/capital-flow` |
| AI 对话/快讯 | `POST /ai/chat/stream`、`GET /ai/events/stream`、`GET /ai/news`、`GET /ai/status`、`GET|POST /ai/config` |
| AI 会话历史 | `GET /ai/sessions`（列表）、`GET /ai/session?session_id=`（回读 display）、`POST /ai/session/delete`（显式删除，抽屉不再用；日常回收是写侧 `SessionStore.prune` 动态自动清理） |
| AI 轻路由 | `POST /ai/conditions/parse`（NL→条件 DSL，自愈校验）、`POST /ai/insight/oneshot`（场景点评：results_overview / data_coverage / risk_alerts）、`POST /ai/optimize`（参数网格寻优，NDJSON） |
| 缺口画像 | `GET /diagnostics/data-gaps`（停更分布/疑似写入失败日/字段尾部，读 warehouse 缓存不触发抓取；AI 工具 `data_health_report` 消费同一明细） |

复盘正文不能塞 user 候选；新闻不能替代行情评价；实时行情失败不能拿本地历史数据伪装成 live；AI 轻路由失败带稳定 code，前端对 oneshot 点评失败静默不显示。

## 5. 实时行情完整性

红绿家数不能只看 `status=live`。必须检查：

- `breadth.total >= 3000`
- 或满足本地股票池合理比例

`total=192`、`total=26` 这类局部样本必须判失败并写入 diagnostics，不能标记为全市场 live。

红绿家数 provider 链：

1. 财联社行情页对应的签名 XHR：`https://www.cls.cn/quotation` 背后的 `x-quote.cls.cn/quote/index/home`，解析 `up_down_dis`；该来源拿到数字后直接展示，不再用 `total>=3000` 额外丢弃。
2. 同花顺市场总览：`q.10jqka.com.cn/index/index/board/all/`
3. Sina 批量实时个股：`hq.sinajs.cn/list=...`
4. Tencent 批量实时个股：`qt.gtimg.cn/q=...`
5. AKShare：`stock_zh_a_spot_em()`
6. 后端重型公开行情爬虫：公开 XHR 优先，headless DOM 其次。
7. 东方财富轻量 spot：只作受控备选，必须有超时、字段校验、数量校验和 diagnostics。

财联社红绿家数是主源且已经代表全市场分布，不能在请求它之前先扫本地股票池或 coverage 数量；本地完整性扫描只允许在非财联社来源需要 `total>=3000`/本地比例校验时懒加载，否则主仓数据量大时会先耗尽红绿家数 2 秒预算，导致 CLS 明明可用却被判超时。

强势板块 provider 链：

1. 同花顺概念题材页。
2. 同花顺行业页。
3. Sina 行业板块。
4. AKShare 概念/行业板块。
5. 东方财富概念/行业板块受控备选。
6. 同花顺热点归因只作题材候选，不伪装成板块涨幅。

重型爬虫边界：

- 固定短超时，快速失败。
- 缓存最近成功结果只能作为 stale/fallback 明确标注。
- 当前请求失败时，内部缓存不能参与本次 live 判定。
- 不做登录、cookie 池、代理池、验证码绕过或付费抓取。

同花顺大盘评分卡片规则：

- 主源是 `q.10jqka.com.cn/api.php?t=indexflash&` 的 `dppj_data`，这是 10 分制同花顺大盘评级。
- 该接口缺少浏览器脚本生成的 `v` cookie 时会 403；后端必须先执行同花顺 `chameleon` 浏览器脚本（当前用 Node/jsdom）生成本次请求 cookie，再请求 `indexflash`。
- 桌面安装版 sidecar 旁边必须同时带 `node.exe`、`ths-cookie-worker.cjs`、`xhr-sync-worker.js`；只在开发机 `.tools` 里有 Node 不算安装版可用。
- 评分解析只能读取 `indexflash` 原始载荷或 `dppj_data` 等结构化字段；HTML 页面兜底只能从可见文本解析，不能把 `<div id="dppj">` 这类标签属性里的数字当评分。
- 成功解析后写入 `emotion.market_degree` / `emotion.market_degree_label`，前端“大盘评分”卡片直接消费该值。
- 不要用财联社热度、新闻、本地启发式或其他评分静默替代同花顺评分；失败时保留 diagnostics/failures 并显示不可用或明确 fallback。

## 6. 行情评价状态机

`/market/commentary` 必须是状态机：

1. 完整实时快照可用，生成盘中评价。
2. 实时失败，使用最近成功完整快照。
3. 再失败，尝试同花顺复盘或公开行情兜底。
4. 最后使用本地简短判断。
5. 新闻只作辅助线索，不能生成确定行情结论。

不要把不完整红绿家数、新闻列表、旧快照包装成实时盘面。

## 7. 同花顺复盘和早盘

`/market/fupan` 和 `/market/zaopan` 是独立模块，不承载 user 候选。

source 语义：

- `ths-fupan` / `ths-zaopan`：真实同花顺正文。
- `ths-fupan+market-fallback` / `ths-zaopan+market-fallback`：公开行情兜底，`source_url` 必须是真实公开行情链接。
- `ths-fupan+local-brief` / `ths-zaopan+local-brief`：本地简短防守口径，`source_url` 必须为空。

原文按钮只能打开真实 `source_url` 或文章链接。无链接时前端禁用。

## 8. 资金流 crawler

资金流 crawler 已纳入当前版本，并且是主要资金流补齐手段：

```text
backend/astock_backtester/data/capital_flow_crawler.py
tests/test_capital_flow_crawler.py
```

边界：

- 东方财富公开 XHR 是主源，百度公开资金流是受控备选源；允许继续接入同花顺或其他可靠公开接口作为备用。
- 返回 `rows/failures/diagnostics`，日期覆盖不足必须通过 diagnostics/failures 明确展示，不能把未补齐股票算完成。
- crawler 本身不直接写 `Warehouse` 或 `LocalCache`；写入边界在 operations/service/sync 层。
- `/fetch/daily-bars` 负责把 `main_net_inflow` 合并到新拉取的日线。
- `/fetch/capital-flow` 负责补齐资金流缺口；即使个股暂无日 K，也允许先写资金流独立行。
- 日线覆盖、回测和实时行情本地兜底只认 OHLC 完整行；资金流独立行不能让股票变成可回测日线数据。

大陆 IP 或上游风控导致断连时，可以做固定 header 变体、Eastmoney `ut` 参数变体、curl_cffi 浏览器 TLS 指纹、短超时、重试退避、限速、分批并发、备用源切换、JSON/JSONP 响应解析、同进程最近成功行缓存和 diagnostics；不能做登录、cookie 池、代理池、验证码绕过或付费抓取。最近成功缓存命中时必须保留原始 failure，并追加 `recent_success_cache_used`。

## 9. 数据中心和交易日历

`/coverage/daily-bars` 必须使用 A 股交易日历。不要用普通工作日直接判断缺失交易日。

数据中心的“补全缺失数据”默认是全市场补齐：股票代码为空时走 `/sync/full-market`；只有用户显式输入股票代码时，才走指定股票 `/fetch/daily-bars`。覆盖表的 `missing_rows` 只能来自刷新后的真实仓库 coverage，前端绝不能用本次 `imported_rows` 抵扣或估算缺失行，否则会出现“任务没补完却显示缺失为 0”的错误。同步进度里 `imported_rows` 表示接口返回并写入/合并的行，不等于缺口减少；全市场日线任务的实际补缺必须拆分展示后端 `filled_daily_rows` 和 `filled_market_cap_rows`，`filled_missing_rows` 仅保留为兼容总数，不能当成单一日线缺口。

全市场日线同步的股票池必须优先来自本地仓库全量 OHLC 股票集合，不能用本次补齐日期范围过滤后的股票集合。否则缺最新交易日 OHLC 的股票会在任务创建时被排除，出现覆盖表显示仍有缺口但同步任务总数小于本地股票数的问题。

全市场日线同步的跳过条件必须同时满足 OHLC 完整和 `float_market_cap` 完整。不能因为某只股票 OHLC 已有就跳过它的市值缺口；市值缺口应随 `/sync/full-market` 或指定 `/fetch/daily-bars` 的日线补齐一并修复。后端计算 `filled_missing_rows` 时应复用任务开始时的仓库完整性快照，避免每个写入批次重复扫仓拖慢数据中心。

**symbol_lifecycle 口径（1.5.0 起）**：`Warehouse` 的 metadata.sqlite 有 `symbol_lifecycle(symbol PRIMARY KEY, listing_date, delisted_date, status)` 表。`build_daily_bars_coverage` 的 `expected_dates`、`read_capital_flow_missing_symbols` 的行过滤、全市场同步的完整性与跳过判定都按每只股票 `[listing_date, delisted_date]` 窗口截断；无生命周期记录的股票保持旧的保守口径（算缺失），因此历史行为不回退。全市场同步前 best-effort 刷新该表：adata `all_code()` 提供上市日期与在市名单，仓库里有数据但名单缺席且近期无新行的股票才标 `delisted`（退市日取其最后交易日）；数据源名单行数不足（<1000）时只刷上市日期、绝不标退市，防止上游抖动误杀同步池。`/fetch/daily-bars` 写库后从行内 `listing_days` 反推上市日补写 lifecycle（9999 表示未知，跳过）。

回测设置的默认日期在用户未手动编辑前应跟随 `daily_bars` coverage 的最新日期，并按最近 A 股交易日范围回填；用户一旦手动修改日期或点“套用数据中心日期”，后续不要再自动覆盖用户选择。这样本地仓库已到 2026-06-18 时，回测候选不应仍停在旧的 2026-01-20。

补缺日线 provider 顺序必须以公开 HTTP 爬虫为主：`HttpAStockProvider -> ADataProvider -> AkshareProvider`，provider 实例**全程复用**（`HttpAStockProvider.__post_init__` 持有单个 adapter——东财增强端点的失败熔断计数挂在 adapter 上，每票新建会把全市场补齐拖成小时级）。`HttpAStockFetcher` 内部日 K 主源顺序是**腾讯 → 新浪 → 百度**（2026-09 实测：百度对直连 IP 恒 403 且只认完整浏览器 UA；腾讯/新浪直连稳定、价格口径与仓库一致均为不复权）。两条公开链路的铁律：腾讯 volume 单位是**手**必须 ×100、新浪是**股**直接入库；腾讯不支持北交所段，`920xxx` 必须走新浪（`a_share_market_symbol` 已把 `92` 前缀映射为 `bj`，900xxx 沪 B 仍是 `sh`）；腾讯 fqkline `count<=320` 且只返回窗口内最后 N 行，长区间必须按窗口回走分页；新浪 `datalen` 按"最新 N 根"取数，长度按窗口跨度动态算。公开 XHR 日 K 不带市值：用 `qt.gtimg.cn` 报价推导 `float_shares×close` 填充（与百度 `volume/turnover×close` 同法）。东财增强（spot 信息 + 120 日资金流）是可选层，连续失败达到 `EASTMONEY_ENRICHMENT_FAILURE_LIMIT` 必须熔断跳过，不要每票白等满超时。`curl_cffi` 备用传输必须经 `http_transport.curl_verify_kwargs()` 注入可加载的 ASCII CA 路径——certifi 装在中文用户名目录下时 libcurl 会报 `curl: (77)` 使整条备用传输失效（守卫在 `tests/test_http_transport.py`）。百度日 K 保留 `curl_cffi` 浏览器 TLS 指纹作为同一 HTTP 主源内的备用，不要因为普通 requests 403 就直接跳到 adata/AKShare；`adata` 数据可能只覆盖到 2025 年底，不能放在近期补缺主路径第一位；AKShare 只能作为最后保底。所有来源都失败或返回空时，错误必须聚合展示每个 provider 的尝试结果，不能只把 AKShare 的断连显示成唯一失败原因。`HttpAStockFetcher` 的注入式构造（`json_get`/`public_json_get`/`public_text_get` 任一传入）必须完全离线：未注入的传输一律停用，绝不允许用例静默打到真实网络。

`/health` 不能同步阻塞重型 `warehouse.coverage()` 扫描。数据中心连接和操作后刷新应快速返回最近 coverage 快照，并用后台刷新更新缺失行数；不要让 60 秒级 coverage 扫描卡住“本地服务已连接”、按钮状态或全市场同步进度。

`Warehouse.coverage()` 缺失行口径（1.5.2 起修订）：**可行动缺口与停牌类缺行分列**。日线 = 可行动缺口（停更尾部 + thin day 内部洞）记入 `missing_rows`，停牌类缺行单列 `suspension_rows`；市值/资金流保留“已有行但字段为空”的内部统计 + 停更尾部（资金流尾部边界取 OHLC 末行与资金流末行的较大者——§8 允许“暂无日 K 先写资金流独立行”，独立行覆盖的日期不算缺失）。分类依据是横截面证据：某交易日全市场 OHLC 完整行数 ≥ `median × 0.5` 为市场正常日，其缺行是停牌（公开渠道天然没有停牌 K 线，不可补）；行数异常的交易日是疑似写入失败（thin day），缺行可补。实测依据：2025 年 15,095 个缺口对 **100%** 落在市场正常日——旧“累计真实缺口”口径（停牌日也计为缺失）由此被实证推翻修订，因为那个数字基本虚假且任何渠道都补不上。旧决策中仍然成立的部分：停更尾部必须可见（“多久没同步”的可行动信号），它保持独立统计、绝不参与停牌分类。无生命周期记录的股票保守口径不变（尾部照算、内部洞随当日分类）。补齐入口以缺口为基准：`SyncJobManager.incomplete_symbols(start, end)` 返回窗口内不完整的股票名单，只对名单发起抓取，不做全量扫描。

配套明细：`Warehouse.data_gap_profile()`（停更分布/疑似写入失败日/市值与资金流停更尾部，只读最近年分区，10 分钟缓存、写入自动失效、lifecycle 变更即失效）。已标记退市的股票从停更分布剔除并单列 `delisted_symbols`（退市是终态不是缺口）。数据中心"缺失数据监控"折叠区与 AI 工具 `data_health_report` 消费同一份明细，保证 UI 与 AI 看到一致的"具体缺什么"；`data_health_report` 另带 coverage 快照汇总与损坏分区区分（`error_code=warehouse_corrupt`），模型据此能分辨"先修损坏还是先补数据"。

节假日硬编码表（`data/trading_calendar.py`）覆盖 **2015~2028** 年；计算范围超出表覆盖年份时会在日志打一次性 warning（上界与下界都查），**每年发布前必须把新一年追加进 `_A_SHARE_HOLIDAY_RANGES`**，否则该年春节/国庆会被计成永远补不回来的缺口。历史教训：表曾只从 2024 年起，2022-2023 年的节假日被当成交易日，制造了 17 万+“节假日幽灵缺口”并被 thin-day 规则误判为可补——新增年份时用数据仓自校验（真节假日当天仓库行数应≈0，相邻交易日应有数千行）。

后台刷新期间如果 `/health` 返回三项 coverage 全是 `symbols=0`、无日期、`missing_rows=0` 且 `coverage_refreshing=true`，前端不能把它当权威结果覆盖已有覆盖表；应保留旧覆盖并继续轮询，等刷新完成后的真实快照再更新。

春节、清明、劳动节、国庆等合法休市日不能进入 `missing_trade_dates`。

主仓路径：

```text
D:\New project 6\运行产物\本地数据仓
```

旧路径：

```text
D:\New project 6\运行产物\本地数据
```

旧路径不能作为 UI、补齐或回测的活跃写入源。如果任务要求删除旧路径，必须确认绝对路径，只处理旧目录本身，不能清理整个 `运行产物`。

## 10. user 模式候选

正式来源：

```text
POST /run/backtest/stream
最后一个 NDJSON 事件 result.latest_strategy_matches.matches
```

旧的非流式 `/run/backtest` 接口不是兼容目标，不要恢复；前端也不要继续保留旧 `matched_stocks` 结果字段。user 候选只认 `latest_strategy_matches`。

不要把 user 候选塞进同花顺复盘正文。

如果实时失败并回退本地最近交易日，前端必须标注：

- 本地最近交易日
- 非实时

## 11. 接口探针

安装后至少探测：

- `GET /ping`
- `GET /health`
- `GET /diagnostics/sources`
- `GET /market/finance`
- `POST /coverage/daily-bars`
- `GET /realtime/market-snapshot`
- `GET /market/commentary`
- `GET /market/fupan`
- `GET /market/zaopan`
- `GET /ai/status`
- `POST /run/backtest/stream`

`/run/backtest/stream` 是 NDJSON，不能用 `Invoke-RestMethod` 当普通 JSON 判断。用 Python/Node 逐行读：

```python
events = [json.loads(line) for line in response_text.splitlines() if line.strip()]
assert events[-1]["type"] == "result"
```

中文路径和空格路径容易被 PowerShell 拆参。启动 sidecar 探针时优先用 Python `subprocess.Popen([...])` 参数数组，不要把 `D:\New project 6\运行产物\本地数据仓` 拼成未转义字符串。

不要落地长期探针。临时 `.py`、`.ps1`、`.js`、`.json`、`.log` 跑完删除。

## 12. 桌面安装包构建、签名和覆盖安装

完整流程（项目内固定工具链清单、构建前检查、签名与覆盖安装、安装后 sidecar 验证、清理边界）见 [`docs/release-build.md`](docs/release-build.md)，只在发布时读。日常改代码只需守住这几条：

- 工具链固定在 D 盘项目内的 `.tools`，缺少依赖先停下来报告，不擅自下载，也不改用 C 盘或 mingw 工具链。
- 安装包、`.sig`、临时 `latest.json` 与签名私钥一律不提交；产物名、版本和 `.sig` 必须同一轮生成，签名缺失时不得复用旧 `.sig`。
- 构建后只点名清理临时探针与缓存，不得用 `git clean -fdX`。

## 13. 验证命令

常规：

```powershell
python -m pytest tests -q
.\.tools\node-v20.18.1-win-x64\npm.cmd run test:ui -- --run
.\.tools\node-v20.18.1-win-x64\npm.cmd run typecheck
.\.tools\node-v20.18.1-win-x64\npm.cmd run build
.\.tools\node-v20.18.1-win-x64\npm.cmd run build:data-service
```

Rust：

```powershell
$env:CARGO_HOME='D:\New project 6\.tools\cargo-home'
$env:RUSTUP_HOME='D:\New project 6\.tools\rustup-home'
$env:PATH='D:\New project 6\.tools\rustup-home\toolchains\stable-x86_64-pc-windows-msvc\bin;' + $env:PATH
cargo test --manifest-path src-tauri\Cargo.toml
```

资金流变更：

```powershell
python -m pytest tests/test_capital_flow_crawler.py tests/test_data_operations.py tests/test_data_service_http.py -q
```

行情/复盘变更：

```powershell
python -m pytest tests/test_market.py tests/test_data_service_http.py -q
```

AI 子系统变更：

```powershell
python -m pytest tests/test_ai_routes_v150.py tests/test_ai_service_http.py tests/test_ai_agent.py tests/test_ai_sessions.py -q
```

## 14. 最终交付前检查

提交前确认：

```powershell
git status --short --untracked-files=all
git diff --check
git remote -v
git branch --show-current
```

不应留下：

- 临时探针 `.py` / `.ps1` / `.js`
- 临时 `.json`
- 日志
- 安装包
- `.sig`
- 临时 `latest.json`
- 无归属 untracked 文件

只提交源码、测试、文档和必要版本文件。提交前额外确认 `项目说明书.md`、`应用创新类项目报告.md`、`演示文档操作提醒.md` 仍为 ignored/untracked，不能进入 GitHub。

## 15. 质量门禁与架构不变量

仓库有 CI（`.github/workflows/ci.yml`，全 windows-latest 环境，push/PR 触发）：pytest+ruff、eslint+tsc+vitest、cargo test 三个 job。提交前先在本地跑等价门禁，不要依赖 CI 兜底。

### 门禁命令（在常规命令之外新增）

```powershell
python -m ruff check backend tests scripts
.\.tools\node-v20.18.1-win-x64\npm.cmd run lint
.\.tools\node-v20.18.1-win-x64\npm.cmd run test:coverage
```

- ruff：line-length 140，规则集 E/F/I/UP/B；`scripts/*.py` 豁免 E402（脚本有意先调 sys.path）。
- eslint：flat config，`react-hooks/rules-of-hooks` 为 error、`exhaustive-deps` 为 warn；测试文件豁免 `no-explicit-any`。
- 覆盖率只出报告不设阈值：`pytest-cov` + `@vitest/coverage-v8`。

### 架构不变量（违反即回退）

1. **符号与数值解析只有一个家**：符号规范化/新浪转换在 `data/symbols.py`，宽松数值解析在 `data/parsing.py`。任何爬虫不得再私建 `_normalize_code`、`_to_float`、`_sina_symbol` 之类的本地副本。
2. **HTTP 策略集中在 `data/http_transport.py`**：UA 常量（MINIMAL/USER/BROWSER）、`create_scraping_session()`（trust_env=False，爬虫请求不读系统代理）、`resilient_get()`（瞬时错误重试 + curl_cffi 降级）。新增数据源先复用这一层。
3. **禁止跨模块私有访问**：service/operations/sync 只能用公共接口——`RealtimeMarketProvider.retained_successful_snapshot()`、`DataServiceState.start_coverage_refresh()/coverage_snapshot()`、`Warehouse.read_capital_flow_missing_symbols()/corrupt_partitions()`、`SyncJobManager.start_full_market()/start_capital_flow_backfill()/get_job()/cancel_job()`、`data/text_cleaning.py`（文本清洗唯一归属：`html_to_plaintext/collapse_ws/is_noisy_market_line`）。不允许再出现 `getattr(obj, "私有名", None)` 式的测试兼容 shim。
4. **依赖方向单向**：`data/*` 只允许依赖 `models` 与 data 内共享模块（symbols/parsing/http_transport/importer/trading_calendar/cls/cache/warehouse/operations/filelock/astock_adapter/cls_finance/realtime/realtime_parsers/text_cleaning/briefing——以 `data/` 目录现状为准，新增共享模块默认入列），禁止反向 import 根包（service/engine/cli）。当前全仓 0 个 import 环。
5. **回测条件必须双注册**：`conditions.py` 里每个 condition_id 必须同时有行级 `EVALUATORS` 和向量化 `MASK_BUILDERS`；`tests/test_core.py::test_condition_registry_stays_in_sync` 是守卫，新增条件只改 conditions.py 一个文件。
6. **错误响应必须带稳定 code**：后端错误码 `no_local_data / validation_error / payload_error / request_failed`（`service.py::_stream_error_code`），数据缺失类失败抛 `LocalDataUnavailable`；AI 模块额外有 `ai_not_configured / ai_upstream_error / ai_session_busy / ai_session_not_found / ai_memory_not_found`（`ai/errors.py`）。前端经 `api.ts` 的 `BackendError` 消费，AI 抽屉经 `aiTypes.ts::translateAiError` 按码翻译。新增错误路径必须带码。
7. **回环测试不走代理**：`tests/test_data_service_http.py` 用 `ProxyHandler({})` 的 opener 发起全部回环请求；开发机开着 Clash 等系统代理时测试也必须绿。AI 回环测试（`tests/test_ai_service_http.py`）沿用同一模式，且 cache_dir 必须指向 tmp 子目录（`tmp_path/"本地数据仓"`），否则 AI 配置会落在 pytest 共享根目录造成跨测试泄漏。
8. **AI 子系统边界（违反即回退）**：
   - `backend/astock_backtester/ai/` 是独立子包，只依赖 `models`、data 公共接口与根包的 backtest_runner/condition_parser/indicators；任何 data/* 或 engine 不得反向 import ai。
   - AI 对数据仓默认只读：`query_warehouse_sql` 只允许 SELECT/WITH（DuckDB 内存连接 + 语句黑名单 + 自动 LIMIT 500）；**唯一写工具**是 `update_stock_data`（`mode=daily_bars` → `fetch_daily_bars_into_cache`；`mode=capital_flow` → `fetch_capital_flow_into_cache`，省略 symbols 时按缺口名单转 `SyncJobManager.start_capital_flow_backfill` 后台任务），不得出现第二个写工具或裸 SQL 写。
   - 分层记忆：短期窗口按条数与字符数双阈值控制（`agent.SHORT_TERM_WINDOW` 条协议消息 + `SHORT_TERM_MAX_CHARS` 字符，两者都未超限才不压缩，具体数值以 `ai/agent.py` 常量为准），溢出先进 `pending_archive` 再压缩为 `rolling_summary`；长期记忆提取是哨兵之后的独立 daemon 线程，**绝不允许阻塞 /ai/chat/stream 的事件流**。
   - 爬取内容（新闻/复盘/研报）进入模型上下文前必须经 `ai/context.py::wrap_untrusted` 分隔——工具路径与定时引擎（`digest._gather_sources`/`reports._gather_review_sources`）同样适用；且必须**先按 `digest_chars` 压缩、再包不可信围栏**，反序会让闭合标记被二次截断切掉，隔离退化成"只有开标记"。哪些工具带爬取正文由**工具自声明**（`AiTool.untrusted_body`，agent 读属性判定，不再维护手工名单）；`truncate_text` 无安全边界时整段丢弃，禁止恢复"cut=limit"式硬切兜底。
   - 工具结果只以摘要进上下文，全量留在内存 `ToolResultStore`（有条数/字节/TTL 三重上限）。摘要必须携带**精确保留元数据**（`context.retain_rows` 的 seen/kept/omitted + `resume_offset`）：只写"已截断"而不说省略多少行、缺的行在哪，模型就不知道要不要、从哪续读（tail 保留时省略的是头部行，resume_offset=0）。表格/榜单类工具用 `AiTool.digest_chars` 自己声明预算（默认 1200 字会把 20 行榜单切成 8 行）；模型可用只读工具 `read_tool_result(call_id, offset)` 按行续读，**不得**为了省事把全量 payload 直接灌进上下文。
   - 工具失败必须以稳定 code 进协议 tool 消息与会话文件（`registry.CODE_*`：`unknown_tool` / `bad_arguments` / `no_data` / `tool_error`，加上 `interrupted` / `result_evicted` / `not_rowset`），让中断恢复与回放能按类别分支（改参数重试 vs 换工具 vs 先补数据）；code 服务的是代码与历史，用户文案仍走中文摘要。
   - 会话 JSON 带 `schema_version`；格式演进只走**相邻迁移**——新版本可加字段，绝不移动、改写或销毁已落盘的会话代，读侧必须继续容忍旧代。
   - 会话历史回读只允许暴露 `display`（`facade.session_view`）：协议消息、`pending_archive` 与 `rolling_summary` 不得出现在任何 HTTP 响应里，否则恢复出来的历史就能反向注入模型指令。会话回收靠**写侧动态自动清理**（`SessionStore.save` 后调 `prune`：条数上限 + 保留期，`exclude` 与忙碌会话永不动；忙判定由 facade 经 `set_busy_check` 注入，且必须同时看 `refs > 0` 与锁——只看 `lock.locked()` 会漏掉"已认领、还没 acquire"的窗口，那正是 `_SessionLockEntry` 注释里否决过的判定）。`prune` 只回收**看起来像会话**的文件（有 `session_id`，与 `list_sessions` 同口径），目录里混进导出/手写 JSON 不得被回收。`POST /ai/session/delete` 保留给脚本/测试，忙时抛 `ai_session_busy`，因为 worker 的 `finally` 会把同名文件重新写回来。
   - **研究风格必须真正改变输出**（1.6.0 起为人设规格）：`prompts.STYLE_PROMPTS` 三种风格各自带六节人设（我是谁/怎么说话/情绪怎么出来/专属词汇/禁用词表/承压与认错）+ 独立输出骨架 + 取证清单 + 决策口径；通用人设 `CORE_RULES` **不得**写死单一输出骨架（旧"四维/评股模板"已删），只保留确定性承诺词禁令等共享纪律。风格必须贯穿所有 AI 出口——chat（`build_system_prompt`）、一次性点评（`build_oneshot_messages` 的 `style_directive`）、快讯（`build_insight_messages` 的轻量口吻）、复盘报告（`build_review_prompt`，返回 messages，三出口签名一致）；`data_coverage` 与聚合要点（DIGEST）按登记口径不吃风格（`STYLE_FREE_ONESHOT_SCENES`/`STYLE_FREE_PROMPTS`，导入期校验）。风格与长期记忆冲突时**风格优先**（facade 追加"风格与记忆的优先级"段落）。语域指纹守卫在 `tests/test_ai_style_prompts.py`。
   - 长期记忆：召回与 hit-boost 必须同源（`MemoryStore.recall()` 返回 `injected_ids`，facade 交给 `bump_hits` 落盘），拆成两次查询会让 `recall_score` 的 hits 项永远是 1；`injected_ids` 只能含**真正渲染进上下文**的记录（被字符预算挡掉的不算，否则未注入的记忆也被加分，与召回排序形成正反馈）。hit-boost 必须封顶（`memory.MAX_HITS_FOR_RECALL`）：注入集合就是当前 top-N，不封顶约 44 轮后新记忆再也挤不进来，且封顶值要满足"weight=1 拉满也压不过 weight=3 的新记忆"。记忆提炼调用不带 chat 的 system prompt，必须显式传 `reference_date`，否则时间性事实落库时无日期。`hits` 累计只用于召回排序，失败一律吞掉（记忆不是关键路径）。**写侧拦截行情数字**：非 profile 类记忆命中行情数字模式即拒绝并计数（`rejected_market_facts`），同标的同类别走 update 合并；显式用户编辑走 `update_record/delete_record`（治理路由 `/ai/memories` 三条），不做拦截——那是用户的编辑权。
   - AI 抽屉样式：`styles.css` 有无作用域的 `table { min-width: 680px }`，抽屉内 Markdown 表格必须由 `.ai-markdown table` 的 `min-width:0` + `table-layout:fixed` 覆盖；`pre`/`img` 必须显式给 `max-width:100%`（sanitize 放行它们且 `<pre>` 的 `white-space:pre` 让 `overflow-wrap` 失效）；容器只写 `overflow-y` 会让另一轴变 `auto` 形成隐蔽横向滚动面，必须显式 `overflow-x:hidden`。约束来自 CSS，不得用内联样式掩盖；间距/宽度一律引用 `design.md` 的 `--space-*`/`--drawer-width`，不得新写裸 px。守卫在 `frontend/src/components/AiOverflow.test.tsx` 与 `AiAssistantPanel.test.tsx`。
   - 工具批次可以并发（并发判定以 registry 的 `read_only` 标志为单一事实来源，`agent.SERIAL_TOOLS` 保留为显式串行名单，上限 `MAX_PARALLEL_TOOLS`），但 **tool 消息必须按 `tool_calls` 原顺序在主线程落盘**——乱序会破坏 `_repair_interrupted_turn` 依赖的 assistant(tool_calls)→tool 配对，整条会话被上游判为协议非法。`update_stock_data`（唯一写工具）与 `run_strategy_backtest`（整表读进 pandas）**永远独占**。
   - 事件流静默满 `AI_STREAM_HEARTBEAT_SECONDS` 必须发 `heartbeat` 事件：前端按"多久没收到字节"判定空闲超时（180 秒），一次长回测期间的静默会被误杀成"回答中断"，而 worker 仍在跑并持着会话锁，用户下一次发送白等 90 秒。`/ai/optimize` 流同理（`AI_OPTIMIZE_HEARTBEAT_SECONDS`）。
   - 上下文截断只能落在行或 JSON 字段边界（`context.truncate_text`）：把 `"close": 12.34` 切成 `12.` 会让模型读到格式合法但数值错误的价格，比整行丢弃危险得多；禁止按字符硬切。
   - 单次 `AgentRunner.run` 产出的 UI 工件（strategy/chart）**只能是 run 内的局部 dict**，绝不允许做成实例属性——runner 是每服务一个单例，两个会话并发时会拿到彼此的策略与权益曲线。
   - LLM 配置只存 `运行产物/AI配置/ai-config.json`；`GET /ai/config` 只回掩码，`GET /ai/config/reveal` 仅用于桌面端展示用户自己的 Key；会话落盘 `运行产物/AI对话/`、记忆落盘 `运行产物/AI记忆/`；三者在 .gitignore 覆盖范围内，不得提交。
   - AI 快讯（insight）必须带 `source="ai-insight"` 与免责声明；`GET /ai/news` 的"AI 聚合要点"记录标签是 `ai-agent`（1.4.0 起的产品约定，与事件流 insight 的 ai-insight 并存）；`data_fresh` 信号只触发刷新，不得替代任何行情模块的 live 判定。
   - LLM 客户端复用 `openai` SDK（`ai/llm_client.py` 只做错误码映射与事件规范化）；测试用 FakeModel/注入 client_factory，禁止网络。
   - LLM 客户端对环回 base_url（127.0.0.1/localhost/::1）必须绕过环境代理：宿主进程可能带 HTTP(S)_PROXY/ALL_PROXY（ZCode 注入、Clash 等），代理进程转发不了本机回环端口，环回请求被劫走只会 502（2026-09-19 排查：9router 网关一直正常，AI 却全挂）。实现是 `llm_client._loopback_base_url` → openai 路径传 `httpx.Client(trust_env=False)`、anthropic 路径 Session `trust_env=False`；远程 base_url 行为不变。桌面端启动时经 `service_manager::ensure_nine_router_gateway` 幂等拉起本机网关，属 fire-and-forget，不得改为跟踪/杀掉网关子进程。
   - a-stock-data 裁剪端点（`ai/tools/astock_data_tools.py`）统一走 `data/symbols.py` + `data/http_transport.py`，东财系请求必须过 `_em_get` 限流。
   - **实时优先纪律（1.6.0 起，违反即回退）**：本地数据仓是历史数据，AI 回答任何"当前/今天/盘中"的行情问题（指数、红绿家数、板块强弱、个股价格、涨停跌停、量能）必须先走实时通道——`realtime_market_snapshot`（盘面）、`realtime_stock_detail`（个股实时价/量比/涨跌停判定，腾讯 `qt.gtimg.cn`，不读仓）、`limit_up_pool`（涨跌停池）。本地工具（`query_warehouse_sql`/`compute_stock_stats`/`recent_daily_bars`）只用于历史区间、横截面与回测，且返回必须带 `as_of_date`+`is_realtime:false`，摘要必须写"截止 X 日，非实时"；实时失败时模型必须明说，不得把本地历史包装成实时。纪律写死在 `prompts.CORE_RULES` 的"数据时效纪律"节、`facade.today_context` 与三个风格的取证清单（守卫 `tests/test_ai_style_prompts.py::test_core_rules_require_realtime_first_for_current_market`；新增实时工具必须同步进 `frontend/src/aiMocks.ts` 的 tool_names，守卫 `test_frontend_mock_status_lists_every_registered_tool`）。
   - 提示词模板含字面 JSON 时必须用 `{{ }}` 转义（`str.format` 会把 `{"content": ...}` 当占位符，曾踩坑）。
9. **symbol_lifecycle 口径（1.5.0 起）**：覆盖/同步/资金流缺口的"缺失"判定必须尊重每只股票的 `[listing_date, delisted_date]` 窗口（表在 `运行产物/本地数据仓/warehouse/metadata.sqlite` 的 `symbol_lifecycle`）；无生命周期记录一律走旧保守口径，禁止用"数据源名单缺席"单独判定退市（必须有近 30 天无新行的佐证，且名单行数 <1000 时禁用退市判定）。绿/中性色不得用于表达"最优/成功"（A 股绿=跌），前端样式只允许引用 `design.md` 的 token。

## 16. AI 子系统

目录地图、事件协议、会话历史三路由、三种 api_style、三种研究风格的骨架与贯穿口径、记忆/简报/快讯引擎与测试分层见 [`docs/ai-subsystem.md`](docs/ai-subsystem.md)。硬约束只有 §15-8 那几条，违反即回退。

改动 AI 时最容易踩的两处（都在 §15-8 里写成红线，这里只给定位）：

- **风格不是一段提示词，是一组出口**：`prompts.STYLE_PROMPTS` 的骨架、`build_system_prompt`/`build_oneshot_messages`/`build_review_prompt` 三个出口、facade 的"风格优先于记忆"段落是同一套机制，改一处要连带检查其余；四个风格表的 key 由 `prompts._check_style_tables()` 在导入期把关。
- **抽屉的视图切换是高度契约**：`AiAssistantPanel` 主区同一时刻只渲染一个面板（`view` 状态），消息区与输入区只在 `chat` 视图挂载。把某个面板改回常驻堆叠会直接吃掉消息区高度——旧实现实测固定占掉约 38%。

## 17. 红线与决策的维护规则

本文之所以再次变臃肿，靠的不是定期打扫，而是下面三条纪律（思路取自 deepseek-harness 的 Agent Notes：按 `{lifecycle}/{class}/日期-主题` 存决策、`implemented` 的事实必须跟着代码改、归档即冻结；本项目规模小，只取用得上的三条）：

1. **新增红线前先做取代检查。** 在本文搜是否已有条目覆盖同一个决策或同一个机制：被完全取代的当场删除，只是部分取代的保留双方并互相指明位置。已知冲突不要留给下一个人去发现——§15-8 曾经同时写着"短期窗口 10 条"和 §16 的 24 条。
2. **事实跟着代码走，判断不跟着改。** 条目里的路径、常量、默认值一旦代码变更，必须在同一次改动里同步（只改事实，不改当初的决策）。凡能从代码读出的数值就**不要抄进本文**，写"以 `agent.SHORT_TERM_WINDOW` 为准"即可：抄一次的数字必然会再次腐烂。
3. **否决一个方案要留下防重提的理由；理由失效就直接删掉那一行。** 见 §18。登记它是因为"不记下来就会有人重新设计一遍"，而不是为了攒清单。

## 18. 已否决的方案（防重提，不是待办）

只登记**仍然诱人、且当初的否决理由依然成立**的方案。前提变了或理由失效，就删掉这一行（它们不是待办事项）。

| 已否决 | 为什么否决 | 什么条件下重提 |
| --- | --- | --- |
| 给 `DataServiceHandler` 设 `protocol_version = "HTTP/1.1"` | NDJSON 靠 HTTP/1.0 + 无 `Content-Length` 才能逐行即时送达；改 1.1 却不手工补 chunked 分帧，整条流会攒到连接关闭才出字 | 实现了分帧写出之后 |
| 为上下文超长 / 429 / 401 新增 5 个面向用户的错误码 | 后端 detail 已带异常类名与原因，前端 `translateAiError` 也已透出，再加一层只是文案微调 | 需要**代码**按失败类别分支时（自动重试、自动开新会话）——那时做"结构化 code 进协议消息与会话文件"，服务回放与恢复，而不是服务用户文案 |
| 把归档压缩挪出首轮、改用抽取式纪要 | 压缩那一轮有 `phase` 明示不是无提示白等；代价换的是后续每轮少带几千字历史，而这块正被 §15-8 分层记忆约束保护 | 实测首轮等待确实成为主要投诉时 |
| `latest_market_digest` 进 `UNTRUSTED_DIGEST_TOOLS` | 它是模型聚合产物而非一手爬取正文，注入属二阶风险 | 简报改成直接拼接爬取正文时 |
