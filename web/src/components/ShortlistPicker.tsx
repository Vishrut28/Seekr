import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, apiSend, errorMessage, isUnauthorized } from "../api/client";
import type { ShortlistDetail } from "../types";

/** "Save to which shortlist?", asked in the page rather than in the browser's
 *  bare prompt box: the lists to pick from, which of them already hold this
 *  person, and a field to start a new one. */

interface ListRow {
  id: number;
  name: string;
  members: number;
  /** this person is already on it */
  has: boolean;
}

const WIDTH = 280;

export function ShortlistPicker({
  anchor,
  personId,
  query,
  onClose,
  onSaved,
}: {
  /** the button it hangs from */
  anchor: HTMLElement;
  personId: string;
  query: string;
  onClose: () => void;
  /** the list the person is now on, and whether they were added just now */
  onSaved: (listName: string, added: boolean) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const field = useRef<HTMLInputElement>(null);
  const [lists, setLists] = useState<ListRow[] | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [place, setPlace] = useState<{ top: number; left: number; up: boolean } | null>(null);

  // Hung from the button, in a layer of its own: the results table scrolls
  // sideways, and anything inside it is clipped at its edge.
  useLayoutEffect(() => {
    const r = anchor.getBoundingClientRect();
    const height = box.current?.offsetHeight || 240;
    const up = r.bottom + 8 + height > window.innerHeight && r.top - 8 - height > 0;
    const left = Math.max(8, Math.min(r.right - WIDTH, window.innerWidth - WIDTH - 8));
    setPlace({ top: up ? r.top - 8 - height : r.bottom + 8, left, up });
  }, [anchor, lists]);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const res = await api<{ shortlists: { id: number; name: string; members: number }[] }>(
          "/v1/shortlists",
        );
        // which lists already hold this person, so saving twice is visible
        // before it is tried rather than reported after
        const details = await Promise.all(
          res.shortlists.slice(0, 30).map((l) =>
            api<ShortlistDetail>(`/v1/shortlists/${l.id}`).catch(() => null),
          ),
        );
        const holding = new Set(
          details
            .filter((d): d is ShortlistDetail => Boolean(d))
            .filter((d) => d.members.some((m) => m.person_id === personId))
            .map((d) => d.id),
        );
        if (!live) return;
        setLists(
          res.shortlists.map((l) => ({ ...l, members: l.members ?? 0, has: holding.has(l.id) })),
        );
      } catch (e) {
        if (!live) return;
        setLists([]);
        if (!isUnauthorized(e)) setError("Could not load your shortlists: " + errorMessage(e));
      }
    })();
    return () => {
      live = false;
    };
  }, [personId]);

  // a reader with no lists yet goes straight to naming the first one
  useEffect(() => {
    if (lists && lists.length === 0) field.current?.focus();
    else if (lists) box.current?.querySelector<HTMLButtonElement>(".slp-row")?.focus();
  }, [lists]);

  // Escape, a click elsewhere, or the page moving under it, closes it
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        anchor.focus();
      }
    };
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!box.current?.contains(t) && !anchor.contains(t)) onClose();
    };
    const onMove = (e: Event) => {
      // its own list scrolling is not the page moving
      if (e.target instanceof Node && box.current?.contains(e.target)) return;
      onClose();
    };
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [anchor, onClose]);

  const addTo = async (id: number, listName: string) => {
    setBusy(listName);
    setError(null);
    try {
      const res = await apiSend<{ added: boolean }>(`/v1/shortlists/${id}/members`, "POST", {
        person_id: personId,
        query,
      });
      setDone(listName);
      setLists((ls) =>
        (ls || []).map((l) =>
          l.id === id ? { ...l, has: true, members: l.members + (res.added ? 1 : 0) } : l,
        ),
      );
      onSaved(listName, res.added);
      // long enough to see the tick land, then out of the way
      window.setTimeout(onClose, 650);
    } catch (e) {
      if (!isUnauthorized(e)) setError("Could not save: " + errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const create = async () => {
    const n = name.trim();
    if (!n) {
      setError("Name the new shortlist first.");
      field.current?.focus();
      return;
    }
    setBusy(n);
    setError(null);
    try {
      // returns the existing list if one already has this name
      const list = await apiSend<{ id: number; name: string; created: boolean }>(
        "/v1/shortlists",
        "POST",
        { name: n },
      );
      if (list.created) {
        setLists((ls) => [...(ls || []), { id: list.id, name: list.name, members: 0, has: false }]);
      }
      setName("");
      await addTo(list.id, list.name);
    } catch (e) {
      setBusy(null);
      if (!isUnauthorized(e)) setError("Could not create it: " + errorMessage(e));
    }
  };

  return createPortal(
    <div
      ref={box}
      className={place?.up ? "slp up" : "slp"}
      role="dialog"
      aria-label="Save to a shortlist"
      style={{ top: place?.top ?? -9999, left: place?.left ?? -9999, width: WIDTH }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="slp-head">Save to shortlist</div>
      <div className="slp-list">
        {lists === null ? (
          <div className="slp-empty">
            <span className="slp-spin" /> Loading your shortlists…
          </div>
        ) : lists.length === 0 ? (
          <div className="slp-empty">No shortlists yet. Name your first one below.</div>
        ) : (
          lists.map((l) => (
            <button
              key={l.id}
              className={l.has ? "slp-row has" : "slp-row"}
              disabled={Boolean(busy)}
              onClick={() => addTo(l.id, l.name)}
              title={l.has ? "Already on this shortlist" : `Add to ${l.name}`}
            >
              <span className={l.has || done === l.name ? "slp-check on" : "slp-check"}>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="3" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M5 12l5 5L20 7" />
                </svg>
              </span>
              <span className="slp-name">{l.name}</span>
              <span className="slp-n">
                {busy === l.name ? "Saving…" : done === l.name ? "Saved" : l.members}
              </span>
            </button>
          ))
        )}
      </div>
      <form
        className="slp-new"
        onSubmit={(e) => {
          e.preventDefault();
          create();
        }}
      >
        <input
          ref={field}
          value={name}
          maxLength={200}
          placeholder="New shortlist"
          aria-label="New shortlist name"
          onChange={(e) => {
            setName(e.target.value);
            setError(null);
          }}
        />
        <button className="btn sm primary" type="submit" disabled={Boolean(busy)}>
          Create
        </button>
      </form>
      {error && <div className="slp-error">{error}</div>}
    </div>,
    document.body,
  );
}
