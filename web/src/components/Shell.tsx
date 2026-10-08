import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../context/auth";
import { Icon } from "../lib/icons";
import { useScrollShade } from "../lib/hooks";
import { Backdrop, Mark } from "../lib/mark";
import type { FacetResponse } from "../types";
import { CountUp } from "./CountUp";

const NAV = [
  { to: "/search", label: "Search", icon: Icon.search },
  { to: "/shortlists", label: "Shortlists", icon: Icon.bookmark },
  { to: "/review", label: "Review", icon: Icon.review },
  { to: "/sources", label: "Sources", icon: Icon.plug },
];

interface Stats {
  people: number;
  sources: number;
  countries: number;
}

function RailStats() {
  const [stats, setStats] = useState<Stats | null>(null);

  useEffect(() => {
    let live = true;
    Promise.all([
      api<FacetResponse>("/v1/facets?field=source"),
      api<FacetResponse>("/v1/facets?field=country&limit=200"),
      // The corpus size, counted once. It used to be taken as the largest
      // source's people-count, on the reasoning that summing the sources
      // would count somebody once per source they appear in. That is true of
      // summing, but the largest source is not the whole corpus either: it
      // silently leaves out everybody known ONLY through a smaller one, and
      // reported 323 people out of 658.
      api<{ total_matches: number }>("/v1/persons?limit=1"),
    ])
      .then(([sources, countries, all]) => {
        if (!live) return;
        setStats({
          people: all.total_matches,
          sources: sources.values.length,
          countries: countries.values.length,
        });
      })
      .catch(() => {
        /* the rail is decoration; a failure here must not take the page down */
      });
    return () => {
      live = false;
    };
  }, []);

  if (!stats) return null;
  return (
    <>
      <div className="stat">
        <span>People</span>
        <CountUp to={stats.people} />
      </div>
      <div className="stat">
        <span>Sources</span>
        <CountUp to={stats.sources} />
      </div>
      <div className="stat">
        <span>Countries</span>
        <CountUp to={stats.countries} />
      </div>
    </>
  );
}

export function Shell({
  topbar,
  landing = false,
  children,
}: {
  topbar?: ReactNode;
  /** the start page: no topbar, and its own large mark in place of the watermark */
  landing?: boolean;
  children: ReactNode;
}) {
  const { signOut, required } = useAuth();
  const scrolled = useScrollShade();

  return (
    <div className="shell">
      <aside className="rail">
        {/* the logo goes home: back to the start page */}
        <Link to="/search" className="brand" title="Start page">
          <div className="mark">
            <Mark size={15} />
          </div>
          <div>
            <b>Seekr</b>
            <span>
              by Deccan<sup>AI</sup>
            </span>
          </div>
        </Link>
        <nav>
          {NAV.map(({ to, label, icon: IconFn }) => (
            <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "active" : "")}>
              <IconFn />
              {label}
            </NavLink>
          ))}
        </nav>
        <div className="rail-foot">
          <RailStats />
          {/* nothing to sign out of when the backend wants no token */}
          {required && (
            <button className="themebtn" onClick={signOut}>
              Sign out
            </button>
          )}
        </div>
      </aside>
      <main className={landing ? "landing" : undefined}>
        {landing ? (
          children
        ) : (
          <>
            <Backdrop />
            <div className={scrolled ? "topbar scrolled" : "topbar"}>{topbar}</div>
            <div className="page">{children}</div>
          </>
        )}
      </main>
    </div>
  );
}
