"""Find person records that are really several people.

Sources disambiguate authors themselves, and they get it wrong. One OpenAlex
author id in this corpus holds a paediatric surgeon's operations and a
combinatorialist's theorems; another spans 1973 sea-urchin gametes to 2025
reinforcement learning. Seekr did not merge those — it created each person
from a single source record, `via new`, and inherited the mistake whole.

A conflated record is worse than a duplicate. A duplicate splits one person's
evidence in two and weakens both halves; a conflation asserts things about
somebody that are not true of them, answers searches with the wrong half, and
offers the merge queue a pair that cannot be decided because one side is not a
person. Nothing detected these at all, so they were invisible.

WHAT IS MEASURED

One person's work hangs together: the papers share topics, or co-authors, or
both. So link every pair of their papers that shares either, and see what
falls out. A career forms one group. Two people under one id form two, with
nothing bridging them — the surgeon's colleagues never appear on the theorems
and the topics have nothing in common.

The score is the second-largest group over the largest. One dominant group
scores near zero; two comparable bodies of work score near one.

HOW WELL, EXACTLY

Measured against seventeen records read by hand — evaluation/conflation_labels
.json, reproduced by scripts/measure_conflation.py — reporting at 0.5 and
above gives HALF PRECISION AND 86% RECALL. Six of the twelve it flags are
really several people; it misses one of seven.

Half. Not the "half to two-thirds" an earlier version of this file claimed
from spot-checking three of them; checking all seventeen gave a worse and
more honest number.

WHAT DID NOT WORK, so nobody spends the afternoon again

Three refinements were implemented and measured, and every one made the
detector worse or did nothing:

  requiring the groups to cover a share of the work   50% -> 44% precision
  topic-graph distance between the two groups         does not separate them
  whether the groups' active years overlap            does not separate them

Coverage fails because a single-group record covers nearly everything, so a
high coverage is the signature of a CLEAN record, not a split one. Distance
fails outright: the widest gap in the whole benchmark, five hops, belongs to
an immunologist who is one person, while the plainest conflation in it —
a paediatric surgeon filed with a combinatorialist — sits at three. Years
fail because a conflation and a change of field look identical from outside.
Raising the threshold does not help either: at 0.9 precision FALLS to 40%,
because the top of the list is where the two-paper artefacts live.

What actually separated them was reading the titles and knowing that
petroleum waterflood and dengue entomology are not one career while arterial
stiffness and COPD pharmacy are. None of the features here encode that.

WHAT IT IS NOT

A candidate generator, not a verdict — the same contract as the merge queue.
It flagged a record holding irrigation engineering, petroleum recovery,
dengue entomology AND gas turbines (three people at least), and also a
biostatistician whose real range across cancer trials, exercise studies and
eating disorders merely looks like two careers.

A LOW SCORE IS NOT A CLEAN BILL. Milder conflations sit right on top of
legitimate range: a record mixing agronomy with wireless sensor networks
scores 0.10, exactly what a forensic scientist with one side interest scores.
This finds the conflations big enough to see, which are the ones that do the
most damage; it does not find them all, and it is not built to.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Authorship, Person, Publication

# Above this many names a paper says nothing about who anyone collaborates
# with: a 100-author collaboration would link every physicist to every other.
# Same cutoff dedupe uses for the same reason.
MAX_AUTHORS = 25
# Below this, groups are too small to mean anything either way.
MIN_PAPERS = 6
# A group has to reach this size to count as a body of work rather than a
# stray paper that simply shares nothing.
MIN_GROUP = 2
# Reported by default, and measured: 0.5 is the best of every threshold tried
# (scripts/measure_conflation.py). Higher is not better — precision falls to
# 40% at 0.9, because two-paper artefacts score 1.00 as easily as two careers.
REPORT_ABOVE = 0.5

_WORD = re.compile(r"[^\W\d_]+")


@dataclass
class Split:
    """One person's work, as the groups it actually falls into."""

    person_id: str
    name: str | None
    papers: int
    groups: list[list[int]] = field(default_factory=list)   # publication ids

    @property
    def sizes(self) -> list[int]:
        return [len(g) for g in self.groups]

    @property
    def score(self) -> float:
        """Second-largest body of work over the largest."""
        big = [g for g in self.groups if len(g) >= MIN_GROUP]
        if len(big) < 2:
            return 0.0
        return len(big[1]) / len(big[0])


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    try:
        return json.loads(value or "[]")
    except (TypeError, ValueError):
        return []


def _coauthors(raw, surname: str) -> set[str]:
    """Who else was on this paper, by name, minus the person themselves."""
    authors = _as_list(raw)
    if len(authors) > MAX_AUTHORS:
        return set()
    out = set()
    for author in authors:
        words = [w for w in _WORD.findall(str(author).lower()) if len(w) > 1]
        if len(words) >= 2 and words[-1] != surname:
            out.add(" ".join(words))
    return out


def _group(papers: dict[int, set]) -> list[list[int]]:
    """Papers joined wherever they share anything, largest group first."""
    parent = {pid: pid for pid in papers}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    shared: dict = {}
    for pid, marks in papers.items():
        for mark in marks:
            shared.setdefault(mark, []).append(pid)
    for pids in shared.values():
        first = pids[0]
        for other in pids[1:]:
            a, b = find(first), find(other)
            if a != b:
                parent[a] = b

    groups: dict = {}
    for pid in papers:
        groups.setdefault(find(pid), []).append(pid)
    return sorted(groups.values(), key=len, reverse=True)


def split_of(session: Session, person_id: str, name: str | None = None) -> Split:
    """How one person's papers group. See the module docstring for the rule."""
    if name is None:
        person = session.get(Person, person_id)
        name = person.canonical_name if person else None
    surname = (name or "").lower().split()[-1] if name else ""
    rows = session.execute(
        select(Publication.id, Publication.topics, Publication.raw_authors)
        .join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == person_id)
    ).all()
    papers = {}
    for pub_id, topics, raw in rows:
        marks = {("topic", t) for t in _as_list(topics)}
        marks |= {("with", c) for c in _coauthors(raw, surname)}
        papers[pub_id] = marks
    return Split(person_id=person_id, name=name, papers=len(papers),
                 groups=_group(papers))


def candidates(session: Session, above: float = REPORT_ABOVE,
               min_papers: int = MIN_PAPERS) -> list[Split]:
    """Every person whose work falls into two comparable halves.

    One pass over authorships rather than a query per person: the whole corpus
    is a few thousand rows, and doing it per person made this too slow to run
    over everybody, which is the only way it finds anything.
    """
    names = dict(session.execute(
        select(Person.id, Person.canonical_name).where(Person.merged_into.is_(None))
    ).all())
    rows = session.execute(
        select(Authorship.person_id, Publication.id, Publication.topics,
               Publication.raw_authors)
        .join(Publication, Publication.id == Authorship.publication_id)
    ).all()

    per_person: dict[str, dict[int, set]] = {}
    for person_id, pub_id, topics, raw in rows:
        if person_id not in names:
            continue
        surname = (names[person_id] or "").lower().split()[-1] if names[person_id] else ""
        marks = {("topic", t) for t in _as_list(topics)}
        marks |= {("with", c) for c in _coauthors(raw, surname)}
        per_person.setdefault(person_id, {})[pub_id] = marks

    out = []
    for person_id, papers in per_person.items():
        if len(papers) < min_papers:
            continue
        split = Split(person_id=person_id, name=names.get(person_id),
                      papers=len(papers), groups=_group(papers))
        if split.score >= above:
            out.append(split)
    out.sort(key=lambda s: (-s.score, -s.papers))
    return out


def describe(session: Session, split: Split, per_group: int = 4) -> list[dict]:
    """The groups as a reader can judge them: titles, years, topics."""
    out = []
    for group in split.groups:
        if len(group) < MIN_GROUP:
            continue
        rows = session.execute(
            select(Publication.title, Publication.published_date, Publication.topics)
            .where(Publication.id.in_(group))
        ).all()
        topics: dict[str, int] = {}
        for _title, _when, raw in rows:
            for topic in _as_list(raw):
                topics[topic] = topics.get(topic, 0) + 1
        years = sorted(w[:4] for _t, w, _x in rows if w)
        out.append({
            "papers": len(group),
            "years": f"{years[0]}-{years[-1]}" if years else "?",
            "topics": [t for t, _n in sorted(topics.items(), key=lambda kv: -kv[1])[:4]],
            "titles": [t for t, _w, _x in rows[:per_group]],
        })
    return out
