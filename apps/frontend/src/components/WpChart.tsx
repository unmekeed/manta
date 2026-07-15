import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { TimelinePoint } from "../lib/api";

const fmtMin = (s: number) => `${Math.floor(s / 60)}'`;

// WP-кривая (Гл. 8.3): вероятность победы Radiant по минутам + разница
// net worth на второй оси. 0.5 — линия равновесия.
export default function WpChart({ points }: { points: TimelinePoint[] }) {
  const data = points.map((p) => ({
    t: p.game_time,
    wp: +(p.radiant_wp * 100).toFixed(1),
    nw: p.net_worth_diff,
  }));
  return (
    <div className="panel" style={{ height: 320 }}>
      <ResponsiveContainer>
        <LineChart data={data} margin={{ top: 10, right: 12, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="#2a333c" strokeDasharray="3 3" />
          <XAxis dataKey="t" tickFormatter={fmtMin} stroke="#8a97a3" fontSize={12} />
          <YAxis
            yAxisId="wp"
            domain={[0, 100]}
            tickFormatter={(v) => `${v}%`}
            stroke="#8a97a3"
            fontSize={12}
          />
          <YAxis
            yAxisId="nw"
            orientation="right"
            tickFormatter={(v) => `${(v / 1000).toFixed(0)}k`}
            stroke="#8a97a3"
            fontSize={12}
          />
          <Tooltip
            contentStyle={{ background: "#171c21", border: "1px solid #2a333c" }}
            labelFormatter={(t) => `Минута ${Math.floor(Number(t) / 60)}`}
            formatter={(value, name) =>
              name === "WP Radiant" ? [`${value}%`, name] : [value, name]
            }
          />
          <ReferenceLine yAxisId="wp" y={50} stroke="#8a97a3" strokeDasharray="4 4" />
          <Line
            yAxisId="nw"
            dataKey="nw"
            name="Net worth diff"
            stroke="#d9a441"
            dot={false}
            strokeWidth={1.5}
            opacity={0.7}
          />
          <Line
            yAxisId="wp"
            dataKey="wp"
            name="WP Radiant"
            stroke="#6bd06b"
            dot={false}
            strokeWidth={2.5}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
