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

FOREIGN WORK, added 2026-09-26, because what follows was not enough

An independent draw of 18 records put the detector below at 1 of 13
conflations found by other means: 8%. Its misses had two shapes, a single
intruder paper with no hole in time around it, and two people publishing in
the same decades. Both are work nothing ties to the career -- no co-author,
no institution on the author's own line, no citation -- on subjects the career
never touches. See FOREIGN_WEIGHTS for the rule.

On the 77 unrepaired records of conflation_labels.json, which it was DESIGNED
on and so cannot be judged by: foreign work or a break reports 15 of 25
conflations for 5 false alarms, against the previous default's 5 for 13.

JUDGED ONCE on evaluation/conflation_judge_draw.json -- 120 records drawn by
hash and committed before any of this was written, then labelled blind: 11
conflated. The previous default flagged none of the 120 and found 0 of 11.
This finds 4 of 11 (36%), and 4 of the 6 records it flags there are
conflated; it flags 11% of people with six or more papers. Eleven positives
make that 4 a wide estimate, somewhere between a sixth and two thirds.

ROUND 2, 2026-09-27, 120 further records from the same order, unopened until
then (17 conflated): at the old threshold of 2 this found 8 of 17 (47%) with
67% of its flags right -- a second, independent reading of the same design.
At the threshold of 1 now in force it finds 10 of 17 (59%), 53% of its
flags right (counting the one unsure record it flags as wrong), queuing 17%
of people with six or more papers. That is the
figure to quote; round 1 chose the threshold and can no longer judge it.

What it still misses, and why, so nobody rediscovers it:
  a record with NO topic in OpenAlex's taxonomy -- people known only from
  Semantic Scholar, Europe PMC or ORCID. Foreign work compares subjects, so
  it is blind there: 5 of round 2's 7 misses, and 27% of people with six or
  more papers. Only the temporal break can see them.
  a broad career already touches the intruder's subfield (a Monte Carlo
  statistician's control-engineering work covers a power-systems paper).
  a spurious link pulls the intruder into the career group.
  an intruder that is a consortium paper, skipped by design (a JET fusion
  overview on a Sandia chemist).

HOW WELL, EXACTLY (the group ratio and the break, before foreign work)

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
# The group ratio's threshold WHEN IT IS ASKED FOR. It no longer reports by
# default: on the 77 unrepaired design labels (2026-09-26) it added 2
# conflations to what the other two signals find, for 11 false alarms. 0.5 is
# still the best of the thresholds tried; higher is not better — precision
# falls at 0.9, because two-paper artefacts score 1.00 as easily as careers.
REPORT_ABOVE = 0.5

# FOREIGN WORK, the signal that replaced the ratio as the default. Join papers
# on what only the same person shares -- a co-author, an institution the
# author put on their own authorship line, a citation between them or a
# reference in common -- and take the largest resulting body as the career.
# Any other body whose OpenAlex subjects are disjoint from the career's, at
# subfield, field or domain level, is foreign work: nothing ties it to this
# person and it is not even about the same things.
#
# Topics alone are NOT a link here, which is the difference from the ratio's
# grouping: OpenAlex topics are broad enough that two people's papers chain
# through them (the parasitologist and the medicinal chemist named William
# Trager share one), and then no group separates them.
#
# Each foreign paper scores by how far away it is -- the weights below -- and
# the weights ORDER the queue, farthest first. A record is reported at
# FOREIGN_REPORT_AT = 1: a single paper foreign only by subfield is enough.
# It was 2 until 2026-09-27, on the worry that one subfield is often a
# mis-tagged topic; round 1 of the judging draw showed four of seven misses
# were exactly such papers, and round 2 -- 120 records nobody had opened --
# judged the change: 10 of 17 conflations found instead of 8, 53% of flags
# right instead of 67%, 17% of the corpus queued instead of 11%. The worry
# was real (five more false alarms) and smaller than the misses.
# Groups made only of papers with more than MAX_AUTHORS names are skipped: a
# consortium paper links to nothing by construction, and Global Burden of
# Disease papers span every disease.
#
# Designed 2026-09-26 on conflation_labels.json and nothing else; judged on
# evaluation/conflation_judge_draw.json, drawn and committed before any of
# this was written, one round per decision. See scripts/measure_conflation.py.
FOREIGN_WEIGHTS = {"subfield": 1, "field": 2, "domain": 3}
FOREIGN_REPORT_AT = 1

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
    # publication id -> how far it is from the career: subfield, field, domain
    foreign: dict[int, str] = field(default_factory=dict)

    @property
    def sizes(self) -> list[int]:
        return [len(g) for g in self.groups]

    @property
    def foreign_score(self) -> int:
        """Foreign papers, each weighted by how far from the career it is."""
        return sum(FOREIGN_WEIGHTS[level] for level in self.foreign.values())

    def reasons(self, above: float | None = None) -> list[str]:
        """Which signals report this record. The ratio only when asked for."""
        out = []
        if self.foreign_score >= FOREIGN_REPORT_AT:
            out.append("foreign")
        if self.break_years > 0:
            out.append("break")
        if above is not None and self.score >= above:
            out.append("ratio")
        return out

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
        """Worth a person's attention by a default signal."""
        return bool(self.reasons())


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


def _tail(url) -> str:
    """The last path segment: the bare OpenAlex id, however it was written."""
    return str(url or "").rsplit("/", 1)[-1]


def _as_payload(raw) -> dict:
    return raw if isinstance(raw, dict) else json.loads(raw or "{}")


def _works_in(payload: dict) -> dict[str, dict]:
    """Per work in one OpenAlex author payload: the institutions THIS author
    put on it, and what the work cites.

    Not the person's affiliations as a whole — the ones attached to their own
    authorship line, paper by paper. That is how author disambiguation is
    really done, and OpenAlex hands it to us inside the payload already on
    disk. Semantic Scholar and Europe PMC records carry neither, so people
    known only through those come back empty, which reads as unknown rather
    than as evidence of anything.
    """
    mine = _tail((payload.get("author") or {}).get("id"))
    if not mine:
        return {}
    out = {}
    for work in payload.get("works") or []:
        named = set()
        for line in work.get("authorships") or []:
            if _tail((line.get("author") or {}).get("id")) != mine:
                continue
            named |= {i.get("display_name") for i in line.get("institutions") or []
                      if i.get("display_name")}
        out[_tail(work.get("id"))] = {
            "institutions": named,
            "references": {_tail(r) for r in work.get("referenced_works") or []},
        }
    return out


def _learn_hierarchy(payload: dict, into: dict) -> None:
    for work in payload.get("works") or []:
        for topic in work.get("topics") or []:
            if topic.get("display_name"):
                into.setdefault(topic["display_name"], tuple(
                    (topic.get(level) or {}).get("display_name")
                    for level in ("subfield", "field", "domain")))


def topic_hierarchy(session: Session) -> dict[str, tuple]:
    """OpenAlex topic name -> (subfield, field, domain), from the payloads on disk.

    Publication rows keep only topic NAMES; where each sits in OpenAlex's
    taxonomy is in the payloads, and a topic seen on anybody's work is placed
    for everybody's. Topics no payload mentions have no place and simply say
    nothing about distance.
    """
    into: dict[str, tuple] = {}
    for (raw,) in session.execute(
            select(SourceRecord.raw).where(SourceRecord.source == "openalex")):
        _learn_hierarchy(_as_payload(raw), into)
    return into


def _openalex_works(session: Session, person_id: str | None = None) -> dict[str, dict]:
    """person id -> work id -> its evidence (see _works_in). Everybody when
    PERSON_ID is None, in one pass, because candidates() needs everybody."""
    query = (select(IdentityLink.person_id, SourceRecord.raw)
             .join(SourceRecord, SourceRecord.id == IdentityLink.source_record_id)
             .where(SourceRecord.source == "openalex"))
    if person_id is not None:
        query = query.where(IdentityLink.person_id == person_id)
    out: dict[str, dict] = {}
    for owner, raw in session.execute(query):
        out.setdefault(owner, {}).update(_works_in(_as_payload(raw)))
    return out


def _foreign(papers: dict[int, dict], hierarchy: dict) -> dict[int, str]:
    """Papers in bodies of work foreign to the career. See FOREIGN_WEIGHTS."""
    groups = _group({pid: paper["links"] for pid, paper in papers.items()})
    if len(groups) < 2:
        return {}

    def subjects(group, index) -> set:
        return {hierarchy[t][index] for pid in group for t in papers[pid]["topics"]
                if t in hierarchy and hierarchy[t][index]}

    career, out = groups[0], {}
    # nearest first, so a paper foreign at several levels keeps the farthest
    for index, level in enumerate(("subfield", "field", "domain")):
        theirs = subjects(career, index)
        if not theirs:
            continue
        for group in groups[1:]:
            if all(papers[pid]["authors"] > MAX_AUTHORS for pid in group):
                continue            # a consortium paper links to nothing by construction
            mine = subjects(group, index)
            if mine and not (mine & theirs):
                out.update(dict.fromkeys(group, level))
    return out


def _build(person_id: str, name: str | None, rows, works: dict, hierarchy: dict) -> Split:
    """ROWS: (publication id, external id, topics, raw authors, date)."""
    surname = (name or "").lower().split()[-1] if name else ""
    marks, papers, years = {}, {}, []
    for pub_id, external_id, topics, raw, when in rows:
        topics = _as_list(topics)
        with_ = _coauthors(raw, surname)
        marks[pub_id] = {("topic", t) for t in topics} | {("with", c) for c in with_}
        links = {("with", c) for c in with_}
        key = _tail(external_id)
        if key in works:
            links |= {("at", i) for i in works[key]["institutions"]}
            links |= {("cites", r) for r in works[key]["references"]}
            links.add(("cites", key))           # another paper here cites this one
        papers[pub_id] = {"links": links, "topics": topics, "authors": len(_as_list(raw))}
        years.extend(_year(when))
    return Split(person_id=person_id, name=name, papers=len(marks),
                 groups=_group(marks), years=years,
                 foreign=_foreign(papers, hierarchy))


_PAPER_COLUMNS = (Publication.id, Publication.external_id, Publication.topics,
                  Publication.raw_authors, Publication.published_date)


def split_of(session: Session, person_id: str, name: str | None = None,
             hierarchy: dict | None = None) -> Split:
    """How one person's papers group. See the module docstring for the rule.

    HIERARCHY is topic_hierarchy(session); pass it when calling this for many
    people, because building it reads every OpenAlex payload.
    """
    if name is None:
        person = session.get(Person, person_id)
        name = person.canonical_name if person else None
    if hierarchy is None:
        hierarchy = topic_hierarchy(session)
    rows = session.execute(
        select(*_PAPER_COLUMNS)
        .join(Authorship, Authorship.publication_id == Publication.id)
        .where(Authorship.person_id == person_id)
    ).all()
    works = _openalex_works(session, person_id).get(person_id, {})
    return _build(person_id, name, rows, works, hierarchy)


def candidates(session: Session, above: float | None = None,
               min_papers: int = MIN_PAPERS,
               check_employer: bool = True) -> list[Split]:
    """Every person reported by foreign work or a temporal break, and by the
    group ratio as well when ABOVE is given.

    CHECK_EMPLOYER drops records reported by the ratio ALONE whose halves share
    an institution the author put on their own papers, because one person
    takes their affiliation with them across a change of subject. It has
    nothing to say about the other two signals: foreign work shares no
    institution with the career by construction, and a paper fifty years from
    anything else is not rescued by a university name.

    One pass over authorships and one over payloads rather than queries per
    person: doing it per person made this too slow to run over everybody,
    which is the only way it finds anything.
    """
    names = dict(session.execute(
        select(Person.id, Person.canonical_name).where(Person.merged_into.is_(None))
    ).all())
    rows = session.execute(
        select(Authorship.person_id, *_PAPER_COLUMNS)
        .join(Publication, Publication.id == Authorship.publication_id)
    ).all()
    per_person: dict[str, list] = {}
    for person_id, *paper in rows:
        if person_id in names:
            per_person.setdefault(person_id, []).append(paper)

    hierarchy: dict[str, tuple] = {}
    works: dict[str, dict] = {}
    for owner, raw in session.execute(
            select(IdentityLink.person_id, SourceRecord.raw)
            .join(SourceRecord, SourceRecord.id == IdentityLink.source_record_id)
            .where(SourceRecord.source == "openalex")):
        payload = _as_payload(raw)
        _learn_hierarchy(payload, hierarchy)
        works.setdefault(owner, {}).update(_works_in(payload))

    out = []
    for person_id, papers in per_person.items():
        if len({p[0] for p in papers}) < min_papers:
            continue
        split = _build(person_id, names.get(person_id), papers,
                       works.get(person_id, {}), hierarchy)
        reasons = split.reasons(above)
        if not reasons:
            continue
        if (check_employer and reasons == ["ratio"]
                and shares_an_employer(session, split) is True):
            continue
        out.append(split)
    # Farthest foreign work first, then the size of the temporal hole, then
    # the ratio: a reading order, most implausible first.
    out.sort(key=lambda s: (-s.foreign_score, -s.break_years, -s.score, -s.papers))
    return out


def _own_affiliations(session: Session, person_id: str) -> dict[str, set]:
    """Per paper, the institutions THIS author put on it (see _works_in)."""
    works = _openalex_works(session, person_id).get(person_id, {})
    return {key: work["institutions"] for key, work in works.items() if work["institutions"]}


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


def foreign_evidence(session: Session, split: Split) -> list[dict]:
    """The foreign papers, farthest first, in terms a reader can check.

    Like an intruder in time, foreign work is often a single paper, which
    describe() never prints; without this a record reported for it would
    reach the review page showing only the career it does not belong to.
    """
    if not split.foreign:
        return []
    rows = session.execute(
        select(Publication.id, Publication.title, Publication.published_date,
               Publication.topics)
        .where(Publication.id.in_(split.foreign))
    ).all()
    farthest = {"domain": 0, "field": 1, "subfield": 2}
    out = [{"publication_id": pub_id, "title": title,
            "year": next(iter(_year(when)), None),
            "distance": split.foreign[pub_id],
            "topics": _as_list(topics)[:2]}
           for pub_id, title, when, topics in rows]
    return sorted(out, key=lambda r: (farthest[r["distance"]], r["year"] or 0,
                                      r["publication_id"]))


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
