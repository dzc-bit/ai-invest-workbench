# 更新记录

发布历史的归档地。`README.md` 只讲"现在能做什么"，每轮发布的新增内容写在这里，不再往 README 上堆。

## 1.6.1

- **修复 `turnover_between` 双注册语义漂移（换手率条件在向量化路径上永远筛不出股票）**：`conditions.py` 的两套实现口径不一致——行级 `EVALUATORS` 会把 >1 的百分数除以 100 归一，向量化 `MASK_BUILDERS` 直接拿分数区间比百分数量纲。实测 2% 换手率行级判 `True`、MASK 判 `False`，而引擎 prefilter 走 MASK，于是 4 个推荐策略的换手率条件把候选**全部**提前丢掉。现在两套共用 `_turnover_ratio`，守卫 `test_turnover_between_row_and_mask_agree_on_percent_scale`。
- **公开 XHR 日 K 不再写入假换手率 0，改为逐行推导**：`_with_derived_columns` 此前只把 `amount` 置 NaN，没管 `turnover_rate`，`normalize_daily_bars` 的缺列默认值 `0.0` 就被当成"换手率 0%"写进新行——0 是合法值，无法与真实 0% 区分，同时污染 `turnover_between` 与候选打分。现在显式置 NaN，再由 `_apply_turnover_rate` 用 `volume/流通股×100` 按仓库口径（**百分数**，实测 229 万行 median≈0.38）回填；推不出流通股的行保持 NaN。
- **`is_st` 从证券简称派生（此前全库恒为 False）**：仓库实测 229 万行 `is_st` 全为 `False`，而 `engine._stock_limit_pct` 依赖它判 5% 涨跌停、`BacktestSettings.exclude_st`（默认 True）依赖它过滤。上游行情接口不带该字段，现在由简称派生（`symbols.is_st_name`，`ST` 子串口径，刻意**不含**"退"字——退市整理期涨跌幅仍是 10%，标成 ST 会给错的 5% 口径）。语义说明：用**当前**简称整窗标记，属保守口径，与 `risk.py` 用最新名称识别 ST 一致。
- **逐行市值不再被报价法覆盖**：百度日 K 按每行 `volume/(turnover/100)×close` 推市值（反映**当日**股本），腾讯/新浪路径用"当前股本 × 历史收盘"。此前后者会用 `combine_first` 整列覆盖前者，解禁/增发后历史市值系统性偏大。现在只补空缺行。
- **腾讯日 K 分页加墙钟预算**：页数上限 `TENCENT_KLINE_MAX_PAGES`(=64) × 单次 `timeout`(=15s) = 最坏 960s/票，页数上限并不是时间上限。新增 `TENCENT_KLINE_WALK_BUDGET_SECONDS`(=90s)，触顶按欠覆盖记录并交上层 provider 接力（`astock_adapter._fetch_tencent_kline`）。
- **北交所日 K 直接走新浪**：实测腾讯 `bj920xxx` 恒返回空 `day`，此前每只北交所股票都要先打一次注定为空的腾讯往返；现在 `bj` 段跳过腾讯。
- **资金流 crawler 四项边界收紧**：① 新浪资金流端点改 **https**（实测与 http 同载荷，同文件其余端点本就是 https）；② 新浪限速的 `sleep` 移出 `_sina_request_lock`——持锁睡眠把 8 个 worker 全串行化在 0.25s 间隔上（全市场约 23 分钟纯睡眠），与 `HostThrottle` 的"sleep 只在锁外"纪律一致；③ 东财变体矩阵加**组合**上限 `EASTMONEY_VARIANT_MAX_COMBINATIONS`(=6)——403/429 这类"连得上但拿不到数据"的失败原本会走满全部组合；按组合而非传输计数是有意的：按传输计数会让双传输形态把预算全花在第一个端点，`push2 kline` 备用端点永远轮不到（守卫 `test_eastmoney_variant_cap_still_reaches_the_kline_endpoint` 实测 cap≤4 即红）；④ 百度补日按「最近 N 个缺失日」封顶 `BAIDU_SUPPLEMENT_MAX_MISSING_DATES`(=20) 并写 `date_coverage_shortfall`；最近成功缓存按符号数封顶 `RECENT_SUCCESS_CACHE_MAX_SYMBOLS`(=512)（全市场补齐原本会常驻约 5500 个符号的全部行）。
- **金额解析收归唯一归属**：crawler 私建的 `_parse_money_amount` 移入 `data/parsing.py::parse_money_amount`（§15-1）。顺带修掉一个静默错值：旧正则 `[-+]?\d+(?:\.\d+)?` 遇到科学计数法 `1.2e8` 只取 `1.2`，丢掉指数、静默差 8 个数量级；新实现整体匹配并保留中文数量级后缀。实测三源同日同值（东财 f52 / 新浪 netamount / 百度 `+3298.68万` 均为**元**），等价性有守卫。
- **`_diagnostics_should_skip_eastmoney` 三处实现合并为一处**（crawler 导出，`sync.py` 与 `scripts/run-capital-flow-backfill.py` 导入），消除谓词漂移（`tests/test_capital_flow_crawler.py` 有跨模块引用守卫）。
- **交易日历查询加缓存**：`has_acceptable_coverage` 逐票调用 `a_share_trade_dates`（5500 票 × 最多 3 个 provider ≈ 1.6 万次全窗口重建 `date_range(freq="B")` + 节假日展开）。节假日表是静态数据，现在按 (start, end) 记忆化（返回副本，容量上限 4096 条），语义与性能都有守卫。
- **`scripts/backfill-market-cap.py` 纳入版本库**：§9 提到的历史遗留 null 市值回填脚本此前未跟踪，补齐（`--dry-run` 只数不写）。
- **`.gitignore` 收敛与清理**：新增 `.zcode/`、`.claude/`、`.ruff_cache/`、`*.egg-info/`、`*.exe`、`*.sig`、`latest.json` 等忽略规则，并保留 `!tests/**/*.log` 否定规则（守卫 `test_gitignore_keeps_test_log_negation_rule`）；同时删除历史上被误提交的 `.zcode/` 三个命令/技能文件与根目录探针残留。
- **数据正确性复核（本条为审查结论，非代码变更）**：另有两处疑似数据错误经**真实上游探针实测证伪**，不应再按误报修复——① 百度日 K `volume` **已是股**（同日腾讯 31239 手 ↔ 百度 3123935 股，且两者推出的流通股与腾讯报价一致），无需 ×100；② 东财/新浪/百度资金流单位**统一为元**，不存在差 1e4 的混列问题。

## 1.6.0

- **AI 实时优先：行情必须走实时通道，数据仓只作历史与回测**：本地数据仓同步有延迟（实测停更时 5219 只股票的数据停在 4 个交易日前），模型曾把几天前的收盘价当成"现在"讲。现在：① 新增实时个股工具 `realtime_stock_detail`（腾讯公开行情，不读仓）——现价/涨跌幅/开高低/当日区间分位/量比/换手/市值，以及**涨停、跌停、炸板（盘中触及涨停后回落）判定**与停牌识别，最多 8 只一次；② `CORE_RULES` 新增"数据时效纪律"硬规则：凡"当前/今天/盘中"的行情问题（指数、红绿家数、板块强弱、个股价格、涨跌停、量能）必须先走实时通道（`realtime_market_snapshot` / `realtime_stock_detail` / `limit_up_pool`），本地工具只用于历史区间、横截面与回测，实时失败必须明说、不得用历史数据包装成实时；`facade.today_context` 与三个风格的取证清单同步改写；③ 本地工具（`recent_daily_bars`/`query_warehouse_sql`/`compute_stock_stats`）返回带 `as_of_date` + `is_realtime:false`，摘要显式写"截止 X 日，距今天 N 个交易日，非实时"。守卫：`test_core_rules_require_realtime_first_for_current_market`、`test_realtime_stock_detail_flags_limit_status_and_range`；新增实时工具同步进前端 mock 并有防漂移守卫。
- **日线补缺主源换成公开 XHR（腾讯 → 新浪 → 百度兜底），全市场补齐从小时级降到分钟级**：实测发现三条旧通道在本机环境全灭——百度日 K 对直连 IP 恒 403 且只认完整浏览器 UA（短 UA 稳定 403，兜底链路已换完整 UA 并保留 curl_cffi 备用）；`curl_cffi` 备用传输因 CA 路径含中文整体失效（见下条）；adata 覆盖不到 2025 年底、AKShare 走系统代理被劫持。现在 `HttpAStockFetcher` 优先走腾讯/新浪公开日 K：价格口径与仓库一致（不复权），腾讯成交量单位"手"×100 归一、新浪"股"直接入库，腾讯不覆盖的北交所段由新浪接管；腾讯 `count≤320` 且只返回窗口内最后 N 行、新浪 `datalen` 按"最新 N 根"取数，两者均按窗口跨度动态分页。公开日 K 不带市值，用腾讯实时报价推导 `float_shares×close` 填充（与百度 `volume/turnover×close` 同法）。provider 实例全程复用（熔断计数挂在 adapter 上）+ 东财增强端点连续失败熔断，单票抓取从 2s 降到 0.2s。百度路径普通 requests 曾用 `proxies={}` 试图绕过系统代理但并不生效，统一改走 `trust_env=False` 会话（§15-2）。
- **北交所 920xxx 符号修复（实时行情与资金流整段缺失的根因）**：`a_share_market_symbol` 把 `92` 前缀映射为 `bj`——旧的全量 `9→sh` 规则让腾讯/新浪对 `sh920xxx` 一律返回 none_match，北交所 322 只的行情、日 K 与资金流因此永远取不到（资金流补齐 0/322 的直接原因）；`market_code` 同步修正（东财 `0.920171` 实测可达）。900xxx 沪 B 仍为 `sh`。
- **`curl_cffi` 备用传输在中文用户名 Windows 上失效的修复**：libcurl 无法从含非 ASCII 字符的路径加载 CA（`curl: (77) error setting certificate verify locations`）——certifi 装在 `C:\Users\<中文用户名>\...` 下时整条 curl_cffi 备用链路静默全灭。现在优先解析 Windows 8.3 短路径（纯 ASCII、零拷贝），否则把 CA 复制到 ASCII 目录（`%ProgramData%` / 项目 `.tools\ca`）；所有 curl_cffi 调用点统一经 `http_transport.curl_verify_kwargs()` 注入，路径筛选与复制逻辑有守卫（`tests/test_http_transport.py`）。
- **数据仓补齐实战（以上修复的实测结果）**：全市场日线停更从 5219 只降到 **24 只**（全部为停牌/退市终态，公开渠道天然无 K 线），thin day（疑似写入失败日）清零，更新到最新交易日的股票从 309/5530 提升到 **5504/5530**；资金流近月缺口 322/322 全部补齐（此前 0/322）。补齐走 `POST /sync/missing-only` 缺口名单链路，两轮共 5562 次标的抓取、补缺 20691 行。
- **研究风格升级为"人设规格"**：1.5.2 只让三种风格的骨架不同，实测语气/情绪温度仍高度趋同——均衡块 623 字是三块里最薄的（与激进差 2.4 倍）、短点评口吻指令只有 18~51 字（却要驱动 80~120 字的输出）、均衡几乎"没有作者"。现在每个风格块写满六节：**我是谁**（具体职业背景，让模型"演"这个人）、**怎么说话**（句式层面区分：保守=短判断句+审计口径、均衡=对照句+设问+对价句式、激进=短句动宾开头+行情黑话）、**情绪怎么出来**（看多/看空/不确定/遇险四处境逐一写清）、**专属词汇表**、**禁用词表**（防串味最有效的单条约束：保守禁打板/卡位/满仓，激进禁安全边际/股息率，均衡两套黑话都不沾）、**承压与认错方式**。口吻指令加厚到 146~168 字。新增语域指纹守卫（块厚比 ≤1.6、指令长度 ≥140、情绪温度可量化差异、专属词汇不串味、全表面无确定性承诺词），"像不像不同的人"从此可回归。情绪纪律五条边界全部保留并写入测试：数字来自工具、免责声明与风险段落齐全、保守显式拒绝进攻性打法、激进声明不作持有型建议、任何表面无"必然/必涨/稳赚"。
- **风格事实来源收敛**：`config.SUPPORTED_RESEARCH_STYLES` 不再是第二份独立元组，直接复用 `prompts.RESEARCH_STYLES`（此前一致性守卫只查 prompts 侧，新增风格漏改 config 会静默降级为 balanced）；"无风格场景"从隐式契约升级为显式声明 `STYLE_FREE_ONESHOT_SCENES` + 导入期校验。
- **风格贯穿第四个出口**：AI 快讯（INSIGHT_PROMPT）注入轻量口吻指令；聚合要点（DIGEST）是事实归纳，按登记口径保持中立。`build_review_prompt` 改为返回 messages，与 chat/oneshot/insight 三出口签名一致，新增风格不再需要隐性知识。
- **定时复盘报告的「风险提示」修复（静默失效两版）**：拼行用了 `RiskAlertItem.summary`——该字段根本不存在，抛出的 AttributeError 被紧邻的 `except Exception: pass` 吞掉，每一份定时复盘（含数据摘要版兜底）的风险提示段都只剩一行表头。改用真实字段（symbol/name/risk_type/severity/reason）拼行，并补上同文件其余段落都有的 `_crawled_review_block` 不可信围栏。
- **归档窗口不再产生孤儿 tool 消息（会话永久不可用的根因）**：`_archive_overflow` 在长工具轮中段（1 条 user + 28 组 assistant(tool_calls)/tool、无尾部 user）找不到 user 边界时回退到字符裁剪点，窗口头变成无主 tool 消息——OpenAI/Anthropic 都拒绝首条为 tool 的请求，该会话此后每轮必败。现在找不到安全切点就归档 0 条并留 warning，宁可一轮超预算也绝不产生非法窗口。配套修复 `_repair_interrupted_turn`：孤儿 tool 直接丢弃；`answered` 集合不再全局共享（`llm_client` 在供应商不回 id 时自造 `call_{index}`，跨轮重复 id 曾让后面的 assistant 永远等不到配对结果）。
- **红绿家数链路不再被 10 秒全仓扫描拖死**：`_coverage_symbol_count` 每次实时请求都可能触发 `warehouse.coverage()`（真实数据仓实测 ~10s），而红绿家数总预算只有 8s——主源不完整时这条路径吃光预算，把后面本可用的 Sina/Tencent/AKShare 全部判超时。现在股票池计数走仓库侧 600s TTL 缓存（与缺口画像同族：TTL + 写入失效），缓存未热转后台预热、diagnostics 留痕，绝不在行情链路上现算。
- **AI 投研原语三件套**：`screen_stocks`（全市场横向筛选，复用 conditions.py 双注册语义与回测同款指标增强，替代手写 SQL 逐条件拼）、`stock_timeline`（单股事件时间轴：本地日线+龙虎榜+涨停池+研报按日期倒序一次合并，跨源失败逐源标注不整体失败，摘要进上下文前压缩再包不可信围栏）、`my_positions`（只读长期记忆中的持仓/自选，明示"记忆是用户自述非行情事实"）。注册表增至 23 个工具，`/ai/status` 与前端 mock 清单同步并有防漂移守卫。
- **AI 记忆治理**：新增 `GET /ai/memories`、`POST /ai/memory/update`、`POST /ai/memory/delete`（此前全仓无任何路由/UI 能查看或修改记忆，前端只有一句"N 条"计数）；写侧拦截"把行情数字当持久事实"的记录（真实记忆文件里已有"9/22 主力净流出 4.44 亿"这类过夜即错的事实），非 profile 类命中行情数字模式即拒绝并计数，不再静默；同标的重叠记录（同 category + 相同 6 位代码）走 update 合并而不是新增。AI 抽屉新增「记忆」视图（进入现有切换条，不常驻堆叠），可编辑/删除；`MEMORY_OPS_PROMPT` 明确"行情数字绝不写入"。
- **后端能力接线**：`/market/commentary`（451 行四段状态机此前前端引用数为 0）接入行情区——状态语义显式：非 intraday/post_close 的 mode 一律标注"非实时"，本地简短判断不包装成实时盘面；`MarketCommentaryResponse` 补齐 TS 类型。`SyncJobManager.incomplete_symbols`（此前是死 API）补 `POST /sync/missing-only` 路由与数据中心「只补缺口」按钮——以缺口为基准只对名单发起抓取，不做小时级全量扫描。`corrupt_partitions` 补 TS 类型并在缺失数据监控折叠区展示，有损坏时明确"先修损坏再补数据"。新增组件级守卫：覆盖表 missing_rows 只能来自刷新后的真实仓库 coverage，绝不能用本次 imported_rows 抵扣。
- **Mock 层退出生产包**：`apiMocks/aiMocks` 此前被无条件静态 import，dist 里能搜到"示例股份 / sk-demo-key / 预览模式"，且 `mockAiStatus` 只列 4 个工具名（真实 23 个）漂移不可见。现在经 `previewMocks` 动态 import + `import.meta.env.DEV` 门控，生产构建整个摇掉；非 Tauri 环境页面常驻"浏览器预览：演示数据"横幅，`callBackend` 在无 mock 的生产浏览器里明确报错而非静默返回假回测结果。`isTauriRuntime` 三处重复实现收敛到 `tauriRuntime.ts`。
- **测试与门禁提效**：资金流爬虫三个用例放过真实退避各睡 6 秒（全量 18 秒），改 monkeypatch 并断言退避序列（比静默等待更强）；`/identity`、`/health` 的耗时断言与 0.4s 假延时互相竞速，改为"coverage 从未被调用"/Event 握手的确定性断言；vitest 覆盖率开 `all: true`——此前 15 个源文件（整个 AI 抽屉面）不在分母里，"90.4%"掩盖 AI 面零覆盖（真实值 84.8%），并为 `AiEquityChart`（83 行 ECharts 生命周期）补测；CI 新增 `package` job 真正跑一次 `build-data-service.ps1` 并断言 sidecar 四件套（exe/node.exe/ths-cookie-worker.cjs/xhr-sync-worker.js）存在——§5 的安装版红线此前只被脚本文本 substring 断言守着，CI 永远验证不到；新增静默吞异常棘轮（`tests/test_no_silent_swallow.py`，借 deepseek-harness"失败必须响亮"纪律）：`except Exception: pass` 圈定在显式豁免名单里（基线 17 处，只许减不许增），新代码再写静默吞必须先登记理由——P1-① 静默失效两版无人发现正是这类形态。
- **发布脚本防旧签名复用**：`write-latest-json.ps1` 校验 .sig 内嵌 file 名与 -AssetName 一致、.sig mtime 不早于安装包、-Version 缺省读 package.json——bundle/nsis 里堆着多个历史版本的 exe+sig，此前传错参数就会把旧 .sig 配上新版本号。
- **性能与协议卫生**：回测 `_next_trade_date` 线性扫描改二分（2500 日 O(days²)≈300 万次比较 → 二分）；`providers.py` 两处全市场 iterrows 改 itertuples；AI `truncate_text` 删掉"cut=limit"硬切兜底——无安全边界时整段丢弃，绝不把 `"close": 12.34` 切成 `12.`（该兜底正是 1.5.2 修的病留下的后门）；`read_tool_result` 摘要先截表体再拼"还剩 M 行"尾注，避免尾注被硬截切掉（模型读到半份数据却不知道还有剩）；工具自声明 `untrusted_body` 取代 agent 手工名单，新增爬取类工具忘加围栏会静默退化隔离；未知工具串行执行（不再扇出线程池只为返回 unknown_tool）。
- **脚本符号收敛**：`run-capital-flow-backfill.py` 与 `run-full-market-import.py` 各自逐字节复制的 `normalize_symbol` 删除，统一从 `data/symbols.py` 导入（§15-1）；后者补上 sys.path 引导。
- **文档履约（§17.2）**：本文件 `## 未发布` 移回顶部（此前排在 1.6.0 之后，读者无法判断归属版本）；AGENTS.md §15-4 依赖白名单措辞修正、§15-9 补 metadata.sqlite 具体路径；`test_release_manifests_use_one_version` 不再硬编码版本号（以 package.json 为锚点校验跨文件一致性）。

## 1.5.2

- **审查修复轮**（独立审查发现问题的集中修复）：
  - **资金流缺口口径修正**：coverage 停更尾部的资金流边界改为取 OHLC 末行与资金流末行的较大者——此前"暂无日 K 先写资金流独立行"的股票会被按日线末行反复计成缺口，表现为补齐资金流后覆盖卡缺口不降、与"缺失数据监控"折叠区互相矛盾。
  - **AI 能补资金流缺口了**：`update_stock_data` 新增 `mode="capital_flow"`（与 `/fetch/capital-flow` 同链路：跳过已完整、可为暂无日 K 的股票写独立行），省略 symbols 时自动按缺口名单启动全市场后台任务并新增只读工具 `sync_job_status` 轮询进度；失败/缺失明细行集化进 `rows`，模型能改参数重试而不是只看到"缺 N 只"。AI 写路径不再同步做 60 秒级全仓 coverage 重扫（转后台刷新）。
  - **AI 缺口探索更准**：`data_health_report` 携带覆盖缺口汇总（累计真实缺口口径）与停更逐条明细（`rows`，可 `read_tool_result` 续读）；缺口画像剔除退市股并单列 `delisted_symbols`；分区损坏时明确回 `warehouse_corrupt`（"先修损坏"而不是"补数据"）；`read_capital_flow_missing_symbols` 改为扫窗口覆盖的所有年分区。
  - **定时简报/复盘报告的爬取正文过不可信围栏**：`digest._gather_sources` 与 `reports._gather_review_sources` 的新闻/涨停池/复盘段落此前裸拼进 prompt（工具路径有 `wrap_untrusted`，定时引擎路径漏了）；同时 agent 的不可信工具摘要改为**先压缩再包围栏**，闭合标记不再被二次截断切掉。
  - **续读 offset 修正**：`retain_rows` 新增 `resume_offset`（"第一行没展示的行号"）——`recent_daily_bars` 是 tail 保留（省略的是头部行），旧提示让模型从已展示的尾部行续读，"近 30 日哪天放量"依旧答不全。
  - **历史对话折叠区空列表死路**：列表为空时也渲染折叠区（空态文案 + 展开即刷新），首次开抽屉没历史的用户产生第一轮对话后能正常看到历史。
  - **其余**：zaopan 页面 200 但解析为空时不再输出"已读取"套话，走与 fupan 相同的行情/本地兜底；`news._clean_html_text` 升级为 text_cleaning 统一清洗（script/style 内文不再残留、空白折叠）；新增 `data/text_cleaning.py` 作为文本清洗唯一归属（briefing/market_commentary/研报标题统一消费）；THS 大盘评分的 dppj 正则限定在 `<script>` 块内且拒绝 0 分占位值；briefing 编码改"声明优先、缺失才探测"；optimize 流补 15 秒 heartbeat；SQL 摘要透出"已达 500 行上限"；`retain_rows` 数字不再用 `%g` 削精度；前端 AI 错误文案补句读、TS 类型补 heartbeat；交易日历超出节假日表覆盖年份时打 warning；并发工具判定收敛到 registry 的 `read_only` 标志。

- **AI 对话历史续用**：`GET /ai/sessions`、`GET /ai/session?session_id=`、`POST /ai/session/delete` 三条路由接上了早已落盘却从未接线过的 `SessionStore.list_sessions/delete`。AI 助手抽屉现在首次打开会把最近一条有内容的会话回读成当前转录，并在"历史对话"折叠区里列出全部历史会话，可切换、可删除、可新建。追问继续带着被恢复的 `session_id`，因此更早的对话与滚动纪要会真的回到 agent 上下文，重启应用不再"失忆"。回读只暴露展示用的 `display`，协议消息与待压缩归档不出网络边界；正在生成回答的会话不允许删除（回 `ai_session_busy`，否则 worker 收尾时会把文件重新写回来）。
- **AI 对话交互修正**：提交后立即清空输入框（此前文本残留，再按一次 Enter 会把同一句话重复发给 agent）；转录行改用含 `ts` 的稳定 key，不再用数组下标，避免整段 display 被替换时复用错位的 DOM 节点。
- **回答出字更跟手**：NDJSON 写出关掉 Nagle（`disable_nagle_algorithm`），此前 Windows 回环上的逐事件小包被攒住，表现为出字一顿一顿；`react-markdown@10` 的 `Markdown()` 每次渲染都重建 processor 并同步重解析且自身不 memo，所以流式期间每个 token 都会把**全部历史轮次**重解析一遍——现在历史与流式块都走 memo 化的 `MarkdownBlock`（插件数组提到模块级，否则内联字面量让 memo 失效），token 再按 `requestAnimationFrame` 合帧提交；自动滚底改为只在用户原本贴着底部时跟随，流式期间能往上翻前文。
- **长工具调用不再被误杀**：一次全市场多年的回测可以几分钟不产生任何事件，前端 180 秒"没收到字节即超时"会把这轮误判成"回答中断"，而 worker 仍在跑并持着会话锁，用户下一次发送要白等 90 秒。事件流现在静默满 15 秒就补一条 `heartbeat`（前端对未知 type 本就静默忽略）。历史会话列表同时改为**展开折叠区时才刷新**：`list_sessions` 会全量读一遍会话 JSON，挂在每轮响应上等于随使用时长变慢。
- **一轮思考更快**：模型一次给出多个只读工具调用时（个股诊断常见 4-5 个），不再串行把上游延迟乘 N 倍——纯读批次用有界线程池扇开，省 4-8 秒；`update_stock_data`（唯一写路径）与 `run_strategy_backtest`（整表读进 pandas）保持独占，tool 消息仍按 `tool_calls` 原顺序落盘以维持协议合法性。
- **模型终于知道"今天"是几天**：system prompt 每轮注入北京时间当天日期与星期，并要求最新交易日必须用工具确认。此前模型没有系统时钟，问"今天/近期"只能猜日期，猜错就查空再编数。
- **修掉三处缺陷**：① 步数耗尽的收尾回答把流式分片与 `final.content` 都收进结果，而 `final.content` 本身就是分片拼接 → 那一次回答内容 100% 双写进 `messages`/`display`/会话 JSON 并从此每轮回放（永久膨胀），现在以 `final` 为准；② `AgentRunner` 是每服务单例而 artifacts 是实例属性 → 两个会话并发时 A 能拿到 B 的策略与权益曲线，改成 run 内局部字典；③ 上下文按字符硬切会把 `"close": 12.34` 切成 `12.`，模型读到的是格式合法但数值错误的价格，现在只允许在行/JSON 字段边界截断。
- **AI 报错更可诊断**：上游失败时前端不再把整条消息换成一句"检查网络、API Key"，服务商返回的病因（上下文超长 / 401 / 429 / 超时）会截断后附在提示里，并补一句"过长请新建对话再拆小问题"。
- **模型终于看得清它查到的数据**：`summarize_sql` 以前只报"成功 N 行 + 首行前 4 列"、`summarize_bars` 把最多 120 个交易日塌成一行，而 system prompt 又要求筛选/排序类问题走 SQL 工具——"涨幅前 20""近 5 日哪天放量"因此必然答不全。现在共享的抽象是**保留**：`context.retain_rows` 输出紧凑表格并给出精确的 `seen/kept/omitted`（近 N 日行情按 `tail` 保留最新的行），表格类工具自己在 `AiTool.digest_chars` 上声明预算（默认 1200 字会把 20 行涨停梯队切成 8 行），新增只读工具 `read_tool_result(call_id, offset)` 让模型按行续读剩下的部分；`ToolResultStore` 补上字节与 TTL 上限，被淘汰的结果明确回 `code=result_evicted`（"不是数据不存在，请重新调用原工具"），不会被误判成仓库缺数据。
- **工具失败带上可分支的类别**：`unknown_tool` / `bad_arguments` / `no_data` / `tool_error`（以及 `interrupted` / `result_evicted` / `not_rowset`）现在跟着摘要进协议 tool 消息与会话文件，中断恢复和回放后仍能区分"改参数重试""换工具""先补数据"；面向用户的仍是中文摘要，不做文案分层。
- **回答被截断不再静默当完整**：三协议统一读取终止标志（`finish_reason == "length"` / `response.incomplete` / anthropic `stop_reason == "max_tokens"`），命中的回答在展示末尾标注"因输出长度上限被截断"；anthropic SSE 里的 `error` 事件（如 overloaded_error）此前落进分支空档被忽略，现在抛为上游失败；Responses 分支也修好了从未传 `temperature` 的问题。输出上限此前只在 anthropic 硬编码 4096、另两种协议根本不传，现统一为设置里可调的 `max_tokens`（256~32000）。
- **会话文件加 schema_version**：刚落盘的历史现在是导入/回读的功能性依赖，因此带上版本位并确立**相邻迁移**规则——新版本可加字段，绝不移动、改写或销毁已落盘的会话代，读侧继续容忍旧代。
- **agent 文档分层**：`AGENT必读.md` 改名为 `AGENTS.md`（跨工具通用入口名），一次性发布流水账与可从代码导出的模块手册迁到本地 `docs/`（该目录被 `.gitignore` 排除、不入库）——`docs/release-build.md`、`docs/ai-subsystem.md`、`docs/frontend-style.md`，README 的历史发布段迁到本文件；`AGENTS.md` §12/§16 各留摘要与红线并标明手册只在维护者本机，README 也不再链接未入库的 `docs/`。同时修正了本文与 §16 关于短期窗口"10 条 / 24 条"的自相矛盾、`AGENTS.md` 两条失效引用，并把版本文件清单换成 `test_release_manifests_use_one_version` 守卫。另新增 §17/§18 两条**防复发**纪律：新增红线前必须先做取代检查、能从代码读出的数值绝不抄进文档、被否决的方案登记"为什么否决 + 什么条件下可重提"（理由失效即删行）——做法取自 [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) 的 Agent Notes 生命周期。

## 1.5.1

本轮聚焦"数据仓在多进程下不再写坏、缺口能说清楚、AI 能定时出报告"：

- **数据仓跨进程写锁**：新增 `data/filelock.py`（`CrossProcessFileLock`），桌面端 sidecar 与外部补齐脚本同时写同一数据仓时串行化写入，修复并发写入导致的 parquet 分区损坏。
- **批量写入从 O(n²) 降到线性**：日线/市值/资金流批量写入不再每个批次重扫已写分区，全市场同步的写入耗时随批次数线性增长。
- **损坏分区不再静默**：读取失败的分区被记录并通过 `Warehouse.corrupt_partitions()` 暴露，数据中心可见，不再表现为"数据凭空缺失"。
- **覆盖缺口改为累计真实缺口口径**：日线缺失 = 每只股票 `[首行日期, min(最新数据日, 退市日)]` 窗口内的期望交易日数 − 实有行数（内部空洞与停更尾部都计入）；市值/资金流在"已有行但字段为空"的统计之外再叠加停更尾部。多日未同步时缺失行数会到数十万级，这是真实缺口而不是异常，唯一补齐手段仍是全市场同步。
- **缺口画像与"缺失数据监控"**：`GET /diagnostics/data-gaps` 与 `Warehouse.data_gap_profile()` 给出停更分布、疑似写入失败日、市值与资金流停更尾部；数据中心折叠区按缺口降序列逐股明细，AI 助手的 `data_health_report` 工具消费同一份明细，保证 UI 与 AI 看到一致的"具体缺什么"。
- **AI 定时报告**：`ai/reports.py` 用单一调度线程按本地时间运行——收盘复盘报告（无模型时退化为数据摘要版）与策略库自动体检（重跑已存策略近 180 天 + 4 变体小网格 + 过拟合检测 + 与上次体检的漂移，不改写用户保存的策略）；报告落盘 `运行产物/AI报告/`，AI 助手面板内可列出并下载（`GET /ai/reports`、`GET /ai/report/file?name=`）。
- **回测过拟合检测**：`POST /ai/overfit/check` 对交易数、胜率、收益结构、网格离散度做确定性检测（无需模型）；回测完成后收益概览出现过拟合卡，分"轻微提示 / 存在疑点 / 高"三档。
- **实时行情降级重试**：快照缺红绿家数（或强势板块）时不再等满整个正常周期，短暂等待后重试一次；本轮确实缺红绿家数时沿用最近一次有数据的宽度并明确标注"沿用"，不伪装成已返回。
- **AI 助手交互修正**：抽屉打开时隐藏悬浮球（原先正好压住输入区发送按钮）；上一轮回答仍在生成时明确提示"等待或先停止"，不再静默吞掉输入。
- **embedding 独立供应商**：可为向量检索单独配置 Base URL 与 API Key，留空则跟随上方主配置。
- **知识库**：RAG 语料新增异动/监管/量能一篇。

## 1.5.0

本轮以"有专业投研深度、UI 有设计品质、数据模型正确"为目标，包含七块内容（设计决策见 [`design.md`](design.md)）：

- **策略条件编辑器 AI 化瘦身**：策略配置页顶部新增"AI 条件理解"自然语言输入框——写"近5天放量上涨、主力净流入为正，破20日线卖"，点"AI 理解并写入"，后端 `POST /ai/conditions/parse` 用 LLM 生成候选条件 DSL、逐条本地校验、校验失败自动带报错重试（≤2 次），返回可勾选的条件清单（带"近似说明"角标与未识别提示），确认后一键写入策略。原三段式手工编辑器（校验→添加→模板面板）收进"高级模式"折叠区，默认收起，AI 未配置时输入框置灰但不影响高级模式。
- **AI 点评全覆盖**（统一走 `POST /ai/insight/oneshot` 轻路由，失败静默）：回测完成后收益概览指标条下自动出现 ≤80 字 AI 短评；数据中心新增"AI 诊断缺失"按钮（分析覆盖缺失模式并指路补齐按钮）；风险股票清单弹窗顶部新增 AI 一句话解读。
- **数据股票池动态化（新上市/退市感知）**：本地仓 metadata 新增 `symbol_lifecycle` 表（上市日/退市日/状态）。覆盖计算的期望交易日截断到每只股票的上市–退市窗口内——新上市股票上市前的交易日不再记为"缺失"，已退市股票退市后的日期也不再永远缺失；全市场同步股票池自动剔除已退市股票（数据源完整性不足时保守跳过，绝不误杀）；逐股覆盖明细带"未上市（YYYY-MM-DD 起）/ 已退市"徽标。上市日期来自 adata 股票列表与抓取行的 `listing_days` 字段，全市场同步时顺带刷新。
- **策略参数寻优 Agent**：`POST /ai/optimize` 对当前策略的数值参数（持仓天数/止盈止损/仓位/挂牌数等白名单键）做网格搜索，最多 48 个组合，NDJSON 流式返回逐组合收益/回撤/胜率对比表，结束后附 AI 解读（最优区间 + 过拟合警告）；前端策略配置页新增"AI 参数寻优"面板（参数行可增删、候选值可编辑、最优组合高亮）。
- **回测报告 HTML 导出**：回测完成后点"导出报告"即可下载单文件 HTML（权益曲线 SVG、指标卡、策略参数、交易明细、AI 解读），纯前端 Blob 生成，无新后端依赖。
- **数据源健康监控**：`GET /diagnostics/sources` 聚合实时行情/市场新闻/财联社看盘三个数据源最近一次成功状态与距今年代（含诊断文本），数据中心折叠卡片"数据源健康监控"展示，帮助排查"大盘评分不可用"一类问题。
- **前端设计系统（Hallmark 重构）**：`styles.css`/`ai-panel.css` 全部落到命名 token（`:root` 设计令牌块），token 之外 0 处硬编码色值、装饰渐变清零；左侧实色边条统一为顶部 accent 条；数字容器统一 `tabular-nums`（A 股红涨绿跌语义不变）；全局 `:focus-visible` 焦点环；320/375/768/1280 宽度实测无页面级横向滚动。信息架构、中文文案与 aria 标注不变。

## 1.4.0

本轮新增 AI 投研助手模块（`backend/astock_backtester/ai/`，独立子包，对存量模块只读）：

- **评股 Agent**：`POST /ai/chat/stream` NDJSON 流式对话。Agent 通过 17 个工具（实时行情、新闻、复盘、风险清单、本地日线+均线、条件校验、受控回测、腾讯估值、东财研报、龙虎榜、涨停池、DuckDB 只读 SQL、统计函数、受控数据补齐、知识检索、多股对比、AI 聚合要点）完成个股诊断与行情问答；正文流式输出，工具调用过程可视化，所有数字要求标注来源工具。其中 `update_stock_data` 是唯一写工具（走数据中心同款补齐链路）。
- **多协议接入**：设置里可选 API 协议格式——`chat-completions`（OpenAI 兼容，默认）、`responses`（OpenAI Responses API）、`anthropic`（Anthropic Messages API，含 tool_use/tool_result 流式映射）；密钥/协议等配置仅存本地。
- **启动 AI 资讯聚合**：服务启动时（已配置模型）Agent 自动汇总新闻/涨停池/昨日涨停表现/实时行情/复盘等多源数据，生成 3-6 条结构化"AI 聚合要点"，展示在资讯面板顶部（标注 ai-agent，与原始资讯模块严格分离），也可通过 `GET /ai/news` 与 `latest_market_digest` 工具消费；之后按固定间隔自动刷新。
- **NL→策略 DSL**：自然语言生成条件 DSL → 先校验（校验失败带模板示例自我修正）→ 受控运行本地回测 → 结果可一键"应用到策略工作台"。
- **上下文工程与分层记忆**（参考 MemGPT/Letta、mem0 的分层思路，本地化裁剪）：短期上下文窗口硬性保留最近若干条协议消息，溢出部分归档并压缩为会话滚动纪要；长期记忆由模型在每轮结束后提取持久事实（关注标的/策略偏好/参数习惯），去重合并进 `运行产物/AI记忆/memory.json`，并按更新时间注入后续 system prompt。工具全量结果留在后端 `ToolResultStore`，进上下文的只有每工具摘要；爬取内容以不可信分隔符包裹（提示词注入防御）。
- **真实执行能力（NL→SQL + 计算函数 + 受控写入）**：Agent 可对本地日线数据仓发起 DuckDB 只读 SQL 查询（`query_warehouse_sql`，hive 分区 parquet 直查，强制 SELECT/WITH、自动 LIMIT 500），可调用统计函数（`compute_stock_stats`：区间收益/年化波动/最大回撤/资金合计），可通过 `update_stock_data` 用数据中心同款补齐链路把指定股票区间数据写回仓库——这是唯一的写路径，SQL 层禁止任何写语句。
- **本地知识检索（RAG）**：投研方法论 / 条件 DSL 语法 / 数据字段规则三份语料，langchain-text-splitters 分块 + OpenAI 兼容 embedding（磁盘缓存）+ numpy 余弦 Top-K，以 `retrieve_knowledge` 工具挂给 Agent。
- **AI 快讯推送**：`GET /ai/events/stream` 长连接。规则触发器（新闻变化 / 市场宽度异动 / 风险清单变化）发出 `data_fresh` 信号，前端立即刷新对应模块（推拉结合，替代死等轮询）；配置模型后按小时级配额生成 AI 快讯（`insight`，强制标注 ai-insight，不构成投资建议）。
- **配置与安全**：LLM 配置存于 `运行产物/AI配置/ai-config.json`（不进 Git；设置弹窗可显示/隐藏自己的 API Key，接口默认只回掩码）；AI 模块对数据仓默认只读，唯一写路径是 `update_stock_data` 补齐链路；AI 生成的策略/结论不进入 `latest_strategy_matches` 候选管线；评测集见 `scripts/ai_eval.py`（20 条 NL→DSL 用例，本地跑，不进 CI）。

LLM 客户端复用官方 `openai` SDK（任何 OpenAI 兼容服务商均可，配置默认留空，由用户在应用内"AI 助手 → 设置"填写）。
