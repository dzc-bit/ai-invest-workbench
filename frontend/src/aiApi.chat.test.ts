import { describe, expect, it, vi } from "vitest";

// 只 mock 传输层：终态判定必须由真实的 aiApi 分发逻辑做出。
const consumeNdjsonStream = vi.hoisted(() => vi.fn());

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  consumeNdjsonStream
}));

vi.mock("./tauriRuntime", () => ({
  isTauriRuntime: () => true
}));

const { runAiChatStream } = await import("./aiApi");

const baseUrl = "http://127.0.0.1:9000";

function lines(...events: Array<Record<string, unknown>>) {
  consumeNdjsonStream.mockImplementationOnce(
    async (_url: string, _init: unknown, _options: unknown, _timeout: number, onLine: (line: string) => void) => {
      for (const event of events) {
        onLine(JSON.stringify(event));
      }
    }
  );
}

describe("runAiChatStream completion contract", () => {
  it("reports interruption after tokens when no result event arrives", async () => {
    // 上一版在此静默成功：只收到 token 也算回答完成，部分内容被 UI 直接丢弃。
    lines(
      { type: "session", session_id: "s-1" },
      { type: "phase", phase: "思考中（第 1/8 步）" },
      { type: "token", text: "盘面先看" },
      { type: "token", text: "量能变化" },
      { type: "heartbeat", session_id: "s-1" }
    );
    const tokens: string[] = [];

    await expect(
      runAiChatStream(baseUrl, { message: "今天盘面怎么看" }, { onToken: (text) => tokens.push(text) })
    ).rejects.toMatchObject({ code: "stream_incomplete" });

    expect(tokens).toEqual(["盘面先看", "量能变化"]);
  });

  it("resolves once the result event has been unwrapped", async () => {
    const display = [{ role: "assistant", content: "结论" }];
    lines(
      { type: "token", text: "结论" },
      { type: "result", session_id: "s-1", display, updated_at: "2026-09-26T10:00:00Z" }
    );
    const results: unknown[] = [];

    await expect(
      runAiChatStream(baseUrl, { message: "hi" }, { onResult: (event) => results.push(event) })
    ).resolves.toBeUndefined();
    expect(results[0]).toMatchObject({ session_id: "s-1", display });
  });

  it("keeps the backend stable code carried by an error event", async () => {
    lines({ type: "error", code: "ai_upstream_error", message: "模型服务调用失败：上下文超长" });

    await expect(runAiChatStream(baseUrl, { message: "hi" })).rejects.toMatchObject({
      code: "ai_upstream_error"
    });
  });

  it("passes a caller abort through as a cancellation, not a success", async () => {
    consumeNdjsonStream.mockImplementationOnce(
      async (_url: string, _init: unknown, options: { signal?: AbortSignal }, _timeout: number) => {
        await new Promise<void>((_resolve, reject) => {
          options.signal?.addEventListener("abort", () => reject(new Error("stream request cancelled")), {
            once: true
          });
        });
      }
    );
    const controller = new AbortController();
    const request = runAiChatStream(baseUrl, { message: "hi" }, {}, { signal: controller.signal });
    controller.abort();

    await expect(request).rejects.toThrow("stream request cancelled");
  });
});
