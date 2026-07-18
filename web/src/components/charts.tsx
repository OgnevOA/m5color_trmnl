import { type ReactNode } from "react";

export const PALETTE = {
  blue: "#6ea8fe",
  green: "#5bc98a",
  amber: "#e3b34c",
  red: "#e5766b",
  gray: "#8b93a3",
  indigo: "#9b8cf0",
};

export interface Point {
  label: string;
  value: number;
}

function niceTicks(min: number, max: number, count = 4): number[] {
  if (min === max) return [min];
  const step = (max - min) / (count - 1);
  return Array.from({ length: count }, (_, i) => min + step * i);
}

// --- Line chart ------------------------------------------------------------
export function LineChart({
  data,
  unit = "",
  color = PALETTE.blue,
  height = 210,
}: {
  data: Point[];
  unit?: string;
  color?: string;
  height?: number;
}) {
  const W = 760;
  const H = height;
  const padL = 46;
  const padR = 14;
  const padT = 12;
  const padB = 28;
  const iw = W - padL - padR;
  const ih = H - padT - padB;

  if (data.length === 0) return null;

  const values = data.map((d) => d.value);
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  if (lo === hi) {
    lo -= 1;
    hi += 1;
  }
  const pad = (hi - lo) * 0.08;
  lo -= pad;
  hi += pad;

  const x = (i: number) =>
    padL + (data.length === 1 ? iw / 2 : (i / (data.length - 1)) * iw);
  const y = (v: number) => padT + ih - ((v - lo) / (hi - lo)) * ih;

  const line = data.map((d, i) => `${x(i)},${y(d.value)}`).join(" ");
  const area = `${padL},${padT + ih} ${line} ${padL + iw},${padT + ih}`;
  const ticks = niceTicks(lo + pad, hi - pad);

  // Sparse x labels: first, ~middle, last.
  const labelIdx = new Set([0, Math.floor((data.length - 1) / 2), data.length - 1]);

  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img">
      {ticks.map((t, i) => (
        <g key={i}>
          <line className="axis" x1={padL} x2={padL + iw} y1={y(t)} y2={y(t)} />
          <text x={padL - 6} y={y(t) + 3} textAnchor="end">
            {Math.round(t)}
          </text>
        </g>
      ))}
      <polygon points={area} fill={color} opacity={0.12} />
      <polyline points={line} fill="none" stroke={color} strokeWidth={2} />
      {data.map((d, i) =>
        labelIdx.has(i) ? (
          <circle key={i} cx={x(i)} cy={y(d.value)} r={2.6} fill={color} />
        ) : null,
      )}
      {data.map((d, i) =>
        labelIdx.has(i) ? (
          <text
            key={`l${i}`}
            x={x(i)}
            y={H - 8}
            textAnchor={i === 0 ? "start" : i === data.length - 1 ? "end" : "middle"}
          >
            {d.label}
          </text>
        ) : null,
      )}
      <text x={padL} y={padT - 2} textAnchor="start" opacity={0.7}>
        {unit}
      </text>
    </svg>
  );
}

// --- Bar chart -------------------------------------------------------------
export function BarChart({
  data,
  unit = "",
  color = PALETTE.blue,
  height = 190,
  yMin,
  yMax,
  showValues = true,
}: {
  data: Point[];
  unit?: string;
  color?: string;
  height?: number;
  yMin?: number;
  yMax?: number;
  showValues?: boolean;
}) {
  const W = 760;
  const H = height;
  const padL = 46;
  const padR = 14;
  const padT = 16;
  const padB = 28;
  const iw = W - padL - padR;
  const ih = H - padT - padB;

  if (data.length === 0) return null;

  const values = data.map((d) => d.value);
  const lo = yMin ?? Math.min(0, ...values);
  const hi = yMax ?? (Math.max(...values) * 1.08 || 1);
  const y = (v: number) => padT + ih - ((v - lo) / (hi - lo)) * ih;
  const bw = (iw / data.length) * 0.62;
  const gap = iw / data.length;
  const ticks = niceTicks(lo, hi);

  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img">
      {ticks.map((t, i) => (
        <g key={i}>
          <line className="axis" x1={padL} x2={padL + iw} y1={y(t)} y2={y(t)} />
          <text x={padL - 6} y={y(t) + 3} textAnchor="end">
            {t.toFixed(t < 10 ? 1 : 0)}
          </text>
        </g>
      ))}
      {data.map((d, i) => {
        const cx = padL + gap * i + gap / 2;
        const top = y(d.value);
        const base = y(lo);
        return (
          <g key={i}>
            <rect
              x={cx - bw / 2}
              y={Math.min(top, base)}
              width={bw}
              height={Math.abs(base - top)}
              fill={color}
              rx={2}
            />
            {showValues && (
              <text x={cx} y={top - 4} textAnchor="middle">
                {d.value.toFixed(d.value < 10 ? 1 : 0)}
              </text>
            )}
            <text x={cx} y={H - 8} textAnchor="middle">
              {d.label}
            </text>
          </g>
        );
      })}
      <text x={padL} y={padT - 4} textAnchor="start" opacity={0.7}>
        {unit}
      </text>
    </svg>
  );
}

// --- Donut -----------------------------------------------------------------
export interface Slice {
  label: string;
  value: number;
  color: string;
}

export function Donut({ data, size = 190 }: { data: Slice[]; size?: number }) {
  const total = data.reduce((s, d) => s + Math.max(0, d.value), 0);
  const r = size / 2;
  const inner = r * 0.6;
  const cx = r;
  const cy = r;
  if (total <= 0) return null;

  let angle = -Math.PI / 2;
  const arcs = data
    .filter((d) => d.value > 0)
    .map((d, i) => {
      const frac = d.value / total;
      const a0 = angle;
      const a1 = angle + frac * Math.PI * 2;
      angle = a1;
      const large = a1 - a0 > Math.PI ? 1 : 0;
      const x0 = cx + r * Math.cos(a0);
      const y0 = cy + r * Math.sin(a0);
      const x1 = cx + r * Math.cos(a1);
      const y1 = cy + r * Math.sin(a1);
      const xi0 = cx + inner * Math.cos(a1);
      const yi0 = cy + inner * Math.sin(a1);
      const xi1 = cx + inner * Math.cos(a0);
      const yi1 = cy + inner * Math.sin(a0);
      const path = [
        `M ${x0} ${y0}`,
        `A ${r} ${r} 0 ${large} 1 ${x1} ${y1}`,
        `L ${xi0} ${yi0}`,
        `A ${inner} ${inner} 0 ${large} 0 ${xi1} ${yi1}`,
        "Z",
      ].join(" ");
      return <path key={i} d={path} fill={d.color} />;
    });

  return (
    <svg
      className="chart"
      viewBox={`0 0 ${size} ${size}`}
      width={size}
      height={size}
      style={{ flex: "none" }}
      role="img"
    >
      {arcs}
    </svg>
  );
}

// --- Legend ----------------------------------------------------------------
export function Legend({ items }: { items: { label: string; color: string }[] }) {
  return (
    <div className="legend">
      {items.map((it, i) => (
        <span key={i}>
          <span className="swatch" style={{ background: it.color }} />
          {it.label}
        </span>
      ))}
    </div>
  );
}

// --- Data table ------------------------------------------------------------
export function DataTable({
  headers,
  rows,
  leftCols = 2,
}: {
  headers: string[];
  rows: ReactNode[][];
  leftCols?: number;
}) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table className="data">
        <thead>
          <tr>
            {headers.map((h, i) => (
              <th key={i} className={i < leftCols ? "l" : ""}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, ri) => (
            <tr key={ri}>
              {r.map((c, ci) => (
                <td key={ci} className={ci < leftCols ? "l" : ""}>
                  {c}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
