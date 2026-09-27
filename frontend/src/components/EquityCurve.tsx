import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

export type EquityPoint = { trade_date: string; equity: number };

type Props = {
  data: EquityPoint[];
};

/** 权益曲线单独成块：recharts 整栈约 366 KB，不该压在首屏启动路径上。 */
export default function EquityCurve({ data }: Props) {
  return (
    <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={220}>
      <LineChart data={data}>
        <XAxis dataKey="trade_date" />
        <YAxis />
        <Tooltip />
        <Line type="monotone" dataKey="equity" stroke="#0f766e" strokeWidth={2} dot={false} />
      </LineChart>
    </ResponsiveContainer>
  );
}
