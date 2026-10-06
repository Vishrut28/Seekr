import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { apiBlob, apiSend, errorMessage, isUnauthorized } from "../api/client";
import { BrandLinks } from "../lib/brands";
import { Icon } from "../lib/icons";
import type { PersonSummary, Shortlist } from "../types";
import { api } from "../api/client";

type Verdict = "good" | "bad";

/** Feedback is recorded against the query it was judged on. It does not
 *  reorder anything live — `/v1/query` ranks on evidence, and these judgements
 *  accumulate as training data for a learned ranker, readable at
 *  GET /v1/feedback. */
function MatchCell({ person, query }: { person: PersonSummary; query: string }) {
  const [verdict, setVerdict] = useState<Verdict | null>(null);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  // What happened, and to WHICH control. One shared note put "Recorded" on
  // every button in the cell: the bookmark claimed a save that never happened,
  // and a thumb the reader had just switched away from still said it counted.
  const [note, setNote] = useState<{ on: string; text: string } | null>(null);
  const tip = (on: string, fallback: string) =>
    note && note.on === on ? note.text : fallback;

  const vote = async (value: Verdict) => {
    const previous = verdict;
    setVerdict(value);
    try {
      await apiSend("/v1/feedback", "POST", {
        person_id: person.id,
        verdict: value,
        query,
      });
      setNote({ on: value, text: "Recorded" });
    } catch (e) {
      setVerdict(previous);
      if (!isUnauthorized(e)) {
        setNote({ on: value, text: "Could not record: " + errorMessage(e) });
      }
    }
  };

  const save = async () => {
    let existing: string[] = [];
    try {
      const lists = await api<{ shortlists: Shortlist[] }>("/v1/shortlists");
      existing = lists.shortlists.map((l) => l.name);
    } catch {
      /* a first-time user has no lists yet; the prompt still works */
    }
    const message = existing.length
      ? `Save to which shortlist?\n\nExisting: ${existing.join(", ")}\n\nType a name (new or existing):`
      : "Name your first shortlist:";
    const name = window.prompt(message, existing[0] || "Shortlist");
    if (!name) return;
    setBusy(true);
    try {
      const list = await apiSend<Shortlist>("/v1/shortlists", "POST", { name });
      const result = await apiSend<{ added: boolean }>(
        `/v1/shortlists/${list.id}/members`,
        "POST",
        { person_id: person.id, query },
      );
      setSaved(true);
      setNote({
        on: "save",
        text: result.added ? `Saved to ${list.name}` : `Already on ${list.name}`,
      });
    } catch (e) {
      if (!isUnauthorized(e)) {
        setNote({ on: "save", text: "Could not save: " + errorMessage(e) });
      }
      setBusy(false);
    }
  };

  return (
    <td className="vote" onClick={(e) => e.stopPropagation()}>
      <button
        className={verdict === "good" ? "vbtn on" : "vbtn"}
        title={tip("good", "Good match for this query")}
        onClick={() => vote("good")}
      >
        <Icon.thumbUp />
      </button>
      <button
        className={verdict === "bad" ? "vbtn on" : "vbtn"}
        title={tip("bad", "Bad match for this query")}
        onClick={() => vote("bad")}
      >
        <Icon.thumbDown />
      </button>
      <button
        className={saved ? "vbtn save on" : "vbtn save"}
        title={tip("save", "Save to a shortlist")}
        disabled={busy}
        onClick={save}
      >
        <Icon.bookmark />
      </button>
      {/* The report a human reads before an interview: everything Seekr holds
          about this person, with the source of each line. Fetched rather than
          linked, so the bearer token stays out of the URL. */}
      <button
        className="vbtn"
        title="Open the dossier as a PDF"
        disabled={busy}
        onClick={async (e) => {
          e.stopPropagation();
          const blob = await apiBlob(`/v1/persons/${person.id}/dossier.pdf`);
          const url = URL.createObjectURL(blob);
          window.open(url, "_blank", "noopener");
          // the tab holds its own reference; release ours once it has loaded
          setTimeout(() => URL.revokeObjectURL(url), 60_000);
        }}
      >
        <Icon.doc />
      </button>
    </td>
  );
}

/** The name as compared for namesakes: case and accents do not matter. */
export const nameKey = (name?: string | null) =>
  (name || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();

/** One line that tells this person apart from others of the same name. */
function identityLine(person: PersonSummary): string {
  const sources = [...new Set((person.attributes || []).flatMap((a) => a.sources || []))];
  const bits = [
    person.orcid ? `ORCID ${person.orcid}` : "",
    person.papers ? `${person.papers} paper${person.papers === 1 ? "" : "s"}` : "",
  ].filter(Boolean);
  if (!bits.length && !person.current_organization && !person.location && !sources.length) {
    return "only a name on record";
  }
  return bits.join(" · ");
}

function PersonRow({
  person,
  query,
  namesakes = 1,
}: {
  person: PersonSummary;
  query: string;
  namesakes?: number;
}) {
  const navigate = useNavigate();
  const idLine = namesakes > 1 ? identityLine(person) : "";

  // the topics that matched the query come first from the API, and are
  // marked; three are enough to tell people apart, the rest are a count
  const allTopics = (person.attributes || []).filter(
    (a) => a.attribute_type === "skill" || a.attribute_type === "research_interest",
  );
  const topics = allTopics.slice(0, 3);
  const moreTopics = Math.max(person.topic_count ?? allTopics.length, allTopics.length) - topics.length;
  const skills = topics.length ? (
    <>
      {topics.map((a, i) => (
        <span key={a.value}>
          {i > 0 && ", "}
          {a.matched ? (
            <span className="skhit" title="Matches your search">
              {a.value}
            </span>
          ) : (
            a.value
          )}
        </span>
      ))}
      {moreTopics > 0 && (
        <span
          className="moretopics"
          title={allTopics.slice(3).map((a) => a.value).join(", ") || undefined}
        >
          {" "}
          +{moreTopics} more
        </span>
      )}
    </>
  ) : null;
  // why this row is here, in a line: what matched, where, and what is missing
  const why = person.match === "partial"
    ? ["missing " + (person.missing || []).map((m) => m.term).join(", ")]
    : person.why || [];
  const sources = [...new Set((person.attributes || []).flatMap((a) => a.sources || []))];

  // the affiliation that satisfied the org filter — often NOT the current one,
  // so showing only current_organization looks wrong
  const primary = person.matched_organization || person.current_organization || "";
  const others = (person.organizations || []).filter((o) => o !== primary);
  const sub = [
    person.matched_organization &&
    person.current_organization &&
    person.current_organization !== primary
      ? "now: " + person.current_organization
      : "",
    others.length ? `+${others.length} more` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <tr onClick={() => navigate(`/person/${person.id}`)}>
      <td className="nm">
        {person.canonical_name || "Unnamed"}
        {/* fetched live for this query rather than already in the corpus */}
        {person.from_live_search && <span className="livetag">new</span>}
        {/* shown behind the full matches when too few people meet every part */}
        {person.match === "partial" && (
          <span
            className="livetag partialtag"
            title={
              "Does not match: " +
              (person.missing || []).map((m) => `${m.term} (${m.as})`).join(", ")
            }
          >
            partial
          </span>
        )}
        <BrandLinks urls={person.profile_urls} />
        {/* several people share this name on the page: say which one this is */}
        {idLine && <div className="sub2 idline">{idLine}</div>}
        {why.length > 0 && (
          <div className={person.match === "partial" ? "whyline partial" : "whyline"}>
            {why.join(" · ")}
          </div>
        )}
      </td>
      <td className="org">
        {primary || <span className="muted">—</span>}
        {sub && <div className="sub2">{sub}</div>}
      </td>
      <td className="org">{person.location || <span className="muted">—</span>}</td>
      <td className="sk">{skills || <span className="muted">—</span>}</td>
      <td>
        {sources.length ? (
          sources.map((s) => (
            <span key={s} className="srcpill">
              {s}
            </span>
          ))
        ) : (
          <span className="muted">—</span>
        )}
      </td>
      <MatchCell person={person} query={query} />
    </tr>
  );
}

export function ResultsTable({
  people,
  query,
  hasMore,
  loadingMore,
  onLoadMore,
}: {
  people: PersonSummary[];
  query: string;
  hasMore?: boolean;
  loadingMore?: boolean;
  onLoadMore: () => void;
}) {
  const named: Record<string, number> = {};
  for (const p of people) named[nameKey(p.canonical_name)] = (named[nameKey(p.canonical_name)] || 0) + 1;
  return (
    <div className="card results-card">
      <div className="tablewrap">
        <table className="list results-table">
          <colgroup>
            <col className="col-name" />
            <col className="col-org" />
            <col className="col-loc" />
            <col className="col-skills" />
            <col className="col-sources" />
            <col className="col-match" />
          </colgroup>
          <thead>
            <tr>
              <th>Name</th>
              <th>Organization</th>
              <th>Location</th>
              <th>Skills &amp; interests</th>
              <th>Sources</th>
              <th title="Tell the ranking tool whether this fits your query">Match</th>
            </tr>
          </thead>
          <tbody>
            {people.map((p) => (
              <PersonRow
                key={p.id}
                person={p}
                query={query}
                namesakes={named[nameKey(p.canonical_name)]}
              />
            ))}
          </tbody>
        </table>
      </div>
      {hasMore && (
        <div className="loadmore">
          <button className="btn" disabled={loadingMore} onClick={onLoadMore}>
            {loadingMore ? "Loading…" : "Load 50 more"}
          </button>
        </div>
      )}
    </div>
  );
}
