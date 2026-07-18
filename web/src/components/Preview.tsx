import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { Card, Button } from "../ui";

export function Preview({ deviceId, bump }: { deviceId: string; bump: number }) {
  const [reloadKey, setReloadKey] = useState(0);
  const [failed, setFailed] = useState(false);

  // Rebuild the URL (cache-busted) whenever the device, an action bump, or a
  // manual refresh happens.
  const src = useMemo(
    () => api.previewUrl(deviceId),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [deviceId, bump, reloadKey],
  );

  useEffect(() => {
    setFailed(false);
  }, [src]);

  return (
    <Card
      title="Next frame preview"
      trailing={
        <Button onClick={() => setReloadKey((k) => k + 1)}>Refresh</Button>
      }
    >
      <div className="preview-frame">
        {failed ? (
          <div className="muted" style={{ padding: 30, textAlign: "center" }}>
            No preview rendered yet.
            <br />
            Send content or press Next.
          </div>
        ) : (
          <img src={src} alt="Next frame preview" onError={() => setFailed(true)} />
        )}
      </div>
    </Card>
  );
}
