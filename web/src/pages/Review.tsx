import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, apiSend, errorMessage, isUnauthorized } from "../api/client";
import { Banner, EmptyState, Loading } from "../components/EmptyState";
import { Shell } from "../components/Shell";
import { useWorking } from "../lib/hooks";
import type { Conflation, DuplicateCandidate, FuzzyMerge } from "../types";

interface Queue {
  possible_duplicates: DuplicateCandidate[];
  fuzzy_merges: FuzzyMerge[];
}

/** Enough of an id to tell two people of the same name apart. */
const short = (id: string) => (id || "").slice(0, 8);

export function Review() {
  const [queue, setQueue] = useState<Queue | null>(null);
  // Its own request: it reads stored source payloads and takes a moment, and
  // the merge queue should not wait behind it.
  const [conflations, setConflations] = useState<Conflation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [acting, setActing] = useState(false);

  useWorking(!queue && !error);

  const load = useCallback(() => {
    api<Queue>("/v1/review/merges")
      .then(setQueue)
      .catch((e) => {
        if (!isUnauthorized(e)) setError(errorMessage(e));
      });
  }, []);

  useEffect(load, [load]);

  /** Every decision here rewrites identity, so the queue is re-read rather
   *  than patched in place — a merge can change more rows than the one acted
   *  on. */
  const act = async (path: string) => {
    setActing(true);
    setError(null);
    try {
      await apiSend(path, "POST");
      setQueue(null);
      load();
    } catch (e) {
      if (!isUnauthorized(e)) setError(errorMessage(e));
    } finally {
      setActing(false);
    }
  };

  useEffect(() => {
    let live = true;
    api<{ conflations: Conflation[] }>("/v1/review/conflations")
      .then((r) => live && setConflations(r.conflations))
      .catch(() => live && setConflations([]));
    return () => {
      live = false;
    };
  }, []);

  /** Move one body of work onto a person of its own. The source record is
   *  frozen afterwards: it describes both people, so re-fetching it would put
   *  them back together. */
  const splitOff = async (c: Conflation, group: number, ids: number[]) => {
    const ok = window.confirm(
      `Move these ${ids.length} papers off "${c.person_name}" onto a new person?` +
        "\n\nTheir subjects are recomputed from the papers each side keeps. " +
        "Any ORCID stays with the original, and the source record stops being " +
        "refreshed, because re-fetching it would merge them again.",
    );
    if (!ok) return;
    try {
      await apiSend(`/v1/review/conflations/${c.person_id}/split`, "POST", {
        publication_ids: ids,
        name: c.person_name,
        note: `split group ${group + 1} from the review queue`,
      });
      setConflations((prev) => (prev || []).filter((x) => x.person_id !== c.person_id));
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  const ruleOn = async (personId: string, verdict: string) => {
    setConflations((prev) => (prev || []).filter((c) => c.person_id !== personId));
    try {
      await apiSend(`/v1/review/conflations/${personId}`, "POST", { verdict });
    } catch {
      load();                 // put it back by reloading rather than guessing
    }
  };

  const topbar = (
    <>
      <h1 className="title">Review queue</h1>
      <div className="sub">
        Merges Seekr was not confident enough to make on its own.
      </div>
    </>
  );

  if (error) {
    return (
      <Shell topbar={topbar}>
        <Banner>{error}</Banner>
      </Shell>
    );
  }
  if (!queue) {
    return (
      <Shell topbar={topbar}>
        <Loading message="Loading queue…" />
      </Shell>
    );
  }

  const duplicates = queue.possible_duplicates || [];
  const fuzzy = queue.fuzzy_merges || [];

  return (
    <Shell topbar={topbar}>
      <section className="block">
        <h2>
          Possible duplicates <span className="n">{duplicates.length}</span>
        </h2>
        {/* Merging folds one person's evidence, affiliations and publications
            into the other and leaves a tombstone pointing at the survivor.
            Nothing in the codebase puts them back, so this one decision has to
            say so — the page used to promise the opposite. */}
        {duplicates.length > 0 && (
          <Banner kind="warn">
            Merging moves everything one person holds onto the other and cannot be
            undone. "Different people" only closes the pair, and changes nothing.
            Use "Can't tell" when nothing settles it: the pair leaves this queue
            and comes back if either person later gains evidence, which
            "Different people" cannot do once said.
          </Banner>
        )}
        {duplicates.length ? (
          duplicates.map((d) => (
            <div className="conflict" key={d.candidate_id}>
              <div className="vs">
                <div className="side">
                  <b>
                    <Link to={`/person/${d.person_id}`}>{d.person_name}</Link>
                  </b>
                </div>
                <div className="mid">same person?</div>
                <div className="side">
                  <b>
                    <Link to={`/person/${d.duplicate_person_id}`}>
                      {d.duplicate_person_name}
                    </Link>
                  </b>
                </div>
              </div>
              <div className="idline">{d.signals?.reason || `score ${d.score}`}</div>
              <div className="btn-row" style={{ marginTop: 10 }}>
                <button
                  className="btn primary sm"
                  disabled={acting}
                  onClick={() => {
                    // Last stop before an irreversible write. Both sides
                    // usually carry the SAME name — that is why they were
                    // queued — so the names alone identify nothing, and the
                    // id and the reason go in with them.
                    const ok = window.confirm(
                      `Merge "${d.duplicate_person_name}" (${short(d.duplicate_person_id)})` +
                        ` into "${d.person_name}" (${short(d.person_id)})?` +
                        `\n\n${d.signals?.reason || `score ${d.score}`}` +
                        "\n\nEverything the first holds moves to the second. " +
                        "This cannot be undone.",
                    );
                    if (ok) act(`/v1/review/duplicates/${d.candidate_id}/merge`);
                  }}
                >
                  Merge
                </button>
                <button
                  className="btn danger sm"
                  disabled={acting}
                  onClick={() => act(`/v1/review/duplicates/${d.candidate_id}/reject`)}
                >
                  Different people
                </button>
                {/* The absence of an answer, not a third one. Thirteen pairs
                    needed it at once: six records of one ALICE physicist
                    holding a hundred papers between them and not one of their
                    own, so there was nothing to read either way. Saying
                    "Different people" there would have made a claim nobody
                    could support, permanently. */}
                <button
                  className="btn ghost sm"
                  disabled={acting}
                  title="Nothing here settles it. Leaves the queue, and returns if evidence appears."
                  onClick={() => act(`/v1/review/duplicates/${d.candidate_id}/defer`)}
                >
                  Can't tell
                </button>
              </div>
            </div>
          ))
        ) : (
          <EmptyState title="Nothing to review" body="No duplicate pairs are waiting." />
        )}
      </section>

      <section className="block">
        <h2>
          Fuzzy merges awaiting confirmation <span className="n">{fuzzy.length}</span>
        </h2>
        {fuzzy.length ? (
          fuzzy.map((f) => (
            <div className="conflict" key={f.link_id}>
              <div>
                <b>
                  <Link to={`/person/${f.person_id}`}>{f.person_name}</Link>
                </b>{" "}
                <span className="muted">
                  ← {f.source}:{f.external_id} ({f.record_name || ""})
                </span>
              </div>
              <div className="idline">{f.signals?.reason || f.match_method}</div>
              <div className="btn-row" style={{ marginTop: 10 }}>
                <button
                  className="btn primary sm"
                  disabled={acting}
                  onClick={() => act(`/v1/review/merges/${f.link_id}/approve`)}
                >
                  Approve
                </button>
                <button
                  className="btn danger sm"
                  disabled={acting}
                  onClick={() => act(`/v1/review/merges/${f.link_id}/split`)}
                >
                  Split apart
                </button>
              </div>
            </div>
          ))
        ) : (
          <EmptyState
            title="All confirmed"
            body="No fuzzy merges are waiting for a decision."
          />
        )}
      </section>

      <section className="block">
        <h2>
          One record, more than one person?{" "}
          <span className="n">{conflations ? conflations.length : "…"}</span>
        </h2>
        {/* Sources disambiguate authors themselves and get it wrong, and a
            record that holds two people's work answers searches with the wrong
            half. Measured 2026-09-21, about a third of these are real at the
            threshold this queue uses; the rest are one person with a wide
            career, which is why the papers are printed rather than a verdict.
            A third still beats the 8% base rate by four times over, so the
            queue is worth reading — but only as a reading order. */}
        {conflations === null ? (
          <Loading message="Reading publication records…" />
        ) : conflations.length === 0 ? (
          <EmptyState
            title="Nothing to look at"
            /* Not "nothing is wrong". The score is the second-largest group
               of papers over the largest, so a record whose intruder is one
               or two papers that share no topic and no co-author scores zero
               — 1895 Labrador geology filed with 2019 materials science among
               them. Measured recall against records found by other signals
               was nil, and saying so here is cheaper than somebody inferring
               a clean corpus from an empty list. */
            body="No record splits into two comparable bodies of work. That is
                  not a clean bill of health: this test cannot see a stray
                  paper or two, only a second career."
          />
        ) : (
          conflations.map((c) => (
            <div className="conflict" key={c.person_id}>
              <div>
                <b>
                  <Link to={`/person/${c.person_id}`}>{c.person_name || "unnamed"}</Link>
                </b>{" "}
                <span className="muted">{c.papers} papers</span>
              </div>
              <div className="idline">
                {c.shares_an_employer === false
                  ? "the two halves name no employer in common"
                  : c.shares_an_employer === null
                    ? "no institutions on file to compare"
                    : "shares an employer across both halves"}
              </div>
              <div className="cgroups">
                {c.groups.slice(0, 3).map((g, i) => (
                  <div className="cgroup" key={i}>
                    <div className="idline">
                      {g.papers} papers · {g.years}
                      {g.topics.length ? ` · ${g.topics.slice(0, 2).join(", ")}` : ""}
                    </div>
                    <ul>
                      {g.titles.slice(0, 3).map((t, j) => (
                        <li key={j}>{t}</li>
                      ))}
                    </ul>
                    {/* Splitting off the ONLY group would rename somebody
                        rather than split them, so it is offered per group
                        and only when there is another group to keep. */}
                    {(c.group_ids[i]?.length || 0) > 0 && c.group_ids.length > 1 && (
                      <button
                        className="btn sm ghost"
                        onClick={() => splitOff(c, i, c.group_ids[i])}
                      >
                        Split these off
                      </button>
                    )}
                  </div>
                ))}
              </div>
              <div className="btn-row" style={{ marginTop: 10 }}>
                {/* Nothing here can split one source record into two people
                    yet, so this records the finding rather than claiming to
                    act on it. */}
                <button
                  className="btn primary sm"
                  onClick={() => ruleOn(c.person_id, "several_people")}
                >
                  Several people — note it
                </button>
                <button
                  className="btn sm"
                  onClick={() => ruleOn(c.person_id, "one_person")}
                >
                  One person
                </button>
              </div>
            </div>
          ))
        )}
      </section>
    </Shell>
  );
}
