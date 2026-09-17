import { useEffect, useState } from "react";
import { api } from "../api/client";
import { fmt } from "../lib/format";
import { Icon } from "../lib/icons";
import type { FacetResponse, FacetValue } from "../types";

/** Every filter, named by the query parameter it sends. Keeping the parameter
 *  name as the key means the form state IS the request — no second mapping to
 *  fall out of step with the backend. */
export const FILTER_FIELDS = [
  { name: "country", label: "Country", kind: "select", facet: "country" },
  { name: "source", label: "Source", kind: "select", facet: "source" },
  { name: "organization", label: "Organization", kind: "text", facet: "organization", placeholder: "Ever affiliated" },
  { name: "current_organization", label: "Current employer", kind: "text", facet: "organization", placeholder: "Present only" },
  { name: "education", label: "Studied at", kind: "text", facet: "organization", placeholder: "University" },
  { name: "role", label: "Role", kind: "text", facet: "role", placeholder: "e.g. professor" },
  { name: "skill", label: "Skill", kind: "text", facet: "skill", placeholder: "e.g. nlp" },
  { name: "technology", label: "Technology", kind: "text", facet: "technology", placeholder: "e.g. rust" },
  { name: "location", label: "Location", kind: "text", placeholder: "City or region" },
  { name: "min_publications", label: "Min publications", kind: "number", min: 0, placeholder: "0" },
  { name: "min_citations", label: "Min citations", kind: "number", min: 0, placeholder: "0" },
  { name: "active_since", label: "Active since", kind: "text", placeholder: "YYYY" },
  { name: "min_sources", label: "Min sources", kind: "number", min: 1, placeholder: "1" },
] as const;

export type FilterState = Record<string, string>;

export const EMPTY_FILTERS: FilterState = { sort: "relevance" };

export interface FilterFlags {
  has_cv: boolean;
  has_email: boolean;
}

export const EMPTY_FLAGS: FilterFlags = { has_cv: false, has_email: false };

/** The request these filters describe. "relevance" is the default order, so it
 *  is left off the wire rather than sent as a no-op sort. */
export function filterParams(values: FilterState, flags: FilterFlags): URLSearchParams {
  const p = new URLSearchParams();
  for (const [name, value] of Object.entries(values)) {
    if (value && value !== "relevance") p.set(name, value);
  }
  if (flags.has_cv) p.set("has_cv", "true");
  if (flags.has_email) p.set("has_email", "true");
  return p;
}

export function activeFilterCount(values: FilterState, flags: FilterFlags): number {
  return [...filterParams(values, flags).keys()].length;
}

/** Free-text filters match whole words, so a guessed fragment finds nothing.
 *  Offer the values that actually exist, with their people-counts, instead of
 *  making people guess at the vocabulary. */
function useFacets(): Record<string, FacetValue[]> {
  const [facets, setFacets] = useState<Record<string, FacetValue[]>>({});

  useEffect(() => {
    let live = true;
    const wanted = [
      ...new Set(FILTER_FIELDS.flatMap((f) => ("facet" in f ? [f.facet] : []))),
    ];
    Promise.all(
      wanted.map((field) =>
        api<FacetResponse>(`/v1/facets?field=${field}&limit=150`)
          .then((d) => [field, d.values] as const)
          .catch(() => [field, [] as FacetValue[]] as const),
      ),
    ).then((pairs) => {
      if (live) setFacets(Object.fromEntries(pairs));
    });
    return () => {
      live = false;
    };
  }, []);

  return facets;
}

export function Filters({
  values,
  flags,
  onChange,
  onFlags,
  onApply,
  onClear,
}: {
  values: FilterState;
  flags: FilterFlags;
  onChange: (next: FilterState) => void;
  onFlags: (next: FilterFlags) => void;
  onApply: () => void;
  onClear: () => void;
}) {
  const facets = useFacets();
  const count = activeFilterCount(values, flags);
  const set = (name: string, value: string) => onChange({ ...values, [name]: value });

  return (
    <details className="filters">
      <summary>
        <Icon.caret /> Filters
        {count > 0 && <span className="count">{count}</span>}
      </summary>
      <div className="fgrid">
        {FILTER_FIELDS.map((f) => {
          const facet = "facet" in f ? facets[f.facet] : undefined;
          if (f.kind === "select") {
            return (
              <label key={f.name}>
                {f.label}
                <select value={values[f.name] || ""} onChange={(e) => set(f.name, e.target.value)}>
                  <option value="">Any</option>
                  {(facet || []).slice(0, 100).map((v) => (
                    <option key={v.value} value={v.value}>
                      {v.value} · {fmt(v.people)}
                    </option>
                  ))}
                </select>
              </label>
            );
          }
          const listId = facet ? `list_${f.name}` : undefined;
          return (
            <label key={f.name}>
              {f.label}
              <input
                type={f.kind === "number" ? "number" : "text"}
                min={"min" in f ? f.min : undefined}
                placeholder={"placeholder" in f ? f.placeholder : undefined}
                list={listId}
                value={values[f.name] || ""}
                onChange={(e) => set(f.name, e.target.value)}
              />
              {listId && (
                <datalist id={listId}>
                  {(facet || []).map((v) => (
                    <option key={v.value} value={v.value}>
                      {fmt(v.people)} people
                    </option>
                  ))}
                </datalist>
              )}
            </label>
          );
        })}
        <label>
          Sort
          <select
            value={values.sort || "relevance"}
            onChange={(e) => set("sort", e.target.value)}
          >
            <option value="relevance">Default order</option>
            <option value="recent">Recently updated</option>
            <option value="name">Name A–Z</option>
          </select>
        </label>
        <label className="chk">
          <input
            type="checkbox"
            checked={flags.has_cv}
            onChange={(e) => onFlags({ ...flags, has_cv: e.target.checked })}
          />{" "}
          Has CV
        </label>
        <label className="chk">
          <input
            type="checkbox"
            checked={flags.has_email}
            onChange={(e) => onFlags({ ...flags, has_email: e.target.checked })}
          />{" "}
          Has email
        </label>
      </div>
      <p className="fhint">
        Text filters match whole words — <code>go</code> finds Go, not Cognitive. Add{" "}
        <code>*</code> for a loose match: <code>go*</code> also finds Golang.
      </p>
      <div className="filter-actions">
        <button className="btn primary" onClick={onApply}>
          Apply filters
        </button>
        <button className="btn" onClick={onClear}>
          Clear
        </button>
      </div>
    </details>
  );
}
