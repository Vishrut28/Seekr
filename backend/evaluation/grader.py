"""Relevance judgments and ranking metrics for Seekr's search.

The ranker had never been measured: six hand-set weights, a handful of
feedback votes, and a benchmark that counted results without asking whether
they were right. This is the yardstick every other search change is held to.

Judgments are criteria, not lists of IDs. Each query in judgments.json says
what makes a person relevant — the subjects that count, and the constraints
(country, organization, name) that must hold — and the grader applies that to
EVERY person in the corpus. Three things follow:

- recall is measurable, because the whole relevant set is known, not just the
  people the ranker happened to return;
- the judgments survive the corpus changing: a newly ingested cosmologist is
  judged by the same rule as the rest;
- they are short enough for a person to read and disagree with.

Graded relevance:
  2  a subject from `strong` in the person's stated topics (skills, research
     interests, specializations), and every constraint holds
  1  a subject from `strong` in at least two of their publication titles,
     bio or role — or a subject from `related` in their topics — constraints
     holding
  0  anything else

The criteria deliberately go wider than the parser's vocabulary ("deep
learning" counts "convolutional" and "neural network" people), because what is
being measured includes whether search understands related subjects.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

JUDGMENTS = Path(__file__).with_name("judgments.json")
TOPIC_ATTRS = ("skill", "research_interest", "specialization")
TEXT_ATTRS = ("bio", "role", "headline", "education")


@dataclass
class Profile:
    """Everything the grader reads about one person, loaded once."""
    person_id: str
    name: str
    country: str | None
    names: list[tuple[str, ...]] = field(default_factory=list)    # canonical + aliases, folded words
    topics: list[tuple[str, ...]] = field(default_factory=list)   # stemmed word runs
    texts: list[tuple[str, ...]] = field(default_factory=list)
    orgs: list[tuple[str, ...]] = field(default_factory=list)


def load_profiles(session: Session) -> dict[str, Profile]:
    from rip.geo import city_country, country_in_text
    from rip.models import Affiliation, Evidence, Organization, Person, Publication, Authorship
    from rip.textnorm import stems, words

    profiles: dict[str, Profile] = {}
    for pid, name, aliases, country, location, current_org in session.execute(
        select(Person.id, Person.canonical_name, Person.aliases, Person.country, Person.location,
               Person.current_organization).where(Person.merged_into.is_(None))
    ).all():
        code = (country or "").strip().upper() or country_in_text(location) or city_country(location)
        profile = Profile(pid, name or "", code or None)
        for n in [name, *(aliases or [])]:
            if n:
                profile.names.append(tuple(words(str(n))))
        if current_org:
            profile.orgs.append(tuple(stems(current_org)))
        profiles[pid] = profile
    for pid, attr, value in session.execute(
        select(Evidence.person_id, Evidence.attribute_type, Evidence.value)
    ).all():
        profile = profiles.get(pid)
        if profile is None or not value:
            continue
        if attr in TOPIC_ATTRS:
            profile.topics.append(tuple(stems(value)))
        elif attr in TEXT_ATTRS:
            profile.texts.append(tuple(stems(value)))
    for pid, title in session.execute(
        select(Authorship.person_id, Publication.title)
        .join(Publication, Publication.id == Authorship.publication_id)
    ).all():
        if pid in profiles and title:
            profiles[pid].texts.append(tuple(stems(title)))
    for pid, org in session.execute(
        select(Affiliation.person_id, Organization.name)
        .join(Organization, Organization.id == Affiliation.organization_id)
    ).all():
        if pid in profiles and org:
            profiles[pid].orgs.append(tuple(stems(org)))
    return profiles


def _mention_count(runs: list[tuple[str, ...]], phrases: list[tuple[str, ...]]) -> int:
    """How many of RUNS (topics, titles) contain any of PHRASES."""
    count = 0
    for run in runs:
        for phrase in phrases:
            n = len(phrase)
            if n and any(run[i:i + n] == phrase for i in range(len(run) - n + 1)):
                count += 1
                break
    return count


def _mentions(runs: list[tuple[str, ...]], phrases: list[tuple[str, ...]]) -> bool:
    return _mention_count(runs, phrases) > 0


# One paper title is not a research area: pandemic-era titles put "COVID-19"
# in front of crop geneticists and database researchers alike. Two is.
MIN_TITLE_MENTIONS = 2


@dataclass
class Case:
    id: str
    query: str
    kind: str
    strong: list[tuple[str, ...]]
    related: list[tuple[str, ...]]
    country: str | None
    org: list[tuple[str, ...]]
    person_ids: list[str]
    name: tuple[str, ...] = ()
    # a query whose constraints nobody may meet in full: the best answer is
    # then the subject without them, graded 1 instead of 0. The fallback needs
    # the subject STATED -- see grade().
    soft: bool = False
    note: str = ""
    # people for this subject were ingested after the query failed: the score
    # describes coverage added on its behalf, not that search generalises
    covered: str = ""
    # the search code was changed after this query was measured failing, so
    # the same caveat applies for a different reason
    informed: str = ""


def load_cases(path: Path = JUDGMENTS) -> list[Case]:
    from rip.textnorm import stems, words

    data = json.loads(path.read_text(encoding="utf-8"))
    cases = []
    for raw in data["cases"]:
        cases.append(Case(
            id=raw["id"], query=raw["query"], kind=raw.get("kind", "topic"),
            strong=[tuple(stems(s)) for s in raw.get("strong", [])],
            related=[tuple(stems(s)) for s in raw.get("related", [])],
            country=raw.get("country"),
            org=[tuple(stems(o)) for o in raw.get("org", [])],
            person_ids=raw.get("person_ids", []),
            name=tuple(words(raw.get("name", ""))),
            soft=bool(raw.get("soft", False)),
            note=raw.get("note", ""),
            covered=raw.get("covered", ""),
            informed=raw.get("informed", ""),
        ))
    return cases


def meets_constraints(case: Case, profile: Profile) -> bool:
    """Whether the country and organization asked for hold. Separate from
    grade() because how many people meet a constraint AT ALL is the thing
    that says whether a query can measure it: eight people are at Oxford, so
    "deep learning researchers at Oxford" returning none of them is a real
    miss, not an empty corpus."""
    return not (
        (case.country and profile.country != case.country)
        or (case.org and not _mentions(profile.orgs, case.org))
    )


def grade(case: Case, profile: Profile) -> int:
    if case.person_ids:
        return 2 if profile.person_id in case.person_ids else 0
    if case.name:
        return _name_grade(case.name, profile.names)
    constrained = meets_constraints(case, profile)
    if not case.strong and not case.related:
        return 2 if constrained else 0   # constraints alone ("people at Google")
    if _mentions(profile.topics, case.strong):
        subject = 2
    elif (_mention_count(profile.texts, case.strong) >= MIN_TITLE_MENTIONS
          or _mentions(profile.topics, case.related)):
        subject = 1
    else:
        return 0
    if constrained:
        return subject
    # The soft fallback says: the subject without the constraint is the best
    # answer left. "The subject" has to mean the one they STATE. Two paper
    # titles is already weak evidence, and weak evidence plus a failed
    # constraint is two levels of not-really that used to add up to relevant
    # -- 61 of the 102 people graded relevant to "deep learning researchers
    # at Oxford" were neither deep learning researchers nor at Oxford.
    return 1 if case.soft and subject == 2 else 0


def _name_grade(wanted: tuple[str, ...], names: list[tuple[str, ...]]) -> int:
    """2: every word of the name asked for is in one of the person's names
    ("Rahul Mulajkar" in "Rahul Mukundrao Mulajkar"). 1: same surname and
    first initial ("R. Mulajkar"). Names are not stemmed: Rogers is not Roger."""
    best = 0
    for have in names:
        if set(wanted) <= set(have):
            return 2
        if have and wanted and have[-1] == wanted[-1] and have[0][:1] == wanted[0][:1]:
            best = 1
    return best


def dcg(gains: list[int]) -> float:
    return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(gains))


def score_case(case: Case, ranked_ids: list[str], profiles: dict[str, Profile], k: int = 10) -> dict:
    grades = {pid: grade(case, p) for pid, p in profiles.items()}
    relevant = {pid for pid, g in grades.items() if g > 0}
    top = [grades.get(pid, 0) for pid in ranked_ids[:k]]
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = dcg(ideal)
    # A soft query whose constraint nobody meets TOGETHER WITH the subject has
    # quietly become the subject query: every relevant person carries the same
    # gain, so any order of them scores 1.000 and a ranker that ignored the
    # constraint entirely is indistinguishable from one that honoured it. The
    # score is still worth having -- relaxing to the subject is what the
    # product should do -- but it measures the subject, and says so here
    # rather than being read as evidence the constraint works.
    holders = (sum(1 for p in profiles.values() if meets_constraints(case, p))
               if (case.country or case.org) else None)
    return {
        "id": case.id,
        "query": case.query,
        "kind": case.kind,
        "returned": len(ranked_ids),
        "relevant_in_corpus": len(relevant),
        "meets_constraint": holders,
        "subject_ingested": bool(case.covered),
        "system_informed": bool(case.informed),
        "subject_only": bool(case.soft and relevant
                             and not any(g == 2 for g in grades.values())),
        "p_at_10": (sum(1 for g in top if g > 0) / k) if relevant else None,
        # precision over what was returned, for queries with fewer than k answers
        "precision": (sum(1 for g in top if g > 0) / len(top)) if top else None,
        "ndcg_at_10": (dcg(top) / idcg) if idcg else None,
        "recall_at_50": (len(relevant & set(ranked_ids[:50])) / len(relevant)) if relevant else None,
        "first_relevant_rank": next((i + 1 for i, g in enumerate(
            grades.get(pid, 0) for pid in ranked_ids) if g > 0), None),
    }


def summarize(rows: list[dict]) -> dict:
    def mean(key, subset=rows):
        values = [r[key] for r in subset if r[key] is not None]
        return round(sum(values) / len(values), 4) if values else None

    judged = [r for r in rows if r["relevant_in_corpus"]]
    out = {
        "queries": len(rows),
        "measuring_the_subject_only": [r["id"] for r in rows if r.get("subject_only")],
        # A query the corpus cannot answer scores nothing and averages into
        # nothing, so it sits in the set looking like a measurement. Name it.
        "grading_nobody": [r["id"] for r in rows if not r["relevant_in_corpus"]],
        # a query whose subject was ingested BECAUSE it failed is answered by
        # a corpus changed on its behalf, so its score is coverage, not reach
        "subject_ingested_after_failing": [r["id"] for r in rows
                                           if r.get("subject_ingested")],
        # the search code was changed knowing this query failed
        "search_changed_after_failing": [r["id"] for r in rows
                                         if r.get("system_informed")],
        "ndcg_at_10": mean("ndcg_at_10"),
        "p_at_10": mean("p_at_10"),
        "precision": mean("precision"),
        "recall_at_50": mean("recall_at_50"),
        "zero_results_with_relevant_people": sum(1 for r in judged if r["returned"] == 0),
        "by_kind": {},
    }
    for kind in sorted({r["kind"] for r in rows}):
        subset = [r for r in rows if r["kind"] == kind]
        out["by_kind"][kind] = {
            "queries": len(subset),
            "ndcg_at_10": mean("ndcg_at_10", subset),
            "recall_at_50": mean("recall_at_50", subset),
            "zero": sum(1 for r in subset if r["returned"] == 0 and r["relevant_in_corpus"]),
        }
    return out
