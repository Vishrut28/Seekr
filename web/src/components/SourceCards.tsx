/* One card per live source, filled as each one answers.
 *
 * A live search asks several providers in turn and can take half a minute.
 * A single spinner for the whole thing hides which source is slow, which was
 * skipped and why, and whether anything is being kept — so the cards report
 * each source separately, from the event stream the API emits as it works. */
import { useEffect, useRef, useState } from "react";
import { getToken } from "../api/client";

export type SourceState = {
  state: "waiting" | "searching" | "done" | "cached" | "skipped" | "failed";
  found?: number;
  stored?: number;
  people?: number;
  reason?: string;
  query?: string;
};

const LABEL: Record<string, string> = {
  openalex: "OpenAlex",
  semanticscholar: "Semantic Scholar",
  dblp: "dblp",
  orcid: "ORCID",
  wikidata: "Wikidata",
  github: "GitHub",
  huggingface: "Hugging Face",
  stackoverflow: "Stack Overflow",
  web: "Web",
  exa: "Exa",
};

export function useLiveSearch() {
  const [sources, setSources] = useState<Record<string, SourceState>>({});
  const [order, setOrder] = useState<string[]>([]);
  const [running, setRunning] = useState(false);
  const ctrl = useRef<AbortController | null>(null);

  useEffect(() => () => ctrl.current?.abort(), []);

  const start = async (q: string, limit: number, onDone: (payload: any) => void) => {
    ctrl.current?.abort();
    setSources({});
    setOrder([]);
    setRunning(true);
    const controller = new AbortController();
    ctrl.current = controller;

    // fetch rather than EventSource: EventSource cannot set headers, which
    // would mean putting the bearer token in the URL, where it lands in
    // server logs and browser history.
    try {
      const res = await fetch(`/v1/query/stream?q=${encodeURIComponent(q)}&limit=${limit}`, {
        // no header at all when there is no token, as everywhere else: an
        // empty "Bearer " is a malformed credential, not the absence of one
        headers: getToken() ? { Authorization: `Bearer ${getToken()}` } : undefined,
        signal: controller.signal,
      });
      if (!res.ok || !res.body) throw new Error(`stream failed (${res.status})`);

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        // events are separated by a blank line; keep any partial tail
        const parts = buffer.split("\n\n");
        buffer = parts.pop() || "";
        for (const part of parts) {
          const line = part.split("\n").find((l) => l.startsWith("data: "));
          if (!line) continue;
          const msg = JSON.parse(line.slice(6));
          if (msg.type === "plan") {
            setOrder(msg.sources);
            setSources(Object.fromEntries(
              msg.sources.map((n: string) => [n, { state: "waiting" as const }])));
          } else if (msg.type === "source") {
            const { source, state, ...facts } = msg;
            setSources((prev) => ({ ...prev, [source]: { state, ...facts } }));
          } else if (msg.type === "parsed" || msg.type === "results" || msg.type === "error") {
            // parsed carries the filters actually applied; without it the page
            // keeps showing the previous query's pills while the new one runs
            onDone(msg);
          }
        }
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") onDone({ type: "error", detail: String(err) });
    } finally {
      setRunning(false);
    }
  };

  /** Abandon a run in progress — the question it answers is gone. */
  const stop = () => {
    ctrl.current?.abort();
    ctrl.current = null;
    setRunning(false);
    setSources({});
    setOrder([]);
  };

  return { sources, order, running, start, stop };
}

export function SourceCards({ order, sources, running }: {
  order: string[];
  sources: Record<string, SourceState>;
  running?: boolean;
}) {
  if (!order.length) return null;
  return (
    <div className="srccards-wrap">
      <div className="srccards">
        {order.map((name) => {
          const s = sources[name] || { state: "waiting" as const };
          return (
            <div key={name} className={`srccard ${s.state}`}>
              <div className="srchead">
                <span className="srcname">{LABEL[name] || name}</span>
                {s.state === "searching" && <span className="srcspin" aria-label="searching" />}
              </div>
              <div className="srcbody">{describe(s)}</div>
            </div>
          );
        })}
      </div>
      {running && (
        <div className="live-hint">
          <span className="live-tag">Live</span>
          Each source answers for itself
        </div>
      )}
    </div>
  );
}

function describe(s: SourceState): string {
  switch (s.state) {
    case "waiting": return "queued";
    case "searching": return "searching…";
    case "done": return s.stored
      ? `${s.found} found · ${s.stored} kept`
      : `${s.found ?? 0} found`;
    case "cached": return `${s.people ?? 0} from an earlier search`;
    case "skipped": return s.reason || "skipped";
    case "failed": return s.reason ? `unavailable (${s.reason})` : "unavailable";
    default: return "";
  }
}
