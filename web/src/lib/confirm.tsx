import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

/** "Are you sure?", asked in the page rather than in the browser's bare
 *  confirm box, which could not say which of two same-named records it
 *  meant without cramming ids into one run of text. */

export interface ConfirmOptions {
  title: string;
  /** what will happen, one paragraph per entry */
  body?: string[];
  /** the exact thing being acted on: names, ids, the reason it was queued */
  detail?: ReactNode;
  confirmLabel: string;
  /** "danger" for what cannot be undone: drawn in red */
  tone?: "danger" | "default";
}

type Ask = (options: ConfirmOptions) => Promise<boolean>;

const ConfirmContext = createContext<Ask>(async () => false);

/** `const confirm = useConfirm(); if (!(await confirm({...}))) return;` */
export function useConfirm(): Ask {
  return useContext(ConfirmContext);
}

interface Pending extends ConfirmOptions {
  resolve: (ok: boolean) => void;
}

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null);

  const ask = useCallback<Ask>(
    (options) =>
      new Promise<boolean>((resolve) => {
        setPending({ ...options, resolve });
      }),
    [],
  );

  return (
    <ConfirmContext.Provider value={ask}>
      {children}
      {pending && (
        <ConfirmDialog
          key={pending.title}
          options={pending}
          onAnswer={(ok) => {
            pending.resolve(ok);
            setPending(null);
          }}
        />
      )}
    </ConfirmContext.Provider>
  );
}

function ConfirmDialog({
  options,
  onAnswer,
}: {
  options: ConfirmOptions;
  onAnswer: (ok: boolean) => void;
}) {
  const danger = options.tone === "danger";
  const cancel = useRef<HTMLButtonElement>(null);
  const accept = useRef<HTMLButtonElement>(null);
  const card = useRef<HTMLDivElement>(null);
  // read through a ref, so a new callback does not re-run the focus setup
  const answer = useRef(onAnswer);
  answer.current = onAnswer;

  useEffect(() => {
    // focus goes back where it came from once this is answered
    const before = document.activeElement as HTMLElement | null;
    // Cancel holds the focus, so a stray Enter never acts: everything
    // asked here writes to the graph, and most of it cannot be taken back
    cancel.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        answer.current(false);
      } else if (e.key === "Tab") {
        // keep the focus inside: two buttons, round and round
        const order = [cancel.current, accept.current].filter(Boolean) as HTMLElement[];
        const at = order.indexOf(document.activeElement as HTMLElement);
        e.preventDefault();
        order[(at + (e.shiftKey ? -1 : 1) + order.length) % order.length]?.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      before?.focus?.();
    };
  }, []);

  return createPortal(
    <div
      className="cfm-backdrop"
      onPointerDown={(e) => {
        if (!card.current?.contains(e.target as Node)) onAnswer(false);
      }}
    >
      <div
        ref={card}
        className={danger ? "cfm danger" : "cfm"}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="cfm-title"
        aria-describedby="cfm-body"
      >
        <div className="cfm-icon" aria-hidden="true">
          {danger ? (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 9v4M12 17h.01" />
              <path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
            </svg>
          ) : (
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="9" />
              <path d="M12 8h.01M11 12h1v4h1" />
            </svg>
          )}
        </div>
        <h3 id="cfm-title">{options.title}</h3>
        {options.detail && <div className="cfm-detail">{options.detail}</div>}
        <div id="cfm-body" className="cfm-body">
          {(options.body || []).map((p) => (
            <p key={p}>{p}</p>
          ))}
        </div>
        <div className="cfm-actions">
          <button ref={cancel} className="btn" onClick={() => onAnswer(false)}>
            Cancel
          </button>
          <button
            ref={accept}
            className={danger ? "btn cfm-go danger-solid" : "btn primary cfm-go"}
            onClick={() => onAnswer(true)}
          >
            {options.confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
