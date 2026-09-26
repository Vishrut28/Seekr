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

  /** Move papers off this record onto a person of their own.
   *
   *  Takes the ids and what to call them, not a group index: the commonest
   *  conflation is ONE paper from somebody else's life, which is a group of
   *  one and was unreachable from this page — a reviewer could read "1952 ·
   *  alone by 52 years" and have no button for it.
   *
   *  The source record is frozen afterwards: it describes both people, so
   *  re-fetching it would put them back together.
   */
  const splitOff = async (c: Conflation, ids: number[], what: string) => {
    const ok = window.confirm(
      `Move ${ids.length === 1 ? "this paper" : `these ${ids.length} papers`}` +
        ` off "${c.person_name}" onto a new person?` +
        "\n\nTheir subjects are recomputed from the papers each side keeps. " +
        "Any ORCID stays with the original, and the source record stops being " +
        "refreshed, because re-fetching it would merge them again.",
    );
    if (!ok) return;
    try {
      await apiSend(`/v1/review/conflations/${c.person_id}/split`, "POST", {
        publication_ids: ids,
        name: c.person_name,
        note: `${what}, split from the review queue`,
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
            half. Judged 2026-09-27 on 120 records nobody had opened: about
            half of what it reports there is real, and it finds about three
            in five of the conflations -- so the papers are printed rather
            than a verdict, and the queue is a reading order. */}
        {conflations === null ? (
          <Loading message="Reading publication records…" />
        ) : conflations.length === 0 ? (
          <EmptyState
            title="Nothing to look at"
            /* Not "nothing is wrong". On a blind draw this found 10 of 17
               conflated records, and it cannot see people whose papers carry
               no OpenAlex topic at all -- a quarter of the corpus. Saying so
               here is cheaper than somebody inferring a clean corpus from an
               empty list. */
            body="No record holds work foreign to its career or a gap in time
                  no career explains. That is not a clean bill of health: on a
                  blind draw this found about three in five of the records
                  that were really several people, and it cannot judge people
                  whose papers carry no subject classification."
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
              {/* Why this record is here, in terms a reader can check. Foreign
                  work and a hole in time are usually SINGLE papers, which the
                  groups below never print — so without these the queue looks
                  broken. */}
              {(c.foreign || []).slice(0, 4).map((p) => (
                <div className="idline" key={`f${p.publication_id}`}>
                  {p.year ?? "undated"} · another {p.distance}
                  {p.topics.length ? ` (${p.topics[0]})` : ""} · {p.title}{" "}
                  {/* Nothing ties this paper to the career, so it is offered on
                      its own, like a paper alone in time. */}
                  <button
                    className="btn sm ghost"
                    onClick={() =>
                      splitOff(
                        c,
                        [p.publication_id],
                        `the ${p.year ?? "undated"} paper in another ${p.distance}`,
                      )
                    }
                  >
                    Not this person
                  </button>
                </div>
              ))}
              {(c.break_years || 0) > 0 && (
                <div className="idline">
                  <b>
                    {c.break?.split_at
                      ? `nothing published between ${c.break.split_at.before} and ${c.break.split_at.after} — a ${c.break.split_at.gap}-year gap`
                      : `${c.break_years} years from the nearest other work`}
                  </b>
                </div>
              )}
              {(c.break?.lonely || []).slice(0, 4).map((p) => (
                <div className="idline" key={p.publication_id}>
                  {p.year} · alone by {p.alone_by} years · {p.title}{" "}
                  {/* A paper standing alone in time is the one thing on this
                      page that cannot be a group, so it gets its own button.
                      Splitting it never empties the record: it is one paper
                      among many, which is why it stood out. */}
                  <button
                    className="btn sm ghost"
                    onClick={() =>
                      splitOff(
                        c,
                        [p.publication_id],
                        `the ${p.year} paper, ${p.alone_by} years from any other work`,
                      )
                    }
                  >
                    Not this person
                  </button>
                </div>
              ))}
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
                        onClick={() =>
                          splitOff(
                            c,
                            c.group_ids[i],
                            `${g.papers} papers on ${g.topics[0] || "another subject"}`,
                          )
                        }
                      >
                        Split these off
                      </button>
                    )}
                  </div>
                ))}
              </div>
              <div className="btn-row" style={{ marginTop: 10 }}>
                {/* Splitting DOES exist now - the buttons above do it - so
                    this is for a conflation the tool cannot divide: papers
                    belonging to different people that share topics or
                    co-authors, so no group separates them. It records the
                    finding and takes the record off the queue. */}
                <button
                  className="btn primary sm"
                  onClick={() => ruleOn(c.person_id, "several_people")}
                >
                  Several people, but not separable
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
