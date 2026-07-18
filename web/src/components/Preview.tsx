import { useEffect, useMemo, useState } from "react";
import { api, type Status } from "../api";
import { Card, Button, useLightbox } from "../ui";

function StarIcon({ filled }: { filled: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
      <path
        d="M12 2.5l2.9 6 6.6.9-4.8 4.6 1.2 6.5L12 17.9 6.1 20.5l1.2-6.5L2.5 9.4l6.6-.9z"
        fill={filled ? "currentColor" : "none"}
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function Frame({
  label,
  src,
  imageId,
  isFavorite,
  onToggleFavorite,
}: {
  label: string;
  src: string | null;
  imageId: string | null;
  isFavorite: boolean;
  onToggleFavorite: (imageId: string, makeFavorite: boolean) => void;
}) {
  const [failed, setFailed] = useState(false);
  const lightbox = useLightbox();
  useEffect(() => setFailed(false), [src]);

  return (
    <div className="frame">
      <div className="frame-head">
        <span>{label}</span>
        <span className="spacer" />
        <button
          className={`star ${isFavorite ? "on" : ""}`}
          disabled={!imageId}
          title={
            !imageId
              ? "No image to favorite"
              : isFavorite
                ? "Remove from favorites"
                : "Add to favorites"
          }
          onClick={() => imageId && onToggleFavorite(imageId, !isFavorite)}
        >
          <StarIcon filled={isFavorite} />
        </button>
      </div>
      <div className="preview-frame">
        {src && !failed ? (
          <img
            src={src}
            alt={label}
            title="Click to enlarge"
            onError={() => setFailed(true)}
            onClick={() => lightbox.open(src, label)}
          />
        ) : (
          <div className="muted" style={{ padding: 24, textAlign: "center" }}>
            Nothing here yet.
          </div>
        )}
      </div>
    </div>
  );
}

export function Preview({
  deviceId,
  bump,
  status,
  favoriteIds,
  onToggleFavorite,
}: {
  deviceId: string;
  bump: number;
  status: Status | null;
  favoriteIds: Set<string>;
  onToggleFavorite: (imageId: string, makeFavorite: boolean) => void;
}) {
  const [reloadKey, setReloadKey] = useState(0);

  const currentId = status?.last_image_id ?? null;
  const nextId = status?.next_image_id ?? null;

  // Cache-busted URLs, rebuilt when the device, the underlying image id, an
  // action bump, or a manual refresh changes.
  const currentSrc = useMemo(
    () => (currentId ? api.currentUrl(deviceId) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [deviceId, currentId, bump, reloadKey],
  );
  const nextSrc = useMemo(
    () => (nextId ? api.previewUrl(deviceId) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [deviceId, nextId, bump, reloadKey],
  );

  return (
    <Card
      title="Frames"
      trailing={<Button onClick={() => setReloadKey((k) => k + 1)}>Refresh</Button>}
    >
      <div className="frames">
        <Frame
          label="Now showing"
          src={currentSrc}
          imageId={currentId}
          isFavorite={!!currentId && favoriteIds.has(currentId)}
          onToggleFavorite={onToggleFavorite}
        />
        <Frame
          label="Up next"
          src={nextSrc}
          imageId={nextId}
          isFavorite={!!nextId && favoriteIds.has(nextId)}
          onToggleFavorite={onToggleFavorite}
        />
      </div>
    </Card>
  );
}
