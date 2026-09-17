from rip.connectors.orcid import OrcidConnector
from rip.ingest import ingest_profile
from rip.models import Affiliation, Evidence, Person, Publication

ORCID_RECORD = {
    "orcid-identifier": {"path": "0000-0002-1111-2222"},
    "person": {
        "name": {
            "given-names": {"value": "Jane"},
            "family-name": {"value": "Doe"},
            "credit-name": None,
        },
        "other-names": {"other-name": [{"content": "J. Doe"}]},
        "keywords": {"keyword": [{"content": "distributed systems"}]},
        "researcher-urls": {
            "researcher-url": [{"url-name": "Site", "url": {"value": "https://janedoe.ai"}}]
        },
        "emails": {"email": []},
    },
    "activities-summary": {
        "employments": {
            "affiliation-group": [
                {
                    "summaries": [
                        {
                            "employment-summary": {
                                "organization": {"name": "Acme AI", "address": {"city": "Berlin"}},
                                "role-title": "Research Engineer",
                                "start-date": {"year": {"value": "2020"}, "month": {"value": "03"}},
                                "end-date": None,
                            }
                        }
                    ]
                }
            ]
        },
        "educations": {
            "affiliation-group": [
                {
                    "summaries": [
                        {
                            "education-summary": {
                                "organization": {"name": "ETH Zurich", "address": {}},
                                "role-title": "PhD",
                                "start-date": {"year": {"value": "2015"}},
                                "end-date": {"year": {"value": "2019"}},
                            }
                        }
                    ]
                }
            ]
        },
        "works": {
            "group": [
                {
                    "work-summary": [
                        {
                            "put-code": 1,
                            "title": {"title": {"value": "Efficient Serving of Large Models"}},
                            "journal-title": {"value": "MLSys"},
                            "publication-date": {"year": {"value": "2023"}, "month": {"value": "05"}},
                            "external-ids": {
                                "external-id": [
                                    {
                                        "external-id-type": "doi",
                                        "external-id-value": "10.1234/abcd",
                                        "external-id-normalized": {"value": "10.1234/abcd"},
                                        "external-id-relationship": "self",
                                    }
                                ]
                            },
                            "url": None,
                        }
                    ]
                }
            ]
        },
    },
}


def orcid_profile():
    return OrcidConnector.normalize(OrcidConnector.__new__(OrcidConnector), ORCID_RECORD)


def test_orcid_search_authors_maps_expanded_search():
    conn = OrcidConnector.__new__(OrcidConnector)

    def fake_get(url, params=None):
        assert "expanded-search" in url
        return {"expanded-result": [
            {"orcid-id": "0000-0002-1111-2222", "given-names": "Jane",
             "family-names": "Doe", "institution-name": ["Acme"]},
        ]}

    conn.get_json = fake_get
    found = OrcidConnector.search_authors(conn, "Jane Doe", limit=5)
    assert found[0]["id"] == "0000-0002-1111-2222"
    assert found[0]["name"] == "Jane Doe"


def test_orcid_normalization(session):
    profile = orcid_profile()
    assert profile.name == "Jane Doe"
    assert profile.orcid == "0000-0002-1111-2222"
    person = ingest_profile(session, profile)
    assert person.current_organization == "Acme AI"
    assert person.current_role == "Research Engineer"
    affs = {(a.relation, a.role) for a in session.query(Affiliation)}
    assert ("worked_at", "Research Engineer") in affs
    assert ("studied_at", "PhD") in affs
    pub = session.query(Publication).one()
    assert pub.doi == "10.1234/abcd"
    assert pub.published_date == "2023-05"
    assert pub.venue == "MLSys"
    interests = {e.value for e in session.query(Evidence).filter_by(attribute_type="research_interest")}
    assert interests == {"distributed systems"}


def test_orcid_merges_with_openalex_via_orcid_key(session):
    from tests.test_ingest import openalex_profile

    p1 = ingest_profile(session, openalex_profile())  # carries orcid 0000-0002-1111-2222
    p2 = ingest_profile(session, orcid_profile())
    assert p1.id == p2.id
    assert session.query(Person).count() == 1
    # same DOI publication from both sources deduped
    assert session.query(Publication).filter_by(doi="10.1234/abcd").count() == 1


def test_keywords_advertising_the_persons_own_name_are_not_interests():
    """"Rahul Kumar ceo" is a name, not a research interest: stored as one, it
    made "Rahul" subject vocabulary and broke every search for a Rahul."""
    import copy

    record = copy.deepcopy(ORCID_RECORD)
    record["person"]["keywords"] = {"keyword": [
        {"content": "Jane Doe ceo"}, {"content": "Doe Jane Helping People"},
        {"content": "distributed systems"}, {"content": "Doe"},
        {"content": "J Doe official"}]}
    profile = OrcidConnector.__new__(OrcidConnector).normalize(record)
    interests = [e.value for e in profile.evidence if e.attribute_type == "research_interest"]
    # a lone surname is not a full name, and may be a real subject ("Doppler")
    assert interests == ["distributed systems", "Doe"]


def test_a_decorated_credit_name_still_identifies_self_advertising_keywords():
    import copy

    record = copy.deepcopy(ORCID_RECORD)
    record["person"]["name"]["credit-name"] = {"value": "Jane Doe - Engineer • Speaker"}
    record["person"]["keywords"] = {"keyword": [{"content": "Jane Doe Helping People"},
                                                {"content": "consensus protocols"}]}
    profile = OrcidConnector.__new__(OrcidConnector).normalize(record)
    assert [e.value for e in profile.evidence if e.attribute_type == "research_interest"] == [
        "consensus protocols"]
