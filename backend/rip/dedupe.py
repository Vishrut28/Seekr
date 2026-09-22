"""Find people stored more than once, and merge only on proof.

Ingest-time resolution merges on strong keys (ORCID, email, profile URL) or on
a near-identical name plus a shared organization. That leaves two kinds of
duplicate behind, both visible in the real graph:

- one person across sources with no shared key: OpenAlex, Semantic Scholar and
  dblp each hold "Dhruv Dixit" of BITS Hyderabad, sharing papers and nearly
  every co-author, but no identifier.
- one person split inside a scholarly index: OpenAlex is known to divide an
  author into several IDs ("Vishesh Jain" at the same three institutions,
  a third of their co-authors in common).

A name on its own proves nothing. "Rahul Gupta" is four different people in
the same graph — a cardiologist and a consumer-retail researcher with
different ORCIDs among them. So a pair is judged on evidence:

  veto     different ORCIDs; different IDs from a source that disambiguates
           people itself (dblp, ORCID, GitHub, Stack Overflow, Hugging Face);
           unrelated fields with nothing else in common
  merge    identical full name, no veto, and shared papers or a large share
           of co-authors (co-authors compared by FULL name, from papers with
           at most 25 authors — surname+initial keys on consortium papers made
           the two Rahul Guptas look like they shared 424 co-authors)
  review   plausible but short of proof -> the existing merge-review queue

Clusters are complete-link: a record joins a cluster only if it has no veto
against ANY member, so one ambiguous record cannot chain two different
people together (a Semantic Scholar record sharing a paper with both an
ecologist and a machine-learning researcher, both named Dhruv Dixit).
"""

from __future__ import annotations

import itertools
import json
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Affiliation,
    Authorship,
    Evidence,
    IdentityLink,
    MergeCandidate,
    Organization,
    Person,
    PersonKey,
    Publication,
    SourceRecord,
)
from .textnorm import org_key, words

# A different ID from one of these sources is that source saying "someone else".
DISAMBIGUATING_SOURCES = {"dblp", "orcid", "github", "stackoverflow", "huggingface", "wikidata"}
MAX_AUTHORS_FOR_COAUTHORS = 25
SKILL_ATTRS = ("skill", "research_interest", "specialization")


@dataclass
class Features:
    person_id: str
    name: str
    name_key: str
    full_words: int                       # name words longer than an initial
    pubs: set = field(default_factory=set)
    coauthors: set = field(default_factory=set)
    topics: set = field(default_factory=set)
    orgs: set = field(default_factory=set)
    orcids: set = field(default_factory=set)
    source_ids: dict = field(default_factory=lambda: defaultdict(set))
    weight: tuple = (0, 0, 0)             # which record to keep when merging


@dataclass
class Judgement:
    decision: str | None                  # "merge" | "review" | None
    reason: str
    signals: dict


def name_key(name: str | None) -> str:
    return " ".join(words(name))


def load_features(session: Session, person_ids: list[str]) -> dict[str, Features]:
    """Everything the comparison needs, in a handful of batched queries."""
    feats: dict[str, Features] = {}
    for pid, name in session.execute(
        select(Person.id, Person.canonical_name).where(Person.id.in_(person_ids))
    ).all():
        ws = words(name)
        feats[pid] = Features(pid, name or "", " ".join(ws), sum(1 for w in ws if len(w) > 1))
    ids = list(feats)
    if not ids:
        return feats

    for pid, src, ext in session.execute(
        select(IdentityLink.person_id, SourceRecord.source, SourceRecord.external_id)
        .join(SourceRecord, SourceRecord.id == IdentityLink.source_record_id)
        .where(IdentityLink.person_id.in_(ids), IdentityLink.review_state != "split")
    ).all():
        feats[pid].source_ids[src].add(ext)
    for pid, value in session.execute(
        select(PersonKey.person_id, PersonKey.key_value)
        .where(PersonKey.person_id.in_(ids), PersonKey.key_type == "orcid")
    ).all():
        feats[pid].orcids.add(value)
    evidence_n: dict[str, int] = defaultdict(int)
    for pid, attr, value in session.execute(
        select(Evidence.person_id, Evidence.attribute_type, Evidence.value)
        .where(Evidence.person_id.in_(ids))
    ).all():
        evidence_n[pid] += 1
        if attr in SKILL_ATTRS:
            feats[pid].topics.add(name_key(value))
    for pid, org in session.execute(
        select(Affiliation.person_id, Organization.name)
        .join(Organization, Organization.id == Affiliation.organization_id)
        .where(Affiliation.person_id.in_(ids))
    ).all():
        feats[pid].orgs.add(org_key(org))
    for pid, pub_id, raw_authors in session.execute(
        select(Authorship.person_id, Authorship.publication_id, Publication.raw_authors)
        .join(Publication, Publication.id == Authorship.publication_id)
        .where(Authorship.person_id.in_(ids))
    ).all():
        f = feats[pid]
        f.pubs.add(pub_id)
        authors = raw_authors if isinstance(raw_authors, list) else json.loads(raw_authors or "[]")
        if len(authors) > MAX_AUTHORS_FOR_COAUTHORS:
            continue
        own_surname = f.name_key.split()[-1] if f.name_key else ""
        for author in authors:
            ws = [w for w in words(str(author)) if len(w) > 1]
            if len(ws) >= 2 and ws[-1] != own_surname:
                f.coauthors.add(" ".join(ws))
    for pid, f in feats.items():
        f.weight = (len(f.orcids), sum(len(v) for v in f.source_ids.values()), evidence_n.get(pid, 0))
    return feats


def names_compatible(a: Features, b: Features) -> bool:
    """Could these two names be written by the same person?

    Used only for pairs someone already queued for review, where the names are
    known to be close but not identical: one name's words contained in the
    other ("Karan Singh" / "Karan P Singh"), or the same words with initials
    standing in ("Vishesh Jain" / "V. Jain"). It has to reject the near miss
    that is two people: "Michael G. Aman" and "M. Javad Aman" align on the
    surname and the first initial, and differ on the middle one.
    """
    wa, wb = a.name_key.split(), b.name_key.split()
    if not wa or not wb or wa[-1] != wb[-1]:        # same surname, or nothing to discuss
        return False
    short, long_ = sorted((wa[:-1], wb[:-1]), key=len)
    matched = 0
    for word in long_:                              # given names, in order
        if matched < len(short) and _same_given(short[matched], word):
            matched += 1
    return matched == len(short)


def _same_given(x: str, y: str) -> bool:
    """One given name written two ways: in full, or as its initial."""
    return x == y or (len(x) == 1 and y.startswith(x)) or (len(y) == 1 and x.startswith(y))


def judge(a: Features, b: Features, names_vouched: bool = False) -> Judgement:
    """Merge, review, or leave alone — and why.

    NAMES_VOUCHED relaxes the identical-name requirement to a compatible one,
    for a pair already sitting in the review queue: the name resemblance is
    what put it there, and the question left is what the evidence says. The
    merge bar is unchanged — proof, plus a distinctive name on both sides —
    so "K. Singh" still cannot be merged into anyone.
    """
    shared_pubs = len(a.pubs & b.pubs)
    shared_co = len(a.coauthors & b.coauthors)
    smaller_co = min(len(a.coauthors), len(b.coauthors))
    co_ratio = shared_co / smaller_co if smaller_co else 0.0
    topic_j = (len(a.topics & b.topics) / len(a.topics | b.topics)) if a.topics and b.topics else None
    shared_orgs = len(a.orgs & b.orgs)
    signals = {
        "shared_publications": shared_pubs, "shared_coauthors": shared_co,
        "coauthor_overlap": round(co_ratio, 2),
        "topic_overlap": None if topic_j is None else round(topic_j, 2),
        "shared_organizations": shared_orgs,
    }

    if not a.name_key or not b.name_key:
        return Judgement(None, "names differ", signals)
    if a.name_key != b.name_key and not (names_vouched and names_compatible(a, b)):
        return Judgement(None, "names differ", signals)
    if a.orcids and b.orcids and not (a.orcids & b.orcids):
        return Judgement(None, "different ORCIDs — two people", signals)
    for src in DISAMBIGUATING_SOURCES:
        if a.source_ids.get(src) and b.source_ids.get(src) \
                and not (a.source_ids[src] & b.source_ids[src]):
            return Judgement(None, f"{src} lists them as different people", signals)
    nothing_shared = shared_pubs == 0 and shared_orgs == 0 and co_ratio < 0.2
    if (topic_j == 0 and len(a.topics) >= 3 and len(b.topics) >= 3 and nothing_shared):
        return Judgement(None, "unrelated fields and nothing in common", signals)

    distinctive = a.full_words >= 2 and b.full_words >= 2
    proof = (
        shared_pubs >= 2
        or (shared_pubs >= 1 and co_ratio >= 0.5)
        or (co_ratio >= 0.3 and shared_co >= 5 and (shared_orgs >= 1 or (topic_j or 0) >= 0.2))
    )
    if proof and distinctive:
        return Judgement("merge", _describe(signals), signals)
    plausible = shared_pubs >= 1 or shared_co >= 2 or shared_orgs >= 1 or (topic_j or 0) >= 0.3
    if plausible:
        why = "initials-only name" if proof else "some evidence, short of proof"
        return Judgement("review", f"{why}: {_describe(signals)}", signals)
    return Judgement(None, "no evidence they are the same person", signals)


def _describe(s: dict) -> str:
    parts = []
    if s["shared_publications"]:
        parts.append(f"{s['shared_publications']} shared papers")
    if s["shared_coauthors"]:
        parts.append(f"{s['shared_coauthors']} shared co-authors ({s['coauthor_overlap']:.0%})")
    if s["shared_organizations"]:
        parts.append(f"{s['shared_organizations']} shared organizations")
    if s["topic_overlap"]:
        parts.append(f"topic overlap {s['topic_overlap']:.0%}")
    return ", ".join(parts) or "no shared evidence"


@dataclass
class Plan:
    merges: list = field(default_factory=list)    # (keep_id, gone_id, Judgement)
    reviews: list = field(default_factory=list)   # (a_id, b_id, Judgement)
    groups_examined: int = 0


def plan(session: Session, person_ids: list[str] | None = None) -> Plan:
    """Decide merges and reviews for same-named people (all, or those sharing a
    name with PERSON_IDS)."""
    rows = session.execute(
        select(Person.id, Person.canonical_name).where(Person.merged_into.is_(None))
    ).all()
    groups: dict[str, list[str]] = defaultdict(list)
    for pid, name in rows:
        key = name_key(name)
        if len(key.split()) >= 2:
            groups[key].append(pid)
    if person_ids is not None:
        wanted = {name_key(n) for pid, n in rows if pid in set(person_ids)}
        groups = {k: v for k, v in groups.items() if k in wanted}

    # Pairs a human (or an earlier run) already has in the review queue —
    # including REJECTED ones: rejecting records that they are two people, and
    # that decision must not be re-proposed.
    # A "deferred" row is not a decision — it says the pair had no evidence
    # either way when it was last looked at, so it is allowed back into the
    # queue once there is some.
    already = {
        frozenset(pair) for pair in session.execute(
            select(MergeCandidate.person_id, MergeCandidate.candidate_person_id)
            .where(MergeCandidate.status != "deferred")
        ).all()
    }

    result = Plan()
    for key, ids in groups.items():
        if len(ids) < 2:
            continue
        result.groups_examined += 1
        feats = load_features(session, ids)
        judged = {
            frozenset((x, y)): judge(feats[x], feats[y])
            for x, y in itertools.combinations(ids, 2)
        }
        vetoed = {pair for pair, j in judged.items()
                  if j.decision is None and j.reason != "no evidence they are the same person"}
        rejected = {frozenset(p) for p in session.execute(
            select(MergeCandidate.person_id, MergeCandidate.candidate_person_id)
            .where(MergeCandidate.status == "rejected",
                   MergeCandidate.person_id.in_(ids), MergeCandidate.candidate_person_id.in_(ids))
        ).all()}
        # a human said "different people": that is a veto, not a suggestion
        vetoed |= rejected
        for pair in rejected:
            if judged[pair].decision == "merge":
                judged[pair] = Judgement(None, "rejected in review", judged[pair].signals)

        # complete-link clustering over proven pairs, strongest first
        cluster_of = {pid: {pid} for pid in ids}
        ambiguous: set = set()
        for pair, j in sorted(((p, j) for p, j in judged.items() if j.decision == "merge"),
                              key=lambda pj: strength(pj[1]), reverse=True):
            x, y = tuple(pair)
            cx, cy = cluster_of[x], cluster_of[y]
            if cx is cy:
                continue
            if any(frozenset((m, n)) in vetoed for m in cx for n in cy):
                continue
            if _torn(x, cy, pair, j, ids, judged, vetoed, cluster_of) \
                    or _torn(y, cx, pair, j, ids, judged, vetoed, cluster_of):
                ambiguous.add(pair)
                continue
            merged = cx | cy
            for m in merged:
                cluster_of[m] = merged

        clusters = {id(c): c for c in cluster_of.values()}.values()
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            keep = max(cluster, key=lambda pid: (feats[pid].weight, pid))
            for gone in sorted(cluster - {keep}):
                j = judged[frozenset((keep, gone))]
                if j.decision != "merge":
                    # joined through another member: say so
                    j = Judgement("merge", "same person as a record it was proven to match", j.signals)
                result.merges.append((keep, gone, j))
        for pair, j in judged.items():
            x, y = sorted(pair)
            if cluster_of[x] is cluster_of[y] or pair in already:
                continue
            if j.decision == "review":
                result.reviews.append((x, y, j))
            elif pair in ambiguous:
                result.reviews.append((x, y, Judgement(
                    "review", f"proven match, but it matches a different person almost as "
                              f"well: {j.reason}", j.signals)))
    return result


def strength(j: Judgement) -> float:
    """One number for how much two records have in common."""
    s = j.signals
    return s["shared_publications"] + 3 * s["coauthor_overlap"] + 0.5 * s["shared_organizations"]


def _torn(record, target, pair, j, ids, judged, vetoed, cluster_of) -> bool:
    """Would joining RECORD to the TARGET cluster be a coin toss?

    True when RECORD has comparable evidence (at least half as strong) toward
    someone the target cluster is vetoed against — a record matching two
    provably different people equally well identifies neither of them.
    """
    best = strength(j)
    for other in ids:
        if other == record or other in target or other in cluster_of[record]:
            continue
        rival = judged.get(frozenset((record, other)))
        if rival is None or strength(rival) < 0.5 * best or strength(rival) == 0:
            continue
        if any(frozenset((other, member)) in vetoed for member in target):
            return True
    return False


def merge_pair(session: Session, keep: str, gone: str, reason: str) -> bool:
    """Merge one proven pair, recording why. False if either side is already gone."""
    from .models import ChangeLog
    from .resolution import sync_name_tokens
    from .review import merge_persons

    k, g = session.get(Person, keep), session.get(Person, gone)
    if k is None or g is None or k.merged_into or g.merged_into:
        return False
    merge_persons(session, keep, gone)
    session.add(ChangeLog(person_id=keep, field="dedupe", old_value=f"person:{gone}",
                          new_value=reason[:500]))
    sync_name_tokens(session, session.get(Person, keep))
    session.commit()
    return True


def apply(session: Session, planned: Plan) -> tuple[int, int]:
    """Carry out a plan: merges through review.merge_persons, reviews queued."""
    merged = 0
    for keep, gone, j in planned.merges:
        if merge_pair(session, keep, gone, j.reason):
            merged += 1

    queued = 0
    for a, b, j in planned.reviews:
        exists = session.execute(
            select(MergeCandidate).where(
                MergeCandidate.person_id.in_([a, b]), MergeCandidate.candidate_person_id.in_([a, b]))
        ).scalars().first()
        pa, pb = session.get(Person, a), session.get(Person, b)
        if pa is None or pb is None or pa.merged_into or pb.merged_into:
            continue
        if exists is not None:
            if exists.status == "deferred":      # evidence has since appeared
                exists.status = "pending"
                exists.score = float(j.signals["coauthor_overlap"])
                exists.signals = {**j.signals, "reason": j.reason}
                queued += 1
            continue
        session.add(MergeCandidate(person_id=a, candidate_person_id=b,
                                   score=float(j.signals["coauthor_overlap"]),
                                   signals={**j.signals, "reason": j.reason}))
        queued += 1
    session.commit()
    return merged, queued
