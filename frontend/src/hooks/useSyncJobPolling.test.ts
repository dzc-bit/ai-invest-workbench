import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { SyncJobStatus } from "../types";
import { isSyncRunningJob, useSyncJobPolling } from "./useSyncJobPolling";

const api = vi.hoisted(() => ({ loadSyncJob: vi.fn() }));

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...api
}));

const baseUrl = "http://127.0.0.1:9011";

function job(status: SyncJobStatus["status"], overrides: Partial<SyncJobStatus> = {}): SyncJobStatus {
  return {
    job_id: "job-1",
    mode: "full_market_bootstrap",
    status,
    total_symbols: 10,
    completed_symbols: 4,
    failed_symbols: 0,
    processed_symbols: 4,
    imported_rows: 0,
    start_date: "2026-06-01",
    end_date: "2026-06-05",
    errors: [],
    ...overrides
  };
}

beforeEach(() => {
  api.loadSyncJob.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("isSyncRunningJob", () => {
  it("treats running and cancelling as in flight, everything else as terminal", () => {
    expect(isSyncRunningJob(job("running"))).toBe(true);
    expect(isSyncRunningJob(job("cancelling"))).toBe(true);
    expect(isSyncRunningJob(job("cancelled"))).toBe(false);
    expect(isSyncRunningJob(job("completed"))).toBe(false);
    expect(isSyncRunningJob(job("completed_with_errors"))).toBe(false);
    expect(isSyncRunningJob(job("failed"))).toBe(false);
    expect(isSyncRunningJob(null)).toBe(false);
  });
});

describe("useSyncJobPolling", () => {
  it("does not poll without a running job", () => {
    const onSnapshot = vi.fn();
    renderHook(() =>
      useSyncJobPolling({ baseUrl, job: job("completed"), onSnapshot, onFailure: () => undefined })
    );
    expect(api.loadSyncJob).not.toHaveBeenCalled();
    expect(onSnapshot).not.toHaveBeenCalled();
  });

  it("polls an in-flight job and reports each snapshot", async () => {
    vi.useFakeTimers();
    api.loadSyncJob.mockResolvedValue({ job: job("running", { completed_symbols: 5 }) });
    const onSnapshot = vi.fn();
    renderHook(() => useSyncJobPolling({ baseUrl, job: job("running"), onSnapshot, onFailure: () => undefined }));

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_500);
    });

    expect(api.loadSyncJob).toHaveBeenCalledWith(baseUrl, "job-1");
    expect(onSnapshot.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(onSnapshot.mock.calls[0][0].completed_symbols).toBe(5);
  });

  it("fires onSettled exactly once, when a poll turns the job terminal", async () => {
    api.loadSyncJob
      .mockResolvedValueOnce({ job: job("running", { completed_symbols: 6 }) })
      .mockResolvedValue({ job: job("completed", { completed_symbols: 10 }) });
    const onSnapshot = vi.fn();
    const onSettled = vi.fn().mockResolvedValue(undefined);

    renderHook(() =>
      useSyncJobPolling({ baseUrl, job: job("running"), onSnapshot, onSettled, onFailure: () => undefined, pollMs: 5 })
    );

    await waitFor(() => expect(onSettled).toHaveBeenCalledTimes(1));
    expect(onSettled.mock.calls[0][0].status).toBe("completed");
    // 调用方还没 setState（job 仍是 running），但轮询已经收流：不会二次触发。
    const pollsAtSettlement = api.loadSyncJob.mock.calls.length;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 40));
    });
    expect(onSettled).toHaveBeenCalledTimes(1);
    expect(api.loadSyncJob.mock.calls.length).toBe(pollsAtSettlement);
  });

  it("reports poll failures without killing the polling loop", async () => {
    api.loadSyncJob.mockRejectedValueOnce(new Error("本地数据服务未连接")).mockResolvedValue({ job: job("running") });
    const onFailure = vi.fn();
    const onSnapshot = vi.fn();

    renderHook(() =>
      useSyncJobPolling({ baseUrl, job: job("running"), onSnapshot, onFailure, pollMs: 5 })
    );

    await waitFor(() => expect(onFailure).toHaveBeenCalledWith(expect.objectContaining({ message: "本地数据服务未连接" })));
    await waitFor(() => expect(onSnapshot).toHaveBeenCalled());
  });

  it("drops a late response after unmount instead of overwriting newer state", async () => {
    let resolveLate: ((value: { job: SyncJobStatus }) => void) | undefined;
    api.loadSyncJob.mockImplementation(() => new Promise((resolve) => {
      resolveLate = resolve;
    }));
    const onSnapshot = vi.fn();
    const { unmount } = renderHook(() =>
      useSyncJobPolling({ baseUrl, job: job("running"), onSnapshot, onFailure: () => undefined, pollMs: 5 })
    );

    await waitFor(() => expect(api.loadSyncJob).toHaveBeenCalled());
    unmount();
    resolveLate?.({ job: job("completed", { completed_symbols: 999 }) });

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 10));
    });
    expect(onSnapshot).not.toHaveBeenCalled();
  });
});
