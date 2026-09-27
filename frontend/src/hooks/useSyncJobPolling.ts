import { useEffect, useRef } from "react";
import { loadSyncJob } from "../api";
import type { SyncJobStatus } from "../types";

const SYNC_JOB_POLL_MS = 1_000;

/** 运行中/取消中都需要继续轮询；其余状态都是终态。 */
export function isSyncRunningJob(job: SyncJobStatus | null | undefined): boolean {
  return job?.status === "running" || job?.status === "cancelling";
}

type Options = {
  baseUrl: string | null | undefined;
  job: SyncJobStatus | null;
  onSnapshot: (job: SyncJobStatus) => void;
  /** 只在本次轮询把任务从在途变成终态时调用一次（衔接下一步补齐/刷新由调用方决定）。 */
  onSettled?: (job: SyncJobStatus) => void | Promise<void>;
  onFailure: (error: Error) => void;
  pollMs?: number;
};

/**
 * 数据同步任务的轮询生命周期：只负责"在途就轮、终态就停、卸载就断"。
 *
 * 独立存在的理由：轮询的正确性全靠 **不让迟到的响应覆盖新状态** ——旧实现把
 * interval、cancelled 标记、终态衔接（资金流结束后自动起全市场）和文案混在一个
 * effect 里，任何一处改动都要重推整个时序。这里回调经 ref 取最新闭包，effect
 * 只依赖 base_url/job_id/是否还在跑，组件的编排逻辑留在调用方。
 */
export function useSyncJobPolling(options: Options): void {
  const { baseUrl, job, pollMs = SYNC_JOB_POLL_MS } = options;
  const callbacksRef = useRef(options);
  callbacksRef.current = options;

  const jobId = job?.job_id;
  const running = isSyncRunningJob(job);

  useEffect(() => {
    if (!baseUrl || !jobId || !running) {
      return;
    }
    let cancelled = false;
    const timer = window.setInterval(() => {
      void loadSyncJob(baseUrl, jobId)
        .then(async (result) => {
          if (cancelled) {
            return;
          }
          callbacksRef.current.onSnapshot(result.job);
          if (!isSyncRunningJob(result.job)) {
            // 先到终态的那一轮负责收流：调用方 setState 之前也不能有第二次轮询，
            // 否则 onSettled 会重复触发（衔接全市场补齐会被起两遍）。
            window.clearInterval(timer);
            await callbacksRef.current.onSettled?.(result.job);
          }
        })
        .catch((error: Error) => {
          if (!cancelled) {
            callbacksRef.current.onFailure(error);
          }
        });
    }, pollMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [baseUrl, jobId, running, pollMs]);
}
