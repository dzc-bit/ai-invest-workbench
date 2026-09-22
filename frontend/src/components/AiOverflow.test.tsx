/**
 * AI 抽屉横向溢出的回归守卫。
 *
 * 问题（1.5.2）：抽屉宽 480px、内容预算 ≈398px，而 styles.css 里有一条**无作用域**的
 * 全局规则 `table { min-width: 680px }`（原本服务数据中心宽表）。Markdown 渲染出的每个
 * GFM 表格都被它硬撑到 680px，逃出 `.ai-markdown` / `.ai-msg`（两者都是 overflow: visible），
 * 最终把 `.ai-messages`（只写 overflow-y，另一轴被规范推成 auto）变成 1.5~2.5 倍宽的横向
 * 滚动面：气泡边框切穿表格，用户得往右拖才能读完。
 *
 * jsdom 没有布局引擎，量不出真实像素，所以这里做两件可验证的事：
 * 1. 静态断言关键 CSS 约束存在（表格可收缩、pre/img 有上限、容器不产生横向滚动面）；
 * 2. 真实走一轮回答，断言表格/代码块确实到达 DOM（不能被"渲染期吞掉"来绕开问题）。
 */

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AiChatHandlers, AiChatRequest } from "../aiTypes";
import { AiAssistantPanel } from "./AiAssistantPanel";

vi.mock("../aiApi", () => ({
  loadAiStatus: vi.fn(),
  loadAiConfig: vi.fn(),
  saveAiConfig: vi.fn(),
  runAiChatStream: vi.fn(),
  openAiEventStream: vi.fn(),
  loadAiReports: vi.fn(),
  loadAiReportFile: vi.fn(),
  loadAiSessions: vi.fn(),
  loadAiSession: vi.fn()
}));

import { loadAiReports, loadAiSessions, loadAiStatus, runAiChatStream } from "../aiApi";

/** 从 cwd 起向上找到 frontend 源码目录（vitest 的 cwd 可能是仓库根，也可能是 frontend）。 */
function readSource(relative: string): string {
  const candidates = [resolve(relative), resolve("frontend", relative), resolve("src", relative)];
  for (const candidate of candidates) {
    if (existsSync(candidate)) {
      return readFileSync(candidate, "utf8");
    }
  }
  throw new Error(`找不到样式文件：${relative}（尝试过 ${candidates.join(", ")}）`);
}

const panelCss = readSource("src/ai-panel.css");
const globalCss = readSource("src/styles.css");

function normalize(css: string): string {
  // 先剥注释：注释里会出现 `table { min-width: 680px }` 这类字面量，会把
  // 朴素的"选择器 -> 规则体"扫描带偏。
  return css
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/\r\n/g, "\n")
    .replace(/[ \t]+/g, " ")
    .replace(/\s*\n\s*/g, "\n");
}

/**
 * 返回所有匹配 `selector` 的规则的声明体（按出现顺序拼接）。
 *
 * 同一选择器在文件里可以出现多次（增量覆盖），只取第一条会漏掉后面的覆盖声明，
 * 而"是否有某条覆盖"正是这些守卫要断言的东西。
 */
function declarationsFor(css: string, selector: string): string {
  const source = normalize(css);
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  const wanted = normalize(selector).trim();
  const found: string[] = [];
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source)) !== null) {
    const selectors = match[1]
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean);
    if (selectors.includes(wanted)) {
      found.push(match[2]);
    }
  }
  if (found.length === 0) {
    throw new Error(`未找到选择器 ${selector}`);
  }
  return found.join("\n");
}

const configuredStatus = {
  configured: true,
  base_url: "https://mock.local/v1",
  model: "demo-model",
  insights_enabled: true,
  tool_names: ["a"],
  knowledge_documents: 1,
  knowledge_chunks: 1,
  knowledge_ready: true
};

describe("AI 抽屉横向溢出（CSS 契约）", () => {
  it("全局 table 的 min-width:680px 必须被 .ai-markdown table 覆盖掉", () => {
    // 全局规则本身仍在：数据中心宽表依赖它，不能靠改它来修抽屉
    expect(declarationsFor(globalCss, "table")).toMatch(/min-width: 680px/);
    // 抽屉内表格必须显式归零 + 固定布局，否则命中 680px
    const markdownTable = declarationsFor(panelCss, ".ai-markdown table");
    expect(markdownTable).toMatch(/min-width: 0/);
    expect(markdownTable).toMatch(/table-layout: fixed/);
    expect(markdownTable).toMatch(/max-width: 100%/);
  });

  it("表格单元格允许在任意字符处换行且可收缩", () => {
    const cells = declarationsFor(panelCss, ".ai-markdown th");
    expect(cells).toMatch(/overflow-wrap: anywhere/);
    expect(cells).toMatch(/min-width: 0/);
    expect(declarationsFor(panelCss, ".ai-markdown td")).toMatch(/word-break: break-word/);
  });

  it("代码块必须有宽度上限与块内滚动（UA 默认 white-space:pre 让 overflow-wrap 失效）", () => {
    const pre = declarationsFor(panelCss, ".ai-markdown pre");
    expect(pre).toMatch(/max-width: 100%/);
    expect(pre).toMatch(/overflow-x: auto/);
  });

  it("图片必须有宽度上限（rehype-sanitize 默认放行 img）", () => {
    const img = declarationsFor(panelCss, ".ai-markdown img");
    expect(img).toMatch(/max-width: 100%/);
    expect(img).toMatch(/height: auto/);
  });

  it("消息区与快讯列表不得留下横向滚动面", () => {
    // 只写 overflow-y 时另一轴会被规范推成 auto → 隐蔽的横向滚动条
    expect(declarationsFor(panelCss, ".ai-messages")).toMatch(/overflow-x: hidden/);
    expect(declarationsFor(panelCss, ".ai-insights ul")).toMatch(/overflow-x: hidden/);
  });

  it("气泡与 markdown 容器必须可收缩（flex 子项默认 min-width:auto）", () => {
    const message = declarationsFor(panelCss, ".ai-msg");
    expect(message).toMatch(/min-width: 0/);
    const markdown = declarationsFor(panelCss, ".ai-markdown");
    expect(markdown).toMatch(/min-width: 0/);
    expect(markdown).toMatch(/max-width: 100%/);
  });

  it("工具步骤行不得被长工具名顶宽", () => {
    expect(declarationsFor(panelCss, ".ai-step-name")).toMatch(/flex: 0 1 auto/);
    expect(declarationsFor(panelCss, ".ai-steps li")).toMatch(/min-width: 0/);
  });

  it("快讯条目允许长 URL 换行", () => {
    expect(declarationsFor(panelCss, ".ai-insight span")).toMatch(/overflow-wrap: anywhere/);
  });
});

describe("AI 抽屉横向溢出（渲染结构）", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(loadAiStatus).mockResolvedValue(configuredStatus);
    vi.mocked(loadAiReports).mockResolvedValue({ items: [] });
    vi.mocked(loadAiSessions).mockResolvedValue({ items: [] });
  });

  it("表格与代码块真实进入 DOM，且不靠内联样式绕过约束", async () => {
    const user = userEvent.setup();
    const reply = [
      "| 指标 | 数值 |",
      "| --- | --- |",
      "| 总收益 | +12.4% |",
      "",
      "```sql",
      "SELECT symbol, close FROM daily_bars WHERE trade_date > TIMESTAMP '2026-01-01';",
      "```"
    ].join("\n");

    vi.mocked(runAiChatStream).mockImplementation(
      (_baseUrl: string, _request: AiChatRequest, handlers: AiChatHandlers = {}) => {
        handlers.onResult?.({
          type: "result",
          session_id: "s1",
          display: [{ role: "assistant", content: reply }]
        });
        return Promise.resolve();
      }
    );

    const { container } = render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    const composer = await screen.findByPlaceholderText(/帮我看看 600519/);
    await user.type(composer, "给我一张表");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => expect(container.querySelector(".ai-markdown table")).not.toBeNull());
    expect(container.querySelector(".ai-markdown pre")).not.toBeNull();

    // 约束必须来自 CSS：渲染层不得塞内联宽度/滚动样式来掩盖问题
    const table = container.querySelector(".ai-markdown table") as HTMLTableElement;
    expect(table.getAttribute("style")).toBeNull();
    const pre = container.querySelector(".ai-markdown pre") as HTMLPreElement;
    expect(pre.getAttribute("style")).toBeNull();
  });
});
