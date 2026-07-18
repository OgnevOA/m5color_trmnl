import { useCallback, useEffect, useState } from "react";
import { api, type StatsRecord, type StatsResponse } from "../api";
import { Card, Stat, Segmented, Button } from "../ui";
import {
  LineChart,
  BarChart,
  Donut,
  Legend,
  DataTable,
  PALETTE,
  type Point,
  type Slice,
} from "./charts";
import { shortTime, num, ms } from "../format";

const RANGES = [
  { value: 1, label: "1d" },
  { value: 7, label: "7d" },
  { value: 30, label: "30d" },
];

function downsample<T>(arr: T[], n: number): T[] {
  if (arr.length <= n) return arr;
  const out: T[] = [];
  for (let i = 0; i < n; i++) {
    out.push(arr[Math.round((i / (n - 1)) * (arr.length - 1))]);
  }
  return out;
}

export function Telemetry({ deviceId }: { deviceId: string }) {
  const [days, setDays] = useState(7);
  const [data, setData] = useState<StatsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.stats(deviceId, days));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [deviceId, days]);

  useEffect(() => {
    load();
  }, [load]);

  const controls = (
    <div className="row">
      <Segmented value={days} options={RANGES} onChange={setDays} />
      <Button onClick={load} busy={loading}>
        Reload
      </Button>
    </div>
  );

  if (error) {
    return (
      <Card title="Telemetry" trailing={controls}>
        <div className="callout danger">
          <span className="bar" />
          <div>{error}</div>
        </div>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card title="Telemetry" trailing={controls}>
        <div className="muted">Loading telemetry...</div>
      </Card>
    );
  }

  const { summary, records } = data;
  if (records.length === 0) {
    return (
      <Card title="Telemetry" trailing={controls}>
        <div className="muted">No telemetry recorded yet.</div>
      </Card>
    );
  }

  const chrono = [...records].reverse();
  const batteryNow = records.find((r) => r.battery_percent !== null)?.battery_percent ?? null;

  // Battery voltage over time.
  const battPts: Point[] = downsample(
    chrono.filter((r) => r.battery_mv !== null),
    16,
  ).map((r) => ({ label: shortTime(r.created_at), value: r.battery_mv as number }));

  // Awake-time budget for a draw cycle (seconds).
  const drawAwake = summary.draw_awake_avg;
  const budget: Slice[] = [];
  if (drawAwake) {
    const panel = summary.draw_ms_avg ?? 0;
    const wifi = summary.wifi_ms_avg ?? 0;
    const dl = summary.draw_download_avg ?? 0;
    const post = summary.post_ms_avg ?? 0;
    const other = Math.max(0, drawAwake - (panel + wifi + dl + post));
    budget.push(
      { label: "Panel draw", value: panel / 1000, color: PALETTE.blue },
      { label: "Wi-Fi", value: wifi / 1000, color: PALETTE.amber },
      { label: "Download", value: dl / 1000, color: PALETTE.indigo },
      { label: "Other", value: other / 1000, color: PALETTE.gray },
      { label: "POST", value: post / 1000, color: PALETTE.green },
    );
  }

  // Draw-duration consistency across recent draw cycles.
  const drawCycles = chrono.filter((r) => (r.draw_ms ?? 0) > 0);
  const drawPts: Point[] = downsample(drawCycles, 12).map((r) => ({
    label: shortTime(r.created_at),
    value: (r.draw_ms as number) / 1000,
  }));
  const drawVals = drawPts.map((p) => p.value);
  const drawMin = drawVals.length ? Math.min(...drawVals) : 0;
  const drawMax = drawVals.length ? Math.max(...drawVals) : 1;

  // Recent draw cycles table.
  const recent = drawCycles.slice(-8).reverse();

  return (
    <Card title="Telemetry" trailing={controls}>
      <div className="grid grid-3" style={{ gap: 12, marginBottom: 18 }}>
        <Stat
          value={batteryNow !== null ? `${num(batteryNow, 1)}%` : "-"}
          label="Battery now"
          tone="success"
        />
        <Stat
          value={summary.battery_drain_mv !== null ? `${num(summary.battery_drain_mv, 1)} mV` : "-"}
          label="Avg drain / cycle"
        />
        <Stat
          value={summary.rssi_avg !== null ? `${num(summary.rssi_avg, 0)} dBm` : "-"}
          label="Avg Wi-Fi RSSI"
        />
        <Stat value={ms(summary.draw_awake_avg)} label="Avg awake / draw" />
        <Stat value={ms(summary.draw_ms_avg)} label="Avg panel draw" />
        <Stat
          value={`${summary.draws} / ${summary.sleeps} / ${summary.noops}`}
          label="Draws / sleeps / noops"
        />
      </div>

      {battPts.length > 1 && (
        <section style={{ marginBottom: 22 }}>
          <h3 style={{ fontSize: 14, marginBottom: 2 }}>Battery voltage over time</h3>
          <LineChart data={battPts} unit="mV" color={PALETTE.green} />
          <div className="chart-caption">
            Y: pack voltage (mV) - X: sample time - last {days} day(s), {summary.samples} wakes.
          </div>
        </section>
      )}

      <div className="grid grid-2" style={{ gap: 18, marginBottom: 22 }}>
        {budget.length > 0 && (
          <section>
            <h3 style={{ fontSize: 14, marginBottom: 8 }}>
              Awake-time budget (draw cycle)
            </h3>
            <div className="row" style={{ alignItems: "center", gap: 16 }}>
              <Donut data={budget} />
              <div style={{ minWidth: 0 }}>
                <div className="stat-value">{ms(summary.draw_awake_avg)}</div>
                <div className="stat-label">avg awake per draw</div>
              </div>
            </div>
            <Legend items={budget.map((b) => ({ label: b.label, color: b.color }))} />
          </section>
        )}

        {drawPts.length > 1 && (
          <section>
            <h3 style={{ fontSize: 14, marginBottom: 8 }}>Panel-draw consistency</h3>
            <BarChart
              data={drawPts}
              unit="s"
              color={PALETTE.blue}
              yMin={Math.max(0, drawMin - 0.4)}
              yMax={drawMax + 0.4}
              showValues={false}
            />
            <div className="chart-caption">
              Y: e-ink draw duration (s) - X: recent draw cycles.
            </div>
          </section>
        )}
      </div>

      {recent.length > 0 && (
        <section>
          <h3 style={{ fontSize: 14, marginBottom: 8 }}>Recent draw cycles</h3>
          <DataTable
            headers={["Time", "Mode", "Wi-Fi", "Download", "Draw", "Awake"]}
            rows={recent.map((r: StatsRecord) => [
              shortTime(r.created_at),
              r.mode ?? "-",
              ms(r.wifi_ms),
              ms(r.download_ms),
              ms(r.draw_ms),
              ms(r.awake_ms),
            ])}
          />
        </section>
      )}
    </Card>
  );
}
