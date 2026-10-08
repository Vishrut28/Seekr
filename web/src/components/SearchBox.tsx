import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { Icon } from "../lib/icons";

/** The search box, with what to search dropping down under it: recent
 *  searches first, then things to try. Nothing shows until the box is in
 *  use, so the page itself stays bare. */

interface Item {
  text: string;
  kind: "recent" | "try";
}

const MAX_RECENT = 4;

/** What a suggestion adds to what was typed is set in bold, as search
 *  engines do: the reader sees at a glance how each one differs. */
function Marked({ text, typed }: { text: string; typed: string }) {
  if (!typed) return <>{text}</>;
  const at = text.toLowerCase().indexOf(typed.toLowerCase());
  if (at < 0) return <b>{text}</b>;
  return (
    <>
      {text.slice(0, at)}
      {text.slice(at, at + typed.length)}
      <b>{text.slice(at + typed.length)}</b>
    </>
  );
}

const ClockIcon = () => (
  <svg className="ic" width="15" height="15" viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2" strokeLinecap="round">
    <circle cx="12" cy="12" r="9" />
    <path d="M12 7v5l3 2" />
  </svg>
);

const SparkIcon = () => (
  <svg className="ic" width="15" height="15" viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2" strokeLinejoin="round">
    <path d="M12 3l1.9 5.3L19 10l-5.1 1.7L12 17l-1.9-5.3L5 10l5.1-1.7z" />
  </svg>
);

const GoIcon = () => (
  <svg className="go" width="13" height="13" viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
    <path d="M5 12h14M13 6l6 6-6 6" />
  </svg>
);

export const LensIcon = () => (
  <svg className="lens" width="20" height="20" viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
    <circle cx="11" cy="11" r="7" />
    <path d="M20 20l-3.5-3.5" />
  </svg>
);

/** The placeholder types out real searches, one after another. */
function useTypewriter(lines: string[], active: boolean): string {
  const [shown, setShown] = useState("");
  useEffect(() => {
    if (!active || !lines.length) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      setShown(lines[0]);
      return;
    }
    let line = 0;
    let at = 0;
    let erasing = false;
    let timer = 0;
    const step = () => {
      const text = lines[line];
      let wait = erasing ? 28 : 55 + Math.random() * 50;
      if (!erasing && at === text.length) {
        erasing = true;
        wait = 1900;
      } else if (erasing && at === 0) {
        erasing = false;
        line = (line + 1) % lines.length;
        wait = 400;
      } else {
        at += erasing ? -1 : 1;
        setShown(text.slice(0, at));
      }
      timer = window.setTimeout(step, wait);
    };
    timer = window.setTimeout(step, 1600);
    return () => window.clearTimeout(timer);
  }, [lines, active]);
  return shown;
}

export function SearchBox({
  variant,
  value,
  onChange,
  onSubmit,
  onEscape,
  recent,
  tries,
  onForget,
  inputRef,
  typing = [],
  children,
}: {
  /** "hero" is the start page's big glass bar; "top" sits in the topbar */
  variant: "hero" | "top";
  value: string;
  onChange: (value: string) => void;
  onSubmit: (query: string) => void;
  /** Escape with nothing left to close */
  onEscape?: () => void;
  recent: string[];
  tries: string[];
  onForget: (query: string) => void;
  inputRef: RefObject<HTMLInputElement | null>;
  /** example searches the empty hero bar types out */
  typing?: string[];
  /** the buttons inside the bar, after the input */
  children?: ReactNode;
}) {
  const [focused, setFocused] = useState(false);
  // Escape closes the list; typing again opens it
  const [dismissed, setDismissed] = useState(false);
  const [hi, setHi] = useState(-1);
  const list = useRef<HTMLDivElement>(null);
  const typed = value.trim();

  const fits = (s: string) => !typed || s.toLowerCase().includes(typed.toLowerCase());
  const shownRecent = recent
    .filter((s) => fits(s) && s.toLowerCase() !== typed.toLowerCase())
    .slice(0, MAX_RECENT);
  const shownTries = tries
    .filter(
      (s) =>
        fits(s) &&
        s.toLowerCase() !== typed.toLowerCase() &&
        !shownRecent.some((r) => r.toLowerCase() === s.toLowerCase()),
    )
    .slice(0, typed ? 3 : 2);
  const items: Item[] = [
    ...shownRecent.map((text) => ({ text, kind: "recent" as const })),
    ...shownTries.map((text) => ({ text, kind: "try" as const })),
  ];
  const open = focused && !dismissed && items.length > 0;

  // a new list starts with nothing highlighted
  const signature = items.map((i) => i.text).join("\n");
  useEffect(() => setHi(-1), [signature]);

  const placeholder = useTypewriter(typing, variant === "hero" && !value);

  const choose = (text: string) => {
    setDismissed(true);
    setHi(-1);
    inputRef.current?.blur();
    onSubmit(text);
  };

  // the highlight glides between rows rather than jumping
  const [pill, setPill] = useState<{ top: number; height: number } | null>(null);
  useEffect(() => {
    const row = list.current?.querySelector<HTMLElement>(`[data-i="${hi}"]`);
    setPill(row ? { top: row.offsetTop, height: row.offsetHeight } : null);
  }, [hi, open]);

  let n = 0;
  const row = (item: Item) => {
    const i = n++;
    return (
      <div
        key={item.kind + item.text}
        data-i={i}
        className={i === hi ? "srow hl" : "srow"}
        style={{ animationDelay: `${i * 35}ms` }}
        onPointerEnter={() => setHi(i)}
        onPointerDown={(e) => {
          // keep the focus in the box, or the list closes under the click
          e.preventDefault();
          choose(item.text);
        }}
      >
        {item.kind === "recent" ? <ClockIcon /> : <SparkIcon />}
        <span className="t">
          <Marked text={item.text} typed={typed} />
        </span>
        {item.kind === "recent" && (
          <button
            className="forget"
            title="Remove from recent searches"
            aria-label={`Remove ${item.text} from recent searches`}
            onPointerDown={(e) => {
              e.preventDefault();
              e.stopPropagation();
              onForget(item.text);
            }}
          >
            ✕
          </button>
        )}
        <GoIcon />
      </div>
    );
  };

  const input = (
    <input
      ref={inputRef}
      className={variant === "top" ? "search" : undefined}
      aria-label="Search people, skills, places"
      placeholder={variant === "top" ? "Search people, skills, organizations…" : undefined}
      spellCheck={false}
      autoComplete="off"
      role="combobox"
      aria-expanded={open}
      aria-autocomplete="list"
      value={value}
      onChange={(e) => {
        setDismissed(false);
        onChange(e.target.value);
      }}
      onFocus={() => {
        setFocused(true);
        setDismissed(false);
      }}
      onBlur={() => setFocused(false)}
      onKeyDown={(e) => {
        if (e.key === "ArrowDown" && open) {
          e.preventDefault();
          setHi((h) => Math.min(h + 1, items.length - 1));
        } else if (e.key === "ArrowUp" && open) {
          e.preventDefault();
          setHi((h) => Math.max(h - 1, -1));
        } else if (e.key === "Enter") {
          e.preventDefault();
          if (open && hi >= 0) choose(items[hi].text);
          else if (typed) choose(typed);
        } else if (e.key === "Escape") {
          if (open) setDismissed(true);
          else onEscape?.();
        }
      }}
    />
  );

  const dropdown = (
    <div className={open ? "sugg open" : "sugg"} role="listbox" aria-hidden={!open}>
      {open && (
        <>
          <div className="sugg-sep" />
          <div className="sugg-list" ref={list}>
            {pill && (
              <div
                className="sugg-pill"
                style={{ transform: `translateY(${pill.top}px)`, height: pill.height }}
              />
            )}
            {shownRecent.length > 0 && <div className="sugg-lab">Recent</div>}
            {items.filter((i) => i.kind === "recent").map(row)}
            {shownTries.length > 0 && <div className="sugg-lab">Try</div>}
            {items.filter((i) => i.kind === "try").map(row)}
          </div>
          <div className="sugg-foot">
            <span><kbd>↑</kbd><kbd>↓</kbd>move</span>
            <span><kbd>↵</kbd>search</span>
            <span><kbd>esc</kbd>close</span>
          </div>
        </>
      )}
    </div>
  );

  if (variant === "top") {
    return (
      <div className={open ? "searchwrap suggesting" : "searchwrap"}>
        <Icon.search />
        {input}
        <span className="kbd">/</span>
        {dropdown}
      </div>
    );
  }

  return (
    <div className={open ? "herobar-wrap open" : "herobar-wrap"}>
      <div className="herobar-bloom" />
      <div className="herobar-ring" />
      <div className="herobar">
        <LensIcon />
        {input}
        {!value && (
          <div className="herobar-ph" aria-hidden="true">
            Search {placeholder ? <span>{placeholder}</span> : "people, skills, places"}
            <i className="caret" />
          </div>
        )}
        <span className="herobar-kbd">/</span>
        {children}
      </div>
      {dropdown}
    </div>
  );
}
