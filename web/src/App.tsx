import { useCallback, useEffect, useState } from "react";
import { api, type DeviceInfo, type Meta, type Status } from "./api";
import { useToast } from "./ui";
import { StatusCard } from "./components/StatusCard";
import { Preview } from "./components/Preview";
import { Controls } from "./components/Controls";
import { SendContent } from "./components/SendContent";
import { Telemetry } from "./components/Telemetry";
import { relativeTime, titleCase } from "./format";

const DEVICE_KEY = "trmnl.device";
const POLL_MS = 15000;

export interface ActionOpts {
  success?: string;
  refreshPreview?: boolean;
}

export default function App() {
  const toast = useToast();
  const [meta, setMeta] = useState<Meta | null>(null);
  const [devices, setDevices] = useState<DeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState<string | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [bootError, setBootError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<Date | null>(null);
  const [previewBump, setPreviewBump] = useState(0);
  const [, forceTick] = useState(0);

  // Initial load: meta + device list.
  useEffect(() => {
    (async () => {
      try {
        const [m, devs] = await Promise.all([api.meta(), api.devices()]);
        setMeta(m);
        setDevices(devs);
        const saved = localStorage.getItem(DEVICE_KEY);
        const initial =
          (saved && devs.some((d) => d.device_id === saved) && saved) ||
          devs[0]?.device_id ||
          null;
        setDeviceId(initial);
      } catch (e) {
        setBootError(e instanceof Error ? e.message : String(e));
      }
    })();
  }, []);

  const loadStatus = useCallback(async () => {
    if (!deviceId) return;
    try {
      const s = await api.status(deviceId);
      setStatus(s);
      setUpdatedAt(new Date());
    } catch (e) {
      toast.push(e instanceof Error ? e.message : String(e), "error");
    }
  }, [deviceId, toast]);

  // Fetch on device change + poll.
  useEffect(() => {
    if (!deviceId) return;
    setStatus(null);
    loadStatus();
    const id = window.setInterval(loadStatus, POLL_MS);
    return () => window.clearInterval(id);
  }, [deviceId, loadStatus]);

  // Keep the "updated Ns ago" label ticking.
  useEffect(() => {
    const id = window.setInterval(() => forceTick((t) => t + 1), 1000);
    return () => window.clearInterval(id);
  }, []);

  const runAction = useCallback(
    async (fn: () => Promise<unknown>, opts?: ActionOpts) => {
      try {
        await fn();
        if (opts?.success) toast.push(opts.success, "success");
        await loadStatus();
        if (opts?.refreshPreview) setPreviewBump((b) => b + 1);
      } catch (e) {
        toast.push(e instanceof Error ? e.message : String(e), "error");
      }
    },
    [loadStatus, toast],
  );

  const selectDevice = (id: string) => {
    localStorage.setItem(DEVICE_KEY, id);
    setDeviceId(id);
  };

  return (
    <div className="app">
      <header className="header">
        <h1>TRMNL Control Panel</h1>
        {devices.length > 0 && deviceId && (
          <select
            value={deviceId}
            onChange={(e) => selectDevice(e.target.value)}
            aria-label="Select device"
          >
            {devices.map((d) => (
              <option key={d.device_id} value={d.device_id}>
                {d.device_id} ({d.device_type})
              </option>
            ))}
          </select>
        )}
        <span className="spacer" />
        <span className="meta">
          {updatedAt ? `updated ${relativeTime(updatedAt.toISOString())}` : ""}
        </span>
        <button className="btn" onClick={loadStatus} disabled={!deviceId}>
          Refresh
        </button>
      </header>

      {bootError && (
        <div className="callout danger" style={{ marginBottom: 16 }}>
          <span className="bar" />
          <div>Could not reach the backend: {bootError}</div>
        </div>
      )}

      {!deviceId && !bootError && (
        <div className="muted">Loading devices...</div>
      )}

      {deviceId && meta && (
        <>
          <div className="grid layout">
            <div className="grid" style={{ gap: 16 }}>
              <StatusCard status={status} />
              <Preview deviceId={deviceId} bump={previewBump} />
            </div>
            <div className="grid" style={{ gap: 16 }}>
              <Controls
                deviceId={deviceId}
                status={status}
                meta={meta}
                runAction={runAction}
              />
              <SendContent
                deviceId={deviceId}
                status={status}
                meta={meta}
                runAction={runAction}
              />
            </div>
          </div>

          <div className="section-title">Telemetry</div>
          <Telemetry deviceId={deviceId} />

          <div className="meta" style={{ marginTop: 26 }}>
            {status ? `${titleCase(status.mode)} - ${status.device_type ?? ""}` : ""}
          </div>
        </>
      )}
    </div>
  );
}
