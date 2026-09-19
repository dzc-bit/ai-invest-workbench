import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AiChatHandlers, AiChatRequest, AiResultEvent } from "../aiTypes";
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
  loadAiSession: vi.fn(),
  deleteAiSession: vi.fn()
}));

import {
  deleteAiSession,
  loadAiReportFile,
  loadAiReports,
  loadAiSession,
  loadAiSessions,
  loadAiStatus,
  runAiChatStream
} from "../aiApi";

const mockedLoadStatus = vi.mocked(loadAiStatus);
const mockedRunChat = vi.mocked(runAiChatStream);
const mockedLoadReports = vi.mocked(loadAiReports);
const mockedLoadReportFile = vi.mocked(loadAiReportFile);
const mockedLoadSessions = vi.mocked(loadAiSessions);
const mockedLoadSession = vi.mocked(loadAiSession);
const mockedDeleteSession = vi.mocked(deleteAiSession);

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
  mockedDeleteSession.mockResolvedValue(true);
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
      expect(await screen.findByText("定时报告（1）")).toBeTruthy();
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
    expect(screen.getByText("历史对话（1）")).toBeTruthy();
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
    await user.click(screen.getByRole("button", { name: /^会话 B/ }));
    await waitFor(() => expect(screen.getByText("s-b 的回答")).toBeTruthy());
    await user.click(screen.getByRole("button", { name: "新建对话" }));
    expect(screen.queryByText(/的回答/)).toBeNull();
    expect(screen.getByText("问行情、评个股、写策略、解读回测")).toBeTruthy();
  });

  it("deletes a stored conversation and resets the transcript with it", async () => {
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
    await screen.findByText("s-a 的回答");
    await user.click(screen.getByRole("button", { name: "删除会话 会话 A" }));
    await waitFor(() => expect(mockedDeleteSession).toHaveBeenCalledWith("http://x", "s-a"));
    expect(screen.queryByText("历史对话（1）")).toBeNull();
    expect(screen.queryByText("s-a 的回答")).toBeNull();
  });
});
