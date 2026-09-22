"""A broad filing category is a shelf, not a subject.

OpenAlex files people under labels like "Pharmacology, Toxicology and
Pharmaceutics". Those belong in the vocabulary -- asking for "chemistry"
should reach the people filed under Chemistry -- but plain containment let any
word in the label answer, so "toxicology" matched that shelf and "Health,
Toxicology and Mutagenesis" and came back with nine people, none of them
toxicologists. nDCG 0.000, the worst query in the set.

The comma is what separates a shelf from a subject, and two blunter rules were
measured before that one was found. Both are pinned here, because both look
obviously right until you run them.
"""

from rip.nlq import _build_aux, _contained
from rip.textnorm import fold

# as OpenAlex spells them
SHELVES = ["Pharmacology, Toxicology and Pharmaceutics",
           "Health, Toxicology and Mutagenesis",
           "Radiology, Nuclear Medicine and Imaging",
           "Chemistry"]
SUBJECTS = ["Cellular and Molecular Neuroscience",
            "Analytical Chemistry and Chromatography"]


def vocabulary():
    skills = {fold(v): v for v in SHELVES + SUBJECTS}
    field_only = frozenset(fold(v) for v in SHELVES + ["Cellular and Molecular Neuroscience"])
    aux = _build_aux(skills, {}, {}, field_only=field_only)
    return skills, aux


def reached(term):
    skills, aux = vocabulary()
    return _contained(term, skills, aux)


def test_a_subject_a_shelf_merely_lists_does_not_answer_for_it():
    """The defect. Somebody filed under "Pharmacology, Toxicology and
    Pharmaceutics" may be doing any one of the three."""
    assert reached("toxicology") == []


def test_a_shelf_answers_when_asked_for_what_it_leads_with():
    """A field label is named after its principal subject and then lists what
    else it houses. "radiologists" reaches the first of them."""
    assert "Radiology, Nuclear Medicine and Imaging" in reached("radiology")
    assert "Pharmacology, Toxicology and Pharmaceutics" in reached("pharmacology")
    assert "Chemistry" in reached("chemistry")


def test_a_label_without_a_comma_is_one_subject_with_modifiers():
    """"Cellular and Molecular Neuroscience" is not a shelf holding three
    subjects; everybody under it is a neuroscientist. Requiring the front of
    every field label cost "neuroscience" 0.498 -> 0.284 before this."""
    assert "Cellular and Molecular Neuroscience" in reached("neuroscience")


def test_requiring_the_whole_label_was_worse_and_is_not_what_this_does():
    """Tried first: it cost "radiologists" 0.915 -> 0.000."""
    assert reached("radiology") != []


def test_an_ordinary_topic_is_untouched_by_any_of_this():
    assert "Analytical Chemistry and Chromatography" in reached("chromatography")
