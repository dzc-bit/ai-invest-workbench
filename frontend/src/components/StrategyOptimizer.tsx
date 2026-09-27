import { useMemo, useState } from "react";
import { SlidersHorizontal } from "lucide-react";
import { runAiOptimizeStream } from "../aiApi";
import { translateAiError } from "../aiTypes";
import type {
  BacktestSettingsConfig,
  OptimizeCombination,
  OptimizeGridKey,
  OptimizeSummary,
  StrategyConfig
} from "../types";
import { OverfitStrip, useOverfitAssessment } from "./OverfitAssessment";

type Props = {
  strategy: StrategyConfig;
  settings: BacktestSettingsConfig;
  baseUrl: string | null;
  disabled?: boolean;
};

type OptimizeParamRow = { key: OptimizeGridKey; valuesText: string; percentScale?: boolean };

const OPTIMIZE_PARAM_LABELS: Record<OptimizeGridKey, string> = {
  fixed_holding_days: "固定持仓天数",
  max_positions: "最大持仓数",
  max_daily_buys: "每日最多买入",
  position_size_pct: "个股仓位上限（%）",
  take_profit_pct: "止盈比例（%）",
  stop_loss_pct: "止损比例（%）",
  min_listing_days: "最少上市天数"
};

const PERCENT_SCALED_KEYS: OptimizeGridKey[] = ["position_size_pct", "take_profit_pct", "stop_loss_pct"];

const MAX_OPTIMIZE_COMBINATIONS = 48;

function formatPercentCell(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}

function formatParamValue(key: OptimizeGridKey, value: number): string {
  if (PERCENT_SCALED_KEYS.includes(key)) {
    return `${Number((value * 100).toFixed(2))}%`;
  }
  return `${value}`;
}

/** Split a "3, 5, 8" cell into numbers, keeping the rejected tokens so the
 * caller can name them instead of quietly shrinking the grid. An empty cell is
 * an error, never an implicit 0. */
function parseValues(text: string): { values: number[]; invalid: string[] } {
  const values: number[] = [];
  const invalid: string[] = [];
  for (const token of text.split(/[,\s，、]+/)) {
    const trimmed = token.trim();
    if (!trimmed) {
      continue;
    }
    const parsed = Number(trimmed);
    if (Number.isFinite(parsed)) {
      values.push(parsed);
    } else {
      invalid.push(trimmed);
    }
  }
  return { values, invalid };
}

export function StrategyOptimizer({ strategy, settings, baseUrl, disabled = false }: Props) {
  const [rows, setRows] = useState<OptimizeParamRow[]>([
    { key: "fixed_holding_days", valuesText: "3,5,8" },
    { key: "take_profit_pct", valuesText: "5,8,12", percentScale: true }
  ]);
  const [isRunning, setIsRunning] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [combinations, setCombinations] = useState<OptimizeCombination[]>([]);
  const [summary, setSummary] = useState<OptimizeSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  const overfitRequest = useMemo(() => {
    if (!summary) {
      return null;
    }
    return {
      metrics: summary.best?.metrics,
      combos: summary.combinations,
      rejectedCombinations: summary.failures.length
    };
  }, [summary]);
  const overfit = useOverfitAssessment(baseUrl, overfitRequest);

  const buildGrid = (): Record<string, number[]> | null => {
    const grid: Record<string, number[]> = {};
    for (const row of rows) {
      const label = OPTIMIZE_PARAM_LABELS[row.key];
      const { values, invalid } = parseValues(row.valuesText);
      if (invalid.length > 0) {
        setError(`参数“${label}”的候选值里有不是数字的项：${invalid.join("、")}。`);
        return null;
      }
      if (values.length === 0) {
        setError(`参数“${label}”的候选值为空，请填写逗号分隔的数字。`);
        return null;
      }
      if (grid[row.key]) {
        setError(`参数“${label}”重复登记了多行，请合并为一行。`);
        return null;
      }
      grid[row.key] = values.map((value) => (row.percentScale ? value / 100 : value));
    }
    const total = Object.values(grid).reduce((acc, values) => acc * values.length, 1);
    if (total > MAX_OPTIMIZE_COMBINATIONS) {
      setError(`网格组合数 ${total} 超过上限 ${MAX_OPTIMIZE_COMBINATIONS}，请减少候选值。`);
      return null;
    }
    return grid;
  };

  const handleRun = async () => {
    if (!baseUrl || isRunning || disabled) {
      return;
    }
    const grid = buildGrid();
    if (!grid) {
      return;
    }
    setIsRunning(true);
    setError(null);
    setCombinations([]);
    setSummary(null);
    setProgress("正在准备历史数据");
    try {
      await runAiOptimizeStream(baseUrl, { strategy, settings, grid }, {
        onPhase: (phase) => setProgress(phase),
        onProgress: (event) =>
          setProgress(`正在寻优 ${event.completed}/${event.total} 个参数组合`),
        onCombination: (combination) => {
          setCombinations((current) => [...current, combination]);
        },
        onResult: (result) => setSummary(result)
      });
    } catch (caught) {
      setError(translateAiError(caught));
    } finally {
      setIsRunning(false);
      setProgress(null);
    }
  };

  const bestIndex = summary?.best?.index ?? null;

  return (
    <div className="optimizer-panel" aria-label="AI 参数寻优">
      <div className="optimizer-head">
        <h3>
          <SlidersHorizontal size={15} aria-hidden="true" />
          AI 参数寻优
        </h3>
        <span className="status-pill compact">最多 {MAX_OPTIMIZE_COMBINATIONS} 个组合</span>
      </div>
      <p className="optimizer-hint">对当前策略的数值参数做网格回测对比，条件本身保持不变。</p>
      {rows.map((row, index) => (
        <div className="optimizer-row" key={`${row.key}-${index}`}>
          <select
            aria-label={`寻优参数 ${index + 1}`}
            value={row.key}
            disabled={isRunning || disabled}
            onChange={(event) => {
              const key = event.target.value as OptimizeGridKey;
              const percentScale = PERCENT_SCALED_KEYS.includes(key);
              setRows((current) =>
                current.map((item, position) =>
                  position === index ? { ...item, key, percentScale } : item
                )
              );
            }}
          >
            {(Object.keys(OPTIMIZE_PARAM_LABELS) as OptimizeGridKey[]).map((key) => (
              <option key={key} value={key}>
                {OPTIMIZE_PARAM_LABELS[key]}
              </option>
            ))}
          </select>
          <input
            aria-label={`参数候选值 ${index + 1}`}
            value={row.valuesText}
            disabled={isRunning || disabled}
            placeholder={row.percentScale ? "5,8,12" : "3,5,8"}
            onChange={(event) =>
              setRows((current) =>
                current.map((item, position) =>
                  position === index ? { ...item, valuesText: event.target.value } : item
                )
              )
            }
          />
        </div>
      ))}
      <div className="inline-actions">
        {rows.length < 4 ? (
          <button
            className="secondary-button"
            type="button"
            disabled={isRunning || disabled}
            onClick={() =>
              setRows((current) => {
                const used = new Set(current.map((item) => item.key));
                const key =
                  (Object.keys(OPTIMIZE_PARAM_LABELS) as OptimizeGridKey[]).find((item) => !used.has(item)) ??
                  "max_positions";
                return [...current, { key, valuesText: key === "position_size_pct" ? "10,20" : "3,5", percentScale: PERCENT_SCALED_KEYS.includes(key) }];
              })
            }
          >
            添加参数
          </button>
        ) : null}
        <button
          className="primary-button"
          type="button"
          aria-label="开始 AI 参数寻优"
          disabled={!baseUrl || isRunning || disabled}
          onClick={handleRun}
        >
          {isRunning ? "寻优运行中" : "开始 AI 参数寻优"}
        </button>
        {!baseUrl ? <span className="muted-code">连接本地服务后可用</span> : null}
      </div>
      {progress ? (
        <p className="optimizer-progress" role="status">
          {progress}
        </p>
      ) : null}
      {error ? (
        <div className="condition-validation bad" role="alert">
          {error}
        </div>
      ) : null}
      {combinations.length > 0 ? (
        <div className="table-wrap optimizer-table">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>参数组合</th>
                <th>总收益</th>
                <th>最大回撤</th>
                <th>胜率</th>
                <th>交易次数</th>
              </tr>
            </thead>
            <tbody>
              {combinations.map((combination) => (
                <tr key={combination.index} className={combination.index === bestIndex ? "best-row" : ""}>
                  <td>{combination.index}</td>
                  <td>
                    {Object.entries(combination.params)
                      .map(([key, value]) => `${OPTIMIZE_PARAM_LABELS[key as OptimizeGridKey]} ${formatParamValue(key as OptimizeGridKey, value)}`)
                      .join(" / ")}
                  </td>
                  <td className={combination.metrics.total_return_pct >= 0 ? "up-text" : "down-text"}>
                    {formatPercentCell(combination.metrics.total_return_pct)}
                  </td>
                  <td>{formatPercentCell(combination.metrics.max_drawdown_pct)}</td>
                  <td>{formatPercentCell(combination.metrics.win_rate_pct)}</td>
                  <td>{combination.metrics.trade_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {summary && summary.failures.length > 0 ? (
        <div className="optimizer-failures" role="status">
          {summary.failures.map((failure, failureIndex) => (
            <p className="condition-validation bad" key={`${failureIndex}-${failure.error}`}>
              已拒绝的组合 {Object.entries(failure.params)
                .map(([key, value]) => `${OPTIMIZE_PARAM_LABELS[key as OptimizeGridKey] ?? key} ${formatParamValue(key as OptimizeGridKey, value)}`)
                .join(" / ")}
              ：{failure.error}
            </p>
          ))}
        </div>
      ) : null}
      <OverfitStrip assessment={overfit} />
      {summary?.insight ? (
        <p className="ai-oneshot-line optimizer-insight">
          {summary.insight}
        </p>
      ) : null}
    </div>
  );
}
