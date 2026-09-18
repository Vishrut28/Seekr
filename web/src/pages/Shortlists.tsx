import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, apiSend, errorMessage, isUnauthorized } from "../api/client";
import { Banner, Loading } from "../components/EmptyState";
import { Shell } from "../components/Shell";
import { day } from "../lib/format";
import { useWorking } from "../lib/hooks";
import { Icon } from "../lib/icons";
import type { Shortlist, ShortlistDetail } from "../types";

export function Shortlists() {
  const [lists, setLists] = useState<ShortlistDetail[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useWorking(!lists && !error);

  useEffect(() => {
    let live = true;
    api<{ shortlists: Shortlist[] }>("/v1/shortlists")
      .then((d) =>
        Promise.all(d.shortlists.map((l) => api<ShortlistDetail>(`/v1/shortlists/${l.id}`))),
      )
      .then((full) => live && setLists(full))
      .catch((e) => {
        if (live && !isUnauthorized(e)) setError(errorMessage(e));
      });
    return () => {
      live = false;
    };
  }, []);

  if (error) {
    return (
      <Shell>
        <Banner>{error}</Banner>
      </Shell>
    );
  }
  if (!lists) {
    return (
      <Shell>
        <Loading message="Loading shortlists…" />
      </Shell>
    );
  }
  if (!lists.length) {
    return (
      <Shell>
        <div className="empty">
          <Icon.empty />
          <p>No shortlists yet. Save someone from a search to start one.</p>
        </div>
      </Shell>
    );
  }

  return (
    <Shell>
      {lists.map((list) => (
        <ShortlistBlock
          key={list.id}
          list={list}
          onRemoved={(personId) =>
            setLists((prev) =>
              (prev || []).map((l) =>
                l.id === list.id
                  ? {
                      ...l,
                      members: l.members.filter((m) => m.person_id !== personId),
                      count: (l.count ?? l.members.length) - 1,
                    }
                  : l,
              ),
            )
          }
        />
      ))}
    </Shell>
  );
}

function ShortlistBlock({
  list,
  onRemoved,
}: {
  list: ShortlistDetail;
  onRemoved: (personId: string) => void;
}) {
  const navigate = useNavigate();
  const [removing, setRemoving] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);

  const remove = async (personId: string) => {
    setRemoving(personId);
    try {
      await apiSend(`/v1/shortlists/${list.id}/members/${personId}`, "DELETE");
      onRemoved(personId);
    } catch {
      setFailed(personId);
      setRemoving(null);
    }
  };

  if (!list.members.length) {
    return (
      <section className="block">
        <h2>
          {list.name} <span className="n">0</span>
        </h2>
        <div className="card">
          <p className="muted pad">
            Nobody in this list yet — save someone from a search to add them.
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="block">
      <h2>
        {list.name} <span className="n">{list.count ?? list.members.length}</span>
      </h2>
      <div className="card">
        <div className="tablewrap">
          <table className="list">
            <thead>
              <tr>
                <th>Name</th>
                <th>Found by</th>
                <th>Added</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {list.members.map((m) => (
                <tr key={m.person_id} onClick={() => navigate(`/person/${m.person_id}`)}>
                  <td className="nm">{m.canonical_name || "Unnamed"}</td>
                  <td className="sk">
                    {m.found_by_query || <span className="muted">—</span>}
                  </td>
                  <td className="org">{day(m.added_at)}</td>
                  <td className="num">
                    <button
                      className="btn sm"
                      disabled={removing === m.person_id}
                      onClick={(e) => {
                        e.stopPropagation();
                        remove(m.person_id);
                      }}
                    >
                      {failed === m.person_id ? "Failed" : "Remove"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
