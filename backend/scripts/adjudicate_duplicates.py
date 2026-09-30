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

What makes a shared paper proof is a NAME SLOT only one person can be in: the
name appears exactly once, on an author list that is all there. Both guards
below come from pairs in this queue that looked proven:

  A truncated list. Two ALICE heavy-ion records shared four papers. Those
  carry thousands of authors and OpenAlex stores the first hundred, so the
  stored list is not the list - the name could be on the paper a second time
  where nothing can see it, and the name being adjudicated was not even among
  the hundred that were kept.

  The name twice. An agronomy paper carried both a Chetan Singh and a Karan
  Singh. Where the adjudicated name appears more than once, the two records
  may be the two different authors - the shared paper would then be evidence
  of the opposite of what it looks like.

An earlier version rejected on CROWD SIZE instead, borrowing dedupe's ceiling
of 25 co-authors. That ceiling is right for counting shared collaborators,
where a crowd tells nobody apart, and wrong here: it threw away the one real
proof in the queue, a 35-author KLOE drift-chamber paper with a single
R. Messi on it, claimed on ORCID by one of the two records. Thirty-five
authors fully listed name one R. Messi; a thousand truncated to a hundred
name nobody in particular.

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


# OpenAlex stores the first 100 authorships of a work and drops the rest. At
# or above that, the stored list may not be the whole list, and the name could
# be on the paper a second time where it cannot be seen.
AUTHOR_LIST_CAP = 100


def self_claimed(pub) -> bool:
    """Did the person put this work on their own record?

    An ORCID work entry is the holder's own claim to a paper, which is a
    stronger statement than an author list a crawler assembled -- and ORCID
    stores no author list at all, so without this the claim reads as missing
    data and gets thrown away.
    """
    return (pub.external_id or "").startswith("orcid-work")


def paper_proves(a_pub, b_pub, who: tuple) -> tuple:
    """Can this shared paper tell two people of the same name apart?

    What makes a shared paper proof is a name slot only one person can be in:
    the name appears exactly once, on a list that is all there. So the limit
    here is TRUNCATION, not crowd size. An earlier version borrowed the
    co-author ceiling of 25 instead, on the reasoning that a crowd tells
    nobody apart -- true of counting shared collaborators, wrong here, and it
    threw away the one real proof in the queue: a 35-author KLOE paper with a
    single R. Messi on it, claimed on ORCID by one of the two records.
    """
    sides = [(names, pub) for names, pub in
             ((author_names(a_pub), a_pub), (author_names(b_pub), b_pub))]
    listed = [(names, pub) for names, pub in sides if names]
    if not listed:
        return False, "neither copy stores an author list"
    # A side with no list is only acceptable when the person claimed the work
    # themselves; otherwise it is simply unknown and proves nothing.
    for names, pub in sides:
        if not names and not self_claimed(pub):
            return False, "one copy stores no author list and was not self-claimed"

    biggest = max(len(names) for names, _pub in listed)
    if biggest >= AUTHOR_LIST_CAP:
        return False, (f"{biggest} authors - at the storage cap, so the list may be "
                       f"cut off and the name could be on it twice unseen")
    counted = [sum(1 for n in names if initialled(n) == who) for names, _pub in listed]
    if any(c == 0 for c in counted):
        return False, "the name is not in the stored author list"
    if any(c > 1 for c in counted):
        return False, f"the name appears {max(counted)} times - could be two people"
    claimed = " and claimed on ORCID by the other" if len(listed) == 1 else ""
    return True, f"{biggest} authors, this name exactly once{claimed}"


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
    from sqlalchemy import select

    from rip.models import Authorship, PersonKey, Publication

    papers = session.execute(
        select(Publication).join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == person_id)
    ).scalars().all()
    orcids = orcids_of(session.execute(
        select(PersonKey.key_type, PersonKey.key_value).where(
            PersonKey.person_id == person_id)
    ).all())
    by_title: dict[str, Publication] = {}
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
            ok, why = paper_proves(a["by_title"][title], b["by_title"][title], who)
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
    parent: dict[str, str] = {}

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

    pooled: dict[str, set[str]] = {}
    for person_id, data in known.items():
        pooled.setdefault(find(person_id), set()).update(data["orcids"])

    settled = []
    for index, (verdict, pair, _why) in enumerate(verdicts):
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

    from sqlalchemy import select

    from rip.db import SessionLocal, init_db
    from rip.models import MergeCandidate, Person

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
            # a name is optional: a bare GitHub login ingests without one
            person = session.get(Person, left)
            name = (person.canonical_name if person else None) or "(no name)"
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
