"""The search index: every searchable word about a person, looked up by seek.

Why this exists. Search used to match text with `LIKE '%term%'`, which no
index can serve, so every filter — and the parser's "does anyone mention
this word?" checks, up to 34 per query — read whole tables. Measured on a
10k-person corpus that was ~340ms a query before any API work, growing
linearly with the graph.

Here every person is broken into (term, field) postings once, at write time:

    field  what it holds
    -----  -----------------------------------------------------------------
    n      canonical-name words              (name filter, name existence)
    na     alias words                       (name filter)
    sv     whole skill values, normalised    (exact topic match)
    s      skill/interest/specialization words + adjacent pairs
    t      bio and job-title evidence words + pairs   (free-text mention)
    w      whole phrases of up to WORK_PHRASE_WORDS words found in at least
           WORK_MENTIONS_TO_MATCH of the person's papers (title or the
           paper's own topics); see work_alt
    r      job titles: current role + affiliation roles
    o      organisation keys ("deccan ai")
    ow     organisation name words, any affiliation     (/v1/persons)
    ew     organisation name words, studied_at only      (/v1/persons)
    co     current organisation words                    (/v1/persons)
    l      stated location words + pairs                 (/v1/persons)
    p      place words: location, bio, education, org names, summary
    c      ISO country: stated, named in the location, or an unambiguous city
    k      project technologies

A lookup is an index seek on (term, field, person_id), so a query costs
roughly the size of its RAREST constraint, not the size of the corpus.

The index maintains itself: a session hook notices every Person, Evidence,
Affiliation or Contribution written in a transaction and re-indexes those
people just before commit, inside the same transaction — so the index can
never describe a change that was rolled back, and no ingest path can forget
to update it. `rebuild()` regenerates everything (run after changing how
text is tokenised; bump INDEX_VERSION so databases rebuild on upgrade).
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import time
import weakref
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime, timezone
from itertools import pairwise
from typing import Any, cast

from sqlalchemy import (
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    and_,
    delete,
    desc,
    event,
    exists,
    func,
    insert,
    inspect,
    literal,
    or_,
    select,
)
from sqlalchemy.orm import InstanceState, Mapped, Session, aliased, mapped_column
from sqlalchemy.sql.elements import ColumnElement

from .db import Base
from .textnorm import (
    MAX_TERM_LEN,
    org_key,
    phrase_terms,
    stem_phrase_terms,
    stem_text_terms,
    stems,
    text_terms,
    value_key,
)

logger = logging.getLogger("rip.search_index")

# Bump whenever tokenisation or field contents change: databases indexed by an
# older version are rebuilt by init_db rather than silently half-matching.
INDEX_VERSION = "10"

# Fields where number is not identity: a bio reading "recommender systems"
# must answer a search for "recommender system". Indexed and queried through
# the same stemmer, so the two sides always agree. Names are deliberately not
# here — "Rogers" is not "Roger" — and neither are places or organisation
# keys, which are matched against gazetteers and stored values instead.
STEMMED_FIELDS = frozenset({"s", "t", "r", "k", "w"})

SKILL_ATTRS = ("skill", "research_interest", "specialization", "research_field")
TEXT_ATTRS = ("bio", "role")
PLACE_EVIDENCE_ATTRS = ("bio", "role", "location", "education")
# How many of someone's papers must be about a subject before search counts it
# as theirs. One title is not a research area -- the pandemic put COVID-19 in
# front of crop geneticists and database researchers alike -- so two, the rule
# ingest.MIN_SUBJECT_TITLES and the benchmark's MIN_TITLE_MENTIONS already use.
WORK_MENTIONS_TO_MATCH = 2
# Longest phrase stored whole. Pairs alone cannot say "in the same paper":
# "AI in healthcare" was found as "ai in" from one paper's topic and "in
# healthcare" from another's, for someone who works on fairness in AI.
WORK_PHRASE_WORDS = 4


class SearchTerm(Base):
    """One posting: TERM occurs in FIELD of PERSON, with a weight."""

    __tablename__ = "search_term"
    # The primary key IS the lookup index, term first: it answers "who has
    # this term" (range scan) and "does this person have it" (point lookup).
    # WITHOUT ROWID stores rows inside that B-tree instead of beside it —
    # measured 30% smaller on disk (120 vs 171 bytes a posting), for free.
    __table_args__ = ({"sqlite_with_rowid": False},)

    term: Mapped[str] = mapped_column(String(255), primary_key=True)
    field: Mapped[str] = mapped_column(String(4), primary_key=True)
    # indexed for re-indexing one person (delete by person)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), primary_key=True, index=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)


class SearchDoc(Base):
    """Per-person facts the ranker needs before it has loaded anyone."""

    __tablename__ = "search_doc"

    person_id: Mapped[str] = mapped_column(ForeignKey("person.id"), primary_key=True)
    # 0..1 query-independent quality: evidence volume, source breadth and
    # shipped output. Orders a candidate pool too large to score in full.
    prior: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    impact: Mapped[float] = mapped_column(Float, default=0.0)
    # Totals for "at least 20 papers" / "over 1000 citations": the larger of
    # what a source reports for the author and what is stored here — stored
    # works are only a sample of anyone's output (see _reported_totals).
    publications: Mapped[int] = mapped_column(Integer, default=0, index=True)
    citations: Mapped[int] = mapped_column(Integer, default=0, index=True)


class SearchIndexState(Base):
    __tablename__ = "search_index_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


# ---------------------------------------------------------------------------
# building postings
# ---------------------------------------------------------------------------

PROJECT_HALF_LIFE_DAYS = 365.0
PUBLICATION_HALF_LIFE_DAYS = 1460.0


def _loose_date(text):
    """'YYYY', 'YYYY-MM' or 'YYYY-MM-DD' (or a datetime) -> datetime | None."""
    if isinstance(text, datetime):
        return text.replace(tzinfo=None)
    text = (str(text) if text else "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _log_scale(value: float, saturation: float) -> float:
    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(saturation))


def _multiword_place_pairs() -> frozenset:
    """Adjacent word pairs that occur inside a known multi-word place name."""
    global _PLACE_PAIRS
    if _PLACE_PAIRS is None:
        from .geo import PLACES
        from .textnorm import words

        pairs: set[str] = set()
        for key in PLACES:
            ws = words(key)
            pairs.update(f"{a} {b}" for a, b in pairwise(ws))
        _PLACE_PAIRS = frozenset(pairs)
    return _PLACE_PAIRS


_PLACE_PAIRS: frozenset | None = None


def _free_text_place_terms(text: str | None) -> set[str]:
    """Place terms from prose (bios, org names, summaries).

    Every word, but only the word pairs that belong to a known multi-word
    place ("new delhi", "san francisco"). A bio's other pairs ("i build",
    "build trust") can never answer a location filter, and on real profiles
    they were the largest avoidable share of the index. A stated location
    keeps all its pairs — see _postings_for.
    """
    terms = text_terms(text)
    allowed = _multiword_place_pairs()
    return {t for t in terms if " " not in t or t in allowed}


def _reported_totals(source: str, raw) -> tuple[int, int]:
    """(publications, citations) a source states for the whole author.

    Connectors store a person's most cited works, not all of them: someone
    with 146 papers holds 25 here. A count filter on stored rows alone would
    turn "at least 50 papers" into "at least 50 papers that we fetched".
    """
    def num(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    if not isinstance(raw, dict):
        return 0, 0
    if source == "openalex":
        author = raw.get("author") or {}
        return num(author.get("works_count")), num(author.get("cited_by_count"))
    if source == "semanticscholar":
        author = raw.get("author") or {}
        return num(author.get("paperCount")), num(author.get("citationCount"))
    if source == "dblp":
        return num(raw.get("publication_count")), 0
    return 0, 0


TOTALS_SOURCES = ("openalex", "semanticscholar", "dblp")


def _phrases(ws: list[str]) -> set[str]:
    """Every run of 1..WORK_PHRASE_WORDS consecutive words, joined."""
    out = set()
    for n in range(1, WORK_PHRASE_WORDS + 1):
        for i in range(len(ws) - n + 1):
            phrase = " ".join(ws[i:i + n])
            if len(phrase) <= MAX_TERM_LEN:
                out.add(phrase)
    return out


def work_alt(text: str | None) -> Alt | None:
    """TEXT as a subject of someone's papers: the whole phrase, in enough of
    them. Longer than WORK_PHRASE_WORDS words, it falls back to the chain of
    pairs, which can only over-match; scoring re-checks every paper."""
    ws = stems(text)
    if not ws:
        return None
    phrase = " ".join(ws)
    if len(ws) <= WORK_PHRASE_WORDS and len(phrase) <= MAX_TERM_LEN:
        return Alt("w", (phrase,))
    return phrase_alt("w", text)


def _postings_for(session: Session, person_ids: list[str]) -> tuple[list[dict], list[dict]]:
    """(search_term rows, search_doc rows) for these people, from the tables."""
    from .geo import city_country, country_in_text
    from .models import (
        Affiliation,
        Authorship,
        Contribution,
        Evidence,
        IdentityLink,
        Organization,
        Person,
        Project,
        Publication,
        SourceRecord,
    )

    persons = session.execute(
        select(Person.id, Person.canonical_name, Person.aliases, Person.location,
               Person.country, Person.summary, Person.current_role,
               Person.current_organization, Person.merged_into)
        .where(Person.id.in_(person_ids))
    ).all()
    live = {p.id: p for p in persons if p.merged_into is None}
    if not live:
        return [], []
    ids = list(live)

    postings: dict[tuple[str, str, str], float] = defaultdict(float)

    def add(pid: str, fld: str, terms, weight: float = 1.0) -> None:
        for term in terms:
            if term:
                postings[(term, fld, pid)] += weight

    evidence_count: dict[str, int] = defaultdict(int)
    for pid, attr, value, conf in session.execute(
        select(Evidence.person_id, Evidence.attribute_type, Evidence.value, Evidence.confidence)
        .where(Evidence.person_id.in_(ids))
    ).all():
        evidence_count[pid] += 1
        if attr in SKILL_ATTRS:
            add(pid, "sv", [value_key(value)], conf or 0.5)
            add(pid, "s", stem_text_terms(value), conf or 0.5)
        if attr in TEXT_ATTRS:
            add(pid, "t", stem_text_terms(value), 0.25)
        if attr == "role":
            add(pid, "r", stem_text_terms(value))
        if attr == "location":
            add(pid, "p", text_terms(value))
        elif attr in PLACE_EVIDENCE_ATTRS:
            add(pid, "p", _free_text_place_terms(value))
        if attr == "location":
            code = country_in_text(value) or city_country(value)
            if code:
                add(pid, "c", [code.lower()])

    for pid, role, org_name, relation in session.execute(
        select(Affiliation.person_id, Affiliation.role, Organization.name, Affiliation.relation)
        .join(Organization, Organization.id == Affiliation.organization_id)
        .where(Affiliation.person_id.in_(ids))
    ).all():
        add(pid, "o", [org_key(org_name)])
        # word-level, for the faceted filter's "organization=Acme" (any word
        # of any affiliation) and "education=IIT" (studied_at only)
        org_words = text_terms(org_name)
        add(pid, "ow", org_words)
        if relation == "studied_at":
            add(pid, "ew", org_words)
        add(pid, "p", _free_text_place_terms(org_name))
        if role:
            add(pid, "r", stem_text_terms(role))

    impact: dict[str, float] = defaultdict(float)
    recency: dict[str, float] = {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def note_date(pid: str, when, half_life: float) -> None:
        when = _loose_date(when)
        if when is not None:
            score = 0.5 ** (max(0.0, (now - when).total_seconds() / 86400.0) / half_life)
            recency[pid] = max(recency.get(pid, 0.0), score)

    for pid, techs, activity, last_active in session.execute(
        select(Contribution.person_id, Project.technologies, Project.activity,
               Project.last_active_at)
        .join(Project, Project.id == Contribution.project_id)
        .where(Contribution.person_id.in_(ids))
    ).all():
        for tech in techs or []:
            add(pid, "k", stem_text_terms(str(tech)))
        activity = activity or {}
        with contextlib.suppress(TypeError, ValueError, AttributeError):
            impact[pid] += float(activity.get("stars") or 0) + 0.5 * float(activity.get("forks") or 0)
        note_date(pid, last_active, PROJECT_HALF_LIFE_DAYS)
    # Subjects people publish on but never state. A term counts once per
    # paper, title and the paper's own topics together, and is indexed only
    # when enough papers carry it: "w" answers "is this a subject of their
    # work", not "did the word ever appear".
    per_paper: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for pid, title, topics in session.execute(
        select(Authorship.person_id, Publication.title, Publication.topics)
        .join(Publication, Publication.id == Authorship.publication_id)
        .where(Authorship.person_id.in_(ids))
    ).all():
        terms = _phrases(stems(title))
        for topic in topics or []:
            terms |= _phrases(stems(str(topic)))
        for term in terms:
            per_paper[pid][term] += 1
    for pid, counted in per_paper.items():
        for term, n in counted.items():
            # weighted like a bio mention, however many papers: the weight
            # orders a candidate pool, and twenty titles must not outweigh
            # one stated topic there
            if n >= WORK_MENTIONS_TO_MATCH:
                postings[(term, "w", pid)] = 0.25

    publications: dict[str, int] = defaultdict(int)
    citations: dict[str, int] = defaultdict(int)
    for pid, cites, newest, n_pubs in session.execute(
        select(Authorship.person_id, func.sum(func.coalesce(Publication.citations, 0)),
               func.max(Publication.published_date), func.count(Authorship.id))
        .join(Publication, Publication.id == Authorship.publication_id)
        .where(Authorship.person_id.in_(ids))
        .group_by(Authorship.person_id)
    ).all():
        impact[pid] += float(cites or 0)
        note_date(pid, newest, PUBLICATION_HALF_LIFE_DAYS)
        publications[pid] = int(n_pubs or 0)
        citations[pid] = int(cites or 0)
    for pid, source, raw in session.execute(
        select(IdentityLink.person_id, SourceRecord.source, SourceRecord.raw)
        .join(SourceRecord, SourceRecord.id == IdentityLink.source_record_id)
        .where(IdentityLink.person_id.in_(ids), IdentityLink.review_state != "split",
               SourceRecord.source.in_(TOTALS_SOURCES))
    ).all():
        # the largest claim, not a sum: two sources describing one author
        # count the same papers twice
        n_pubs, n_cites = _reported_totals(source, raw)
        publications[pid] = max(publications[pid], n_pubs)
        citations[pid] = max(citations[pid], n_cites)

    sources: dict[str, int] = defaultdict(int)
    for pid, n in session.execute(
        select(IdentityLink.person_id, func.count(IdentityLink.id))
        .where(IdentityLink.person_id.in_(ids), IdentityLink.review_state != "split")
        .group_by(IdentityLink.person_id)
    ).all():
        sources[pid] = int(n or 0)

    docs = []
    for pid, p in live.items():
        add(pid, "n", text_terms(p.canonical_name))
        for alias in p.aliases or []:
            add(pid, "na", text_terms(str(alias)))
        add(pid, "p", text_terms(p.location))
        for text in (p.summary, p.current_organization):
            add(pid, "p", _free_text_place_terms(text))
        if p.current_role:
            add(pid, "r", stem_text_terms(p.current_role))
        if p.current_organization:
            add(pid, "o", [org_key(p.current_organization)])
            add(pid, "co", text_terms(p.current_organization))
        # the stated location on its own, for the faceted location filter —
        # "p" also holds bios and org names, which that filter never matched
        add(pid, "l", text_terms(p.location))
        # stated country, else one the location names outright, else the
        # country of an unambiguous major city ("Bengaluru" is in India). An
        # index entry, not a stored claim — Person.country stays as sourced.
        code = ((p.country or "").strip() or country_in_text(p.location)
                or city_country(p.location) or "")
        if code:
            add(pid, "c", [code.lower()])
        # Exactly the query-independent half of the final score (output 0.25,
        # recency 0.15, breadth 0.10 in nlq.WEIGHTS), rescaled to 0..1, so a
        # pool cut by it keeps the people the ranker would have put first.
        # Undated counts a neutral 0.5, as it does in the ranker. Evidence
        # volume only breaks ties.
        static = (
            0.25 * _log_scale(impact.get(pid, 0.0), 5000.0)
            + 0.15 * recency.get(pid, 0.5)
            + 0.10 * _log_scale(sources.get(pid, 0), 4.0)
        ) / 0.5
        prior = 0.98 * static + 0.02 * _log_scale(evidence_count.get(pid, 0), 12.0)
        docs.append({
            "person_id": pid, "prior": round(prior, 5),
            "evidence_count": evidence_count.get(pid, 0),
            "source_count": sources.get(pid, 0), "impact": impact.get(pid, 0.0),
            "publications": publications.get(pid, 0), "citations": citations.get(pid, 0),
        })

    rows = [
        {"term": t, "field": f, "person_id": pid, "weight": round(w, 4)}
        for (t, f, pid), w in postings.items()
    ]
    return rows, docs


def index_people(session: Session, person_ids) -> int:
    """Re-index these people in the session's current transaction."""
    ids = [pid for pid in dict.fromkeys(person_ids) if pid]
    written = 0
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        with session.no_autoflush:
            rows, docs = _postings_for(session, chunk)
            session.execute(delete(SearchTerm).where(SearchTerm.person_id.in_(chunk)))
            session.execute(delete(SearchDoc).where(SearchDoc.person_id.in_(chunk)))
            if rows:
                session.execute(insert(SearchTerm), rows)
            if docs:
                session.execute(insert(SearchDoc), docs)
        written += len(rows)
    _df_cache.pop(_bind_key(session), None)
    _memo(session).clear()
    _generation[_bind_key(session)] = _generation.get(_bind_key(session), 0) + 1
    return written


# Bumped whenever this process re-indexes anyone: corpus-level caches (facet
# counts) key on it, so a write made here is visible on the very next read.
_generation: weakref.WeakKeyDictionary[Any, int] = weakref.WeakKeyDictionary()


def generation(session: Session) -> int:
    return _generation.get(_bind_key(session), 0)


def rebuild(session: Session, batch: int = 1000, progress=None) -> int:
    """Regenerate the whole index. Safe to re-run; commits per batch.

    BATCH is checked BEFORE anything is deleted, because the order of those
    two things is the whole difference between a refused command and an
    unsearchable corpus. `range(0, n, -1)` is empty, so a negative batch
    emptied both tables, indexed nobody, and returned 0 -- which the CLI
    printed as "indexed 0 people", a success message for a destroyed index.
    A batch of 0 emptied them and then raised out of range() with the delete
    already committed. Either way the person running it was trying to REPAIR
    the index, and had no reason to read the number as a failure.
    """
    from .models import Person

    if batch < 1:
        raise ValueError(f"batch must be at least 1, not {batch}")

    # an index written with an older table layout is recreated, not patched
    bind = session.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns("search_term")} \
        if inspect(bind).has_table("search_term") else set()
    if columns and "id" in columns:
        session.close()
        table = cast(Table, SearchTerm.__table__)
        table.drop(bind)
        table.create(bind)
    session.execute(delete(SearchTerm))
    session.execute(delete(SearchDoc))
    session.commit()
    ids = session.execute(
        select(Person.id).where(Person.merged_into.is_(None)).order_by(Person.id)
    ).scalars().all()
    done = 0
    for start in range(0, len(ids), batch):
        chunk = ids[start:start + batch]
        session.info[_SUPPRESS] = True
        try:
            index_people(session, chunk)
            session.commit()
        finally:
            session.info.pop(_SUPPRESS, None)
        done += len(chunk)
        if progress:
            progress(done, len(ids))
    _set_version(session)
    session.commit()
    _ready_cache.pop(_bind_key(session), None)
    return done


def _set_version(session: Session) -> None:
    row = session.get(SearchIndexState, "version")
    if row is None:
        session.add(SearchIndexState(key="version", value=INDEX_VERSION))
    else:
        row.value = INDEX_VERSION


def needs_rebuild(session: Session) -> bool:

    if not _has_tables(session):
        return False
    row = session.get(SearchIndexState, "version")
    if row is not None:
        return row.value != INDEX_VERSION
    return not _fully_indexed(session)


def _fully_indexed(session: Session) -> bool:
    from .models import Person

    people = session.execute(
        select(func.count()).select_from(Person).where(Person.merged_into.is_(None))
    ).scalar_one()
    docs = session.execute(select(func.count()).select_from(SearchDoc)).scalar_one()
    return docs >= people


# ---------------------------------------------------------------------------
# keeping it current
# ---------------------------------------------------------------------------

_DIRTY = "rip.search_dirty"
_SUPPRESS = "rip.search_suppress"
_MEMO = "rip.search_memo"


def _bind_key(session: Session):
    # The engine object itself, held weakly — never id(engine). Python reuses
    # the id of a collected object, so an id-keyed cache handed a brand-new
    # database the vocabulary/counts of a dead one (seen as an order-
    # dependent test failure: a fresh in-memory engine per test).
    bind = session.get_bind()
    return getattr(bind, "engine", bind)


def _memo(session: Session) -> dict:
    memo: dict = session.info.setdefault(_MEMO, {})
    return memo


@event.listens_for(Session, "after_flush")
def _track_changes(session: Session, _ctx) -> None:
    from .models import Affiliation, Authorship, Contribution, Evidence, IdentityLink, Person

    dirty = session.info.setdefault(_DIRTY, set())
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, Person):
            dirty.add(obj.id)
        # Authorship: papers feed the "w" postings and search_doc's totals
        elif isinstance(obj, (Evidence, Affiliation, Contribution, IdentityLink, Authorship)):
            dirty.add(obj.person_id)
            # a row moved from one person to another (merge/split) changes both
            hist = cast("InstanceState[Any]", inspect(obj)).attrs.person_id.history
            dirty.update(v for v in (hist.deleted or ()) if v)
    dirty.discard(None)


@event.listens_for(Session, "before_commit")
def _apply_changes(session: Session) -> None:
    if session.info.get(_SUPPRESS):
        return
    if session.new or session.dirty or session.deleted:
        session.flush()
    ids = session.info.pop(_DIRTY, None)
    if not ids or not _has_tables(session):
        return
    index_people(session, ids)


@event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    session.info.pop(_MEMO, None)


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_DIRTY, None)
    session.info.pop(_MEMO, None)


_tables_cache: weakref.WeakKeyDictionary[Any, bool] = weakref.WeakKeyDictionary()
_ready_cache: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
READY_TTL_SECONDS = 30.0


def _has_tables(session: Session) -> bool:
    key = _bind_key(session)
    if key not in _tables_cache:
        try:
            _tables_cache[key] = inspect(session.connection()).has_table("search_term")
        except Exception:
            return False
    return _tables_cache[key]


def is_ready(session: Session) -> bool:
    """Can queries trust the index for this database?

    True when it was built by the current INDEX_VERSION, or when the corpus
    is empty (then everything written from here on is indexed as it lands).
    """

    key = _bind_key(session)
    cached = _ready_cache.get(key)
    if cached and cached[1] and time.monotonic() - cached[0] < READY_TTL_SECONDS:
        return True
    if not _has_tables(session):
        return False
    try:
        row = session.get(SearchIndexState, "version")
        if row is not None:
            ready = row.value == INDEX_VERSION
        else:
            # never formally built: trustworthy only if every live person
            # already has a document, i.e. it was indexed as it was written
            ready = _fully_indexed(session)
    except Exception:
        ready = False
    _ready_cache[key] = (time.monotonic(), ready)
    return ready


# ---------------------------------------------------------------------------
# querying
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Alt:
    """One way to satisfy a constraint: all TERMS present in FIELD."""

    field: str
    terms: tuple[str, ...]


@dataclass
class Constraint:
    """Satisfied by ANY of its alternatives. Constraints are ANDed."""

    label: str
    alts: list[Alt] = dc_field(default_factory=list)

    def key(self):
        return tuple(sorted((a.field, a.terms) for a in self.alts))


def phrase_alt(fld: str, text: str | None) -> Alt | None:
    terms = stem_phrase_terms(text) if fld in STEMMED_FIELDS else phrase_terms(text)
    return Alt(fld, tuple(terms)) if terms else None


def exact_alt(fld: str, key: str | None) -> Alt | None:
    return Alt(fld, (key,)) if key else None


_df_cache: weakref.WeakKeyDictionary[Any, tuple[float, dict[tuple[str, str], int]]] = (
    weakref.WeakKeyDictionary())
DF_TTL_SECONDS = float(os.environ.get("RIP_VOCAB_TTL", "60"))


def term_counts(session: Session, pairs) -> dict[tuple[str, str], int]:
    """How many people carry each (field, term). Cached like the vocabulary."""
    key = _bind_key(session)
    now = time.monotonic()
    entry = _df_cache.get(key)
    if entry is None or now - entry[0] > DF_TTL_SECONDS:
        entry = (now, {})
        _df_cache[key] = entry
    cache = entry[1]
    wanted = [p for p in dict.fromkeys(pairs) if p not in cache]
    for start in range(0, len(wanted), 200):
        chunk = wanted[start:start + 200]
        by_field: dict[str, list[str]] = defaultdict(list)
        for f, t in chunk:
            by_field[f].append(t)
        for f, terms in by_field.items():
            for t, n in session.execute(
                select(SearchTerm.term, func.count())
                .where(SearchTerm.field == f, SearchTerm.term.in_(terms))
                .group_by(SearchTerm.term)
            ).all():
                cache[(f, t)] = int(n)
        for p in chunk:
            cache.setdefault(p, 0)
    return {p: cache[p] for p in pairs}


def corpus_size(session: Session) -> int:
    memo = _memo(session)
    if "N" not in memo:
        memo["N"] = session.execute(select(func.count()).select_from(SearchDoc)).scalar_one()
    return cast(int, memo["N"])


def _alt_estimate(alt: Alt, counts) -> int:
    return min(counts.get((alt.field, t), 0) for t in alt.terms) if alt.terms else 0


def estimate(constraint: Constraint, counts) -> int:
    return sum(_alt_estimate(a, counts) for a in constraint.alts)


def _has_term(fld: str, term: str, pid_col):
    s = aliased(SearchTerm)
    return exists().where(s.term == term, s.field == fld, s.person_id == pid_col)


def _has_any(fld: str, terms: list[str], pid_col):
    s = aliased(SearchTerm)
    if len(terms) == 1:
        return exists().where(s.term == terms[0], s.field == fld, s.person_id == pid_col)
    return exists().where(s.term.in_(terms), s.field == fld, s.person_id == pid_col)


def _constraint_check(c: Constraint, pid_col, counts):
    """Point-lookup form: does the person in PID_COL satisfy C?"""
    singles: dict[str, list[str]] = defaultdict(list)
    multi = []
    for a in c.alts:
        if _alt_estimate(a, counts) == 0:
            continue
        if len(a.terms) == 1:
            singles[a.field].append(a.terms[0])
        else:
            multi.append(and_(*[_has_term(a.field, t, pid_col) for t in a.terms]))
    clauses = [_has_any(f, ts, pid_col) for f, ts in singles.items()] + multi
    return or_(*clauses) if clauses else literal(False)


@dataclass
class Restrict:
    """What a match must NOT be, and output it must reach.

    `exclude`: a person satisfying ANY of these constraints is left out —
    "robotics researchers not at Google". Thresholds read the per-person
    totals in SearchDoc.
    """

    exclude: list[Constraint] = dc_field(default_factory=list)
    min_publications: int | None = None
    min_citations: int | None = None

    def key(self):
        return (tuple(c.key() for c in self.exclude), self.min_publications, self.min_citations)

    def thresholds(self) -> bool:
        return bool(self.min_publications or self.min_citations)

    def __bool__(self) -> bool:
        return bool(self.exclude) or self.thresholds()


def _restrict_conds(restrict: Restrict | None, pid_col, counts) -> list:
    if not restrict:
        return []
    conds = [~_constraint_check(c, pid_col, counts) for c in restrict.exclude]
    if restrict.thresholds():
        d = aliased(SearchDoc)
        doc = [d.person_id == pid_col]
        if restrict.min_publications:
            doc.append(d.publications >= restrict.min_publications)
        if restrict.min_citations:
            doc.append(d.citations >= restrict.min_citations)
        conds.append(exists().where(*doc))
    return conds


def _match_stmt(constraints: list[Constraint], counts, restrict: Restrict | None = None):
    """SELECT DISTINCT person_id for people satisfying every constraint.

    Driven by the rarest constraint: its postings are range-scanned and every
    other constraint is checked by point lookup on the same unique index, so
    cost follows the smallest set rather than the corpus. With no constraint
    at all, output thresholds alone drive the scan ("over 50000 citations").
    """
    if not constraints:
        d = aliased(SearchDoc, name="drv")
        return select(d.person_id).where(*_restrict_conds(restrict, d.person_id, counts)), d
    ordered = sorted(constraints, key=lambda c: estimate(c, counts))
    driver, rest = ordered[0], ordered[1:]
    st = aliased(SearchTerm, name="drv")
    driver_conds = []
    for a in driver.alts:
        if _alt_estimate(a, counts) == 0:
            continue
        rare = min(a.terms, key=lambda t: counts.get((a.field, t), 0))
        conds = [st.field == a.field, st.term == rare]
        conds += [_has_term(a.field, t, st.person_id) for t in a.terms if t != rare]
        driver_conds.append(and_(*conds))
    where = [or_(*driver_conds)] + [_constraint_check(c, st.person_id, counts) for c in rest]
    where += _restrict_conds(restrict, st.person_id, counts)
    return select(st.person_id).where(*where).distinct(), st


def _prepare(session: Session, constraints: list[Constraint],
             restrict: Restrict | None = None):
    """(counts, possible) — False when some constraint provably matches nobody."""
    every = [*constraints, *(restrict.exclude if restrict else [])]
    pairs = [(a.field, t) for c in every for a in c.alts for t in a.terms]
    counts = term_counts(session, pairs)
    if not constraints:
        return counts, bool(restrict and restrict.thresholds())
    possible = all(estimate(c, counts) > 0 for c in constraints)
    return counts, possible


def match_select(session: Session, constraints: list[Constraint],
                 restrict: Restrict | None = None):
    """A SELECT of person_id for everyone satisfying CONSTRAINTS, for use as
    `Person.id.in_(...)` alongside other SQL — or None when some constraint
    provably matches nobody (the caller can return empty without querying)."""
    counts, possible = _prepare(session, constraints, restrict)
    if not possible:
        return None
    stmt, _ = _match_stmt(constraints, counts, restrict)
    return stmt


def count(session: Session, constraints: list[Constraint],
          restrict: Restrict | None = None) -> int:
    key = ("count", tuple(c.key() for c in constraints), restrict.key() if restrict else None)
    memo = _memo(session)
    if key in memo:
        return cast(int, memo[key])
    counts, possible = _prepare(session, constraints, restrict)
    if not possible:
        n = 0
    else:
        stmt, _ = _match_stmt(constraints, counts, restrict)
        n = session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one()
    memo[key] = n
    return n


def candidates(
    session: Session, constraints: list[Constraint], pool: int,
    score_alts: list[Alt] | None = None, restrict: Restrict | None = None,
) -> list[str]:
    """Up to POOL matching person ids, strongest first when there are more.

    When everything fits in the pool the order does not matter — the ranker
    scores all of it. When it does not, the pool is cut by the precomputed
    prior plus how much of the query's topical postings each person carries,
    so the strongest candidates reach the ranker instead of arbitrary ones.
    """
    key = ("cand", tuple(c.key() for c in constraints), pool,
           tuple(sorted((a.field, a.terms) for a in (score_alts or []))),
           restrict.key() if restrict else None)
    memo = _memo(session)
    if key in memo:
        return cast(list[str], memo[key])
    counts, possible = _prepare(session, constraints, restrict)
    if not possible:
        memo[key] = []
        return []
    stmt, _ = _match_stmt(constraints, counts, restrict)
    total = count(session, constraints, restrict)
    ids: Sequence[str]
    if total <= pool:
        ids = sorted(session.execute(stmt).scalars().all())
    else:
        sub = stmt.subquery()
        pid = sub.c.person_id
        order: ColumnElement[float] = func.coalesce(SearchDoc.prior, 0.0)
        topical = [(a.field, a.terms[0]) for a in (score_alts or []) if len(a.terms) == 1]
        if topical:
            s = aliased(SearchTerm)
            depth = (
                select(func.coalesce(func.sum(s.weight), 0.0))
                .where(s.person_id == pid,
                       or_(*[and_(s.field == f, s.term == t) for f, t in topical]))
                .scalar_subquery()
            )
            # The final score is half static (the prior) and ~40% topical
            # depth + confidence; mirror that split. depth/(depth+2) is a
            # saturating curve in plain arithmetic, portable to any engine.
            order = 0.5 * order + 0.4 * (depth / (depth + 2.0))
        ids = session.execute(
            select(pid)
            .select_from(sub)
            .outerjoin(SearchDoc, SearchDoc.person_id == pid)
            .order_by(desc(order), pid)
            .limit(pool)
        ).scalars().all()
    memo[key] = list(ids)
    return cast(list[str], memo[key])


def any_match(session: Session, alt: Alt | None) -> bool:
    """Does anybody at all satisfy ALT?"""
    return bool(people_with(session, alt, limit=1))


def people_with(session: Session, alt: Alt | None, limit: int = 20) -> list[str]:
    """Some person ids satisfying ALT (for existence checks that must also
    look at the matched record)."""
    if alt is None:
        return []
    c = [Constraint("probe", [alt])]
    counts, possible = _prepare(session, c)
    if not possible:
        return []
    stmt, _ = _match_stmt(c, counts)
    return list(session.execute(stmt.limit(limit)).scalars().all())
