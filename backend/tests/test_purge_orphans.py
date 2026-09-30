"""Rows nothing can reach, and rows pointing at nothing.

The corpus had two source records no identity link named, one of them holding
a publication with no author. Nothing in the tree deletes an identity link,
so their cause was never established -- which is the point: this sweep exists
so the next one is found by looking rather than by accident.

The distinction these pin down is the whole design. An unreachable row is
litter and can go. A dangling foreign key is corruption, and deleting the row
that points would finish destroying the evidence of what was lost.
"""

from sqlalchemy import func, select

from rip.models import Authorship, IdentityLink, Person, PersonNameToken, Publication, SourceRecord
from scripts.purge_orphans import find, purge


def record(session, source="semanticscholar", external_id="150034040",
           linked=True, papers=1):
    rec = SourceRecord(source=source, source_type="scholarly",
                       external_id=external_id, raw={})
    session.add(rec)
    session.flush()
    person = None
    if linked:
        person = Person(canonical_name="Ann Example")
        session.add(person)
        session.flush()
        session.add(IdentityLink(person_id=person.id, source_record_id=rec.id,
                                 match_confidence=1.0, match_method="new"))
    pubs = []
    for i in range(papers):
        pub = Publication(title=f"A paper {external_id} {i}",
                          external_id=f"{external_id}-w{i}",
                          source_record_id=rec.id, raw_authors=["Ann Example"])
        session.add(pub)
        session.flush()
        pubs.append(pub)
        if person is not None:
            session.add(Authorship(person_id=person.id, publication_id=pub.id))
    session.commit()
    return rec, person, pubs


def test_a_record_no_identity_link_names_is_unreachable(session):
    orphan, _p, pubs = record(session, external_id="150034040", linked=False)
    found = find(session)
    assert found["records"] == [orphan.id]
    assert found["publications"] == [pubs[0].id]

    purge(session, found)
    assert session.get(SourceRecord, orphan.id) is None
    assert session.get(Publication, pubs[0].id) is None


def test_a_record_somebody_holds_is_left_alone(session):
    kept, person, pubs = record(session, external_id="alive", linked=True)
    found = find(session)
    assert found["records"] == [] and found["publications"] == []

    purge(session, found)
    assert session.get(SourceRecord, kept.id) is not None
    assert session.get(Publication, pubs[0].id) is not None
    assert session.get(Person, person.id) is not None


def test_an_unauthored_publication_whose_record_is_alive_is_kept(session):
    """Not litter: a refresh of a live record can still give it an author.
    Only a publication going down with its record is removed."""
    alive, person, pubs = record(session, external_id="alive", linked=True)
    session.execute(Authorship.__table__.delete().where(
        Authorship.publication_id == pubs[0].id))
    session.commit()

    found = find(session)
    assert found["publications"] == []
    assert found["publications_kept"] == [pubs[0].id]

    purge(session, found)
    assert session.get(Publication, pubs[0].id) is not None, \
        "a live record's publication must survive the sweep"


def test_a_dangling_foreign_key_is_reported_and_never_deleted(session):
    """The row left behind by a hand-written delete that missed a table --
    exactly how person_name_token was found. It is evidence of what was lost,
    so the sweep must not tidy it away."""
    stray = PersonNameToken(person_id="gone-0000-0000-0000-000000000000",
                            token="vish1810")
    session.add(stray)
    session.commit()

    found = find(session)
    assert "person_name_token.person_id" in found["dangling"]
    assert found["dangling"]["person_name_token.person_id"] == [stray.person_id]

    purge(session, found)
    assert session.get(PersonNameToken, stray.id) is not None, \
        "corruption is reported, not deleted"


def test_a_clean_corpus_reports_nothing(session):
    record(session, external_id="alive", linked=True)
    found = find(session)
    assert found["records"] == [] and found["publications"] == []
    assert found["dangling"] == {} and found["publications_kept"] == []


def test_the_sweep_finds_every_kind_at_once(session):
    """Two unreachable records, one with a paper, plus a dangling key -- the
    shape the real corpus was in."""
    orphan_a, _p, pubs = record(session, external_id="150034040", linked=False)
    orphan_b, _p2, _n = record(session, source="huggingface",
                               external_id="Rahul-0", linked=False, papers=0)
    alive, _p3, _n3 = record(session, external_id="alive", linked=True)
    session.add(PersonNameToken(person_id="gone-0000-0000-0000-000000000000",
                                token="vish1810"))
    session.commit()

    found = find(session)
    assert sorted(found["records"]) == sorted([orphan_a.id, orphan_b.id])
    assert found["publications"] == [pubs[0].id]
    assert list(found["dangling"]) == ["person_name_token.person_id"]

    purge(session, found)
    assert session.get(SourceRecord, alive.id) is not None
    assert session.execute(select(func.count()).select_from(SourceRecord)).scalar() == 1
    assert session.execute(select(func.count()).select_from(PersonNameToken)).scalar() == 1
