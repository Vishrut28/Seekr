import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, errorMessage, isUnauthorized } from "../api/client";
import { Banner, Loading } from "../components/EmptyState";
import { Network } from "../components/Network";
import { Dossier } from "../components/Dossier";
import { Shell } from "../components/Shell";
import { BRANDS, BrandLinks } from "../lib/brands";
import { day, year } from "../lib/format";
import { useWorking } from "../lib/hooks";
import { Icon } from "../lib/icons";
import type {
  Affiliation,
  Attribute,
  Conflict,
  DocumentLink,
  GraphEdge,
  GraphNode,
  PersonSummary,
  Project,
  ProvenanceSource,
  Publication,
} from "../types";

/** Everything the profile shows, fetched in one shot. Nine endpoints look
 *  like a lot, but each is a different table and they are independent — one
 *  slow section should not hold up the rest of the page. */
interface Profile {
  person: PersonSummary;
  publications: Publication[];
  projects: Project[];
  affiliations: Affiliation[];
  provenance: ProvenanceSource[];
  conflicts: Conflict[];
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
  cvs: DocumentLink[];
  profiles: DocumentLink[];
}

async function loadProfile(id: string): Promise<Profile> {
  const at = (suffix: string) => `/v1/persons/${id}${suffix}`;
  const [person, publications, projects, organizations, provenance, conflicts, graph, documents] =
    await Promise.all([
      api<PersonSummary>(at("")),
      api<{ publications: Publication[] }>(at("/publications")),
      api<{ projects: Project[] }>(at("/projects")),
      api<{ affiliations: Affiliation[] }>(at("/organizations")),
      api<{ sources: ProvenanceSource[] }>(at("/provenance")),
      api<{ conflicts: Conflict[] }>(at("/conflicts")),
      api<{ nodes: GraphNode[]; edges: GraphEdge[] }>(at("/graph")),
      api<{ cvs: DocumentLink[]; profiles: DocumentLink[] }>(at("/documents")),
    ]);
  return {
    person,
    publications: publications.publications,
    projects: projects.projects,
    affiliations: organizations.affiliations,
    provenance: provenance.sources,
    conflicts: conflicts.conflicts,
    graph,
    cvs: documents.cvs,
    profiles: documents.profiles,
  };
}

/** Co-author network, one to three hops out. Depth 1 comes with the profile;
 *  deeper walks are fetched only when asked for, since they grow quickly. */
function NetworkSection({
  personId,
  initial,
}: {
  personId: string;
  initial: { nodes: GraphNode[]; edges: GraphEdge[] };
}) {
  const [depth, setDepth] = useState(1);
  const [graph, setGraph] = useState(initial);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDepth(1);
    setGraph(initial);
  }, [personId, initial]);

  const choose = (next: number) => {
    setDepth(next);
    setError(null);
    if (next === 1) {
      setGraph(initial);
      return;
    }
    setLoading(true);
    api<{ nodes: GraphNode[]; edges: GraphEdge[] }>(
      `/v1/persons/${personId}/graph?depth=${next}&limit_coauthors=10&max_nodes=200`,
    )
      .then(setGraph)
      .catch((e) => {
        if (!isUnauthorized(e)) setError(errorMessage(e));
      })
      .finally(() => setLoading(false));
  };

  if (initial.edges.length === 0) return null;
  return (
    <section className="block">
      <h2>
        Network <span className="n">{graph.nodes.length - 1}</span>
        <span className="depthsel" role="group" aria-label="Co-author hops">
          {[1, 2, 3].map((d) => (
            <button
              key={d}
              className={"btn sm" + (d === depth ? " primary" : "")}
              aria-pressed={d === depth}
              disabled={loading}
              onClick={() => choose(d)}
            >
              {d === 1 ? "1 hop" : `${d} hops`}
            </button>
          ))}
        </span>
      </h2>
      {error && <Banner>{error}</Banner>}
      <div className="card">
        {loading ? (
          <Loading message="Walking the network…" inline />
        ) : (
          <Network nodes={graph.nodes} edges={graph.edges} selfId={personId} />
        )}
      </div>
    </section>
  );
}

export function Person() {
  const { id = "" } = useParams();
  const [profile, setProfile] = useState<Profile | null>(null);
  const [error, setError] = useState<string | null>(null);

  useWorking(!profile && !error);

  useEffect(() => {
    let live = true;
    setProfile(null);
    setError(null);
    loadProfile(id)
      .then((p) => live && setProfile(p))
      .catch((e) => {
        if (live && !isUnauthorized(e)) setError(errorMessage(e));
      });
    return () => {
      live = false;
    };
  }, [id]);

  const topbar = (
    <Link className="btn sm" to="/search">
      <Icon.back /> Back to results
    </Link>
  );

  if (error) {
    return (
      <Shell topbar={topbar}>
        <Banner>{error}</Banner>
      </Shell>
    );
  }
  if (!profile) {
    return (
      <Shell topbar={topbar}>
        <Loading message="Loading profile…" />
      </Shell>
    );
  }

  const { person, provenance, conflicts, graph } = profile;
  const attrs: Attribute[] = [...(person.attributes || [])].sort(
    (a, b) => b.evidence_count - a.evidence_count,
  );
  const corroborated = attrs.some((a) => a.evidence_count > 1);
  const disputed = conflicts.filter((c) => c.status === "active").length;

  return (
    <Shell topbar={topbar}>
      {person.merged_into && (
        <Banner kind="info">
          This record was merged; showing the canonical profile.
        </Banner>
      )}

      <div className="phead">
        <div style={{ flex: 1, minWidth: 240 }}>
          <h1>{person.canonical_name || "Unnamed"}</h1>
          <div className="role">
            {[person.current_role, person.current_organization, person.location]
              .filter(Boolean)
              .join(" · ") || " "}
          </div>
          <BrandLinks urls={person.profile_urls} max={BRANDS.length} />
          <div className="badges">
            {corroborated && <span className="badge ok">Corroborated</span>}
            {disputed > 0 && <span className="badge warn">{disputed} disputed</span>}
            <span className="badge">
              {provenance.length} source{provenance.length === 1 ? "" : "s"}
            </span>
            {person.country && <span className="badge">{person.country}</span>}
          </div>
          {person.aliases && person.aliases.length > 0 && (
            <div className="idline aka" title={person.aliases.join(" · ")}>
              also {person.aliases.slice(0, 5).join(" · ")}
              {person.aliases.length > 5 ? ` +${person.aliases.length - 5} more` : ""}
            </div>
          )}
          <div className="idline">
            {person.id} · updated {day(person.updated_at)}
          </div>
        </div>

        {/* the report a human reads before a conversation — viewable inline,
            or downloaded as a PDF, from the same place */}
        <Dossier personId={person.id} />
      </div>

      <div className="grid2">
        <section className="block">
          <h2>
            Attributes <span className="n">{attrs.length}</span>
          </h2>
          <div className="card">
            <div className="inner">
              <table className="data">
                <tbody>
                  <tr>
                    <th>Type</th>
                    <th>Value</th>
                    <th className="num">Sources</th>
                  </tr>
                  {attrs.length === 0 ? (
                    <tr>
                      <td className="muted">No attributes yet</td>
                    </tr>
                  ) : (
                    attrs.slice(0, 40).map((a) => (
                      <tr key={a.attribute_type + a.value}>
                        <td className="muted">{a.attribute_type}</td>
                        <td>
                          {a.value}
                          <div className="idline">{a.sources.join(", ")}</div>
                        </td>
                        <td className="num">{a.evidence_count}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>

        <section className="block">
          <h2>
            Affiliations <span className="n">{profile.affiliations.length}</span>
          </h2>
          <div className="card">
            <div className="inner">
              <table className="data">
                <tbody>
                  <tr>
                    <th>Organization</th>
                    <th>Role</th>
                    <th>Period</th>
                  </tr>
                  {profile.affiliations.length === 0 ? (
                    <tr>
                      <td className="muted">None recorded</td>
                    </tr>
                  ) : (
                    profile.affiliations.map((a, i) => (
                      <tr key={a.organization + i}>
                        <td>
                          {a.organization}
                          <div className="idline">{a.relation}</div>
                        </td>
                        <td>{a.role || "—"}</td>
                        <td className="muted">
                          {[a.start_date, a.end_date].filter(Boolean).join("–") ||
                            (a.is_current ? "current" : "—")}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      </div>

      {disputed > 0 && (
        <section className="block">
          <h2>
            Disputed facts <span className="n">{conflicts.length}</span>
          </h2>
          {conflicts.map((c, i) => (
            <div className="conflict" key={c.attribute + i}>
              <div className="ct">{c.attribute}</div>
              <div className="vs">
                <div className="side">
                  <b>{c.side_a.value}</b>
                  <span>per {c.side_a.source || "unknown"}</span>
                </div>
                <div className="mid">vs</div>
                <div className="side">
                  <b>{c.side_b.value}</b>
                  <span>per {c.side_b.source || "unknown"}</span>
                </div>
              </div>
            </div>
          ))}
        </section>
      )}

      <section className="block">
        <h2>Links &amp; documents</h2>
        <div className="card">
          <div className="inner">
            {profile.cvs.length > 0 ? (
              <table className="data">
                <tbody>
                  <tr>
                    <th>CV / résumé</th>
                    <th>Found on</th>
                  </tr>
                  {profile.cvs.map((c) => (
                    <tr key={c.url}>
                      <td>
                        <a href={c.url} target="_blank" rel="noopener noreferrer">
                          {c.url}
                        </a>
                      </td>
                      <td className="muted">{c.evidence || c.found_on || ""}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted" style={{ fontSize: 13 }}>
                No published CV found. Seekr only links documents a person or their
                institution publishes.
              </p>
            )}
            {profile.profiles.length > 0 && (
              <table className="data" style={{ marginTop: 12 }}>
                <tbody>
                  <tr>
                    <th>Profile</th>
                    <th>Type</th>
                  </tr>
                  {profile.profiles.map((pf) => (
                    <tr key={pf.url}>
                      <td>
                        <a href={pf.url} target="_blank" rel="noopener noreferrer">
                          {pf.url}
                        </a>
                      </td>
                      <td className="muted">{pf.kind}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </section>

      {profile.publications.length > 0 && (
        <section className="block">
          <h2>
            Publications <span className="n">{profile.publications.length}</span>
          </h2>
          <div className="card">
            <div className="tablewrap">
              <table className="list">
                <thead>
                  <tr>
                    <th>Title</th>
                    <th>Venue</th>
                    <th>Year</th>
                    <th className="num">Citations</th>
                  </tr>
                </thead>
                <tbody>
                  {profile.publications.slice(0, 30).map((w, i) => (
                    <tr key={w.title + i}>
                      <td>
                        {w.url ? (
                          <a href={w.url} target="_blank" rel="noopener noreferrer">
                            {w.title}
                          </a>
                        ) : (
                          w.title
                        )}
                      </td>
                      <td className="muted">{w.venue || "—"}</td>
                      <td className="muted">{year(w.published_date)}</td>
                      <td className="num">{w.citations ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      {profile.projects.length > 0 && (
        <section className="block">
          <h2>
            Projects <span className="n">{profile.projects.length}</span>
          </h2>
          <div className="card">
            <div className="tablewrap">
              <table className="list">
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Tech</th>
                    <th>Activity</th>
                    <th>Last active</th>
                  </tr>
                </thead>
                <tbody>
                  {profile.projects.map((x, i) => (
                    <tr key={x.name + i}>
                      <td>
                        {x.url ? (
                          <a href={x.url} target="_blank" rel="noopener noreferrer">
                            {x.name}
                          </a>
                        ) : (
                          x.name
                        )}
                      </td>
                      <td className="muted">{(x.technologies || []).join(", ") || "—"}</td>
                      <td className="muted">
                        {Object.entries(x.activity || {})
                          .map(([k, v]) => `${k} ${v}`)
                          .join(" · ") || "—"}
                      </td>
                      <td className="muted">{x.last_active_at || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      )}

      <NetworkSection personId={person.id} initial={graph} />

      <section className="block">
        <h2>
          Provenance <span className="n">{provenance.length}</span>
        </h2>
        <div className="card">
          <div className="tablewrap">
            <table className="list">
              <thead>
                <tr>
                  <th>Source</th>
                  <th>Record</th>
                  <th>Matched by</th>
                  <th>Review</th>
                  <th>Seen</th>
                </tr>
              </thead>
              <tbody>
                {provenance.map((s, i) => (
                  <tr key={s.source + s.external_id + i}>
                    <td>
                      <span className="srcpill">{s.source}</span>
                    </td>
                    <td>
                      {s.url ? (
                        <a href={s.url} target="_blank" rel="noopener noreferrer">
                          {s.external_id}
                        </a>
                      ) : (
                        s.external_id
                      )}
                    </td>
                    <td className="muted">{s.match_signals?.reason || s.match_method}</td>
                    <td>
                      {s.review_state === "approved" ? (
                        <span className="vstate corroborated">approved</span>
                      ) : (
                        <span className="vstate unverified">
                          {s.review_state || "unreviewed"}
                        </span>
                      )}
                    </td>
                    <td className="muted">{day(s.last_observed)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    </Shell>
  );
}
