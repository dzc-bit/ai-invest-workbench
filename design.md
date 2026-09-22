# Design — A股策略回测工作台

A locked design system for this app, produced by the v1.5.0 Hallmark redesign.
Every page/style change reads this file first. Do not regenerate per page —
extend or amend this file when the system needs to grow.

## Genre
**modern-minimal**（数据工作台 / 金融工具）。功能密度优先，装饰归零。

## Hallmark audit（v1.5.0 重构前基线，punch list）

### Critical
1. **Mid-render token improvisation** — `styles.css` 510 处硬编码 hex、57 处 rgba，仅 19 处 `var()`；127 处 font-size、92 处 border-radius 全部裸值。无 token 块可言。
2. **多色装饰渐变** — `body` 背景 120° 三色渐变（teal→blue→amber），另有 6+ 处卡片级白→色渐变（summary 卡/heat 卡等），读作"AI 高级感"。
3. **Side-stripe card** — `.ai-oneshot-line` 左侧 3px 实色边条（2018-SaaS tell）。
4. **Card-in-card** — `.surface` 白卡内再嵌 `.config-panel`/`.condition-expression-box`/`.template-panel` 等带边框卡片。**修正裁量**：数据密度要求保留分组容器，但统一为 hairline（--rule）单层语言，不再制造第二套视觉层级（已在 design.md 记录为 app 页面豁免）。

### Major
5. **数字未统一 tabular-nums** — 收益/回撤/家数等关键数字列只有 6 处 tabular-nums，行情工作台的核心数据没有对齐纪律。
6. **字阶无比例** — 12px×60、13px×30 等 14 种裸值散布。
7. **圆角无比例** — 8px×34、999px×25、12px×11、10px×8、14px、6px、18px 共 9 种。
8. **focus-visible 仅 3 处**，无统一 ring token。

### Minor
9. `.optimizer-table tr.best-row` 用绿色底 —— **A 股语境里绿色=跌**（--fall），最优组合标绿是语义事故；改 accent 选中色。
10. 15 处 box-shadow 无系统（大扩散阴影当深度）。
11. `--blue/--amber/--violet` 等 token 定义了但 19 处 var() 之外几乎无人引用。

**计数：4 critical · 6 major · 3 minor。**

## Macrostructure family
单页应用（app shell），信息架构由产品契约锁定（顶栏 → 行情区 → 概览带 → 工作台 → 数据中心 → 悬浮 AI）。本文件不改变页面骨架，只锁定视觉系统：

- App 页面：Workbench 家族 —— 面板网格 + 密度优先，禁止 hero/enrichment。
- 概览带（summary-band）：允许一张主卡（市场热度）+ 一张状态卡（大盘评分）携带色带身份，其余卡片保持中性。

## Theme（锚定 A 股语义的自定义调色板）

| Token | 值 | 角色 |
| --- | --- | --- |
| `--paper` | `#ffffff` | 卡片表面 |
| `--paper-2` | `#f8fafc` | 次级表面 / 内嵌面板 |
| `--paper-3` | `#fbfcfe` | 表单面板 |
| `--backdrop` | `#edf3f7` | 应用背景（纯色，无渐变） |
| `--ink` | `#17212f` | 主文字 |
| `--ink-2` | `#526071` | 次级文字 |
| `--ink-3` | `#64748b` | 弱化文字 |
| `--rule` | `#d7dee8` | hairline |
| `--rule-soft` | `#e3e9f1` | 更浅分隔 |
| `--rule-strong` | `#b8c5d5` | 强分隔 |
| `--rule-tint` | `#cfe0f5` | 行情面板蓝调 hairline |
| `--accent` | `#0f766e` | 品牌青（按钮/选中/链接） |
| `--accent-strong` | `#0d5f59` | hover 深青 |
| `--accent-soft` | `#99d6cc` | 青色 hairline/wash 边 |
| `--accent-wash` | `#eef6f5` | 青色浅底 |
| `--rise` | `#d92d20` | **A 股红=涨** |
| `--rise-deep` / `--rise-tint` / `--rise-wash` | `#b42318` / `#f3b0a5` / `#feecec` | 红系层级 |
| `--fall` | `#079455` | **A 股绿=跌** |
| `--fall-deep` / `--fall-tint` / `--fall-wash` | `#067647` / `#bbe6d1` / `#ecfdf3` | 绿系层级 |
| `--warn` / `--warn-deep` / `--warn-ink` / `--warn-tint` / `--warn-wash` | `#d97706` / `#b45309` / `#9a3412` / `#fed7aa` / `#fff7ed` | 风险/警示 |
| `--info` / `--info-deep` / `--info-tint` / `--info-wash` | `#2563eb` / `#1d4ed8` / `#bfdbfe` / `#eff6ff` | 信息蓝 |
| `--ai` / `--ai-deep` / `--ai-tint` / `--ai-wash` | `#7c3aed` / `#6d28d9` / `#ddd6fe` / `#f5f3ff` | AI 内容专用紫（用户能一眼认出"这是 AI 说的"） |
| `--focus` | `#0891b2` | focus ring |

规则：**语义色不做装饰用途**——"最优行/选中"用 accent，绝不用 --fall 绿（绿=跌）；AI 生成内容只允许紫系强调。

## Typography
- Body：`"Microsoft YaHei", "Segoe UI", Arial, sans-serif`（本地中文桌面应用，不引入 webfont）。
- 数字纪律：所有指标/表格/快照数字容器统一 `font-variant-numeric: tabular-nums`。
- 字阶（token）：11 / 12 / 13 / 14 / 15 / 16 / 18 / 20 / 22 / 26 px，全部经 `--text-*` 引用。
- 标题一律 roman；强调用字重与 accent，不用斜体。

## Spacing
继承现有 8pt 系裸值（本次仅锁定颜色/字号/圆角；间距 token 化列为后续工作）。

## Radius
`--radius-s: 6px` · `--radius-m: 8px` · `--radius-l: 12px` · `--radius-xl: 16px` · `--radius-pill: 999px`。

## Motion
**Motion-cut**：0 处 transition/animation（数据工具，状态即时呈现）。因此无需 reduced-motion 分支；后续若加动效，先补 `prefers-reduced-motion`。

## Microinteractions stance
- 静默成功（silent success），无庆祝性 toast。
- focus ring：全局 `:focus-visible` 用 `--focus`，≥3:1 对比，不动画。

## CTA voice
- Primary：实底 `--accent`，白字，`--radius-m`。
- Secondary：白底 hairline（`--rule`）+ `--ink` 字。
- 危险/警示按钮用 `--warn` 系，不用红（红留给"涨"）。

## What pages MUST share
- `--rise`/`--fall` 的 A 股语义（红涨绿跌）不可动摇。
- 顶栏/卡片的 hairline 语言（1px `--rule`）。
- 中文文案与 aria 标注（由前端测试守卫）。

## What pages MAY differ on
- 各功能区的密度（行情区高密度、设置区低密度）。
- 卡片色带身份（heat-card 红 / market-degree-card 语义着色）。

## Exports
本系统直接以 CSS 自定义属性落在 `frontend/src/styles.css` 的 `:root` 块（app 为非 Tailwind 项目，无需其他导出格式）。
