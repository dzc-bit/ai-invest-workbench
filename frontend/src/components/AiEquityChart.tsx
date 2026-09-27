import { useEffect, useRef, useState } from "react";
import type { AiEquityPoint } from "../aiTypes";
import type { ECharts } from "./echartsBundle";

type Props = {
  title: string;
  points: AiEquityPoint[];
};

function buildOption(title: string, points: AiEquityPoint[]) {
  return {
    title: { text: title, left: 4, top: 2, textStyle: { fontSize: 12, fontWeight: 600, color: "#44506b" } },
    tooltip: { trigger: "axis", textStyle: { fontSize: 11 } },
    legend: { data: ["权益", "回撤"], top: 20, right: 4, textStyle: { fontSize: 11 } },
    grid: { left: 52, right: 44, top: 48, bottom: 24 },
    xAxis: {
      type: "category",
      data: points.map((point) => point.trade_date),
      axisLabel: { fontSize: 10, interval: Math.max(0, Math.floor(points.length / 6)) }
    },
    yAxis: [
      { type: "value", scale: true, axisLabel: { fontSize: 10 } },
      {
        type: "value",
        axisLabel: { fontSize: 10, formatter: (value: number) => `${(value * 100).toFixed(0)}%` }
      }
    ],
    series: [
      {
        name: "权益",
        type: "line",
        data: points.map((point) => point.equity),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: "#0f766e" },
        areaStyle: { opacity: 0.06, color: "#0f766e" }
      },
      {
        name: "回撤",
        type: "line",
        yAxisIndex: 1,
        data: points.map((point) => point.drawdown_pct),
        showSymbol: false,
        lineStyle: { width: 1, color: "#d92d20", type: "dashed" }
      }
    ]
  };
}

/**
 * ECharts rendering of a backtest equity curve (equity + drawdown) inside the
 * AI drawer. echarts 只在图表真正出现时才按需加载并注册用到的组件；加载与
 * 加载失败都有明确状态，不靠静默空白表达"图没了"。jsdom 下 init 会抛错，
 * 容器仍然渲染。
 */
export function AiEquityChart({ title, points }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<ECharts | null>(null);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    let observer: ResizeObserver | null = null;
    const container = containerRef.current;
    if (!container) {
      return;
    }
    void import("./echartsBundle")
      .then(({ init }) => {
        if (cancelled) {
          return undefined;
        }
        try {
          const chart = init(container);
          chartRef.current = chart;
          observer = new ResizeObserver(() => chart.resize());
          observer.observe(container);
          setReady(true);
        } catch {
          // jsdom / 无 canvas 环境：容器仍渲染，只是没有图形。
          setFailed(true);
        }
        return undefined;
      })
      .catch(() => {
        if (!cancelled) {
          setFailed(true);
        }
      });
    return () => {
      cancelled = true;
      observer?.disconnect();
      chartRef.current?.dispose();
      chartRef.current = null;
      setReady(false);
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!ready || !chart || points.length === 0) {
      return;
    }
    chart.setOption(buildOption(title, points));
  }, [ready, points, title]);

  return (
    <div className="ai-chart-wrap">
      <div ref={containerRef} className="ai-chart" role="img" aria-label={title} />
      {failed ? <p className="ai-chart-state">图表组件未能加载，可点「AI 解读」用文字看同一段权益与回撤。</p> : null}
      {!failed && !ready ? <p className="ai-chart-state">正在加载图表组件…</p> : null}
    </div>
  );
}
