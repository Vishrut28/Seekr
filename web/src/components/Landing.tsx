import { useRef, useState, type PointerEvent, type RefObject } from "react";
import { MARK_PATH } from "../lib/mark";
import { SearchBox } from "./SearchBox";

/** The start page: the Deccan mark, the name, and one search bar. What to
 *  search appears only once the bar is in use. Searching lifts the page away
 *  and the results take its place. */

const LEAVE_MS = 420;

export function Landing({
  text,
  onText,
  onSearch,
  onFilters,
  recent,
  tries,
  typing,
  onForget,
  inputRef,
}: {
  text: string;
  onText: (value: string) => void;
  /** `paid`: the globe was switched on, so the metered source is asked too */
  onSearch: (query: string, paid: boolean) => void;
  onFilters: () => void;
  recent: string[];
  tries: string[];
  typing: string[];
  onForget: (query: string) => void;
  inputRef: RefObject<HTMLInputElement | null>;
}) {
  const stage = useRef<HTMLDivElement>(null);
  const [leaving, setLeaving] = useState(false);
  const [ripple, setRipple] = useState(0);
  // The paid source costs money, so it is never remembered: switched on for
  // the next search only, and visibly on while it is.
  const [paid, setPaid] = useState(false);

  const search = (query: string) => {
    if (leaving) return;
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (still) return onSearch(query, paid);
    setRipple((r) => r + 1);
    setLeaving(true);
    window.setTimeout(() => onSearch(query, paid), LEAVE_MS);
  };

  // The light follows the cursor, the dot grid shifts against it, and the
  // mark leans toward it. Written straight to CSS variables: a re-render on
  // every mouse move would be wasted work.
  const onMove = (e: PointerEvent) => {
    const el = stage.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width;
    const y = (e.clientY - r.top) / r.height;
    el.style.setProperty("--mx", `${x * 100}%`);
    el.style.setProperty("--my", `${y * 100}%`);
    el.style.setProperty("--px", `${(x - 0.5) * -14}px`);
    el.style.setProperty("--py", `${(y - 0.5) * -14}px`);
    el.style.setProperty("--tilt-y", `${(x - 0.5) * 18}deg`);
    el.style.setProperty("--tilt-x", `${(0.5 - y) * 18}deg`);
  };
  const onLeave = () => {
    stage.current?.style.setProperty("--tilt-x", "0deg");
    stage.current?.style.setProperty("--tilt-y", "0deg");
  };

  return (
    <div className="lp" ref={stage} onPointerMove={onMove} onPointerLeave={onLeave}>
      <div className="lp-aurora" aria-hidden="true">
        <i />
        <i />
        <i />
      </div>
      <div className="lp-grid" aria-hidden="true" />
      <div className="lp-spot" aria-hidden="true" />

      <section className={leaving ? "lp-hero leaving" : "lp-hero"}>
        <div className="lp-emblem" aria-hidden="true">
          <div className="lp-halo" />
          <div className="lp-tile">
            <svg viewBox="0 0 512 512">
              <path className="draw" d={MARK_PATH} pathLength={1} />
              <path className="fill" d={MARK_PATH} fillRule="evenodd" />
            </svg>
          </div>
        </div>
        <h1 className="lp-word" aria-label="Seekr">
          {[..."Seekr"].map((c, i) => (
            <span key={i} style={{ animationDelay: `${0.75 + i * 0.06}s` }} aria-hidden="true">
              {c}
            </span>
          ))}
        </h1>
        <div className="lp-by">
          by Deccan<sup>AI</sup>
        </div>

        <div className="lp-bar">
          <SearchBox
            variant="hero"
            value={text}
            onChange={onText}
            onSubmit={search}
            onEscape={() => onText("")}
            recent={recent}
            tries={tries}
            onForget={onForget}
            inputRef={inputRef}
            typing={typing}
          >
            <button
              type="button"
              className={paid ? "lp-ib on" : "lp-ib"}
              aria-pressed={paid}
              aria-label="Also search the paid source"
              onClick={() => setPaid((p) => !p)}
            >
              <span className="lp-tip">
                {paid
                  ? "Next search also asks the paid source (costs money)"
                  : "Also search the paid source"}
              </span>
              <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   strokeWidth="1.9">
                <circle cx="12" cy="12" r="9" />
                <path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18" />
              </svg>
            </button>
            <button type="button" className="lp-ib" aria-label="Filters" onClick={onFilters}>
              <span className="lp-tip">Filters</span>
              <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   strokeWidth="1.9" strokeLinecap="round">
                <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
                <circle cx="16" cy="7" r="2" />
                <circle cx="10" cy="17" r="2" />
              </svg>
            </button>
          </SearchBox>
          {ripple > 0 && <div key={ripple} className="lp-ripple" aria-hidden="true" />}
        </div>
      </section>
    </div>
  );
}
