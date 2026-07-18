// Typed client for the open /api/ui control-panel endpoints.

const BASE = "/api/ui";

export interface DeviceInfo {
  device_id: string;
  device_type: string;
}

export interface Meta {
  modes: string[];
  collage_counts: number[];
  collage_count_default: number;
  photo_collage_max: number;
}

export interface Status {
  device_id: string;
  device_type?: string;
  mode: string;
  interval_minutes: number;
  night_mode_enabled: boolean;
  is_night_now: boolean;
  manual_override: boolean;
  overlay_enabled: boolean;
  collage_enabled: boolean;
  collage_count: number;
  last_seen: string | null;
  last_wake_reason: string | null;
  last_image_id: string | null;
  next_image_id: string | null;
  battery_percent: number | null;
  queue_pending: number;
  queue_ready: number;
  presence: string | null;
}

export interface Favorite {
  image_id: string;
  title: string | null;
  created_at: string;
}

export interface StatsSummary {
  hours: number;
  samples: number;
  total_samples: number;
  battery_min: number | null;
  battery_max: number | null;
  battery_avg: number | null;
  battery_drain_mv: number | null;
  rssi_avg: number | null;
  wifi_ms_avg: number | null;
  post_ms_avg: number | null;
  draw_cycles: number;
  idle_cycles: number;
  draw_awake_avg: number | null;
  draw_ms_avg: number | null;
  draw_download_avg: number | null;
  draw_render_avg: number | null;
  idle_awake_avg: number | null;
  draws: number;
  sleeps: number;
  noops: number;
}

export interface StatsRecord {
  created_at: string;
  action: string | null;
  mode: string | null;
  wake_reason: string | null;
  battery_percent: number | null;
  battery_mv: number | null;
  wifi_rssi: number | null;
  firmware_version: string | null;
  next_wake_seconds: number | null;
  is_night: number | null;
  wifi_ms: number | null;
  post_ms: number | null;
  download_ms: number | null;
  draw_ms: number | null;
  awake_ms: number | null;
  render_ms: number | null;
}

export interface StatsResponse {
  device_id: string;
  days: number;
  summary: StatsSummary;
  records: StatsRecord[];
}

export class ApiError extends Error {}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, init);
  } catch {
    throw new ApiError("Network error - is the backend reachable?");
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail);
  }
  const ctype = res.headers.get("content-type") || "";
  if (ctype.includes("application/json")) return (await res.json()) as T;
  return undefined as T;
}

function post<T = unknown>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const api = {
  meta: () => request<Meta>("/meta"),
  devices: () => request<{ devices: DeviceInfo[] }>("/devices").then((r) => r.devices),
  status: (id: string) => request<Status>(`/devices/${id}/status`),
  stats: (id: string, days: number) =>
    request<StatsResponse>(`/devices/${id}/stats?days=${days}`),
  previewUrl: (id: string) => `${BASE}/devices/${id}/preview.png?t=${Date.now()}`,
  currentUrl: (id: string) => `${BASE}/devices/${id}/current.png?t=${Date.now()}`,
  favoriteUrl: (id: string, imageId: string) =>
    `${BASE}/devices/${id}/favorites/${imageId}.png`,
  favorites: (id: string) =>
    request<{ favorites: Favorite[] }>(`/devices/${id}/favorites`).then(
      (r) => r.favorites,
    ),
  addFavorite: (id: string, imageId: string) =>
    post(`/devices/${id}/favorite`, { image_id: imageId }),
  removeFavorite: (id: string, imageId: string) =>
    post(`/devices/${id}/unfavorite`, { image_id: imageId }),

  setMode: (id: string, name: string) => post(`/devices/${id}/mode`, { name }),
  setInterval: (id: string, minutes: number) =>
    post(`/devices/${id}/interval`, { minutes }),
  setNight: (id: string, enabled: boolean) => post(`/devices/${id}/night`, { enabled }),
  setOverlay: (id: string, enabled: boolean) =>
    post(`/devices/${id}/overlay`, { enabled }),
  setCollage: (id: string, enabled: boolean) =>
    post(`/devices/${id}/collage`, { enabled }),
  setCollageCount: (id: string, count: number) =>
    post(`/devices/${id}/collage_count`, { count }),
  next: (id: string) => post(`/devices/${id}/next`),
  skip: (id: string) => post(`/devices/${id}/skip`),
  clear: (id: string) => post(`/devices/${id}/clear`),
  sendText: (id: string, text: string) => post(`/devices/${id}/text`, { text }),
  sendQr: (id: string, payload: string) => post(`/devices/${id}/qr`, { payload }),
  uploadImages: (id: string, files: File[]) => {
    const form = new FormData();
    for (const file of files) form.append("files", file);
    return request<{ ok: boolean; kind: string }>(`/devices/${id}/image`, {
      method: "POST",
      body: form,
    });
  },
};
