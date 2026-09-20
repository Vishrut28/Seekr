"""Pulling one record apart into the people it describes.

The thing worth testing hardest is not that papers move — it is what happens
NEXT TIME the source is fetched. A split that a refresh silently undoes is
worse than no split at all, because somebody did the work and cannot tell it
was thrown away.
"""

import pytest

from rip import split
from rip.ingest import SplitRecordError, ingest_profile
from rip.models import (Authorship, Evidence, IdentityLink, Person,
                        PersonSplit, Publication, SourceRecord)
from rip.normalize import EvidenceItem, OrgAffiliation, PublicationData
from sqlalchemy import select
from tests.test_resolution import make_profile


SURGERY = ["Congenital gastrointestinal anomalies", "Paediatric Surgery"]
MATHS = ["Markov Chains and Monte Carlo", "Graph Theory"]


def conflated(session, tag="conf", name="Ann Double"):
    """One record holding two people's work, as OpenAlex hands it over."""
    person = ingest_profile(session, make_profile(
        source="openalex", external_id=tag, url=f"https://openalex.org/{tag}",
        raw={"id": tag}, name=name, usernames=[f"openalex:{tag}"],
        organizations=[OrgAffiliation(name="AIIMS Delhi"),
                       OrgAffiliation(name="University of Illinois Chicago")],
        # the author-level topic list: it describes BOTH halves at once, which
        # is exactly why it cannot be divided and has to be recomputed
        evidence=[EvidenceItem(attribute_type="research_interest", value=t)
                  for t in SURGERY + MATHS],
        publications=(
            [PublicationData(title=f"Surgery paper {i}", external_id=f"W{tag}-s{i}",
                             topics=SURGERY, raw_authors=[name, "Surgeon Colleague"])
             for i in range(3)]
            + [PublicationData(title=f"Maths paper {i}", external_id=f"W{tag}-m{i}",
                               topics=MATHS, raw_authors=[name, "Maths Colleague"])
               for i in range(3)]
        )))
    session.commit()
    return person


def papers_titled(session, person_id, word):
    return [
        pid for pid, title in session.execute(
            select(Publication.id, Publication.title)
            .join(Authorship, Authorship.publication_id == Publication.id)
            .where(Authorship.person_id == person_id)
        ).all() if word in (title or "")
    ]


def subjects(session, person_id):
    return {
        v for (v,) in session.execute(
            select(Evidence.value).where(
                Evidence.person_id == person_id,
                Evidence.attribute_type.in_(split.DERIVED_ATTRS),
            )
        )
    }


def test_the_papers_move_and_the_rest_stay(session):
    who = conflated(session)
    maths = papers_titled(session, who.id, "Maths")
    out = split.split_off(session, who.id, maths, name="Ann Double (maths)")

    assert out.papers_moved == 3
    assert sorted(papers_titled(session, out.to_person_id, "Maths")) == sorted(maths)
    assert papers_titled(session, who.id, "Maths") == []
    assert len(papers_titled(session, who.id, "Surgery")) == 3


def test_each_side_ends_up_with_the_subjects_its_own_papers_are_about(session):
    """The source's topic list covers both halves, so dividing it is
    impossible; it is recomputed from each side's papers instead."""
    who = conflated(session)
    assert subjects(session, who.id) == set(SURGERY + MATHS)      # before: everything

    out = split.split_off(session, who.id, papers_titled(session, who.id, "Maths"))

    assert subjects(session, out.from_person_id) == set(SURGERY)
    assert subjects(session, out.to_person_id) == set(MATHS)


def test_a_self_declared_skill_is_left_alone(session):
    """Somebody's own word about themselves is not derived from the papers and
    cannot be attributed to either half, so it is not rewritten."""
    who = conflated(session)
    session.add(Evidence(person_id=who.id, attribute_type="skill", value="Python",
                         source="orcid", confidence=0.5))
    session.commit()

    split.split_off(session, who.id, papers_titled(session, who.id, "Maths"))
    kept = {v for (v,) in session.execute(
        select(Evidence.value).where(Evidence.person_id == who.id,
                                     Evidence.attribute_type == "skill"))}
    assert kept == {"Python"}


def test_a_refresh_of_a_split_record_refuses_instead_of_re_merging(session):
    """The whole point. The source says these are one author; a human said
    otherwise; the next fetch must not overrule them silently."""
    who = conflated(session)
    out = split.split_off(session, who.id, papers_titled(session, who.id, "Maths"))
    assert out.frozen_record_id is not None

    with pytest.raises(SplitRecordError):
        conflated(session)                       # the same record, fetched again

    # and the two people are still two people
    assert session.get(Person, out.from_person_id).merged_into is None
    assert session.get(Person, out.to_person_id).merged_into is None
    assert len(papers_titled(session, out.to_person_id, "Maths")) == 3
    assert papers_titled(session, out.from_person_id, "Maths") == []


def test_the_split_is_written_down_with_what_moved(session):
    who = conflated(session)
    maths = papers_titled(session, who.id, "Maths")
    out = split.split_off(session, who.id, maths, note="surgeon vs combinatorialist")

    row = session.query(PersonSplit).one()
    assert row.from_person_id == out.from_person_id
    assert row.to_person_id == out.to_person_id
    assert sorted(row.publication_ids) == sorted(maths)
    assert row.note == "surgeon vs combinatorialist"
    assert split.was_split(session, row.source_record_id) is True


def test_the_source_record_stays_with_one_person_only(session):
    """Ingest looks the link up with one-or-none, so a second link on the same
    record would make the next refresh raise instead of run."""
    who = conflated(session)
    out = split.split_off(session, who.id, papers_titled(session, who.id, "Maths"))
    links = session.execute(
        select(IdentityLink).where(IdentityLink.source_record_id == out.frozen_record_id)
    ).scalars().all()
    assert len(links) == 1 and links[0].person_id == out.from_person_id


def test_moving_every_paper_is_refused(session):
    who = conflated(session)
    everything = session.execute(
        select(Authorship.publication_id).where(Authorship.person_id == who.id)
    ).scalars().all()
    with pytest.raises(ValueError, match="has to leave some"):
        split.split_off(session, who.id, list(everything))


def test_somebody_elses_paper_is_refused(session):
    who = conflated(session)
    other = conflated(session, tag="other", name="Bob Separate")
    theirs = papers_titled(session, other.id, "Maths")
    with pytest.raises(ValueError, match="not this person's papers"):
        split.split_off(session, who.id, theirs)


def test_nothing_to_split_is_refused(session):
    who = conflated(session)
    with pytest.raises(ValueError, match="no publications"):
        split.split_off(session, who.id, [])


def test_the_endpoint_splits_and_reports_what_it_did(session):
    from rip import api

    who = conflated(session)
    maths = papers_titled(session, who.id, "Maths")
    out = api.split_person(who.id, {"publication_ids": maths,
                                    "name": "Ann Double (maths)"}, db=session)

    assert out["papers_moved"] == 3
    assert out["frozen_source_record"] is not None
    assert session.get(Person, out["to_person_id"]).canonical_name == "Ann Double (maths)"
    assert subjects(session, out["to_person_id"]) == set(MATHS)


def test_the_endpoint_refuses_a_bad_request_rather_than_failing(session):
    from rip import api

    who = conflated(session)
    for payload in ({"publication_ids": "all"}, {"publication_ids": ["x"]}, {}):
        with pytest.raises(Exception) as caught:
            api.split_person(who.id, payload, db=session)
        assert "publication_ids" in str(caught.value)
    # a real request that is not a split is also a 400, not a crash
    with pytest.raises(Exception) as caught:
        api.split_person(who.id, {"publication_ids": [999999]}, db=session)
    assert "not this person's papers" in str(caught.value)
    assert session.query(PersonSplit).count() == 0
