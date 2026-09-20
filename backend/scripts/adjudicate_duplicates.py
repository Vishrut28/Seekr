"""Sort the pending duplicate pairs into the ones that are PROVEN and the rest.

review_digest.py lays a pair out for a human to read. This answers a narrower
question first, so the human only reads the pairs that still need them: does
anything here actually prove these two records are one person?

Only two things do.

  A SHARED ORCID. One identifier, claimed by one person.
  A SHARED PAPER.  The same work under both records, where the paper is
                   capable of telling two same-named people apart.

Two DIFFERENT ORCIDs prove the opposite, and that is worth as much: it takes
a pair off the queue for good rather than leaving it to be re-read every time.

Everything else the queue reports - co-authors, employers, topic overlap - is
circumstantial. It is often persuasive and it is sometimes right, but a merge
cannot be undone, so this script never calls it proof. Those pairs come back
marked hold, which means a person decides, not that nothing is known.

WHY A SHARED PAPER IS NOT AUTOMATICALLY PROOF

Both guards below come from pairs in this queue that looked proven:

  Mass authorship. Two ALICE heavy-ion records shared four papers. Those
  papers carry thousands of authors, OpenAlex stores the first hundred, and
  the name being adjudicated was not among the hundred - so the shared paper
  could not be checked at all, and every heavy-ion physicist shares it anyway.
  dedupe refuses co-author evidence above MAX_AUTHORS_FOR_COAUTHORS for the
  same reason, and the same ceiling applies here.

  The name twice. An agronomy paper carried both a Chetan Singh and a Karan
  Singh. Where the adjudicated name appears more than once, the two records
  may be the two different authors - the shared paper would then be evidence
  of the opposite of what it looks like.

WHY IT RUNS TWICE

A proven merge gives one person both records' ORCIDs, and that can settle a
pair neither side could settle alone. Here a shared paper tied an ORCID-less
record to an ORCID, which then contradicted the ORCID on a third record and
disproved a pair that had been sitting at "some evidence, short of proof".

Read-only: it decides nothing and writes nothing.

    python scripts/adjudicate_duplicates.py
    python scripts/adjudicate_duplicates.py --verbose   # show each test
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def normalise(text: str) -> str:
    """Titles differ by punctuation and markup between sources; this is what
    the arXiv copy and the proceedings copy of one paper have in common."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def initialled(name: str) -> tuple:
    """('v', 'jain') - because one source says "Vishesh Jain" and another
    "V. Jain", and they have to compare equal to be counted at all."""
    parts = [p for p in normalise(name).split() if p]
    return (parts[0][0], parts[-1]) if parts else ("", "")


def author_names(publication) -> list:
    raw = publication.raw_authors
    try:
        listed = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except (TypeError, ValueError):
        return []
    return [n for n in listed if isinstance(n, str)]


def paper_proves(a_pub, b_pub, who: tuple, ceiling: int) -> tuple:
    """Can this shared paper tell two people of the same name apart?"""
    sides = [names for names in (author_names(a_pub), author_names(b_pub)) if names]
    biggest = max((len(n) for n in sides), default=0)
    if biggest > ceiling:
        return False, f"{biggest} authors - mass authorship, tells nobody apart"
    if not sides:
        return False, "neither copy stores an author list"
    counted = [sum(1 for n in side if initialled(n) == who) for side in sides]
    if any(c == 0 for c in counted):
        return False, "the name is not in the stored author list"
    if any(c > 1 for c in counted):
        return False, f"the name appears {max(counted)} times - could be two people"
    return True, f"{biggest} authors, this name exactly once on each"


ORCID = re.compile(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", re.I)


def orcids_of(keys) -> set:
    """Every ORCID a record carries, however it is stored.

    person_key is unique on (key_type, key_value), so two living records can
    never both hold one ORCID under key_type "orcid" - resolution has already
    merged them. What does happen is one record holding it as an "orcid" key
    and another holding the same identifier as a profile "url", which the
    constraint does not touch. Reading only the typed key would miss the one
    case where a shared ORCID is still there to be found.
    """
    found = set()
    for key_type, key_value in keys:
        if key_type in ("orcid", "url"):
            match = ORCID.search(key_value or "")
            if match:
                found.add(match.group(1).upper())
    return found


def facts(session, person_id: str) -> dict:
    from rip.models import Authorship, PersonKey, Publication
    from sqlalchemy import select

    papers = session.execute(
        select(Publication).join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == person_id)
    ).scalars().all()
    orcids = orcids_of(session.execute(
        select(PersonKey.key_type, PersonKey.key_value).where(
            PersonKey.person_id == person_id)
    ).all())
    by_title = {}
    for pub in papers:
        if pub.title:
            by_title.setdefault(normalise(pub.title), pub)
    return {"orcids": orcids, "by_title": by_title, "papers": len(papers)}


def still_open(session, pairs) -> tuple:
    """The pairs that are still a live question, and where each side ended up.

    A candidate row names the person ids the sweep saw. Merges since then have
    turned some of those into tombstones, so the row has to be followed to the
    surviving records the way list_suspicious does - otherwise this adjudicates
    an emptied record, finds no papers and no ORCID on it, and calls a settled
    pair "circumstantial". Two rows that resolve to one pair are one question,
    and a row whose sides have already become one person is not a question.
    """
    from rip.review import _living

    open_pairs, living, seen = [], {}, set()
    for pair in pairs:
        a = _living(session, pair.person_id)
        b = _living(session, pair.candidate_person_id)
        if a is None or b is None or a.id == b.id:
            continue
        key = tuple(sorted((a.id, b.id)))
        if key in seen:
            continue
        seen.add(key)
        open_pairs.append(pair)
        living[pair.id] = (a.id, b.id)
    return open_pairs, living


def adjudicate(session, pairs, living, verbose=False) -> tuple:
    """[(verdict, pair, why)] where verdict is merge | reject | hold."""
    from rip.dedupe import MAX_AUTHORS_FOR_COAUTHORS as CEILING
    from rip.models import Person

    known = {}

    def cached(person_id):
        if person_id not in known:
            known[person_id] = facts(session, person_id)
        return known[person_id]

    out = []
    for pair in pairs:
        left, right = living[pair.id]
        a, b = cached(left), cached(right)
        who = initialled(session.get(Person, left).canonical_name)

        both = a["orcids"] & b["orcids"]
        if both:
            out.append(("merge", pair, f"same ORCID {sorted(both)[0]}"))
            continue

        reason = None
        for title in sorted(set(a["by_title"]) & set(b["by_title"]), key=len, reverse=True):
            ok, why = paper_proves(a["by_title"][title], b["by_title"][title], who, CEILING)
            if verbose:
                print(f"       {'OK ' if ok else '-- '}{title[:44]}: {why}")
            if ok:
                reason = f"shared paper - {why}"
                break
        if reason:
            out.append(("merge", pair, reason))
        elif a["orcids"] and b["orcids"]:
            out.append(("reject", pair,
                        f"different ORCIDs {sorted(a['orcids'])[0]} vs {sorted(b['orcids'])[0]}"))
        else:
            out.append(("hold", pair, "circumstantial only - a person decides"))
    return out, known


def propagate(verdicts, known, living) -> list:
    """Re-test the held pairs once the proven merges have pooled their ORCIDs."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for verdict, pair, _why in verdicts:
        if verdict == "merge":
            left, right = living[pair.id]
            parent[find(left)] = find(right)

    pooled = {}
    for person_id, data in known.items():
        pooled.setdefault(find(person_id), set()).update(data["orcids"])

    settled = []
    for index, (verdict, pair, why) in enumerate(verdicts):
        if verdict != "hold":
            continue
        a, b = (find(side) for side in living[pair.id])
        left, right = pooled.get(a, set()), pooled.get(b, set())
        if a != b and left and right and not (left & right):
            verdicts[index] = ("reject", pair,
                               "different ORCIDs once the proven merges are applied: "
                               f"{sorted(left)[0]} vs {sorted(right)[0]}")
            settled.append(pair)
    return settled


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", help="show every test")
    args = parser.parse_args()

    from rip.db import SessionLocal, init_db
    from rip.models import MergeCandidate, Person
    from sqlalchemy import select

    init_db()
    with SessionLocal() as session:
        pairs = session.execute(
            select(MergeCandidate).where(MergeCandidate.status == "pending")
            .order_by(MergeCandidate.id)
        ).scalars().all()
        rows = len(pairs)
        pairs, living = still_open(session, pairs)
        verdicts, known = adjudicate(session, pairs, living, verbose=args.verbose)
        settled = propagate(verdicts, known, living)

        rank = {"merge": 0, "reject": 1, "hold": 2}
        print(f"{len(pairs)} pending pairs"
              f"{f' (from {rows} candidate rows)' if rows != len(pairs) else ''}\n")
        for verdict, pair, why in sorted(verdicts, key=lambda v: (rank[v[0]], v[1].id)):
            left, right = living[pair.id]
            name = session.get(Person, left).canonical_name
            print(f"{verdict.upper():<7} #{pair.id:<5} {name[:24]:<26}"
                  f"{left[:8]} x {right[:8]}  {why}")
        tally = {k: sum(1 for v, _p, _w in verdicts if v == k) for k in rank}
        print(f"\nproven merge {tally['merge']}   disproven {tally['reject']}"
              f"   needs a human {tally['hold']}")
        if settled:
            print(f"({len(settled)} disproven only after the proven merges pooled their ORCIDs)")
        print("\nNothing was changed. Merges are irreversible; apply them from the "
              "Review page or the API once you agree.")


if __name__ == "__main__":
    main()
