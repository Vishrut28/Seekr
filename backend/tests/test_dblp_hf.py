from rip.connectors.dblp import DblpConnector
from rip.connectors.huggingface import HuggingFaceConnector
from rip.discover import discover_dblp_coauthors
from rip.ingest import ingest_profile
from rip.models import Affiliation, DiscoveryLead, Evidence, Project, Publication

DBLP_XML = """<?xml version="1.0"?>
<dblpperson name="Jane Doe" pid="99/1234" n="2">
<person key="homepages/99/1234">
<author pid="99/1234">Jane Doe</author>
<author pid="99/1234">Jane A. Doe</author>
<url>https://janedoe.ai</url>
<url>https://scholar.google.com/citations?user=XYZ</url>
<note type="affiliation">Acme AI, Berlin, Germany</note>
<note label="2024" type="award">Test Prize</note>
</person>
<r><article key="journals/x/Doe23">
<author pid="99/1234">Jane Doe</author>
<author pid="88/777">Bob Ray</author>
<title>Efficient Serving of Large Models.</title>
<journal>MLSys</journal>
<year>2023</year>
<ee>https://doi.org/10.1234/abcd</ee>
</article></r>
<r><inproceedings key="conf/y/Doe22">
<author pid="99/1234">Jane Doe</author>
<title>Another Paper.</title>
<booktitle>NeurIPS</booktitle>
<year>2022</year>
</inproceedings></r>
</dblpperson>
"""


def dblp_profile():
    return DblpConnector.normalize(DblpConnector.__new__(DblpConnector), "99/1234", DBLP_XML)


def test_dblp_normalization_and_ingest(session):
    profile = dblp_profile()
    assert profile.name == "Jane Doe"
    assert "Jane A. Doe" in profile.aliases
    assert "https://janedoe.ai" in profile.websites
    person = ingest_profile(session, profile)
    pubs = session.query(Publication).all()
    assert {p.title for p in pubs} == {"Efficient Serving of Large Models", "Another Paper"}
    doi_pub = session.query(Publication).filter_by(doi="10.1234/abcd").one()
    assert doi_pub.venue == "MLSys"
    assert doi_pub.published_date == "2023"
    awards = [e for e in session.query(Evidence).filter_by(attribute_type="award")]
    assert awards and awards[0].value == "Test Prize (2024)"
    affs = session.query(Affiliation).all()
    assert affs[0].organization.name == "Acme AI"
    assert person.canonical_name == "Jane Doe"


def test_dblp_coauthor_discovery(session):
    ingest_profile(session, dblp_profile())
    assert discover_dblp_coauthors(session) == 1
    lead = session.query(DiscoveryLead).one()
    assert (lead.source, lead.identifier) == ("dblp", "88/777")
    assert discover_dblp_coauthors(session) == 0  # idempotent


def test_dblp_merges_with_github_via_website(session):
    from tests.test_ingest import github_profile  # blog: https://janedoe.ai

    p1 = ingest_profile(session, github_profile())
    p2 = ingest_profile(session, dblp_profile())
    assert p1.id == p2.id  # merged on shared personal site


HF_OVERVIEW = {
    "fullname": "Jane Doe",
    "orgs": [{"name": "acme", "fullname": "Acme AI"}],
    "numModels": 2,
    "numFollowers": 100,
}
HF_MODELS = [
    {
        "modelId": "jdoe/fast-llm",
        "id": "jdoe/fast-llm",
        "likes": 40,
        "downloads": 5000,
        "pipeline_tag": "text-generation",
        "library_name": "transformers",
        "createdAt": "2024-01-15T00:00:00.000Z",
    },
    {
        "modelId": "jdoe/tiny-vit",
        "id": "jdoe/tiny-vit",
        "likes": 5,
        "downloads": 200,
        "pipeline_tag": "image-classification",
        "library_name": "transformers",
        "createdAt": "2023-06-01T00:00:00.000Z",
    },
]
HF_DATASETS = [
    {"id": "jdoe/eval-set", "likes": 3, "downloads": 90, "createdAt": "2024-02-01T00:00:00.000Z"}
]


def test_huggingface_search_users():
    conn = HuggingFaceConnector.__new__(HuggingFaceConnector)

    def fake_get(url, params=None):
        if "quicksearch" in url:
            return [{"id": "jdoe", "fullname": "Jane Doe"}]
        raise AssertionError(url)

    conn.get_json = fake_get
    found = HuggingFaceConnector.search_users(conn, "Jane", limit=5)
    assert found[0]["id"] == "jdoe"


def test_huggingface_normalization_and_ingest(session):
    profile = HuggingFaceConnector.normalize(
        HuggingFaceConnector.__new__(HuggingFaceConnector),
        "jdoe", HF_OVERVIEW, HF_MODELS, HF_DATASETS,
    )
    person = ingest_profile(session, profile)
    assert person.canonical_name == "Jane Doe"
    assert person.current_organization == "Acme AI"
    skills = {e.value for e in session.query(Evidence).filter_by(attribute_type="skill")}
    assert {"text-generation", "image-classification", "transformers"} <= skills
    projects = {p.name for p in session.query(Project)}
    assert projects == {"fast-llm", "tiny-vit", "eval-set"}
