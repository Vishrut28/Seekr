import { useEffect, useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { api } from "../api/client";
import { useAuth } from "../context/auth";
import { Icon } from "../lib/icons";
import { useScrollShade, useTheme } from "../lib/hooks";
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
    ])
      .then(([sources, countries]) => {
        if (!live) return;
        setStats({
          // every source counts the same people, so the largest source is the
          // corpus size — summing them would count each person once per source
          people: sources.values.reduce((m, v) => Math.max(m, v.people), 0),
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
  children,
}: {
  topbar?: ReactNode;
  children: ReactNode;
}) {
  const { signOut, required } = useAuth();
  const [, toggleTheme] = useTheme();
  const scrolled = useScrollShade();

  return (
    <div className="shell">
      <aside className="rail">
        <div className="brand">
          <div className="mark">
            <Mark size={15} />
          </div>
          <div>
            <b>Seekr</b>
            <span>
              by Deccan<sup>AI</sup>
            </span>
          </div>
        </div>
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
          <button className="themebtn" onClick={toggleTheme}>
            Toggle theme
          </button>
          {/* nothing to sign out of when the backend wants no token */}
          {required && (
            <button className="themebtn" onClick={signOut}>
              Sign out
            </button>
          )}
        </div>
      </aside>
      <main>
        <Backdrop />
        <div className={scrolled ? "topbar scrolled" : "topbar"}>{topbar}</div>
        <div className="page">{children}</div>
      </main>
    </div>
  );
}
