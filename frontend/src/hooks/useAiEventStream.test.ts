import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AiEventStreamEvent, AiInsight } from "../aiTypes";
import { useAiEventStream } from "./useAiEventStream";

const openAiEventStream = vi.hoisted(() => vi.fn());

vi.mock("../aiApi", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  openAiEventStream
}));

const baseUrl = "http://127.0.0.1:9000";
const RECONNECT_DELAY_MS = 3_000;

const insight: AiInsight = {
  id: "i-1",
  created_at: "2026-09-26T10:00:00Z",
  level: "info",
  title: "盘面提示",
  digest: "两市成交额回落",
  source: "ai-insight",
  disclaimer: "AI 生成，不构成投资建议"
};

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** 常驻订阅没有终态可言：断线必须自己重连，卸载必须真停。 */
describe("useAiEventStream", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    openAiEventStream.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("connects once immediately and again after each stream ends", async () => {
    openAiEventStream.mockResolvedValue(undefined);
    renderHook(() => useAiEventStream({ baseUrl, enabled: true }));
    expect(openAiEventStream).toHaveBeenCalledTimes(1);

    await tick(RECONNECT_DELAY_MS);
    expect(openAiEventStream).toHaveBeenCalledTimes(2);

    await tick(RECONNECT_DELAY_MS);
    expect(openAiEventStream).toHaveBeenCalledTimes(3);
  });

  it("keeps reconnecting after a failure instead of dying", async () => {
    openAiEventStream.mockRejectedValue(new Error("stream idle timeout"));
    renderHook(() => useAiEventStream({ baseUrl, enabled: true }));

    await tick(RECONNECT_DELAY_MS * 3);
    // 第一次建立 + 两轮重连；空闲超时把常驻订阅误杀时不能永久失联。
    expect(openAiEventStream.mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("routes insight and data_fresh events to the handlers", async () => {
    openAiEventStream.mockImplementation(async (_url: string, onEvent: (event: AiEventStreamEvent) => void) => {
      onEvent({ type: "insight", insight });
      onEvent({ type: "data_fresh", module: "news" });
    });
    const onInsight = vi.fn();
    const onDataFresh = vi.fn();

    renderHook(() => useAiEventStream({ baseUrl, enabled: true, onInsight, onDataFresh }));

    expect(onInsight).toHaveBeenCalledWith(insight);
    expect(onDataFresh).toHaveBeenCalledWith("news");
  });

  it("aborts the live connection and stops reconnecting on unmount", async () => {
    openAiEventStream.mockImplementation(
      (_url: string, _onEvent: unknown, options: { signal?: AbortSignal }) =>
        new Promise<void>((_resolve, reject) => {
          options.signal?.addEventListener("abort", () => reject(new Error("stream request cancelled")), {
            once: true
          });
        })
    );
    const { unmount } = renderHook(() => useAiEventStream({ baseUrl, enabled: true }));
    const signal = openAiEventStream.mock.calls[0][2].signal as AbortSignal;

    unmount();

    expect(signal.aborted).toBe(true);
    await tick(RECONNECT_DELAY_MS * 3);
    expect(openAiEventStream).toHaveBeenCalledTimes(1);
  });

  it("does not connect while disabled", async () => {
    openAiEventStream.mockResolvedValue(undefined);
    renderHook(() => useAiEventStream({ baseUrl, enabled: false }));

    await tick(RECONNECT_DELAY_MS * 2);
    expect(openAiEventStream).not.toHaveBeenCalled();
  });
});
