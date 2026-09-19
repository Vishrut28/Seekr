import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, apiSend, errorMessage, isUnauthorized } from "../api/client";
import { Banner, EmptyState, Loading } from "../components/EmptyState";
import { Shell } from "../components/Shell";
import { useWorking } from "../lib/hooks";
import type { DuplicateCandidate, FuzzyMerge } from "../types";

interface Queue {
  possible_duplicates: DuplicateCandidate[];
  fuzzy_merges: FuzzyMerge[];
}

/** Enough of an id to tell two people of the same name apart. */
const short = (id: string) => (id || "").slice(0, 8);

export function Review() {
  const [queue, setQueue] = useState<Queue | null>(null);
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
    </Shell>
  );
}
