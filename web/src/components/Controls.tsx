import { useEffect, useState } from "react";
import { api, type Meta, type Status } from "../api";
import type { ActionOpts } from "../App";
import { Card, Toggle, Segmented, Button, useToast } from "../ui";
import { titleCase } from "../format";

interface Props {
  deviceId: string;
  status: Status | null;
  meta: Meta;
  runAction: (fn: () => Promise<unknown>, opts?: ActionOpts) => Promise<void>;
}

export function Controls({ deviceId, status, meta, runAction }: Props) {
  const toast = useToast();
  const [interval, setIntervalValue] = useState<number>(60);

  useEffect(() => {
    if (status) setIntervalValue(status.interval_minutes);
  }, [status?.interval_minutes, deviceId]);

  if (!status) {
    return (
      <Card title="Controls">
        <div className="muted">Loading...</div>
      </Card>
    );
  }

  return (
    <Card title="Controls">
      <div className="field">
        <label>Mode</label>
        <select
          value={status.mode}
          onChange={(e) =>
            runAction(() => api.setMode(deviceId, e.target.value), {
              success: `Switched to ${titleCase(e.target.value)}`,
              refreshPreview: true,
            })
          }
        >
          {meta.modes.map((m) => (
            <option key={m} value={m}>
              {titleCase(m)}
            </option>
          ))}
          {/* Show the current mode even if it isn't a selectable one. */}
          {!meta.modes.includes(status.mode) && (
            <option value={status.mode}>{titleCase(status.mode)}</option>
          )}
        </select>
      </div>

      <div className="field">
        <label>Refresh interval (minutes)</label>
        <div className="row">
          <input
            type="number"
            min={1}
            style={{ width: 110 }}
            value={interval}
            onChange={(e) => setIntervalValue(Number(e.target.value))}
          />
          <Button
            onClick={() =>
              runAction(() => api.setInterval(deviceId, interval), {
                success: `Interval set to ${interval} min`,
              })
            }
            disabled={interval < 1 || interval === status.interval_minutes}
          >
            Set
          </Button>
        </div>
      </div>

      <div className="field">
        <label>Toggles</label>
        <div className="grid" style={{ gap: 10 }}>
          <Toggle
            label="Night mode"
            checked={status.night_mode_enabled}
            onChange={(v) =>
              runAction(() => api.setNight(deviceId, v), {
                success: `Night mode ${v ? "on" : "off"}`,
              })
            }
          />
          <Toggle
            label="Info overlay"
            checked={status.overlay_enabled}
            onChange={(v) =>
              runAction(() => api.setOverlay(deviceId, v), {
                success: `Overlay ${v ? "on" : "off"}`,
                refreshPreview: true,
              })
            }
          />
          <Toggle
            label="Collage mode"
            checked={status.collage_enabled}
            onChange={(v) =>
              runAction(() => api.setCollage(deviceId, v), {
                success: `Collage ${v ? "on" : "off"}`,
              })
            }
          />
        </div>
      </div>

      {status.collage_enabled && (
        <div className="field">
          <label>Works per collage</label>
          <Segmented
            value={status.collage_count}
            options={meta.collage_counts.map((c) => ({ value: c, label: String(c) }))}
            onChange={(c) =>
              runAction(() => api.setCollageCount(deviceId, c), {
                success: `Collage set to ${c} works`,
              })
            }
          />
        </div>
      )}

      <div className="field" style={{ marginBottom: 0 }}>
        <label>Queue</label>
        <div className="row">
          <Button
            variant="primary"
            onClick={() =>
              runAction(() => api.next(deviceId), {
                success: "Generated next item",
                refreshPreview: true,
              })
            }
          >
            Next
          </Button>
          <Button
            onClick={() =>
              runAction(
                async () => {
                  const r = await api.skip(deviceId);
                  toast.push(
                    r.regenerated
                      ? "Skipped - loading a new image"
                      : r.skipped
                        ? "Skipped the next item"
                        : "Nothing queued to skip",
                    r.skipped || r.regenerated ? "success" : "info",
                  );
                },
                { refreshPreview: true },
              )
            }
          >
            Skip
          </Button>
          <Button
            variant="danger"
            onClick={() =>
              runAction(() => api.clear(deviceId), { success: "Queue cleared" })
            }
          >
            Clear queue
          </Button>
        </div>
      </div>
    </Card>
  );
}
