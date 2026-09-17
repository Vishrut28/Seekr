import { useCallback, useEffect, useRef, useState } from "react";
import { SourceCards, useLiveSearch } from "../components/SourceCards";
import { api, apiSend, errorMessage, isUnauthorized } from "../api/client";
import {
  EMPTY_FILTERS,
  EMPTY_FLAGS,
  Filters,
  filterParams,
  type FilterFlags,
  type FilterState,
} from "../components/Filters";
import { Banner, EmptyState, Loading } from "../components/EmptyState";
import { ResultsTable } from "../components/ResultsTable";
import { Shell } from "../components/Shell";
import { fmt } from "../lib/format";
import { useWorking } from "../lib/hooks";
import { Icon } from "../lib/icons";
import type {
  DiscoverySuggestion,
  FacetResponse,
  PersonSummary,
  QueryResponse,
} from "../types";

const EXAMPLES = [
  "machine learning at University of Toronto",
  "deep learning, top 20",
  "product designers at Swiggy",
];

const RECENT_KEY = "seekr_recent";
const LAST_QUERY_KEY = "seekr_q";

function readRecent(): string[] {
  try {
    return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]");
  } catch {
    return [];
  }
}

type Mode = "query" | "filters";

export function Search() {
  const [text, setText] = useState(() => sessionStorage.getItem(LAST_QUERY_KEY) || "");
  const [rows, setRows] = useState<PersonSummary[]>([]);
  const [data, setData] = useState<QueryResponse | null>(null);
  const [mode, setMode] = useState<Mode>("query");
  const [ranQuery, setRanQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recent, setRecent] = useState<string[]>(readRecent);
  const [trending, setTrending] = useState<string[]>(EXAMPLES);
  const [values, setValues] = useState<FilterState>(EMPTY_FILTERS);
  const [flags, setFlags] = useState<FilterFlags>(EMPTY_FLAGS);

  const input = useRef<HTMLInputElement>(null);
  // read inside callbacks that must not be rebuilt on every keystroke
  const textRef = useRef(text);
  textRef.current = text;

  useWorking(Boolean(loading));

  const rememberQuery = (q: string) => {
    const list = [q, ...readRecent().filter((x) => x !== q)].slice(0, 8);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
    setRecent(list);
  };

  const live = useLiveSearch();

  const runQuery = useCallback(async (discover?: boolean, from?: number) => {
    const q = textRef.current.trim();
    if (!q) return;
    sessionStorage.setItem(LAST_QUERY_KEY, q);
    rememberQuery(q);
    const paging = typeof from === "number" && from > 0;
    setMode("query");
    setRanQuery(q);
    setError(null);
    if (paging) {
      setLoadingMore(true);
    } else {
      setRows([]);
      setOffset(0);
      setLoading(discover ? "Querying live sources…" : "Searching…");
    }
    if (discover && !paging) {
      // stream it, so each source reports for itself while the work happens
      live.start(q, 50, (msg: any) => {
        if (msg.type === "error") { setLoading(null); setError(msg.detail); return; }
        if (msg.type === "parsed") {
          // show what this query matched, not what the last one did
          setData((prev) => ({
            ...(prev as any),
            applied_filters: msg.applied_filters,
            applied_clauses: msg.applied_clauses,
            unmatched_terms: msg.unmatched_terms,
            corrections: msg.corrections,
            rewrites: msg.rewrites,
            protected_terms: msg.protected_terms,
            exclusions: msg.exclusions,
            require_all_orgs: msg.require_all_orgs,
            min_publications: msg.min_publications,
            min_citations: msg.min_citations,
            results: [],
          }) as QueryResponse);
          setRows([]);
          return;
        }
        setLoading(null);
        setData((prev) => ({ ...(prev as any), ...msg }) as QueryResponse);
        setRows(msg.results || []);
        setOffset((msg.results || []).length);
      });
      return;
    }
    try {
      const params = new URLSearchParams({ q });
      if (paging) params.set("offset", String(from));
      if (discover) params.set("discover", "true");
      const res = await api<QueryResponse>(`/v1/query?${params}`);
      setData(res);
      setRows((prev) => (paging ? [...prev, ...res.results] : res.results));
      setOffset(
        res.next_offset ?? (paging ? from + res.results.length : res.results.length),
      );
    } catch (e) {
      if (!isUnauthorized(e)) setError(errorMessage(e));
    } finally {
      setLoading(null);
      setLoadingMore(false);
    }
  }, []);

  const runFilters = useCallback(
    async (from?: number) => {
      // The search box holds a question; on this endpoint `q` is a NAME filter.
      // Sending a whole sentence there matches nobody and silently empties the
      // result, so only a short, name-shaped value is passed through.
      const q = textRef.current.trim();
      const params = filterParams(values, flags);
      if (q && q.split(/\s+/).length <= 3) params.set("q", q);
      const paging = typeof from === "number" && from > 0;
      setMode("filters");
      setRanQuery(q);
      setError(null);
      if (paging) {
        params.set("offset", String(from));
        setLoadingMore(true);
      } else {
        setRows([]);
        setOffset(0);
        setLoading("Filtering…");
      }
      params.set("limit", "50");
      try {
        const res = await api<QueryResponse>(`/v1/persons?${params}`);
        setData(res);
        setRows((prev) => (paging ? [...prev, ...res.results] : res.results));
        setOffset(
          res.next_offset ?? (paging ? from + res.results.length : res.results.length),
        );
      } catch (e) {
        if (!isUnauthorized(e)) setError(errorMessage(e));
      } finally {
        setLoading(null);
        setLoadingMore(false);
      }
    },
    [values, flags],
  );

  const loadMore = () =>
    mode === "filters" ? runFilters(offset) : runQuery(false, offset);

  // Trending is drawn from the corpus itself — the roles most people in Seekr
  // actually carry. Roles only: skills skew to whatever the corpus happens to
  // hold, and the top ones are particle physics topics, which is not a job
  // anyone searches for. Job titles are what people actually look for.
  useEffect(() => {
    let live = true;
    api<FacetResponse>("/v1/facets?field=role&limit=8")
      .then((d) => {
        const chips = (d.values || [])
          .map((v) => v.value)
          .filter(Boolean)
          .slice(0, 6);
        if (live && chips.length) setTrending(chips);
      })
      .catch(() => {
        /* keep the static examples if facets are unavailable */
      });
    return () => {
      live = false;
    };
  }, []);

  // A query carried over from the last visit is answered on arrival.
  const openedWith = useRef(sessionStorage.getItem(LAST_QUERY_KEY) || "");
  useEffect(() => {
    if (openedWith.current) runQuery();
  }, [runQuery]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = document.activeElement;
      const typing = el ? /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) : false;
      if ((e.key === "/" && !typing) || ((e.metaKey || e.ctrlKey) && e.key === "k")) {
        e.preventDefault();
        input.current?.focus();
        input.current?.select();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const useExample = (value: string) => {
    setText(value);
    textRef.current = value;
    runQuery();
  };

  const clearFilters = () => {
    setValues(EMPTY_FILTERS);
    setFlags(EMPTY_FLAGS);
    setRows([]);
    setData(null);
  };

  const topbar = (
    <div className="searchrow">
      <div className="searchwrap">
        <Icon.search />
        <input
          ref={input}
          className="search"
          placeholder="Search people, skills, organizations…"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") runQuery();
            if (e.key === "Escape") {
              setText("");
              e.currentTarget.blur();
            }
          }}
        />
        <span className="kbd">/</span>
      </div>
      <button className="btn primary" onClick={() => runQuery()}>
        Search
      </button>
      <button
        className="btn"
        title="Also query live sources"
        onClick={() => runQuery(true)}
      >
        Live
      </button>
    </div>
  );

  const isSearching = Boolean(loading) || live.running;
  const showResults = Boolean(data) || rows.length > 0 || live.running;

  return (
    <Shell topbar={topbar}>
      <div className="search-page">
        <div className="search-controls">
          <div className="examples">
            <em>Trending</em>
            {trending.map((x) => (
              <button key={x} className="chipbtn" onClick={() => useExample(x)}>
                {x}
              </button>
            ))}
          </div>
          {recent.length > 0 && (
            <div className="examples">
              <em>Recent</em>
              {recent.map((x) => (
                <button key={x} className="chipbtn" onClick={() => useExample(x)}>
                  {x}
                </button>
              ))}
              <button
                className="chipbtn muted"
                onClick={() => {
                  localStorage.removeItem(RECENT_KEY);
                  setRecent([]);
                }}
              >
                clear
              </button>
            </div>
          )}

          <Filters
            values={values}
            flags={flags}
            onChange={setValues}
            onFlags={setFlags}
            onApply={() => runFilters()}
            onClear={clearFilters}
          />

          {/* what each source is doing, while it does it */}
          <SourceCards order={live.order} sources={live.sources} running={live.running} />
        </div>

        <div className="results-zone">
          {error ? (
            <Banner>{error}</Banner>
          ) : isSearching && !showResults ? (
            <Loading message={loading || "Querying live sources…"} inline />
          ) : showResults ? (
            <Results
              data={data}
              rows={rows}
              query={ranQuery}
              mode={mode}
              loadingMore={loadingMore}
              searching={isSearching && rows.length === 0}
              onLoadMore={loadMore}
              onDiscover={() => runQuery(true)}
            />
          ) : null}
        </div>
      </div>
    </Shell>
  );
}

function Results({
  data,
  rows,
  query,
  mode,
  loadingMore,
  searching,
  onLoadMore,
  onDiscover,
}: {
  data: QueryResponse | null;
  rows: PersonSummary[];
  query: string;
  mode: Mode;
  loadingMore: boolean;
  searching?: boolean;
  onLoadMore: () => void;
  onDiscover: () => void;
}) {
  const f = data?.applied_filters;
  const unmatched = data?.unmatched_terms || [];
  const notFound = data?.not_found || [];
  const corrections = data?.corrections || [];
  const suggestions = data?.discovery_suggestions || [];
  const total = data?.total_matches ?? rows.length;
  const dropped = notFound.map((d) => d.term);

  const pills: { kind: string; value: string }[] = [
    ...(f?.skills || []).map((v) => ({ kind: "skill", value: v })),
    ...(f?.skill_patterns || []).map((v) => ({ kind: "matches", value: v })),
    ...(f?.organizations || []).map((v) => ({ kind: "org", value: v })),
    ...(f?.locations || []).map((v) => ({ kind: "place", value: v })),
    ...(f?.countries || []).map((v) => ({ kind: "country", value: v })),
    ...(f?.name_terms || []).map((v) => ({ kind: "name", value: v })),
    ...(f?.roles || []).map((v) => ({ kind: "role", value: v })),
    ...(data?.exclusions || [])
      .filter((e) => e.as.length > 0)
      .map((e) => ({ kind: "excluding", value: e.term })),
    ...(data?.min_publications
      ? [{ kind: "papers ≥", value: fmt(data.min_publications) }]
      : []),
    ...(data?.min_citations ? [{ kind: "citations ≥", value: fmt(data.min_citations) }] : []),
  ];
  // an exclusion nothing matched removes nobody — say so, or it looks applied
  const unappliedExclusions = (data?.exclusions || []).filter((e) => e.as.length === 0);
  const rewrites = data?.rewrites || [];
  const protectedTerms = data?.protected_terms || [];

  const missingTerms = [...new Set([...unmatched, ...dropped])];

  return (
    <>
      <div className="meta">
        <div className="count">
          {rows.length > 0 ? (
            <>
              <b>{fmt(rows.length)}</b> of {fmt(Math.max(total, rows.length))} matching
            </>
          ) : searching ? (
            <>Searching…</>
          ) : data ? (
            <>No matches</>
          ) : null}
        </div>
        {pills.length > 0 && (
          <div className="pills">
            {pills.map((p) => (
              <span key={p.kind + p.value} className="pill">
                <b>{p.kind}</b>
                {p.value}
              </span>
            ))}
          </div>
        )}
      </div>

      {missingTerms.length > 0 && (
        <Banner kind="warn">
          <b>{missingTerms.join(", ")}</b> {missingTerms.length === 1 ? "was" : "were"} not
          applied — nothing in Seekr matches{" "}
          {missingTerms.length === 1 ? "that term" : "those terms"} yet
          {rows.length
            ? `, so these ${fmt(Math.max(total, rows.length))} results ignore ${
                missingTerms.length === 1 ? "it" : "them"
              }`
            : ""}
          .{" "}
          <button className="btn sm" onClick={onDiscover}>
            Search paid sources too
          </button>
        </Banner>
      )}

      {data?.require_all_orgs && (f?.organizations?.length || 0) > 1 && (
        <Banner kind="info">People affiliated with every one of those organizations, not any of them.</Banner>
      )}

      {protectedTerms.length > 0 && (
        <Banner kind="warn">
          <b>{protectedTerms.map((p) => p.term).join(", ")}</b> {protectedTerms.length === 1 ? "was" : "were"}{" "}
          not applied — Seekr does not select people by{" "}
          {[...new Set(protectedTerms.map((p) => p.attribute))].join(", ")}.
        </Banner>
      )}

      {unappliedExclusions.length > 0 && (
        <Banner kind="warn">
          <b>{unappliedExclusions.map((e) => e.term).join(", ")}</b> excluded nobody — nothing in
          Seekr matches {unappliedExclusions.length === 1 ? "that term" : "those terms"}.
        </Banner>
      )}

      {rewrites.length > 0 && (
        <Banner kind="info">
          Searched{" "}
          {rewrites.map((r, i) => (
            <span key={r.typed + i}>
              {i > 0 && "; "}
              <b>{Array.isArray(r.searched) ? r.searched.join(", ") : r.searched}</b> for{" "}
              <i>{r.typed}</i>
            </span>
          ))}
          .
        </Banner>
      )}

      {/* A corrected spelling must be visible, or the answer quietly belongs to
          a different question than the one that was asked. */}
      {corrections.length > 0 && (
        <Banner>
          Showing results for{" "}
          {corrections.map((c, i) => (
            <span key={c.matched}>
              {i > 0 && ", "}
              <b>{c.matched}</b>
            </span>
          ))}{" "}
          — you typed{" "}
          {corrections.map((c, i) => (
            <span key={c.typed}>
              {i > 0 && ", "}
              <i>{c.typed}</i>
            </span>
          ))}
          .
        </Banner>
      )}

      {data?.storage === "read-only" &&
        data.stored_from_live === 0 &&
        suggestions.length > 0 && (
          <Banner kind="warn">
            This deployment reads a fixed snapshot, so people found live are shown but
            not saved. Point <code>RIP_DATABASE_URL</code> at a writable database to let
            the graph grow here.
          </Banner>
        )}

      {searching && rows.length === 0 ? (
        <Loading message="Querying live sources…" inline />
      ) : rows.length > 0 ? (
        <ResultsTable
          people={rows}
          query={query}
          hasMore={data?.has_more}
          loadingMore={loadingMore}
          onLoadMore={onLoadMore}
        />
      ) : data?.matched_nothing ? (
        <EmptyState
          title="No filters could be applied"
          body={data.explanation || "None of those terms exist in the corpus yet."}
        >
          <button className="btn primary" onClick={onDiscover}>
            Search live sources
          </button>
        </EmptyState>
      ) : suggestions.length === 0 ? (
        <EmptyState
          title="No matches"
          body={
            data?.empty_reason?.message || "No one in the corpus matches these filters."
          }
        >
          {(data?.empty_reason?.each_filter_alone || []).length > 0 && (
            <ul className="whylist">
              {data?.empty_reason?.each_filter_alone?.map((a) => (
                <li key={a.filter}>
                  <code>
                    {a.filter}
                    {a.value === true ? "" : "=" + String(a.value)}
                  </code>{" "}
                  {a.matches === null ? "—" : `${fmt(a.matches)} on its own`}
                </li>
              ))}
            </ul>
          )}
          {mode !== "filters" && (
            <button className="btn primary" onClick={onDiscover}>
              Search paid sources too
            </button>
          )}
        </EmptyState>
      ) : null}

      {suggestions.length > 0 && <LiveCandidates suggestions={suggestions} />}
    </>
  );
}

function LiveCandidates({ suggestions }: { suggestions: DiscoverySuggestion[] }) {
  return (
    <section className="block">
      <h2>
        Live candidates <span className="n">{suggestions.length}</span>
      </h2>
      <div className="card">
        <div className="tablewrap">
          <table className="list">
            <thead>
              <tr>
                <th>Name</th>
                <th>Affiliation</th>
                <th>Role &amp; place</th>
                <th>Source</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {suggestions.map((s) => (
                <LiveCandidateRow key={`${s.source}:${s.external_id}`} candidate={s} />
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function LiveCandidateRow({ candidate }: { candidate: DiscoverySuggestion }) {
  const [label, setLabel] = useState("Add");
  const [busy, setBusy] = useState(false);

  const queue = async () => {
    setBusy(true);
    setLabel("Adding…");
    try {
      const r = await apiSend<{ status: string }>("/v1/leads", "POST", {
        source: candidate.source,
        external_id: candidate.external_id,
        reason: "queued from Seekr UI",
      });
      setLabel(r.status === "queued" ? "Queued" : r.status.replace(/_/g, " "));
    } catch {
      setLabel("Failed");
      setBusy(false);
    }
  };

  const where = [candidate.role, candidate.location].filter(Boolean).join(" · ");
  return (
    <tr>
      <td className="nm">{candidate.name || "Unnamed"}</td>
      <td className="org">{candidate.affiliation || <span className="muted">—</span>}</td>
      <td className="sk">{where || <span className="muted">—</span>}</td>
      <td>
        <span className="srcpill">{candidate.source}</span>
      </td>
      <td className="num">
        <button className="btn sm" disabled={busy} onClick={queue}>
          {label}
        </button>
      </td>
    </tr>
  );
}
