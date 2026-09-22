"""Re-ingesting a record retracts what it no longer claims."""

from rip.ingest import ingest_profile
from rip.models import Evidence, Person
from rip.normalize import EvidenceItem
from sqlalchemy import select
from tests.test_resolution import make_profile


def interests(*values):
    return [EvidenceItem(attribute_type="research_interest", value=v) for v in values]


def claims(session, person_id):
    return sorted((e.value, e.source, e.verification_state) for e in session.execute(
        select(Evidence).where(Evidence.person_id == person_id)).scalars())


def test_a_claim_the_record_dropped_is_removed(session):
    person = ingest_profile(session, make_profile(evidence=interests("Robotics", "Rahul Kumar ceo")))
    ingest_profile(session, make_profile(evidence=interests("Robotics")))
    assert claims(session, person.id) == [("Robotics", "github", "unverified")]


def test_a_changed_location_is_history_not_a_retraction(session):
    person = ingest_profile(session, make_profile(location="Pune, India"))
    ingest_profile(session, make_profile(location="Berlin, Germany"))
    assert [v for v, _, _ in claims(session, person.id)] == ["Berlin, Germany", "Pune, India"]


def test_an_unchanged_record_keeps_everything(session):
    person = ingest_profile(session, make_profile(
        evidence=interests("Robotics", "Control Theory"), location="Pune, India"))
    before = claims(session, person.id)
    ingest_profile(session, make_profile(
        evidence=interests("Robotics", "Control Theory"), location="Pune, India"))
    assert claims(session, person.id) == before
    assert len(before) == 3


def test_other_sources_claims_are_untouched_and_lose_corroboration(session):
    github = ingest_profile(session, make_profile(
        orcid="0000-0002-1825-0097", evidence=interests("Robotics", "Vision")))
    orcid = make_profile(
        source="orcid", external_id="0000-0002-1825-0097", url="https://orcid.org/0000-0002-1825-0097",
        raw={}, usernames=[], orcid="0000-0002-1825-0097", evidence=interests("Robotics"))
    assert ingest_profile(session, orcid).id == github.id
    assert ("Robotics", "github", "corroborated") in claims(session, github.id)

    orcid.evidence = []
    ingest_profile(session, orcid)
    # the GitHub record still says both; only the ORCID claim is gone, and
    # Robotics is back to one source
    assert claims(session, github.id) == [
        ("Robotics", "github", "unverified"), ("Vision", "github", "unverified")]
    assert session.get(Person, github.id) is not None
