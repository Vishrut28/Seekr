"""The same person's name written differently: nicknames and spelling variants.

Two relations, used differently on purpose:

- **Transliterations** are one name romanised more than one way: Agarwal /
  Aggarwal / Agrawal, Mohammed / Muhammad / Mohamed, Chaudhary / Choudhury.
  Search treats them as the same word, and so does entity resolution.

- **Look-alikes** are different names that read alike: Katherine / Kathryn,
  Stephen / Steven. Search accepts them; resolution does not, because a
  Katherine and a Kathryn at one institute are two people as often as one.

- **Nicknames** are a different name for the same person: Bill for William,
  Bob for Robert. Search expands them ("Bill Gates" finds William H. Gates),
  but resolution does not: "Sam" is Samuel or Samantha, "Alex" is Alexander
  or Alexandra, and a nickname alone is too weak to merge two records on.
"""

from __future__ import annotations

from functools import lru_cache

from .textnorm import fold

# One name, romanised more than one way. The same person writes it either way
# across sources, so resolution treats these as one name.
_TRANSLITERATIONS = [
    {"mohammad", "mohammed", "muhammad", "mohamed", "mohamad", "muhammed", "mohd"},
    {"ahmed", "ahmad"}, {"hussain", "hussein", "husain"}, {"yusuf", "yousef", "youssef", "yousuf"},
    {"abdul", "abdel"}, {"aggarwal", "agarwal", "agrawal", "agarwala"},
    {"srivastava", "shrivastava", "srivastav"},
    {"chaudhary", "choudhary", "chowdhury", "chaudhari", "choudhury", "chaudhuri"},
    {"mukherjee", "mukerjee", "mukherji"}, {"banerjee", "banerji"}, {"chatterjee", "chatterji"},
    {"bhattacharya", "bhattacharyya", "bhattacharjee"}, {"reddy", "reddi"},
    {"pillai", "pillay"}, {"lakshmi", "laxmi"}, {"vijay", "vijai"},
    {"sanjay", "sanjai"}, {"ajay", "ajai"}, {"deepak", "dipak"}, {"pradeep", "pradip"},
    {"sandeep", "sandip"}, {"kuldeep", "kuldip"}, {"rajeev", "rajiv"}, {"sanjeev", "sanjiv"},
    {"abhijit", "abhijeet"}, {"shrinivas", "srinivas", "sreenivas"},
    {"sergey", "sergei"}, {"aleksandr", "aleksander", "alexandr"},
    {"dmitry", "dmitri", "dmitriy"}, {"yuri", "yury", "iurii"}, {"andrey", "andrei"},
    {"mikhail", "michail"},
]
# Different names that sound or look alike. Someone searching "Katherine" may
# want a Kathryn, so search accepts them — but a Katherine and a Kathryn at the
# same institute are two people as often as one, so resolution does not.
_LOOKALIKES = [
    {"catherine", "katherine", "kathryn", "katharine", "kathrine"}, {"stephen", "steven"},
    {"sara", "sarah"}, {"hannah", "hanna"}, {"phillip", "philip"}, {"jeffrey", "geoffrey"},
    {"nicolas", "nicholas", "nikolas"}, {"verma", "varma"},
    {"alexander", "aleksandr", "aleksander", "alexandr"},
]
_NICKNAMES = {
    "william": {"bill", "will", "billy", "liam"}, "robert": {"bob", "rob", "bobby", "robbie"},
    "richard": {"rick", "rich", "richie", "dick"}, "michael": {"mike", "mikey"},
    "james": {"jim", "jimmy", "jamie"}, "john": {"jack", "johnny"}, "joseph": {"joe", "joey"},
    "thomas": {"tom", "tommy"}, "charles": {"charlie", "chuck"}, "christopher": {"chris"},
    "daniel": {"dan", "danny"}, "david": {"dave"}, "edward": {"ed", "eddie", "ted"},
    "elizabeth": {"liz", "beth", "lizzie", "eliza"}, "jennifer": {"jen", "jenny"},
    "katherine": {"kate", "katie", "kathy", "kat"}, "margaret": {"maggie", "meg", "peggy"},
    "matthew": {"matt"}, "nicholas": {"nick"}, "patrick": {"pat"}, "peter": {"pete"},
    "samuel": {"sam"}, "samantha": {"sam"}, "stephen": {"steve"}, "susan": {"sue", "susie"},
    "anthony": {"tony"}, "andrew": {"andy", "drew"}, "alexander": {"alex", "sasha"},
    "alexandra": {"alex", "sasha"}, "benjamin": {"ben"}, "gregory": {"greg"},
    "jonathan": {"jon"}, "joshua": {"josh"}, "kenneth": {"ken", "kenny"},
    "lawrence": {"larry"}, "ronald": {"ron"}, "timothy": {"tim"}, "victoria": {"vicky"},
    "zachary": {"zach"}, "nathaniel": {"nate", "nathan"}, "abhishek": {"abhi"},
    "venkatesh": {"venky"}, "krishnamurthy": {"krish"},
}


def _index(groups) -> dict[str, frozenset]:
    out: dict[str, set] = {}
    for group in groups:
        for word in group:
            out.setdefault(word, set()).update(group)
    return {k: frozenset(v) for k, v in out.items()}


_SPELLING_OF = _index(_TRANSLITERATIONS)
_NICK_GROUPS = []
for full, nicks in _NICKNAMES.items():
    spellings = _SPELLING_OF.get(full, frozenset({full}))
    _NICK_GROUPS.append(set(spellings) | nicks)
_EVERY_FORM_OF = _index([*_TRANSLITERATIONS, *_LOOKALIKES, *_NICK_GROUPS])


@lru_cache(maxsize=65536)
def spelling_key(word: str) -> str:
    """One key per transliteration group: agarwal, aggarwal and agrawal share
    one. Look-alikes and nicknames are NOT folded (see the module docstring)."""
    w = fold(word)
    group = _SPELLING_OF.get(w)
    return min(group) if group else w


def search_forms(word: str) -> frozenset:
    """Every way to write WORD that a search should accept: its spellings and
    its nicknames, or the full names a nickname stands for."""
    w = fold(word)
    return _EVERY_FORM_OF.get(w, frozenset({w}))


def search_key(word: str) -> str:
    """One key per word for comparing names in search: spellings and
    nicknames together ("bill" and "william" compare equal)."""
    return min(search_forms(word))


def name_phrase_forms(phrase: str, limit: int = 12) -> list[str]:
    """PHRASE with each word in each of its search forms, the typed form first."""
    parts = fold(phrase).split()
    forms = [""]
    for part in parts:
        options = [part] + sorted(search_forms(part) - {part})
        forms = [f"{f} {o}".strip() for f in forms for o in options][:limit]
    return forms
