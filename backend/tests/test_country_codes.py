"""Countries written as short codes: US, U.S., UK.

Found by the second held-out evaluation set: "machine learning researchers in
the US" returned people from anywhere, because "us" is a stopword, and "UK"
matched the University of Karachi by acronym.
"""

import pytest

from rip.ingest import ingest_profile
from rip.nlq import parse
from rip.normalize import EvidenceItem, OrgAffiliation
from tests.test_resolution import make_profile


def seed(session):
    ingest_profile(session, make_profile(
        external_id="a", url="https://github.com/a", raw={"login": "a"}, name="Ada Karachi",
        usernames=["github:a"], organizations=[OrgAffiliation(name="University of Karachi")],
        evidence=[EvidenceItem(attribute_type="research_interest", value="Robotics")]))


@pytest.mark.parametrize("query,code", [
    ("robotics researchers in the US", "US"),
    ("robotics researchers in US", "US"),
    ("robotics researchers in the U.S.", "US"),
    ("robotics researchers from the us", "US"),
    ("US robotics researchers", "US"),
    ("robotics researchers in the UK", "GB"),
    ("robotics researchers in UK", "GB"),
])
def test_country_codes_are_countries(session, query, code):
    seed(session)
    parsed = parse(session, query)
    assert parsed.countries == [code], query
    assert parsed.organizations == []


def test_us_the_pronoun_is_still_a_stopword(session):
    seed(session)
    parsed = parse(session, "help us find robotics researchers")
    assert parsed.countries == []
    assert parsed.unmatched_terms == []


def test_not_in_the_us_is_an_excluded_country(session):
    seed(session)
    parsed = parse(session, "robotics researchers not in the US")
    assert parsed.countries == []
    assert parsed.exclusions[0]["clauses"][0]["payload"] == "US"
