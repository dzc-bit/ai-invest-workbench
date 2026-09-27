import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./tauriRuntime", () => ({
  isTauriRuntime: () => true
}));

const { loadAiStatus, aiOverfitCheck, aiInsightOneshot } = await import("./aiApi");

const baseUrl = "http://127.0.0.1:9000";

function jsonResponse(body: unknown, status = 200) {
  return new Response(typeof body === "string" ? body : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" }
  });
}

function hangingFetch() {
  const signals: Array<AbortSignal | undefined> = [];
  const fetchMock = vi.fn().mockImplementation(
    (_url: string, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        const signal = init?.signal as AbortSignal | undefined;
        signals.push(signal);
        signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), {
          once: true
        });
      })
  );
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, signals };
}

beforeEach(() => {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: {} });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
});

describe("AI JSON transport", () => {
  it("keeps the backend business code from a non-2xx response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ code: "ai_session_busy", message: "上一轮回答仍在生成中，请稍候再发送新消息。" }, 409)
      )
    );

    // 旧实现把稳定码改成了 request_failed + 固定文案，调用方无法按码分支。
    await expect(loadAiStatus(baseUrl)).rejects.toMatchObject({
      code: "ai_session_busy",
      message: "上一轮回答仍在生成中，请稍候再发送新消息。"
    });
  });

  it("falls back to http_error only when the error body is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse("<html>gateway down</html>", 502)));

    await expect(loadAiStatus(baseUrl)).rejects.toMatchObject({
      code: "http_error",
      message: expect.stringContaining("502")
    });
  });

  it("reports a malformed success body as payload_error", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse("{not json", 200)));

    await expect(aiOverfitCheck(baseUrl, { metrics: {} })).rejects.toMatchObject({ code: "payload_error" });
  });

  it("times out a stuck quick read and aborts the request", async () => {
    vi.useFakeTimers();
    const { signals } = hangingFetch();

    const settled = loadAiStatus(baseUrl).then(
      () => ({ ok: true as const, code: undefined as string | undefined }),
      (error: Error & { code?: string }) => ({ ok: false as const, code: error.code })
    );

    await vi.advanceTimersByTimeAsync(19_000);
    expect(signals[0]?.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(1_500);
    await expect(settled).resolves.toEqual({ ok: false, code: "timeout" });
    expect(signals[0]?.aborted).toBe(true);
  });

  it("gives model-backed requests a longer budget than quick reads", async () => {
    vi.useFakeTimers();
    const { signals } = hangingFetch();

    const settle = (request: Promise<unknown>) =>
      request.then(
        () => ({ ok: true as const, code: undefined as string | undefined }),
        (error: Error & { code?: string }) => ({ ok: false as const, code: error.code })
      );
    // 处理器必须在推进定时器之前挂上，否则先 reject 的那条会被判成未捕获拒绝。
    const quickSettled = settle(loadAiStatus(baseUrl));
    const modelSettled = settle(aiInsightOneshot(baseUrl, "results_overview", {}));
    const quickSignal = signals[0];
    const modelSignal = signals[1];

    await vi.advanceTimersByTimeAsync(30_000);
    await expect(quickSettled).resolves.toEqual({ ok: false, code: "timeout" });
    expect(quickSignal?.aborted).toBe(true);
    // 一次性点评要等模型：后端自身 120 秒才超时，前端不能先到点。
    expect(modelSignal?.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(120_000);
    await expect(modelSettled).resolves.toEqual({ ok: false, code: "timeout" });
    expect(modelSignal?.aborted).toBe(true);
  });

  it("reports a caller cancellation separately from a timeout", async () => {
    const controller = new AbortController();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(
        (_url: string, init?: RequestInit) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), {
              once: true
            });
          })
      )
    );

    const request = aiOverfitCheck(baseUrl, { metrics: {} }, { signal: controller.signal });
    controller.abort();

    await expect(request).rejects.toMatchObject({ code: "cancelled" });
  });
});
