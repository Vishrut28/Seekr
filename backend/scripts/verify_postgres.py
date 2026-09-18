"""Run the query paths that are engine-specific against a real Postgres.

SQLite forgives things Postgres does not, and the search index writes SQL by
hand: correlated EXISTS per constraint, negated EXISTS for exclusions, a
totals lookup for count filters, and additive column/index migrations. This
builds a small corpus in a Postgres database, asks the same questions, and
checks the answers match what the same data gives on SQLite.

    createdb seekr_verify                      # or: psql -c "CREATE DATABASE seekr_verify"
    export RIP_TEST_POSTGRES_URL=postgresql+psycopg://user:pass@127.0.0.1:5432/seekr_verify
    python scripts/verify_postgres.py

It writes only to that database, and drops every table it made when it is
done (--keep to inspect them instead).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CASES = [
    ("machine learning researchers", {"Ada Both", "Bob Google", "Cy Micro", "Dee Vision"}),
    ("machine learning researchers not at Google", {"Cy Micro", "Dee Vision"}),
    ("machine learning researchers at both Google and Microsoft", {"Ada Both"}),
    ("machine learning researchers at Google", {"Ada Both", "Bob Google"}),
    ("machine learning researchers in India", {"Dee Vision"}),
    ("machine learning researchers not in India", {"Ada Both", "Bob Google", "Cy Micro"}),
    ("machine learning researchers with at least 3 papers", {"Ada Both"}),
    ("machine learning researchers with over 100 citations", {"Ada Both", "Bob Google"}),
    ("people with at least 500 citations", {"Ada Both"}),
    ("computer vision but not deep learning", {"Cy Micro"}),
    ("Dee Vision", {"Dee Vision"}),
]

PEOPLE = [
    # name, topics, orgs, location, papers (citations each)
    ("Ada Both", ["Machine Learning and Algorithms"], ["Google", "Microsoft"], "Berlin, Germany",
     [300, 200, 100]),
    ("Bob Google", ["Machine Learning and Algorithms"], ["Google"], "Zurich, Switzerland", [150]),
    ("Cy Micro", ["Machine Learning and Algorithms", "Computer Vision and Pattern Recognition"],
     ["Microsoft"], "Seattle, United States", [10]),
    ("Dee Vision", ["Machine Learning and Algorithms", "Deep Learning"], [], "Pune, India", [5]),
]


def seed(session):
    from rip.ingest import ingest_profile
    from rip.normalize import (EvidenceItem, NormalizedProfile, OrgAffiliation,
                               PublicationData)

    for index, (name, topics, orgs, location, papers) in enumerate(PEOPLE):
        ingest_profile(session, NormalizedProfile(
            source="openalex", source_type="scholarly", external_id=f"A{index}",
            url=f"https://openalex.org/A{index}", raw={}, name=name, location=location,
            organizations=[OrgAffiliation(name=o) for o in orgs],
            evidence=[EvidenceItem(attribute_type="research_interest", value=t) for t in topics],
            publications=[PublicationData(title=f"{name} paper {i}", external_id=f"A{index}-{i}",
                                          citations=c) for i, c in enumerate(papers)]))
    session.commit()


def answers(session):
    from rip.nlq import count_matches, execute_progressive, parse

    out = {}
    for query, _expected in CASES:
        parsed = parse(session, query)
        rows, _applied, _dropped = execute_progressive(session, parsed)
        names = {p.canonical_name for p in rows if not getattr(p, "partial_match", None)}
        out[query] = (names, count_matches(session, parsed))
    return out


def run(url: str, keep: bool) -> int:
    os.environ["RIP_DATABASE_URL"] = url
    for module in [m for m in list(sys.modules) if m.startswith("rip")]:
        del sys.modules[module]
    from rip import models  # noqa: F401
    from rip import search_index  # noqa: F401
    from rip.db import Base, SessionLocal, engine, init_db

    print(f"connecting to {url.split('@')[-1]}")
    init_db()
    with SessionLocal() as session:
        seed(session)
        search_index.rebuild(session)
        session.commit()
        got = answers(session)

    failures = 0
    for query, expected in CASES:
        names, total = got[query]
        ok = names == expected and total == len(expected)
        failures += not ok
        mark = "ok  " if ok else "FAIL"
        print(f"  {mark} {query:52s} {sorted(names)} (total {total})")
        if not ok:
            print(f"       expected {sorted(expected)}")
    if not keep:
        Base.metadata.drop_all(engine)
        print("dropped the tables it created")
    return failures


def from_env_file() -> str | None:
    """RIP_TEST_POSTGRES_URL out of .env, so the password stays in your file."""
    path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    if not os.path.exists(path):
        return None
    for raw in open(path, encoding="utf-8"):
        line = raw.strip()
        if line.startswith("RIP_TEST_POSTGRES_URL="):
            return line.split("=", 1)[1].strip().strip("\"'") or None
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("RIP_TEST_POSTGRES_URL") or from_env_file())
    parser.add_argument("--keep", action="store_true", help="leave the tables in place")
    args = parser.parse_args()
    if not args.url:
        sys.exit("set RIP_TEST_POSTGRES_URL (or pass --url) to a Postgres database "
                 "this may create and drop tables in")
    if "postgres" not in args.url:
        sys.exit("that is not a Postgres URL")
    failures = run(args.url, args.keep)
    print("\nall query paths agree with SQLite" if not failures else f"\n{failures} mismatches")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
