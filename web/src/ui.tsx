import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";

// --- Card ------------------------------------------------------------------
export function Card({
  title,
  trailing,
  children,
}: {
  title?: ReactNode;
  trailing?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="card">
      {title !== undefined && (
        <div className="card-head">
          <span>{title}</span>
          <span className="spacer" />
          {trailing}
        </div>
      )}
      <div className="card-body">{children}</div>
    </div>
  );
}

// --- Stat ------------------------------------------------------------------
export function Stat({
  value,
  label,
  tone,
}: {
  value: ReactNode;
  label: string;
  tone?: "success" | "warning" | "danger" | "accent";
}) {
  return (
    <div className="stat">
      <div className={`stat-value ${tone ?? ""}`}>{value}</div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

// --- Button ----------------------------------------------------------------
export function Button({
  children,
  onClick,
  variant,
  disabled,
  busy,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "danger";
  disabled?: boolean;
  busy?: boolean;
}) {
  return (
    <button
      className={`btn ${variant ?? ""}`}
      onClick={onClick}
      disabled={disabled || busy}
    >
      {busy ? <span className="spin" /> : children}
    </button>
  );
}

// --- Toggle ----------------------------------------------------------------
export function Toggle({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
  disabled?: boolean;
}) {
  return (
    <label className="toggle" style={disabled ? { opacity: 0.6 } : undefined}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="track">
        <span className="knob" />
      </span>
      <span className="label">{label}</span>
    </label>
  );
}

// --- Segmented -------------------------------------------------------------
export function Segmented<T extends string | number>({
  value,
  options,
  onChange,
}: {
  value: T;
  options: { value: T; label: string }[];
  onChange: (v: T) => void;
}) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button
          key={String(o.value)}
          className={o.value === value ? "active" : ""}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

// --- Pill ------------------------------------------------------------------
export function Pill({
  children,
  tone,
}: {
  children: ReactNode;
  tone?: "success" | "warning" | "danger";
}) {
  return (
    <span className="pill">
      <span className={`dot ${tone ?? ""}`} />
      {children}
    </span>
  );
}

// --- Callout ---------------------------------------------------------------
export function Callout({
  children,
  tone = "info",
}: {
  children: ReactNode;
  tone?: "info" | "warning" | "danger" | "success";
}) {
  return (
    <div className={`callout ${tone}`}>
      <span className="bar" />
      <div>{children}</div>
    </div>
  );
}

// --- Lightbox --------------------------------------------------------------
interface LightboxState {
  src: string;
  title?: string | null;
}
interface LightboxApi {
  open: (src: string, title?: string | null) => void;
  close: () => void;
}
const LightboxCtx = createContext<LightboxApi>({ open: () => {}, close: () => {} });
export const useLightbox = () => useContext(LightboxCtx);

export function LightboxProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<LightboxState | null>(null);
  const open = useCallback(
    (src: string, title?: string | null) => setState({ src, title }),
    [],
  );
  const close = useCallback(() => setState(null), []);

  useEffect(() => {
    if (!state) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    // Prevent the page behind the dialog from scrolling.
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [state, close]);

  return (
    <LightboxCtx.Provider value={{ open, close }}>
      {children}
      {state && (
        <div
          className="lightbox"
          role="dialog"
          aria-modal="true"
          onClick={close}
        >
          <div className="lightbox-inner" onClick={(e) => e.stopPropagation()}>
            <button
              className="lightbox-close"
              onClick={close}
              aria-label="Close"
            >
              &times;
            </button>
            <img src={state.src} alt={state.title ?? "Enlarged image"} />
            {state.title && (
              <div className="lightbox-caption">{state.title}</div>
            )}
          </div>
        </div>
      )}
    </LightboxCtx.Provider>
  );
}

// --- Toasts ----------------------------------------------------------------
type ToastTone = "error" | "success" | "info";
interface Toast {
  id: number;
  message: string;
  tone: ToastTone;
}
interface ToastApi {
  push: (message: string, tone?: ToastTone) => void;
}
const ToastCtx = createContext<ToastApi>({ push: () => {} });
export const useToast = () => useContext(ToastCtx);

let toastSeq = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((message: string, tone: ToastTone = "info") => {
    const id = toastSeq++;
    setToasts((t) => [...t, { id, message, tone }]);
    window.setTimeout(() => {
      setToasts((t) => t.filter((x) => x.id !== id));
    }, 4200);
  }, []);
  return (
    <ToastCtx.Provider value={{ push }}>
      {children}
      <div className="toasts">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.tone}`}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}
