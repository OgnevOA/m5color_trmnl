import { api, type Favorite } from "../api";
import { Card } from "../ui";

export function Favorites({
  deviceId,
  favorites,
  onRemove,
}: {
  deviceId: string;
  favorites: Favorite[];
  onRemove: (imageId: string) => void;
}) {
  return (
    <Card title={`Favorites (${favorites.length})`}>
      {favorites.length === 0 ? (
        <div className="muted">
          No favorites yet. Star the current or next frame to save an
          overlay-free copy, then switch to the <strong>favorites</strong> mode to
          replay them.
        </div>
      ) : (
        <div className="fav-grid">
          {favorites.map((f) => (
            <figure className="fav" key={f.image_id}>
              <img
                src={api.favoriteUrl(deviceId, f.image_id)}
                alt={f.title ?? "favorite"}
                loading="lazy"
              />
              <button
                className="fav-remove"
                title="Remove favorite"
                onClick={() => onRemove(f.image_id)}
              >
                &times;
              </button>
              <figcaption title={f.title ?? ""}>
                {f.title ?? "Untitled"}
              </figcaption>
            </figure>
          ))}
        </div>
      )}
    </Card>
  );
}
