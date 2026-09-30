"""A topical search answers with the word, not the subject.

`scripts/ingest_topics.py` fills coverage gaps by asking OpenAlex and Europe
PMC for authors on a subject and storing whoever comes back. Nothing checked
that they worked on it. Asking Europe PMC for "headache" returned a thyroid
oncologist and two Wuhan clinicians whose COVID papers list headache among
the symptoms patients reported — three of the fourteen people that pass
stored, filed under a subject none of them research.

This is the same root cause already fixed three times in search: a term
containing the word is not the subject. It was fixed there in the vocabulary
(`web services` is not `web development`), in the criteria (`neural` is not
`neuroscience`) and in the field labels (a shelf that merely lists toxicology
is not toxicology). This is its ingest-side instance, and it is the one that
puts wrong data in the graph rather than wrong results on a page.

The rule is the grader's, because the question is the same: say it, or
publish on it more than once.
"""

import pytest

from rip.ingest import MIN_SUBJECT_TITLES, on_subject
from rip.normalize import EvidenceItem, PublicationData
from tests.test_resolution import make_profile


def profile(name, *, topics=(), papers=()):
    return make_profile(
        source="europepmc", source_type="scholarly", external_id=name,
        url=f"https://europepmc.org/{name}", name=name,
        usernames=[f"europepmc:{name}"], raw={"id": name},
        evidence=[EvidenceItem(attribute_type="research_interest", value=t,
                               confidence=0.7) for t in topics],
        publications=[PublicationData(title=t, external_id=f"{name}-{i}",
                                      topics=list(tp))
                      for i, (t, tp) in enumerate(papers)])


# --------------------------------------------------------------------------
# the case that provoked it
# --------------------------------------------------------------------------

def test_a_symptom_in_one_paper_is_not_a_research_area():
    """The Wuhan clinicians. Their COVID paper genuinely contains the word,
    which is exactly why the source returned them."""
    clinician = profile("Wuhan Clinician", topics=["COVID-19 clinical management"],
                        papers=[("Clinical features of patients with COVID-19: fever, "
                                 "cough and headache", ["COVID-19"])])
    assert not on_subject(clinician, "headache")


def test_an_oncologist_returned_for_a_symptom_is_declined():
    oncologist = profile("Thyroid Oncologist", topics=["Thyroid neoplasms"],
                         papers=[("Postoperative headache after thyroidectomy",
                                  ["Thyroid Cancer"])])
    assert not on_subject(oncologist, "headache")


def test_someone_who_says_it_is_on_subject():
    """A stated research interest is the person's own claim, and one is
    enough — this is the same thing the grader counts as a grade 2."""
    researcher = profile("Rigmor Jensen",
                         topics=["Migraine and Headache Studies"], papers=[])
    assert on_subject(researcher, "headache")


def test_someone_who_publishes_on_it_is_on_subject_without_saying_so():
    """Europe PMC often records no keywords at all, so the titles have to be
    able to carry it alone."""
    researcher = profile("Quiet Researcher", topics=[], papers=[
        ("Chronic headache in the general population", []),
        ("Headache and neck pain: a cohort study", []),
    ])
    assert on_subject(researcher, "headache")


def test_one_paper_is_not_enough_and_two_is():
    """MIN_SUBJECT_TITLES stated as a property, so retuning it cannot quietly
    repeal the rule."""
    def with_papers(n):
        return profile(f"Author{n}", topics=[],
                       papers=[(f"Headache study {i}", []) for i in range(n)])

    assert not on_subject(with_papers(MIN_SUBJECT_TITLES - 1), "headache")
    assert on_subject(with_papers(MIN_SUBJECT_TITLES), "headache")


def test_a_papers_own_topic_labels_count_as_well_as_its_title():
    """OpenAlex titles are often oblique where its topic labels are not."""
    researcher = profile("Labelled", topics=[], papers=[
        ("Cortical spreading depression revisited", ["Migraine and Headache Studies"]),
        ("Trigeminal activation in rodents", ["Migraine and Headache Studies"]),
    ])
    assert on_subject(researcher, "Migraine and Headache Studies")


# --------------------------------------------------------------------------
# it must not be so strict that it empties the pass
# --------------------------------------------------------------------------

def test_a_multi_word_subject_matches_as_a_phrase_not_as_words():
    """"computational pathology" must not be satisfied by someone doing
    computational mechanics — the split-word failure this codebase keeps
    finding."""
    mechanic = profile("Mechanic", topics=["Computational Mechanics"],
                       papers=[("Finite elements for pathology of steel", [])])
    assert not on_subject(mechanic, "computational pathology")

    real = profile("Pathologist", topics=["Computational Pathology"], papers=[])
    assert on_subject(real, "computational pathology")


def test_number_does_not_decide_it():
    """The corpus writes both, and a coverage pass that missed everyone
    because the source said "systems" would be worse than no check."""
    person = profile("Plural", topics=["Recommender Systems"], papers=[])
    assert on_subject(person, "recommender system")
    assert on_subject(person, "recommender systems")


@pytest.mark.parametrize("subject", ["", "   ", None])
def test_an_empty_subject_admits_nobody(subject):
    """A blank subject is a caller bug. Returning True would wave the whole
    search through under a rule that checked nothing."""
    assert not on_subject(profile("Anyone", topics=["Anything"]), subject)


# --------------------------------------------------------------------------
# the gate it feeds
# --------------------------------------------------------------------------

class FakeConnector:
    source = "europepmc"

    def __init__(self, to_return):
        self.to_return = to_return
        self.fetched = []

    def fetch(self, identifier):
        self.fetched.append(identifier)
        return self.to_return


def test_a_declined_profile_is_not_stored_and_is_not_an_error(session):
    """run_connector's keep gate. Declining is a decision, not a failure: the
    fetch worked and the answer was no."""
    from rip.ingest import run_connector
    from rip.models import IngestionRun, Person

    connector = FakeConnector(profile("Wuhan Clinician",
                                      topics=["COVID-19 clinical management"],
                                      papers=[("Fever, cough and headache", [])]))
    person = run_connector(session, connector, "X1",
                           keep=lambda p: on_subject(p, "headache"))

    assert person is None
    assert session.query(Person).count() == 0, "stored a profile it declined"
    run = session.query(IngestionRun).one()
    assert run.status == "skipped", run.status
    assert run.error is None, "a decline is not an error"


def test_without_a_gate_everything_is_stored_as_before(session):
    """The default has to stay what it was: every other caller passes no
    predicate and must be unaffected."""
    from rip.ingest import run_connector
    from rip.models import Person

    connector = FakeConnector(profile("Wuhan Clinician",
                                      topics=["COVID-19 clinical management"],
                                      papers=[("Fever, cough and headache", [])]))
    assert run_connector(session, connector, "X1") is not None
    assert session.query(Person).count() == 1


def test_a_profile_that_passes_is_stored(session):
    from rip.ingest import run_connector
    from rip.models import Person

    connector = FakeConnector(profile("Rigmor Jensen",
                                      topics=["Migraine and Headache Studies"]))
    person = run_connector(session, connector, "X2",
                           keep=lambda p: on_subject(p, "headache"))
    assert person is not None
    assert session.query(Person).count() == 1
