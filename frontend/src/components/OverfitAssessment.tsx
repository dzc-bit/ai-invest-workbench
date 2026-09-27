import { useEffect, useState } from "react";
import { ShieldQuestion } from "lucide-react";
import { aiOverfitCheck } from "../aiApi";
import type { AiOverfitResult } from "../aiTypes";
import type { BacktestMetrics, OptimizeCombination } from "../types";

/** Deterministic overfit check request. `combos` is only supplied when a
 * parameter grid actually ran — sending an empty list means "the grid produced
 * nothing comparable", which is a different statement from "there was no grid". */
export type OverfitRequest = {
  metrics?: BacktestMetrics;
  combos?: OptimizeCombination[];
  rejectedCombinations?: number;
};

const LEVEL_LABELS: Record<string, string> = {
  critical: "过拟合风险：高",
  warning: "过拟合风险：存在疑点",
  info: "过拟合检测：轻微提示",
  none: ""
};

/** Callers must pass a request with a stable identity (memoised on the result
 * object), otherwise every re-render starts a new check. */
export function useOverfitAssessment(
  aiBaseUrl: string | null | undefined,
  request: OverfitRequest | null
): AiOverfitResult | null {
  const [assessment, setAssessment] = useState<AiOverfitResult | null>(null);

  useEffect(() => {
    if (!aiBaseUrl || !request) {
      setAssessment(null);
      return;
    }
    let cancelled = false;
    const payload: {
      metrics: Record<string, unknown>;
      combos?: Array<Record<string, unknown>>;
      rejected_combinations?: number;
    } = { metrics: (request.metrics ?? {}) as unknown as Record<string, unknown> };
    if (request.combos) {
      payload.combos = request.combos as unknown as Array<Record<string, unknown>>;
      payload.rejected_combinations = request.rejectedCombinations ?? 0;
    }
    aiOverfitCheck(aiBaseUrl, payload)
      .then((next) => {
        if (!cancelled) {
          setAssessment(next);
        }
      })
      .catch(() => {
        // 检测失败不影响结果展示：宁可不显示，也不伪造一个"无风险"结论。
        if (!cancelled) {
          setAssessment(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [aiBaseUrl, request]);

  return assessment;
}

export function OverfitStrip({ assessment }: { assessment: AiOverfitResult | null }) {
  if (!assessment || assessment.level === "none" || assessment.findings.length === 0) {
    return null;
  }
  return (
    <div className={`risk-strip overfit-strip overfit-${assessment.level}`} role="status">
      <strong>
        <ShieldQuestion size={14} aria-hidden="true" /> {LEVEL_LABELS[assessment.level] ?? "过拟合检测"}
      </strong>
      <span>{assessment.findings.map((finding) => finding.message).join("；")}</span>
    </div>
  );
}
