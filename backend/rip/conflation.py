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

Measured 2026-09-21 against records read by hand - evaluation/conflation
_labels.json, reproduced by scripts/measure_conflation.py. At 0.5 and above,
with the employer check on, counting a record reported by EITHER signal:

                                    score only     with the temporal break
  precision                            35%                  52%
  recall, on conflations found
  WITHOUT this detector                 0%                  83%

Both columns are the same 63 records, labelled before the break signal
existed, so the gain is not an artefact of labelling what it found. Nine more
records the break surfaced have since been labelled too; counting those as
well puts precision at 57%, and they are marked `flagged` in the label file so
they can never count towards recall.

The recall figure is the one that matters, and it is why the break exists. An
earlier label set reported 86% recall, but every record in it came from this
detector's own candidates, so that number only ever meant: of the conflations
it pointed at, it pointed at 86%. The re-labelled set samples three ways and
records which arm chose each record - the detector's own output, signals it
cannot see (a career span of 45 years or more, breadth across the OpenAlex
domain hierarchy), and an unbiased draw. Over the arms the detector had no
part in choosing, the group-ratio score alone found NONE of six conflations:

    0f57a85c  0.29  1928-61 insulation physics + 2023-26 science education
    835353de  0.00  1895 Labrador geology + 1982 middle managers + materials
    a13f78eb  0.00  a 1952 aircraft-dynamics paper + 2000s nanomagnetics
    e3404899  0.00  a 1976 coal-boiler paper + IRENA energy roadmaps
    ed70afd5  0.09  quantum optics + Hukushima-Nemoto exchange Monte Carlo
    f0fd5769  0.12  1971 HeLa autophagy + thermophotovoltaics + LC-MS assays

The temporal break finds five of the six. The one it cannot is ed70afd5: a
1996 replica-exchange Monte Carlo paper five years before the quantum-optics
work it is filed with. Five years is a normal gap, so no rule about time will
ever see it, and that record is the honest limit of this file.

What the break costs is one false positive worth naming: cc175355, twenty-five
years of silence inside one career in Tibetan Buddhist philology, 1960 and
then 1985-2008 on the same manuscripts. A long gap is not always two people,
and the label set keeps that record deliberately.

Against a BASE RATE OF 8% in the unbiased draw, flagging is now six times
better than guessing rather than four. But this is still a ranker of records a
person should read, not a verdict: half of what it reports is one person with
range, and the Review page must go on printing the papers rather than a score.

A LOW SCORE IS STILL NOT A CLEAN BILL, for the reason ed70afd5 shows.

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

from .models import Authorship, IdentityLink, Person, Publication, SourceRecord

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

# A TEMPORAL BREAK, which is the other half of the job. The score above is a
# ratio of group sizes, so it cannot see the commonest conflation in this
# corpus: one or two intruder papers sharing no topic and no co-author with
# anything else. Those form singleton groups, and a singleton is never the
# second-LARGEST group, so a 45-paper physicist filed with a 1952 aeronautics
# paper scores 0.00 -- right by the rule and useless in fact. Measured recall
# against records found by other signals was nil.
#
# What those records have instead is a hole in time. Two shapes of it:
#
#   LONELY_PAPER_YEARS  one paper whose nearest other work is this far away.
#                       No active publication record has a fifteen-year hole
#                       around a single paper with work on both sides of it.
#   SPLIT_CAREER_YEARS  the largest gap between consecutive papers, which
#                       catches a record made of two blocks where no single
#                       paper is lonely: 1928-61 electrical insulation, then
#                       2023-26 science education, 62 years apart. Set well
#                       beyond a career break, because a sabbatical, a move
#                       to industry and a late return are all real.
#
# Measured 2026-09-21 on the 63-record labelled set: together these flag 4% of
# the corpus, 8 of the labelled records, and ALL EIGHT are conflated -- while
# finding 5 of the 6 conflations the score missed entirely. The sixth shares
# no signal with them: a 1996 replica-exchange Monte Carlo paper five years
# before the quantum-optics work it is filed with, which no gap can see.
LONELY_PAPER_YEARS = 15
SPLIT_CAREER_YEARS = 40

_WORD = re.compile(r"[^\W\d_]+")


@dataclass
class Split:
    """One person's work, as the groups it actually falls into."""

    person_id: str
    name: str | None
    papers: int
    groups: list[list[int]] = field(default_factory=list)   # publication ids
    years: list[int] = field(default_factory=list)          # one per dated paper

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

    @property
    def break_years(self) -> int:
        """The size of the temporal hole in this record, or 0 if there is none.

        Papers are compared BY POSITION, not by year value: asking for other
        years unequal to this one makes a record whose papers all share a year
        look infinitely isolated, and thirty papers from 2025 is the opposite
        of a conflation.
        """
        years = self.years
        if len(years) < 2:
            return 0
        worst = 0
        for i, year in enumerate(years):
            nearest = min(abs(year - years[j]) for j in range(len(years)) if j != i)
            if nearest >= LONELY_PAPER_YEARS:
                worst = max(worst, nearest)
        ordered = sorted(years)
        widest = max(b - a for a, b in zip(ordered, ordered[1:]))
        if widest >= SPLIT_CAREER_YEARS:
            worst = max(worst, widest)
        return worst

    @property
    def reportable(self) -> bool:
        """Worth a person's attention, by either signal."""
        return self.score >= REPORT_ABOVE or self.break_years > 0


def _year(published_date) -> list[int]:
    """[1952] or [], because a paper with no date says nothing about time."""
    text = str(published_date or "")[:4]
    return [int(text)] if text.isdigit() else []


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
        select(Publication.id, Publication.topics, Publication.raw_authors,
               Publication.published_date)
        .join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == person_id)
    ).all()
    papers, years = {}, []
    for pub_id, topics, raw, when in rows:
        marks = {("topic", t) for t in _as_list(topics)}
        marks |= {("with", c) for c in _coauthors(raw, surname)}
        papers[pub_id] = marks
        years.extend(_year(when))
    return Split(person_id=person_id, name=name, papers=len(papers),
                 groups=_group(papers), years=years)


def candidates(session: Session, above: float = REPORT_ABOVE,
               min_papers: int = MIN_PAPERS,
               check_employer: bool = True) -> list[Split]:
    """Every person whose work falls into two comparable halves.

    CHECK_EMPLOYER drops the ones whose halves share an institution the author
    put on their own papers, because one person takes their affiliation with
    them across a change of subject. Measured on the hand-labelled set that
    lifts precision from 50% to 60% and costs no recall — the only refinement
    of four tried that did anything. It reads stored OpenAlex payloads, so it
    runs over the shortlist rather than over everybody.

    One pass over authorships rather than a query per person: the whole corpus
    is a few thousand rows, and doing it per person made this too slow to run
    over everybody, which is the only way it finds anything.
    """
    names = dict(session.execute(
        select(Person.id, Person.canonical_name).where(Person.merged_into.is_(None))
    ).all())
    rows = session.execute(
        select(Authorship.person_id, Publication.id, Publication.topics,
               Publication.raw_authors, Publication.published_date)
        .join(Publication, Publication.id == Authorship.publication_id)
    ).all()

    per_person: dict[str, dict[int, set]] = {}
    per_years: dict[str, list[int]] = {}
    for person_id, pub_id, topics, raw, when in rows:
        if person_id not in names:
            continue
        surname = (names[person_id] or "").lower().split()[-1] if names[person_id] else ""
        marks = {("topic", t) for t in _as_list(topics)}
        marks |= {("with", c) for c in _coauthors(raw, surname)}
        per_person.setdefault(person_id, {})[pub_id] = marks
        per_years.setdefault(person_id, []).extend(_year(when))

    out = []
    for person_id, papers in per_person.items():
        if len(papers) < min_papers:
            continue
        split = Split(person_id=person_id, name=names.get(person_id),
                      papers=len(papers), groups=_group(papers),
                      years=per_years.get(person_id, []))
        # Either signal. ABOVE tunes the score only, so asking for a lower
        # score never hides a record the temporal break found.
        if split.score >= above or split.break_years > 0:
            out.append(split)
    if check_employer:
        out = [s for s in out if shares_an_employer(session, s) is not True]
    # Score first, then the size of the temporal hole, so a record found only
    # by the break is ordered by how implausible its gap is rather than
    # arriving at the bottom with every other 0.00.
    out.sort(key=lambda s: (-s.score, -s.break_years, -s.papers))
    return out


def _own_affiliations(session: Session, person_id: str) -> dict[str, set]:
    """Per paper, the institutions THIS author put on it.

    Not the person's affiliations as a whole — the ones attached to their own
    authorship line, paper by paper. That is how author disambiguation is
    really done, and OpenAlex hands it to us inside the payload already on
    disk. Only OpenAlex: Semantic Scholar and Europe PMC records carry no
    per-paper institutions, so people known only through those come back empty
    and are reported as unknown rather than as evidence of anything.
    """
    rows = session.execute(
        select(SourceRecord.raw)
        .join(IdentityLink, IdentityLink.source_record_id == SourceRecord.id)
        .where(IdentityLink.person_id == person_id, SourceRecord.source == "openalex")
    ).all()
    out: dict[str, set] = {}
    for (raw,) in rows:
        payload = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        mine = ((payload.get("author") or {}).get("id") or "").rsplit("/", 1)[-1]
        if not mine:
            continue
        for work in payload.get("works") or []:
            key = (work.get("id") or "").rsplit("/", 1)[-1]
            for line in work.get("authorships") or []:
                who = ((line.get("author") or {}).get("id") or "").rsplit("/", 1)[-1]
                if who != mine:
                    continue
                named = {i.get("display_name") for i in line.get("institutions") or []
                         if i.get("display_name")}
                if named:
                    out.setdefault(key, set()).update(named)
    return out


def shares_an_employer(session: Session, split: Split) -> bool | None:
    """Do the two bodies of work carry an institution in common?

    One person takes their affiliation with them: even a career that changed
    subject entirely keeps naming the same university across the turn. Two
    people filed under one name share nothing, because they were never in the
    same place.

    True when the halves overlap, False when they demonstrably do not, and
    None when the papers carry no institutions to compare — which is most of
    the corpus outside OpenAlex, and must not be read as either answer.

    On the seventeen records read by hand this was right every time it could
    answer at all: all five conflations came back False, all four single
    people True.
    """
    big = [g for g in split.groups if len(g) >= MIN_GROUP]
    if len(big) < 2:
        return None
    by_work = _own_affiliations(session, split.person_id)
    if not by_work:
        return None

    def employers(group) -> set:
        found = set()
        for (external_id,) in session.execute(
            select(Publication.external_id).where(Publication.id.in_(group))
        ):
            found |= by_work.get((external_id or "").rsplit("/", 1)[-1], set())
        return found

    first, second = employers(big[0]), employers(big[1])
    if not first or not second:
        return None
    return bool(first & second)


def break_evidence(session: Session, split: Split) -> dict:
    """Why the temporal break fired, in the terms a reader can check.

    describe() only reports groups of MIN_GROUP or more, because a stray paper
    that shares nothing is usually just a stray paper. But an intruder IS a
    singleton -- that is precisely why the score cannot see it -- so a record
    reported by the break would otherwise reach the review page showing only
    its coherent work, with nothing to say why it is there.

    Two shapes, matching the two thresholds:
      lonely    papers whose nearest other work is LONELY_PAPER_YEARS away
      split_at  the (before, after, gap) of a hole wider than a career
    """
    if not split.break_years:
        return {"lonely": [], "split_at": None}

    rows = session.execute(
        select(Publication.id, Publication.title, Publication.published_date)
        .join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == split.person_id)
    ).all()
    dated = [(pub_id, title, _year(when)[0])
             for pub_id, title, when in rows if _year(when)]
    years = [year for _id, _title, year in dated]

    lonely = []
    for index, (pub_id, title, year) in enumerate(dated):
        # by position, exactly as break_years does: papers sharing a year are
        # each other's neighbours, not outliers
        nearest = min([abs(year - years[j]) for j in range(len(years)) if j != index]
                      or [0])
        if nearest >= LONELY_PAPER_YEARS:
            lonely.append({"publication_id": pub_id, "title": title,
                           "year": year, "alone_by": nearest})

    split_at = None
    ordered = sorted(years)
    if len(ordered) >= 2:
        before, after = max(zip(ordered, ordered[1:]), key=lambda ab: ab[1] - ab[0])
        if after - before >= SPLIT_CAREER_YEARS:
            split_at = {"before": before, "after": after, "gap": after - before}

    return {"lonely": sorted(lonely, key=lambda r: -r["alone_by"]),
            "split_at": split_at}


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
