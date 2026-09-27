import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AiChatHandlers, AiChatRequest, AiResultEvent } from "../aiTypes";
import { BackendError } from "../api";
import { AiAssistantPanel } from "./AiAssistantPanel";

vi.mock("../aiApi", () => ({
  loadAiStatus: vi.fn(),
  loadAiConfig: vi.fn(),
  saveAiConfig: vi.fn(),
  runAiChatStream: vi.fn(),
  cancelAiChat: vi.fn(),
  openAiEventStream: vi.fn(),
  loadAiReports: vi.fn(),
  loadAiReportFile: vi.fn(),
  loadAiSessions: vi.fn(),
  loadAiSession: vi.fn()
}));

import { loadAiReportFile, loadAiReports, loadAiSession, loadAiSessions, loadAiStatus, cancelAiChat, runAiChatStream } from "../aiApi";

const mockedCancelChat = vi.mocked(cancelAiChat);

const mockedLoadStatus = vi.mocked(loadAiStatus);
const mockedRunChat = vi.mocked(runAiChatStream);
const mockedLoadReports = vi.mocked(loadAiReports);
const mockedLoadReportFile = vi.mocked(loadAiReportFile);
const mockedLoadSessions = vi.mocked(loadAiSessions);
const mockedLoadSession = vi.mocked(loadAiSession);

const configuredStatus = {
  configured: true,
  base_url: "https://mock.local/v1",
  model: "demo-model",
  insights_enabled: true,
  tool_names: ["a", "b"],
  knowledge_documents: 3,
  knowledge_chunks: 12,
  knowledge_ready: true
};

function scriptChatStream(reply: string, resultEvent: Partial<AiResultEvent> = {}) {
  mockedRunChat.mockImplementation(
    (_baseUrl: string, _request: AiChatRequest, handlers: AiChatHandlers = {}) => {
      handlers.onPhase?.("思考中");
      handlers.onToolCall?.({ type: "tool_call", id: "t1", name: "realtime_market_snapshot", args: {} });
      handlers.onToolResult?.({
        type: "tool_result",
        id: "t1",
        name: "realtime_market_snapshot",
        ok: true,
        summary: "上证指数 3100",
        duration_ms: 320
      });
      handlers.onToken?.(reply.slice(0, 4));
      handlers.onToken?.(reply.slice(4));
      handlers.onResult?.({
        type: "result",
        session_id: "s1",
        display: [
          { role: "user", content: "行情如何" },
          {
            role: "assistant",
            content: reply,
            tool_steps: [{ id: "t1", name: "realtime_market_snapshot", ok: true, summary: "上证指数 3100", duration_ms: 320 }]
          }
        ],
        ...resultEvent
      });
      return Promise.resolve();
    }
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockedLoadStatus.mockResolvedValue(configuredStatus);
  mockedLoadReports.mockResolvedValue({ items: [] });
  mockedLoadSessions.mockResolvedValue({ items: [] });
});

describe("AiAssistantPanel", () => {
  it("renders nothing when closed", () => {
    const { container } = render(
      <AiAssistantPanel open={false} baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("lists scheduled reports and downloads one as a file", async () => {
    const user = userEvent.setup();
    mockedLoadReports.mockResolvedValue({
      items: [{ name: "复盘报告-20260913-1530.md", size: 42, created_at: "2026-09-13T07:30:00Z" }]
    });
    mockedLoadReportFile.mockResolvedValue("# 收盘复盘正文");
    const anchorClick = vi.fn();
    const originalCreate = document.createElement.bind(document);
    vi.stubGlobal(
      "URL",
      Object.assign(URL, {
        createObjectURL: vi.fn(() => "blob:mock"),
        revokeObjectURL: vi.fn()
      })
    );
    vi.spyOn(document, "createElement").mockImplementation((tag: string, options) => {
      const node = originalCreate(tag, options);
      if (tag === "a") {
        node.click = anchorClick;
      }
      return node;
    });
    try {
      render(
        <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
      );
      // 报告列表已移到「报告」切换面板：默认视图是对话，切过去才渲染。
      await user.click(await screen.findByRole("tab", { name: /报告/ }));
      expect(await screen.findByText("复盘报告-20260913-1530")).toBeTruthy();
      await user.click(screen.getByRole("button", { name: /下载/ }));
      await waitFor(() => expect(mockedLoadReportFile).toHaveBeenCalledWith("http://x", "复盘报告-20260913-1530.md"));
      expect(anchorClick).toHaveBeenCalled();
    } finally {
      vi.restoreAllMocks();
    }
  });

  it("shows the unconfigured hint with a settings entry", async () => {
    mockedLoadStatus.mockResolvedValue({ ...configuredStatus, configured: false });
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    expect(await screen.findByText("AI 服务尚未配置")).toBeTruthy();
    expect(screen.getByRole("button", { name: "前往设置" })).toBeTruthy();
  });

  it("streams tool steps and markdown reply into the transcript", async () => {
    const user = userEvent.setup();
    scriptChatStream("市场偏暖，红盘占比 62%。仅供辅助观察，不构成投资建议。");
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    const composer = await screen.findByPlaceholderText(/帮我看看 600519/);
    await user.type(composer, "行情如何");
    await user.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      expect(screen.getByText(/市场偏暖，红盘占比 62%/)).toBeTruthy();
    });
    expect(screen.getByText(/已调用 1 个工具/)).toBeTruthy();
    expect(mockedRunChat).toHaveBeenCalledWith(
      "http://x",
      expect.objectContaining({ message: "行情如何" }),
      expect.anything(),
      expect.objectContaining({ signal: expect.anything() })
    );
  });

  it("keeps the partial answer and marks it unfinished when the stream never reaches a result", async () => {
    const user = userEvent.setup();
    // 传输层现在会因缺少终态而报中断；残缺回答必须留在记录里并标明未完成。
    mockedRunChat.mockImplementation(async (_baseUrl: string, _request: AiChatRequest, handlers: AiChatHandlers = {}) => {
      handlers.onToken?.("盘面先看");
      handlers.onToken?.("量能变化");
      throw new BackendError("stream_incomplete", "AI 回答没有返回最终结果，连接提前结束（已收到的内容不会被丢弃）。");
    });
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    const composer = await screen.findByPlaceholderText(/帮我看看 600519/);
    await user.type(composer, "行情如何");
    await user.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByText(/盘面先看量能变化/)).toBeTruthy();
    expect(screen.getByText(/回答中断，可重试/)).toBeTruthy();
    expect(screen.getByText(/连接提前结束/)).toBeTruthy();
  });

  it("asks the backend to stop and keeps the reader attached until the turn ends", async () => {
    let finish: () => void = () => undefined;
    let signal: AbortSignal | undefined;
    mockedRunChat.mockImplementation((_baseUrl, _request, handlers = {}, options = {}) => {
      signal = options.signal ?? undefined;
      handlers.onSession?.({ type: "session", session_id: "s-stop", title: "会话" });
      handlers.onToken?.("已经写出来的部分");
      return new Promise<void>((resolve) => {
        finish = () => {
          handlers.onResult?.({ type: "result", session_id: "s-stop", display: [{ role: "assistant", content: "已经写出来的部分（已停止）" }] });
          resolve();
        };
      });
    });
    mockedCancelChat.mockResolvedValue({ ok: true, cancelling: true });

    const user = userEvent.setup();
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    const composer = await screen.findByPlaceholderText(/帮我看看 600519/);
    await user.type(composer, "长任务");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(screen.getByText(/已经写出来的部分/)).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: "停止生成" }));

    await waitFor(() => expect(mockedCancelChat).toHaveBeenCalledWith("http://x", "s-stop"));
    expect(screen.getByRole("button", { name: "正在停止" })).toBeDisabled();
    // 关键：没有断开接收，worker 的终态 result 才能带着保存好的历史回来。
    expect(signal?.aborted).toBe(false);

    await act(async () => {
      finish();
    });
    await waitFor(() => expect(screen.queryByRole("button", { name: "正在停止" })).not.toBeInTheDocument());
    expect(screen.getByText(/已经写出来的部分/)).toBeInTheDocument();
  });

  it("releases the reader when the backend reports nothing left to stop", async () => {
    let signal: AbortSignal | undefined;
    mockedRunChat.mockImplementation((_baseUrl, _request, handlers = {}, options = {}) => {
      signal = options.signal ?? undefined;
      handlers.onSession?.({ type: "session", session_id: "s-done", title: "会话" });
      // 真实传输在 abort 后会 reject，面板靠 finally 复位流式状态。
      return new Promise<void>((_resolve, reject) => {
        signal?.addEventListener("abort", () => reject(new Error("stream request cancelled")), { once: true });
      });
    });
    // 后台说"没有在途轮次"（多半刚好结束）：这时不能一直挂着接收。
    mockedCancelChat.mockResolvedValue({ ok: true, cancelling: false });

    const user = userEvent.setup();
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    const composer = await screen.findByPlaceholderText(/帮我看看 600519/);
    await user.type(composer, "长任务");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(mockedRunChat).toHaveBeenCalled());

    await user.click(screen.getByRole("button", { name: "停止生成" }));

    await waitFor(() => expect(mockedCancelChat).toHaveBeenCalledWith("http://x", "s-done"));
    await waitFor(() => expect(signal?.aborted).toBe(true));
    // 收流后面板回到可发送状态，不再挂着"正在停止"。
    await waitFor(() => expect(screen.queryByRole("button", { name: /停止/ })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "发送" })).toBeInTheDocument();
  });

  it("offers strategy application when the result carries a strategy artifact", async () => {
    const user = userEvent.setup();
    const onApplyStrategy = vi.fn();
    const onClose = vi.fn();
    scriptChatStream("策略已生成。", {
      strategy: {
        name: "AI 生成策略",
        market_filters: [],
        entry_groups: [],
        exit_rules: [],
        score_threshold: null
      }
    });
    render(
      <AiAssistantPanel
        open
        baseUrl="http://x"
        insights={[]}
        task={null}
        onTaskConsumed={() => undefined}
        onClose={onClose}
        onApplyStrategy={onApplyStrategy}
      />
    );
    const composer = await screen.findByPlaceholderText(/帮我看看 600519/);
    await user.type(composer, "生成策略");
    await user.click(screen.getByRole("button", { name: "发送" }));
    const applyButton = await screen.findByRole("button", { name: "应用到策略工作台" });
    await user.click(applyButton);
    expect(onApplyStrategy).toHaveBeenCalledWith(expect.objectContaining({ name: "AI 生成策略" }));
    expect(onClose).toHaveBeenCalled();
  });

  it("maps stream failures to a friendly error banner", async () => {
    const user = userEvent.setup();
    mockedRunChat.mockRejectedValue(new Error("模型服务调用失败（APIConnectionError）：boom"));
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    const composer = await screen.findByPlaceholderText(/帮我看看 600519/);
    await user.type(composer, "行情如何");
    await user.click(screen.getByRole("button", { name: "发送" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("模型服务调用失败");
  });

  it("clears the composer once the message is sent", async () => {
    const user = userEvent.setup();
    scriptChatStream("市场偏暖。");
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    const composer = await screen.findByPlaceholderText(/帮我看看 600519/);
    await user.type(composer, "行情如何");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(screen.getByText(/市场偏暖/)).toBeTruthy());
    expect(composer).toHaveValue("");
    expect(screen.getByRole("button", { name: "发送" })).toBeDisabled();
  });

  it("sends each message exactly once when Enter is pressed twice", async () => {
    const user = userEvent.setup();
    scriptChatStream("市场偏暖。");
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    const composer = await screen.findByPlaceholderText(/帮我看看 600519/);
    await user.type(composer, "行情如何{Enter}");
    await waitFor(() => expect(composer).toHaveValue(""));
    await user.type(composer, "{Enter}");
    await waitFor(() => expect(screen.getByText(/市场偏暖/)).toBeTruthy());
    expect(mockedRunChat).toHaveBeenCalledTimes(1);
  });

  it("restores the latest stored conversation when the drawer opens", async () => {
    mockedLoadSessions.mockResolvedValue({
      items: [{ session_id: "s-1", title: "600519 诊断", updated_at: "2026-09-18T10:00:00Z", message_count: 2 }]
    });
    mockedLoadSession.mockResolvedValue({
      session_id: "s-1",
      title: "600519 诊断",
      display: [
        { role: "user", content: "帮我看看 600519" },
        { role: "assistant", content: "茅台近 30 日 +5.2%。" }
      ]
    });
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    expect(await screen.findByText(/茅台近 30 日/)).toBeTruthy();
    expect(mockedLoadSession).toHaveBeenCalledWith("http://x", "s-1");
    // 会话数徽标在切换条上，不再是常驻的折叠块标题。
    expect(await screen.findByRole("tab", { name: /历史（1）/ })).toBeTruthy();
  });

  it("keeps follow-ups inside the restored session so the agent keeps its history", async () => {
    const user = userEvent.setup();
    mockedLoadSessions.mockResolvedValue({
      items: [{ session_id: "s-1", title: "600519 诊断", updated_at: null, message_count: 2 }]
    });
    mockedLoadSession.mockResolvedValue({
      session_id: "s-1",
      title: "600519 诊断",
      display: [
        { role: "user", content: "帮我看看 600519" },
        { role: "assistant", content: "茅台近 30 日 +5.2%。" }
      ]
    });
    scriptChatStream("资金面以主力净流入为主。");
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    const composer = await screen.findByPlaceholderText(/帮我看看 600519/);
    await user.type(composer, "再看下资金面{Enter}");
    await waitFor(() => expect(mockedRunChat).toHaveBeenCalled());
    expect(mockedRunChat.mock.calls[0][1]).toEqual(
      expect.objectContaining({ message: "再看下资金面", session_id: "s-1" })
    );
  });

  it("switches between stored conversations and starts a fresh one", async () => {
    const user = userEvent.setup();
    mockedLoadSessions.mockResolvedValue({
      items: [
        { session_id: "s-a", title: "会话 A", updated_at: null, message_count: 2 },
        { session_id: "s-b", title: "会话 B", updated_at: null, message_count: 2 }
      ]
    });
    mockedLoadSession.mockImplementation(async (_baseUrl: string, sessionId: string) => ({
      session_id: sessionId,
      title: sessionId === "s-a" ? "会话 A" : "会话 B",
      display: [{ role: "assistant", content: `${sessionId} 的回答` }]
    }));
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    expect(await screen.findByText("s-a 的回答")).toBeTruthy();
    // 会话列表现在在「历史」面板里，且点选后会自动切回对话视图。
    await user.click(await screen.findByRole("tab", { name: /历史/ }));
    await user.click(await screen.findByRole("button", { name: /^会话 B/ }));
    await waitFor(() => expect(screen.getByText("s-b 的回答")).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "新建对话" }));
    expect(screen.queryByText(/的回答/)).toBeNull();
    expect(screen.getByText("问行情、评个股、写策略、解读回测")).toBeTruthy();
  });

  it("lists stored conversations without a per-row delete control", async () => {
    // 会话回收改由后端 SessionStore.prune 动态自动完成；抽屉里不应再有删除按钮
    // 把列表挤成一团，只保留"点标题切换会话"。
    const user = userEvent.setup();
    mockedLoadSessions.mockResolvedValue({
      items: [{ session_id: "s-a", title: "会话 A", updated_at: null, message_count: 2 }]
    });
    mockedLoadSession.mockResolvedValue({
      session_id: "s-a",
      title: "会话 A",
      display: [{ role: "assistant", content: "s-a 的回答" }]
    });
    render(
      <AiAssistantPanel open baseUrl="http://x" insights={[]} task={null} onTaskConsumed={() => undefined} onClose={() => undefined} />
    );
    expect(await screen.findByText("s-a 的回答")).toBeTruthy();
    await user.click(await screen.findByRole("tab", { name: /历史/ }));
    expect(screen.queryByRole("button", { name: /删除会话/ })).toBeNull();
    // 切换会话的能力必须保留
    await user.click(await screen.findByRole("button", { name: /^会话 A/ }));
    expect(mockedLoadSession).toHaveBeenCalledWith("http://x", "s-a");
  });

  it("keeps the composer height out of the non-chat panels", async () => {
    // 方案 A 的核心收益：历史/快讯/报告不再常驻堆叠，切走对话时它们不占消息区高度。
    const user = userEvent.setup();
    render(
      <AiAssistantPanel
        open
        baseUrl="http://x"
        insights={[
          {
            id: "i1",
            created_at: "2026-09-22T00:00:00Z",
            level: "info",
            title: "快讯",
            digest: "红盘占比回升",
            source: "ai-insight",
            disclaimer: "AI 生成内容，仅供辅助观察，不构成投资建议"
          }
        ]}
        task={null}
        onTaskConsumed={() => undefined}
        onClose={() => undefined}
      />
    );
    // 默认停在对话视图：消息区与输入区在，历史列表不在。
    expect(await screen.findByPlaceholderText(/帮我看看 600519/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^会话/ })).toBeNull();

    await user.click(await screen.findByRole("tab", { name: /快讯/ }));
    // 切到快讯后：面板出现，消息区与输入区让位（同一槽位只渲染一个面板）。
    expect(await screen.findByText("红盘占比回升")).toBeTruthy();
    expect(screen.queryByPlaceholderText(/帮我看看 600519/)).toBeNull();

    await user.click(screen.getByRole("tab", { name: /对话/ }));
    expect(await screen.findByPlaceholderText(/帮我看看 600519/)).toBeTruthy();
  });
});
