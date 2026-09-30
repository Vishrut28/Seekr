"""Draw the pool for relevance labels that nothing in Seekr wrote.

evaluation/judgments.json grades people by criteria this project wrote for
itself, from the same stored topics the ranker reads -- twice a perfect score
turned out to be the grader and the ranker agreeing with each other. Labels
that mean something have to come from reading a person, not from matching
their stored words.

This draws WHO gets read. For a stratified, salted sample of the judged
queries it pools four arms, so no single one decides who is looked at:

    seekr     the top ten search actually returns
    criteria  people the self-written criteria call relevant (so the labels
              also test the criteria)
    words     people whose papers, bio or topics hold every content word of
              the query -- a naive baseline
    random    anyone, for the floor

Cards carry what a reader needs -- affiliations, stated topics, paper titles,
bio, place -- and nothing about the arm, a score or a rank; they are shuffled
by the salt. The arms are kept in the file, for scoring after labelling, and
must not be read while labelling.

Seven cards are marked for calibration: a person labels them, the same cards
are labelled independently here without seeing theirs, and only if the two
agree are the rest labelled the same way.

    python scripts/draw_relevance_pool.py            # writes evaluation/relevance_pool.json

No network (connectors are tripwired) and not the live database (it reads a
copy), as every benchmark here must.
"""

import hashlib
import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

SALT = "seekr relevance pool 2026-09-30"
DRAWN_AT = "2026-09-30"
PER_KIND = {"topic": 5, "concept": 4, "agent": 2, "constrained": 3, "soft": 2,
            "typo": 1, "holdout": 3}
ARMS = {"seekr": 10, "criteria": 5, "words": 5, "random": 3}
CALIBRATION = {"seekr": 3, "criteria": 1, "words": 2, "random": 1}
OUT = HERE.parent / "evaluation" / "relevance_pool.json"


def salted(key: str) -> str:
    return hashlib.sha256(f"{SALT}|{key}".encode()).hexdigest()


def tripwire() -> None:
    """Any attempt to reach the network fails loudly."""
    import httpx

    def refuse(*_a, **_k):
        raise RuntimeError("the relevance pool must not reach the network")

    httpx.Client.send = refuse                                 # type: ignore[method-assign]
    from rip.connectors.openalex import OpenAlexConnector
    OpenAlexConnector.fetch = refuse                           # type: ignore[method-assign]


def card(session, person_id: str) -> dict:
    """What a reader needs to judge relevance, and nothing about why they are here."""
    from rip.models import Affiliation, Authorship, Evidence, Organization, Person, Publication
    from sqlalchemy import select

    p = session.get(Person, person_id)
    orgs = [name for (name,) in session.execute(
        select(Organization.name).join(Affiliation, Affiliation.organization_id == Organization.id)
        .where(Affiliation.person_id == person_id)).all()]
    topics, bio = [], None
    for attr, value in session.execute(
            select(Evidence.attribute_type, Evidence.value).where(Evidence.person_id == person_id)):
        if attr in ("skill", "research_interest", "specialization") and value not in topics:
            topics.append(value)
        elif attr == "bio" and not bio:
            bio = value
    titles = [t for (t,) in session.execute(
        select(Publication.title).join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == person_id)
        .order_by(Publication.citations.desc().nullslast()).limit(6)).all()]
    return {
        "person_id": person_id,
        "name": p.canonical_name,
        "role": p.current_role,
        "organizations": list(dict.fromkeys([o for o in [p.current_organization, *orgs] if o]))[:4],
        "location": p.location or p.country,
        "topics": topics[:10],
        "titles": titles,
        "bio": ((bio or p.summary or "")[:400]) or None,
    }


def main() -> None:
    from evaluation.grader import grade, load_cases, load_profiles

    live = HERE.parent / "rip.db"
    work = Path(tempfile.mkdtemp(prefix="relevance-pool-")) / "corpus.db"
    src, dst = sqlite3.connect(f"file:{live.as_posix()}?mode=ro", uri=True), sqlite3.connect(work)
    src.backup(dst)
    src.close()
    dst.close()
    os.environ["RIP_DATABASE_URL"] = f"sqlite:///{work.as_posix()}"
    tripwire()

    from rip.db import SessionLocal
    from rip.nlq import execute_progressive, parse
    from rip.textnorm import stems

    cases = load_cases()
    chosen = []
    for kind, n in PER_KIND.items():
        of_kind = sorted((c for c in cases if c.kind == kind), key=lambda c: salted(c.id))
        chosen.extend(of_kind[:n])

    rng = random.Random(SALT)
    queries, cards, arms_of = [], {}, {}
    with SessionLocal() as session:
        profiles = load_profiles(session)
        everyone = sorted(profiles)
        for case in chosen:
            parsed = parse(session, case.query)
            people, _effective, _dropped = execute_progressive(session, parsed)
            arms = {"seekr": [p.id for p in people[:ARMS["seekr"]]]}
            relevant = sorted(pid for pid, prof in profiles.items() if grade(case, prof) > 0)
            arms["criteria"] = sorted(relevant, key=lambda pid: salted(f"{case.id}|c|{pid}"))
            words = [w for w in stems(case.query) if len(w) > 2]
            matching = sorted(
                pid for pid, prof in profiles.items()
                if words and all(any(w in run for run in (*prof.topics, *prof.texts))
                                 for w in words))
            arms["words"] = sorted(matching, key=lambda pid: salted(f"{case.id}|w|{pid}"))
            arms["random"] = rng.sample(everyone, min(len(everyone), 12))

            pool: dict[str, list[str]] = {}
            for arm, want in ARMS.items():
                taken = 0
                for pid in arms[arm]:
                    if taken >= want:
                        break
                    if pid in pool:
                        pool[pid].append(arm)
                        continue
                    pool[pid] = [arm]
                    taken += 1
            order = sorted(pool, key=lambda pid: salted(f"{case.id}|order|{pid}"))
            ids = []
            for pid in order:
                card_id = salted(f"{case.id}|{pid}")[:12]
                cards[card_id] = {"query_id": case.id, **card(session, pid)}
                arms_of[card_id] = pool[pid]
                ids.append(card_id)
            queries.append({"id": case.id, "kind": case.kind, "query": case.query, "cards": ids})

    calibration = []
    for arm, n in CALIBRATION.items():
        candidates = sorted((cid for cid, a in arms_of.items() if a[0] == arm and cid not in calibration),
                            key=lambda cid: salted(f"calibration|{cid}"))
        calibration.extend(candidates[:n])

    OUT.write_text(json.dumps({
        "_comment": [
            "Pool for relevance labels nothing in Seekr wrote. Drawn by",
            "scripts/draw_relevance_pool.py BEFORE any labelling; see its docstring.",
            "Grades: 2 clearly what the query asks for, 1 partly or arguably,",
            "0 not. Judge the person on the card, not the words.",
            "`arms` says how each card entered the pool: do not read it while labelling.",
        ],
        "drawn_at": DRAWN_AT, "salt": SALT, "per_kind": PER_KIND, "arms_taken": ARMS,
        "queries": queries, "cards": cards, "calibration": sorted(calibration),
        "arms": arms_of,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    shutil.rmtree(work.parent, ignore_errors=True)
    print(f"{len(queries)} queries, {len(cards)} cards, {len(calibration)} for calibration -> {OUT}")


if __name__ == "__main__":
    main()
