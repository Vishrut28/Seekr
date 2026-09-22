"""Lay the pending duplicate pairs out side by side, with the evidence.

The review page shows a pair, a score and a one-line reason. Deciding
actually needs the things that tell two people of the same name apart: who
employs them, where they are, what they work on, and whether they have ever
shared a paper or a co-author. This prints that for every pending pair, so a
queue can be worked through by reading rather than by clicking into profiles.

    python scripts/review_digest.py            # every pending pair
    python scripts/review_digest.py --id 7     # just one

Read-only: it decides nothing and writes nothing.
"""

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TOP_TOPICS = 6


def profile(session, person_id: str) -> dict:
    from rip.models import (
        Affiliation,
        Authorship,
        Evidence,
        IdentityLink,
        Organization,
        Person,
        PersonKey,
        Publication,
        SourceRecord,
    )
    from sqlalchemy import select

    person = session.get(Person, person_id)
    orgs = session.execute(
        select(Organization.name).join(Affiliation,
                                       Affiliation.organization_id == Organization.id)
        .where(Affiliation.person_id == person_id).distinct()
    ).scalars().all()
    topics = session.execute(
        select(Evidence.value).where(
            Evidence.person_id == person_id,
            Evidence.attribute_type.in_(["skill", "research_interest", "research_field"]),
        ).distinct()
    ).scalars().all()
    papers = session.execute(
        select(Publication.id, Publication.title, Publication.published_date)
        .join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == person_id)
    ).all()
    # The strong keys: an ORCID on both sides that disagrees is proof of two
    # people, and one that agrees is proof of one.
    keys = session.execute(
        select(PersonKey.key_type, PersonKey.key_value)
        .where(PersonKey.person_id == person_id).distinct()
    ).all()
    # which record each identity came from: the source is on the record, not
    # on the link, and it is the strongest "are these the same account" signal
    sources = session.execute(
        select(SourceRecord.source, SourceRecord.external_id)
        .join(IdentityLink, IdentityLink.source_record_id == SourceRecord.id)
        .where(IdentityLink.person_id == person_id).distinct()
    ).all()
    return {
        "person": person, "orgs": orgs, "topics": topics, "keys": keys,
        "papers": {pid: (title, when) for pid, title, when in papers},
        "sources": sources,
    }


def show(session, mc) -> None:
    """One pair, with the SAME signals the judge used to queue it.

    An earlier version of this counted shared co-authors by resolved person
    id and reported nought for every pair, while the judge — which counts them
    by name, off the publication records — had found twenty-three for one of
    them. A digest that disagrees with the thing it is summarising is worse
    than no digest, so the numbers come from dedupe itself.
    """
    from rip import dedupe

    a, b = profile(session, mc.person_id), profile(session, mc.candidate_person_id)
    feats = dedupe.load_features(session, [mc.person_id, mc.candidate_person_id])
    judged = dedupe.judge(feats[mc.person_id], feats[mc.candidate_person_id],
                          names_vouched=True)
    sig = judged.signals
    shared_papers = set(a["papers"]) & set(b["papers"])
    print(f"\n#{mc.id}  {a['person'].canonical_name}   vs   {b['person'].canonical_name}")
    print(f"     {(mc.signals or {}).get('reason', '')}")
    for side, data in (("A", a), ("B", b)):
        p = data["person"]
        ident = [f"{s}:{e}" for s, e in data["sources"]]
        print(f"  {side} {p.id[:8]}  {p.canonical_name}")
        ids = [f"{t}={v}" for t, v in data["keys"] if t in ("orcid", "email")]
        print(f"      strong   {', '.join(ids) or '-'}")
        print(f"      where    {p.location or '-'}   country {p.country or '-'}")
        print(f"      orgs     {', '.join(data['orgs']) or '-'}")
        print(f"      topics   {', '.join(sorted(data['topics'])[:TOP_TOPICS]) or '-'}"
              f"{' ...' if len(data['topics']) > TOP_TOPICS else ''}")
        print(f"      papers   {len(data['papers'])}   sources {', '.join(ident) or '-'}")
    print(f"  OVERLAP  papers {sig['shared_publications']}   "
          f"co-authors {sig['shared_coauthors']} ({sig['coauthor_overlap']:.0%})   "
          f"orgs {sig['shared_organizations']}   "
          f"topics {'-' if sig['topic_overlap'] is None else format(sig['topic_overlap'], '.0%')}")
    print(f"  VERDICT  {judged.decision or 'leave alone'} - {judged.reason}")
    if shared_papers:
        for pid in list(shared_papers)[:2]:
            title, when = a["papers"][pid]
            print(f"      shared: {(title or '')[:78]} ({when or '?'})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", type=int, action="append", help="only this candidate")
    args = parser.parse_args()

    from rip.db import SessionLocal, init_db
    from rip.models import MergeCandidate, Person
    from sqlalchemy import select

    init_db()
    with SessionLocal() as session:
        rows = session.execute(
            select(MergeCandidate).where(MergeCandidate.status == "pending")
            .order_by(MergeCandidate.id)
        ).scalars().all()
        if args.id:
            rows = [r for r in rows if r.id in set(args.id)]
        # one heading per name, because the queue repeats it: eight of these
        # pairs are the same name against itself
        groups = defaultdict(list)
        for mc in rows:
            # a name is optional: a GitHub account with nothing on it but a
            # login ingests as a person with aliases and no canonical name
            who = session.get(Person, mc.person_id).canonical_name or "(no name)"
            groups[who.lower()].append(mc)
        print(f"{len(rows)} pending pairs, in {len(groups)} name groups")
        for name in sorted(groups):
            print(f"\n{'=' * 72}\n{name.upper()}  ({len(groups[name])} pairs)")
            for mc in groups[name]:
                show(session, mc)


if __name__ == "__main__":
    main()
