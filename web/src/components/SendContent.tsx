import { useRef, useState } from "react";
import { api, type Meta, type Status } from "../api";
import type { ActionOpts } from "../App";
import { Card, Button } from "../ui";

interface Props {
  deviceId: string;
  status: Status | null;
  meta: Meta;
  runAction: (fn: () => Promise<unknown>, opts?: ActionOpts) => Promise<void>;
}

export function SendContent({ deviceId, status, meta, runAction }: Props) {
  const [text, setText] = useState("");
  const [qr, setQr] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);

  const max = meta.photo_collage_max;
  const collageOn = !!status?.collage_enabled;

  const imageHint =
    files.length <= 1
      ? "One image is shown full-frame."
      : collageOn
        ? `Album -> face-aware collage (${Math.min(files.length, max)} tiles).`
        : `Album -> image carousel (collage is off).`;

  return (
    <Card title="Send content">
      <div className="field">
        <label>Text message</label>
        <textarea
          value={text}
          placeholder="Type a message to show..."
          onChange={(e) => setText(e.target.value)}
        />
        <div className="row">
          <Button
            variant="primary"
            disabled={!text.trim()}
            onClick={() =>
              runAction(() => api.sendText(deviceId, text), {
                success: "Text queued",
                refreshPreview: true,
              }).then(() => setText(""))
            }
          >
            Send text
          </Button>
        </div>
      </div>

      <div className="field">
        <label>QR code (text or URL)</label>
        <input
          type="text"
          value={qr}
          placeholder="https://example.com"
          onChange={(e) => setQr(e.target.value)}
        />
        <div className="row">
          <Button
            variant="primary"
            disabled={!qr.trim()}
            onClick={() =>
              runAction(() => api.sendQr(deviceId, qr), {
                success: "QR code queued",
                refreshPreview: true,
              }).then(() => setQr(""))
            }
          >
            Send QR
          </Button>
        </div>
      </div>

      <div className="field" style={{ marginBottom: 0 }}>
        <label>Image / album (up to {max})</label>
        <input
          ref={fileRef}
          type="file"
          accept="image/*"
          multiple
          onChange={(e) => setFiles(Array.from(e.target.files ?? []).slice(0, max))}
        />
        <div className="muted" style={{ fontSize: 12 }}>
          {files.length > 0 ? `${files.length} selected - ${imageHint}` : imageHint}
        </div>
        <div className="row">
          <Button
            variant="primary"
            disabled={files.length === 0}
            onClick={() =>
              runAction(() => api.uploadImages(deviceId, files), {
                success: `Uploaded ${files.length} image(s)`,
                refreshPreview: true,
              }).then(() => {
                setFiles([]);
                if (fileRef.current) fileRef.current.value = "";
              })
            }
          >
            Upload
          </Button>
        </div>
      </div>
    </Card>
  );
}
