import { type Status } from "../api";
import { Card, Pill, Callout } from "../ui";
import { relativeTime, titleCase, num } from "../format";

function batteryTone(pct: number | null): "success" | "warning" | "danger" | undefined {
  if (pct === null) return undefined;
  if (pct <= 15) return "danger";
  if (pct <= 25) return "warning";
  return "success";
}

export function StatusCard({ status }: { status: Status | null }) {
  if (!status) {
    return (
      <Card title="Status">
        <div className="muted">Loading status...</div>
      </Card>
    );
  }

  const pct = status.battery_percent;
  const tone = batteryTone(pct);

  return (
    <Card
      title="Status"
      trailing={
        <Pill tone={status.presence === "away" ? "warning" : "success"}>
          {status.device_type ?? "device"}
        </Pill>
      }
    >
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 10 }}>
        <div>
          <div className={`stat-value ${tone ?? ""}`}>
            {pct !== null ? `${num(pct, 1)}%` : "-"}
          </div>
          <div className="stat-label">Battery</div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div className="stat-value accent">{titleCase(status.mode)}</div>
          <div className="stat-label">Active mode</div>
        </div>
      </div>

      {pct !== null && pct <= 20 && (
        <div style={{ marginBottom: 10 }}>
          <Callout tone="warning">Battery is low ({num(pct, 0)}%) - time to charge.</Callout>
        </div>
      )}

      <div className="kv">
        <span className="k">Refresh interval</span>
        <span className="v">{status.interval_minutes} min</span>
      </div>
      <div className="kv">
        <span className="k">Night mode</span>
        <span className="v">
          {status.night_mode_enabled ? "on" : "off"}
          {status.is_night_now ? " (sleeping now)" : ""}
        </span>
      </div>
      <div className="kv">
        <span className="k">Overlay</span>
        <span className="v">{status.overlay_enabled ? "on" : "off"}</span>
      </div>
      <div className="kv">
        <span className="k">Collage</span>
        <span className="v">
          {status.collage_enabled ? `on (${status.collage_count} works)` : "off"}
        </span>
      </div>
      <div className="kv">
        <span className="k">Queue</span>
        <span className="v">
          {status.queue_ready} ready / {status.queue_pending} pending
        </span>
      </div>
      <div className="kv">
        <span className="k">Last wake</span>
        <span className="v">{status.last_wake_reason ?? "-"}</span>
      </div>
      <div className="kv">
        <span className="k">Last seen</span>
        <span className="v">{relativeTime(status.last_seen)}</span>
      </div>
      {status.presence && (
        <div className="kv">
          <span className="k">Presence</span>
          <span className="v">{status.presence}</span>
        </div>
      )}
    </Card>
  );
}
