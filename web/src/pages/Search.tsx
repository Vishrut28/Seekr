import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { SourceCards, useLiveSearch } from "../components/SourceCards";
import { api, apiSend, errorMessage, isUnauthorized } from "../api/client";
import {
  activeFilterCount,
  EMPTY_FILTERS,
  EMPTY_FLAGS,
  Filters,
  filterLabel,
  filterParams,
  type FilterFlags,
  type FilterState,
  nameFilter,
} from "../components/Filters";
import { Banner, EmptyState, Loading } from "../components/EmptyState";
import { Landing } from "../components/Landing";
import { SearchBox } from "../components/SearchBox";
import { ResultsTable, nameKey } from "../components/ResultsTable";
import { Shell } from "../components/Shell";
import { fmt } from "../lib/format";
import { useWorking } from "../lib/hooks";
import type {
  DiscoverySuggestion,
  FacetResponse,
  PersonSummary,
  QueryResponse,
} from "../types";

/** What the empty start-page bar types out: real searches with real answers. */
const TYPING = [
  "NLP researchers in India",
  "machine learning at Google DeepMind",
  "Python developers in India",
  "computer vision researchers",
  "deep learning, top 20",
];

/** Offered under the box as "Try", after the reader's own recent searches. */
const EXAMPLES = [
  ...TYPING,
  "machine learning at University of Toronto",
  "product designers at Swiggy",
];

const RECENT_KEY = "seekr_recent";
const LAST_QUERY_KEY = "seekr_q";

interface SearchPrefs {
  near: boolean;
  related: boolean;
}

const PREFS_KEY = "seekr_search_prefs";
const DEFAULT_PREFS: SearchPrefs = { near: false, related: true };

/** The reader's switches; storage can be missing or blocked, so defaults stand. */
function readPrefs(): SearchPrefs {
  try {
    return { ...DEFAULT_PREFS, ...JSON.parse(localStorage.getItem(PREFS_KEY) || "{}") };
  } catch {
    return DEFAULT_PREFS;
  }
}

function writePrefs(prefs: SearchPrefs) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch {
    /* a private window: the switch still holds for this visit */
  }
}

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
  // This button spends money: it adds the metered provider to the search, and
  // forces a live pass even when the corpus could answer on its own. One
  // stray click next to "Search" did that twice, so it takes two — the first
  // arms it and says what it will do.
  //
  // It does NOT decide whether anything goes out to the internet: an ordinary
  // search already asks the FREE sources whenever the corpus cannot answer
  // fully, and already keeps the people they return. Saying otherwise here
  // implied a plain search stayed local, which it does not.
  const [liveArmed, setLiveArmed] = useState(false);
  const [rows, setRows] = useState<PersonSummary[]>([]);
  const [data, setData] = useState<QueryResponse | null>(null);
  const [mode, setMode] = useState<Mode>("query");
  const [ranQuery, setRanQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [recent, setRecent] = useState<string[]>(readRecent);
  const [trending, setTrending] = useState<string[]>([]);
  const [values, setValues] = useState<FilterState>(EMPTY_FILTERS);
  const [flags, setFlags] = useState<FilterFlags>(EMPTY_FLAGS);
  // the start page's filter button: the full page, with the filters open
  const [browsing, setBrowsing] = useState(false);
  // a search carried over from the last visit is about to be re-run, so the
  // start page must not flash up in the meantime
  const [restoring, setRestoring] = useState(() => Boolean(sessionStorage.getItem(LAST_QUERY_KEY)));
  const navigate = useNavigate();
  const location = useLocation();

  const input = useRef<HTMLInputElement>(null);
  // read inside callbacks that must not be rebuilt on every keystroke
  const textRef = useRef(text);
  textRef.current = text;
  // Two switches the reader owns, remembered in this browser: near matches
  // (people meeting only part of the query) and related subjects ("NLP"
  // reaching Topic Modeling). Read inside callbacks, so kept in a ref too.
  const [prefs, setPrefs] = useState<SearchPrefs>(readPrefs);
  const prefsRef = useRef(prefs);
  prefsRef.current = prefs;

  useWorking(Boolean(loading));

  const rememberQuery = (q: string) => {
    const list = [q, ...readRecent().filter((x) => x !== q)].slice(0, 8);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
    setRecent(list);
  };

  const forgetQuery = (q: string) => {
    const list = readRecent().filter((x) => x !== q);
    localStorage.setItem(RECENT_KEY, JSON.stringify(list));
    setRecent(list);
  };

  const live = useLiveSearch();

  /** Nothing asked, nothing shown. An empty box used to leave the last
   *  answer on screen, so the results claimed to be about a question that was
   *  no longer there — and a reload re-ran it. */
  const clearResults = () => {
    live.stop();
    setRows([]);
    setData(null);
    setRanQuery("");
    setOffset(0);
    setError(null);
    setLoading(null);
    setRestoring(false);
    sessionStorage.removeItem(LAST_QUERY_KEY);
  };

  // Back from the results, or Search / the logo clicked in the rail, is a
  // visit to the start page: the history entry without `results` on it.
  const lastKey = useRef(location.key);
  useEffect(() => {
    if (lastKey.current === location.key) return;
    lastKey.current = location.key;
    if ((location.state as { results?: boolean } | null)?.results) return;
    clearResults();
    setText("");
    textRef.current = "";
    setBrowsing(false);
    setMode("query");
  });

  /** A later page, minus anyone already on screen. A live search stores
   *  people mid-session, so the next page of the corpus can contain someone
   *  already shown — and React keys on person id, so a duplicate is both a
   *  repeated row and a console error. */
  const append = (prev: PersonSummary[], next: PersonSummary[]) => {
    const have = new Set(prev.map((p) => p.id));
    return [...prev, ...next.filter((p) => !have.has(p.id))];
  };

  const runQuery = useCallback(async (discover?: boolean, from?: number, near?: boolean) => {
    const q = textRef.current.trim();
    if (!q) return;
    sessionStorage.setItem(LAST_QUERY_KEY, q);
    rememberQuery(q);
    const paging = typeof from === "number" && from > 0;
    if (!paging && typeof near === "boolean" && near !== prefsRef.current.near) {
      const next = { ...prefsRef.current, near };
      prefsRef.current = next;
      setPrefs(next);
      writePrefs(next);
    }
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
        setOffset(msg.next_offset ?? (msg.results || []).length);
      });
      return;
    }
    try {
      const params = new URLSearchParams({ q });
      if (paging) params.set("offset", String(from));
      if (discover) params.set("discover", "true");
      if (prefsRef.current.near) params.set("near", "true");
      if (!prefsRef.current.related) params.set("related", "false");
      const res = await api<QueryResponse>(`/v1/query?${params}`);
      setData(res);
      setRows((prev) => (paging ? append(prev, res.results) : res.results));
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
      const name = nameFilter(q);
      if (name) params.set("q", name);
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
        setRows((prev) => (paging ? append(prev, res.results) : res.results));
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

  const changeText = (value: string) => {
    setText(value);
    textRef.current = value;
    setLiveArmed(false);
    if (value.trim()) return;
    // In filter mode the box is only a name filter, so the other filters still
    // stand and the question becomes "everyone matching them" — unless there
    // are none, in which case nothing was asked and nothing should be shown.
    if (mode === "filters" && activeFilterCount(values, flags) > 0) runFilters();
    else clearResults();
  };

  const searchFor = (value: string, paid?: boolean) => {
    setText(value);
    textRef.current = value;
    runQuery(paid || undefined);
  };

  /** Leaving the start page is a step the browser's Back button undoes. */
  const leaveLanding = () => {
    navigate("/search", { state: { results: true } });
    // a phone may have scrolled the start page; results start at their top
    window.scrollTo(0, 0);
  };

  const clearFilters = () => {
    setValues(EMPTY_FILTERS);
    setFlags(EMPTY_FLAGS);
    setRows([]);
    setData(null);
  };

  // the corpus's commonest roles join the examples as things to try
  const tries = [...new Set([...EXAMPLES, ...trending])];

  const topbar = (
    <div className="searchrow">
      <SearchBox
        variant="top"
        value={text}
        onChange={changeText}
        onSubmit={(q) => searchFor(q)}
        onEscape={() => {
          changeText("");
          input.current?.blur();
        }}
        recent={recent}
        tries={tries}
        onForget={forgetQuery}
        inputRef={input}
      />
      <button className="btn primary" onClick={() => runQuery()}>
        Search
      </button>
      <span className="btnsplit" aria-hidden="true" />
      <button
        className={liveArmed ? "btn armed" : "btn"}
        title={
          "Also search the metered provider, which costs money, and go out " +
          "to the sources even when the corpus could answer on its own. " +
          "An ordinary search already asks the free ones (OpenAlex, " +
          "Europe PMC, the web) and keeps the people they return."
        }
        aria-label={
          liveArmed
            ? "Confirm searching the paid source, which costs money"
            : "Also search paid sources"
        }
        onClick={() => {
          if (!liveArmed) {
            setLiveArmed(true);
            return;
          }
          setLiveArmed(false);
          runQuery(true);
        }}
        onBlur={() => setLiveArmed(false)}
      >
        {liveArmed ? "Search paid sources?" : "Live"}
      </button>
    </div>
  );

  const isSearching = Boolean(loading) || live.running;
  const showResults = Boolean(data) || rows.length > 0 || live.running;
  // the start page is the page with nothing asked of it yet
  const landing = !browsing && !restoring && !showResults && !isSearching && !error;

  if (landing) {
    return (
      <Shell landing>
        <Landing
          text={text}
          onText={changeText}
          onSearch={(q, paid) => {
            leaveLanding();
            searchFor(q, paid);
          }}
          onFilters={() => {
            leaveLanding();
            setBrowsing(true);
          }}
          recent={recent}
          tries={tries}
          typing={TYPING}
          onForget={forgetQuery}
          inputRef={input}
        />
      </Shell>
    );
  }

  return (
    <Shell topbar={topbar}>
      <div className="search-page">
        <div className="search-controls">
          <Filters
            values={values}
            flags={flags}
            onChange={setValues}
            onFlags={setFlags}
            onApply={() => runFilters()}
            onClear={clearFilters}
            nameText={text}
            startOpen={browsing}
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
              onNear={(show) => runQuery(false, undefined, show)}
              prefs={prefs}
              onRelated={(on) => {
                const next = { ...prefsRef.current, related: on };
                prefsRef.current = next;
                setPrefs(next);
                writePrefs(next);
                runQuery();
              }}
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
  onNear,
  prefs,
  onRelated,
}: {
  data: QueryResponse | null;
  rows: PersonSummary[];
  query: string;
  mode: Mode;
  loadingMore: boolean;
  searching?: boolean;
  onLoadMore: () => void;
  onDiscover: () => void;
  onNear: (show: boolean) => void;
  prefs: SearchPrefs;
  onRelated: (on: boolean) => void;
}) {
  const f = data?.applied_filters;
  const unmatched = data?.unmatched_terms || [];
  const notFound = data?.not_found || [];
  const corrections = data?.corrections || [];
  const suggestions = data?.discovery_suggestions || [];
  const total = data?.total_matches ?? rows.length;
  const dropped = notFound.map((d) => d.term);

  // One pill per subject asked for, not per topic it resolved to: "machine
  // learning" matches nine stored topics and listed all nine, while
  // "physicists" resolved to related subjects only and listed nothing at all.
  const subjects = f?.subjects?.length ? f.subjects : f?.skills || [];
  const pills: { kind: string; value: string }[] = [
    ...subjects.map((v) => ({ kind: "subject", value: v })),
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

  // Two different things, and saying the wrong one is a lie about the corpus:
  // a term nothing matches ("Zzyzx"), and a constraint that exists but whose
  // intersection is empty — "at both Google and Stanford with over 1000
  // citations" dropped both employers because nobody meets all three.
  const unknownTerms = [...new Set(unmatched)];
  const relaxedTerms = dropped.filter((t) => !unknownTerms.includes(t));
  // "...so these 8 results ignore it" was false whenever the results came
  // back from a live search FOR that very term: the rows were the term's own
  // answers, and the page told the reader to disregard them.
  const corpusRows = rows.filter((r) => !r.from_live_search).length;
  const nearRows = rows.filter((r) => r.match === "partial").length;
  const fullRows = rows.length - nearRows;
  // Several rows with one name read as duplicates. They are kept apart because
  // nothing proves them one person, and the page should say so.
  const byName = new Map<string, { name: string; n: number }>();
  for (const r of rows) {
    const k = nameKey(r.canonical_name);
    if (!k) continue;
    const e = byName.get(k) || { name: r.canonical_name || "", n: 0 };
    e.n += 1;
    byName.set(k, e);
  }
  const shared = [...byName.values()].filter((e) => e.n > 1).sort((a, b) => b.n - a.n);

  return (
    <>
      <div className="meta">
        <div className="count">
          {rows.length > 0 ? (
            // near matches are counted apart: "22 of 22 matching" over six
            // full matches and sixteen partial ones claimed all of them matched
            <>
              <b>{fmt(fullRows)}</b> of {fmt(Math.max(total, fullRows))} matching
              {nearRows > 0 && <> · {fmt(nearRows)} near</>}
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
        {mode === "query" && (
          <div className="switches">
            <label title="People who meet only part of the search, after the full matches">
              <input type="checkbox" checked={prefs.near}
                onChange={(e) => onNear(e.target.checked)} />
              Near matches
            </label>
            <label title='Subjects next to the one asked for: "NLP" also reaching Topic Modeling'>
              <input type="checkbox" checked={prefs.related}
                onChange={(e) => onRelated(e.target.checked)} />
              Related topics
            </label>
          </div>
        )}
      </div>

      {unknownTerms.length > 0 && (
        <Banner kind="warn">
          <b>{unknownTerms.join(", ")}</b> {unknownTerms.length === 1 ? "was" : "were"} not
          applied — nothing stored in Seekr matches{" "}
          {unknownTerms.length === 1 ? "that term" : "those terms"} yet
          {corpusRows > 0
            ? ", so the people below who were already in Seekr meet only the rest of the search (marked partial)"
            : rows.length
              ? `. The ${fmt(rows.length)} below were found by a live search for the whole question and checked against the rest of it`
              : ""}
          .{" "}
          <button className="btn sm" onClick={onDiscover}>
            Search paid sources too
          </button>
        </Banner>
      )}

      {relaxedTerms.length > 0 && rows.length > 0 && (
        <Banner kind="warn">
          Nobody matches every part of that question, so{" "}
          <b>{relaxedTerms.join(", ")}</b> {relaxedTerms.length === 1 ? "was" : "were"} set
          aside. {rows.length ? "Each result below says what it misses." : ""}{" "}
          <button className="btn sm" onClick={onDiscover}>
            Search paid sources too
          </button>
        </Banner>
      )}

      {mode === "query" && (data?.near_matches || 0) > 0 && !data?.near && (
        <Banner kind="info">
          {rows.length > 0
            ? <>Showing only people who match every part of the search. </>
            : <>Nobody matches every part of the search. </>}
          <b>{fmt(data?.near_matches || 0)}</b>{" "}
          {data?.near_matches === 1 ? "person matches" : "people match"} only part of it
          {relaxedTerms.length + unknownTerms.length > 0 ? (
            <> (not <b>{[...relaxedTerms, ...unknownTerms].join(", ")}</b>)</>
          ) : null}
          .{" "}
          <button className="btn sm" onClick={() => onNear(true)}>
            Show near matches
          </button>
        </Banner>
      )}

      {mode === "query" && data?.near && rows.some((r) => r.match === "partial") && (
        <Banner kind="info">
          Near matches are shown after the full matches, each marked <b>partial</b>.{" "}
          <button className="btn sm" onClick={() => onNear(false)}>
            Hide near matches
          </button>
        </Banner>
      )}

      {shared.length > 0 && (
        <Banner kind="info">
          {shared.length === 1 ? (
            <>
              These <b>{fmt(shared[0].n)}</b> people named <b>{shared[0].name}</b> are different
              people.
            </>
          ) : (
            <>
              Rows with the same name ({shared.slice(0, 3).map((e) => e.name).join(", ")}) are
              different people.
            </>
          )}{" "}
          Seekr joins records only when a shared ORCID or a shared paper proves they are one
          person; each row says which one it is.
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
                    {filterLabel(a.filter)}
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

      {/* only the ones nobody has checked: anyone fetched and checked is in
          the results above if they answer, and was dropped if they do not */}
      {suggestions.some((s) => !s.stored) && (
        <LiveCandidates suggestions={suggestions.filter((s) => !s.stored)} />
      )}
    </>
  );
}

function LiveCandidates({ suggestions }: { suggestions: DiscoverySuggestion[] }) {
  return (
    <details className="block unchecked">
      <summary>
        <h2>
          Unchecked candidates <span className="n">{suggestions.length}</span>
        </h2>
        <span className="muted">
          Found by a source but not fetched, so not checked against your search. Add one to
          fetch and check it later.
        </span>
      </summary>
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
    </details>
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
