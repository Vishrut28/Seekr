"""Natural-language query parsing for internal search.

Turns "distributed systems researchers at Google in London, top 10" into
structured filters. Deliberately NOT ranking: the parser extracts filter
constraints only, results stay in DB order, and the response is honest about
which terms were applied and which weren't.

The vocabulary is built from the live corpus (skill/interest values, org
names, locations) rather than hardcoded gazetteers, so it grows with the data.
"""

import logging
import math
import os
import re
import weakref
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, TypeGuard

from rapidfuzz import fuzz
from sqlalchemy import and_, case, desc, false, func, literal, or_, select
from sqlalchemy.orm import Session

from . import geo
from . import search_index as si
from .models import Evidence, IdentityLink, Organization, Person
from .textnorm import (
    MAX_TERM_LEN,
    contains_phrase,
    fold,
    is_unspaced,
    org_key,
    singular,
    stems,
    value_key,
    words,
)

logger = logging.getLogger("rip.nlq")

# Words that describe what the searcher wants rather than what a person does.
# Without these, "strong in computer vision" matches the physics topic "Strong
# Light-Matter Interactions", and "at top conferences" matches "Conferences and
# Exhibitions Management". They are query grammar, not domain vocabulary.
STOPWORDS = {
    # articles, prepositions, conjunctions
    "a", "an", "and", "or", "the", "of", "in", "at", "on", "to", "from", "for",
    "with", "who", "whom", "whose", "that", "which", "both", "also", "than",
    "then", "into", "across", "about", "over", "under", "between", "not", "just", "only", "merely", "simply",
    "but", "as", "by", "is", "are", "was", "were", "be", "been", "have", "has",
    "had", "do", "does", "did", "can", "could", "would", "should", "will",
    # asking for things
    "find", "show", "list", "give", "get", "need", "want", "looking", "look",
    "search", "me", "my", "i", "we", "us", "you", "please", "help", "few",
    "some", "any", "someone", "somebody", "people", "person", "persons",
    "profiles", "profile", "candidates", "folks", "individuals", "seem",
    "seems", "actually", "really", "ideally", "preferably", "unusually",
    "particularly", "especially", "strongest", "strong", "best", "top",
    "good", "great", "excellent", "solid", "leading", "notable", "prominent",
    # job words that are titles everywhere and topics nowhere
    "engineer", "engineers", "developer", "developers", "researcher",
    "researchers", "scientist", "scientists", "expert", "experts",
    "specialist", "specialists", "professional", "professionals",
    "practitioner", "practitioners", "contributor", "contributors",
    "maintainer", "maintainers", "founder", "founders", "manager", "managers",
    "designer", "designers", "architect", "architects", "lead", "leads",
    "senior", "junior", "staff", "principal", "head", "chief",
    "doctor", "doctors", "professor", "professors", "phd", "phds", "postdoc", "postdocs",
    "student", "students", "faculty", "lecturer", "lecturers", "academics",
    # career/evidence talk
    "experience", "experienced", "expertise", "background", "worked", "works",
    "working", "work", "built", "build", "building", "contributed",
    "contributing", "contributions", "published", "publishing", "publication",
    "publications", "papers", "paper", "authored", "spoken", "speaking",
    "joined", "later", "previously", "currently", "current", "former",
    "years", "year", "public", "publicly", "presence", "evidence", "online",
    "portfolio", "showing", "track", "record",
    "conferences", "conference", "venues", "companies", "company", "startup",
    "startups", "industry", "academia", "academic", "production", "real",
    "stuff", "scale", "large", "major", "popular", "significant",
    "substantial", "deploying", "deployed", "moved", "continue", "know",
    "knows", "understand", "understands", "spent", "time", "since",
    # generic product/role nouns: "product designers" must not reach
    # "Natural product bioactivities", and "tools" matches nothing useful
    "tools", "tool", "platform",
    # generic business nouns: "partner up with firms" must not reach
    # "Risk Management in Financial Firms"
    "firm", "firms", "organisation", "organisations", "organization", "organizations",
    "business", "businesses", "agency", "agencies", "vendor", "vendors",
    "client", "clients", "customer", "customers", "industries",
    "platforms", "application", "applications", "apps", "app", "solutions",
    "services", "service", "technology", "technologies", "tech", "software",
    "systems" if False else "__unused__",
}
STOPWORDS.discard("__unused__")

# Words that are no search on their own and yet begin or end real subjects:
# "public health", "software engineering", "time series", "online learning",
# "web services", "user experience". As stopwords they did worse than nothing
# — a phrase starting or ending with one was skipped before it was looked up,
# so none of those subjects could ever be found. Now they are ordinary words
# inside a phrase, and never a match by themselves (see _is_generic), which is
# the only thing their being stopwords was ever meant to prevent.
WEAK_WORDS = frozenset({
    "public", "publicly", "presence", "evidence", "online", "portfolio", "track",
    "record", "real", "scale", "large", "major", "popular", "significant",
    "substantial", "production", "industry", "industries", "academia", "academic",
    "time", "conferences", "conference", "venues", "tools", "tool", "platform",
    "platforms", "firm", "firms", "company", "companies", "startup", "startups",
    "organisation", "organisations", "organization", "organizations", "business",
    "businesses", "agency", "agencies", "vendor", "vendors", "client", "clients",
    "customer", "customers", "application", "applications", "apps", "app",
    "solutions", "services", "service", "technology", "technologies", "tech",
    "software", "experience", "background", "work", "building",
})
STOPWORDS -= WEAK_WORDS
# Not worth searching for or reporting on its own: filler, and weak words.
NOISE_WORDS = STOPWORDS | WEAK_WORDS

# Words that name a job function rather than a subject — but ONLY when they sit
# in front of a role noun. "Delivery managers" must not reach "Nanoparticle-Based
# Drug Delivery", while "content delivery networks" still has to work.
ROLE_MODIFIERS = {
    "delivery", "program", "project", "product", "account", "operations",
    "business", "engagement", "quality", "release", "service", "client",
    "customer", "general", "technical", "solution", "solutions", "people",
    "talent", "category", "channel", "portfolio", "practice",
}
ROLE_NOUNS = {
    "manager", "managers", "management", "lead", "leads", "head", "heads",
    "director", "directors", "officer", "officers", "analyst", "analysts",
    "specialist", "specialists", "consultant", "consultants", "owner",
    "owners", "designer", "designers", "architect", "architects",
    "executive", "executives", "associate", "associates",
}


def _strip_role_modifiers(tokens: list[str]) -> list[str]:
    """Drop a job-function word that only qualifies the role beside it."""
    out = []
    for i, tok in enumerate(tokens):
        nxt = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
        if tok.lower() in ROLE_MODIFIERS and nxt in ROLE_NOUNS:
            continue
        out.append(tok)
    return out
# research_field: the subfield and field a scholarly index files someone's
# topics under ("Oncology", "Astronomy and Astrophysics"). Broader than a
# topic, and the answer to broad questions: nobody states "oncology" as a
# research interest, they state "Glioma Diagnosis and Treatment".
SKILL_ATTRS = ("skill", "research_interest", "specialization", "research_field")
# A GitHub bio reading "backend engineer, distributed systems" is real evidence
# of what someone does, even though no source emitted it as a tidy skill value.
TEXT_ATTRS = SKILL_ATTRS + ("bio", "role")
# Country names -> ISO codes. Reference data, not a guess about people: it
# lets "in India" become a real country filter even when no source recorded a
# city. Extend freely; unknown names simply stay unmatched.
COUNTRIES = {
    "india": "IN", "united states": "US", "usa": "US", "america": "US",
    "united kingdom": "GB", "uk": "GB", "britain": "GB", "england": "GB",
    "germany": "DE", "france": "FR", "canada": "CA", "china": "CN",
    "japan": "JP", "australia": "AU", "brazil": "BR", "spain": "ES",
    "italy": "IT", "netherlands": "NL", "switzerland": "CH", "sweden": "SE",
    "singapore": "SG", "israel": "IL", "south korea": "KR", "korea": "KR",
    "russia": "RU", "poland": "PL", "belgium": "BE", "austria": "AT",
    "denmark": "DK", "norway": "NO", "finland": "FI", "ireland": "IE",
    "portugal": "PT", "greece": "GR", "turkey": "TR", "mexico": "MX",
    "argentina": "AR", "chile": "CL", "south africa": "ZA", "egypt": "EG",
    "nigeria": "NG", "kenya": "KE", "pakistan": "PK", "bangladesh": "BD",
    "indonesia": "ID", "malaysia": "MY", "thailand": "TH", "vietnam": "VN",
    "philippines": "PH", "new zealand": "NZ", "czech republic": "CZ",
    "hungary": "HU", "romania": "RO", "ukraine": "UA", "saudi arabia": "SA",
    "united arab emirates": "AE", "uae": "AE", "iran": "IR", "taiwan": "TW",
    "hong kong": "HK", "colombia": "CO", "peru": "PE",
}

# Places are never surnames. A live hit named "A. M. U. O. California" must
# not turn the next search for "… california" into a name filter.
PLACES = {
    "california": "California", "texas": "Texas", "washington": "Washington",
    "new york": "New York", "florida": "Florida", "illinois": "Illinois",
    "massachusetts": "Massachusetts", "colorado": "Colorado", "oregon": "Oregon",
    "nevada": "Nevada", "arizona": "Arizona", "georgia": "Georgia",
    "pennsylvania": "Pennsylvania", "ohio": "Ohio", "michigan": "Michigan",
    "north carolina": "North Carolina", "virginia": "Virginia",
    "san francisco": "San Francisco", "los angeles": "Los Angeles",
    "seattle": "Seattle", "austin": "Austin", "boston": "Boston",
    "chicago": "Chicago", "denver": "Denver", "atlanta": "Atlanta",
    "miami": "Miami", "london": "London", "paris": "Paris", "berlin": "Berlin",
    "amsterdam": "Amsterdam", "toronto": "Toronto", "vancouver": "Vancouver",
    "sydney": "Sydney", "melbourne": "Melbourne", "tokyo": "Tokyo",
    "singapore": "Singapore", "dubai": "Dubai",
    "bangalore": "Bangalore", "bengaluru": "Bengaluru", "mumbai": "Mumbai",
    "delhi": "Delhi", "new delhi": "New Delhi", "hyderabad": "Hyderabad",
    "chennai": "Chennai", "pune": "Pune", "kolkata": "Kolkata",
    "ahmedabad": "Ahmedabad", "noida": "Noida", "gurgaon": "Gurgaon",
    "gurugram": "Gurugram", "jaipur": "Jaipur", "kochi": "Kochi",
}
# City renames: a bio that says Bengaluru must still match a search for Bangalore.
PLACE_SYNONYMS = {
    "bangalore": ("bangalore", "bengaluru"),
    "bengaluru": ("bangalore", "bengaluru"),
    "gurgaon": ("gurgaon", "gurugram"),
    "gurugram": ("gurgaon", "gurugram"),
    "mumbai": ("mumbai", "bombay"),
    "bombay": ("mumbai", "bombay"),
    "kolkata": ("kolkata", "calcutta"),
    "calcutta": ("kolkata", "calcutta"),
    "chennai": ("chennai", "madras"),
    "madras": ("chennai", "madras"),
    "delhi": ("delhi", "new delhi"),
    "new delhi": ("new delhi", "delhi"),
    "new york": ("new york", "nyc", "new york city"),
}
# "Indian researchers" means people in India, not the topic "Indian History".
DEMONYMS = {
    "indian": "IN", "american": "US", "british": "GB", "german": "DE",
    "french": "FR", "canadian": "CA", "chinese": "CN", "japanese": "JP",
    "australian": "AU", "brazilian": "BR", "spanish": "ES", "italian": "IT",
    "dutch": "NL", "swiss": "CH", "swedish": "SE", "israeli": "IL",
    "korean": "KR", "russian": "RU", "polish": "PL", "danish": "DK",
    "norwegian": "NO", "finnish": "FI", "irish": "IE", "portuguese": "PT",
    "greek": "GR", "turkish": "TR", "mexican": "MX", "nigerian": "NG",
    "kenyan": "KE", "pakistani": "PK", "singaporean": "SG",
}
# Worldwide coverage: the curated tables above win where they overlap, and
# rip/geo.py adds every other country (with endonyms — "Deutschland",
# "中国"), demonym and major world city with its alternate spellings.
for _k, _v in geo.COUNTRIES.items():
    COUNTRIES.setdefault(_k, _v)
for _k, _v in geo.DEMONYMS.items():
    DEMONYMS.setdefault(_k, _v)
for _k, _v in geo.PLACES.items():
    PLACES.setdefault(_k, _v)
for _k, _spellings in geo.PLACE_SYNONYMS.items():
    PLACE_SYNONYMS.setdefault(_k, _spellings)

# A word occurring in more than this share of vocabulary values is too generic
# to match on: "systems" appears in 147 topics, "robotics" in 7.
GENERIC_DF_RATIO = 0.01
GENERIC_DF_ABSOLUTE = 25
FUZZY_VOCAB_THRESHOLD = 90.0
# Words scholarly titles use for every subject at once. On its own, one of
# these names no subject, so it never matches a topic by containment — only a
# phrase ("cancer treatment", "systems biology") or a value that IS the word.
# The document-frequency test above does this on a large vocabulary; a graph
# of a few hundred people has too few values for any word to cross it, and
# "LLM evaluation" matched "Textile materials and evaluations". Chosen from the
# words spread widest across both real graphs' vocabularies, keeping the ones
# that name a field when they stand alone ("security", "learning", "cancer").
# Suffixes that glue a generic word onto a subject stem ("nanotechnology").
GLUED_GENERIC_SUFFIXES = ("technology", "technologies", "science", "sciences", "engineering")
GENERIC_ACADEMIC_WORDS = frozenset({
    "advanced", "analysis", "data", "application", "approach", "aspect", "assessment",
    "based", "challenge", "characterization", "design", "detection",
    "development", "diagnosis", "disease", "disorder", "dynamic", "effect",
    "evaluation", "factor", "function", "impact", "interaction", "issue",
    "management", "material", "mechanism", "method", "model", "modeling",
    "modelling", "novel", "outcome", "performance", "practice", "problem",
    "process", "processing", "property", "related", "research", "role",
    "science", "structure", "study", "system", "technique", "technology",
    "theory", "toward", "treatment", "use",
})
# Names need to be stricter than topics: "Sharma" and "Verma" are both real
# surnames, and silently swapping one for the other is worse than no answer.
FUZZY_NAME_THRESHOLD = 92.0


def _typo_score(typed: str, candidate: str) -> float:
    """How close two terms are, tolerant of one slip in a longer word.

    A ratio alone punishes short words unfairly: "rustt" scores 88 against
    "rust" and would be dropped. A single edit in a word of five or more
    characters is a typo, not a different word.
    """
    from rapidfuzz.distance import Levenshtein

    ratio = fuzz.ratio(typed, candidate)
    if len(typed) >= 5 and Levenshtein.distance(typed, candidate) <= 1:
        return max(ratio, FUZZY_VOCAB_THRESHOLD)
    return ratio


def _prefix_blocks(vocab: dict) -> dict:
    """Bucket a lowercase vocabulary by its entries' first two characters.

    Turns "compare against everything" into "compare against the bucket that
    could plausibly match" for the typo pass in parse().
    """
    blocks: dict[str, list[tuple[str, str]]] = {}
    for key, value in vocab.items():
        blocks.setdefault(key[:2], []).append((key, value))
    return blocks


DEFAULT_LIMIT = 50
MAX_LIMIT = 500


@dataclass
class NLQuery:
    raw: str
    skills: list[str] = field(default_factory=list)
    organizations: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    name_terms: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    # terms that matched many vocabulary values: filtered by pattern rather
    # than by enumerating every match (which does not scale with the corpus)
    skill_patterns: list[str] = field(default_factory=list)
    # One entry per distinct concept the user asked for. Matches are ORed
    # WITHIN a group and ANDed ACROSS groups, so "robotics and computer
    # vision" means both, not either.
    skill_groups: list[dict] = field(default_factory=list)
    limit: int = DEFAULT_LIMIT
    offset: int = 0
    unmatched_terms: list[str] = field(default_factory=list)
    # {"typed": ..., "matched": ...} for anything we corrected, so the answer
    # can say what it actually searched for instead of quietly substituting
    corrections: list[dict] = field(default_factory=list)
    # job titles like "community manager" — matched against role evidence,
    # never against research topics
    roles: list[str] = field(default_factory=list)
    # Constraints in the order the user typed them, used to relax AND-search
    # from the right: all three, then the first two, then the first word.
    clause_order: list = field(default_factory=list)
    # {"term": ..., "attribute": ...} for words asking to select people by a
    # protected attribute. Never applied, and said so rather than silently lost.
    protected_terms: list[dict] = field(default_factory=list)
    # {"typed", "searched", "how"} for what was searched in place of the words
    # typed: a people noun ("physicists" -> physics), a phrase found another
    # way ("climate science" -> climate), related subjects for a broad one
    rewrites: list[dict] = field(default_factory=list)
    # {"term": as typed, "orgs": [stored names]}: one typed organization can be
    # several stored ones ("Google" is Google (United States), Google (United
    # Kingdom) and google), and relaxing a query drops them together
    org_terms: list[dict] = field(default_factory=list)
    # "both Google and Microsoft": every organization named, not any of them
    require_all_orgs: bool = False
    # {"term": as typed ("not at Google"), "clauses": [clause, ...]}: people
    # matching any clause are left out. A negated phrase that matched nothing
    # has no clauses and excludes nobody — the response says so.
    exclusions: list[dict] = field(default_factory=list)
    # "at least 20 papers", "over 1,000 citations"
    min_publications: int | None = None
    min_citations: int | None = None


# --- compound questions ------------------------------------------------------

_COUNT_UNIT = r"(papers?|publications?|articles?|citations?|cites)"
_COUNT_NUMBER = r"(\d[\d,]*(?:\.\d+)?)\s*(k)?"
_AT_LEAST = re.compile(
    r"\b(at\s+least|a\s+minimum\s+of|minimum\s+of|no\s+(?:fewer|less)\s+than|upwards\s+of"
    r"|over|more\s+than|above|greater\s+than|>=|>)\s*" + _COUNT_NUMBER + r"\+?\s+" + _COUNT_UNIT + r"\b",
    re.I)
_OR_MORE = re.compile(
    r"(?<![\w.,])" + _COUNT_NUMBER + r"(?:\+|\s+or\s+more|\s+plus)\s+" + _COUNT_UNIT + r"\b", re.I)
_CITED = re.compile(
    r"\bcited\s+(at\s+least|over|more\s+than)\s+" + _COUNT_NUMBER + r"(?:\s+times)?\b", re.I)
# "over 1000" is 1001 or more; "at least 1000" is 1000
_STRICTLY_MORE = {"over", "more than", "above", "greater than", ">"}


def _count_value(number: str, thousands: str | None) -> int | None:
    try:
        value = float(number.replace(",", ""))
    except ValueError:
        return None
    return int(round(value * (1000 if thousands else 1)))


def _count_filters(text: str, result: "NLQuery") -> str:
    """Take output thresholds out of TEXT onto RESULT; return what is left.

    A qualifier is required: "authors of 3 papers on RNA" is not a threshold,
    "3+ papers" and "at least 3 papers" are.
    """
    def keep(unit: str, value: int | None) -> None:
        if value is None:
            return
        if unit.lower().startswith("cit"):
            result.min_citations = max(result.min_citations or 0, value)
        else:
            result.min_publications = max(result.min_publications or 0, value)

    def at_least(m):
        value = _count_value(m.group(2), m.group(3))
        if value is not None and " ".join(m.group(1).lower().split()) in _STRICTLY_MORE:
            value += 1
        keep(m.group(4), value)
        return " "

    def or_more(m):
        keep(m.group(3), _count_value(m.group(1), m.group(2)))
        return " "

    def cited(m):
        value = _count_value(m.group(2), m.group(3))
        if value is not None and " ".join(m.group(1).lower().split()) in _STRICTLY_MORE:
            value += 1
        keep("citations", value)
        return " "

    text = _AT_LEAST.sub(at_least, text)
    text = _OR_MORE.sub(or_more, text)
    return _CITED.sub(cited, text)


_NEGATOR = (r"(?:but\s+not|and\s+not|(?:do|does|did|is|are|was|were|has|have|had)\s+not|not|never|without|except(?:\s+for)?|excluding|apart\s+from"
            r"|other\s+than|no\s+longer|(?:do|does|did|are|is|have|has|were|was)n['’]t)")
# "not just ML" and "not only at Google" widen a question, they do not exclude
_NOT_A_NEGATION = (r"(?:just|only|merely|simply|necessarily|limited|restricted|exclusively"
                   r"|exactly|always|all|yet|sure|to)\b")
_NEGATED_VERB = (r"(?:(?:currently|presently|ever)\s+)?(?:work(?:s|ed|ing)?|employed|based|located"
                 r"|affiliated|stud(?:y|ies|ied|ying)|publish(?:es|ed|ing)?|research(?:es|ed|ing)?"
                 r"|focus(?:es|ed|ing)?(?:\s+on)?|specializ(?:e|es|ed|ing)(?:\s+in)?"
                 r"|specialis(?:e|es|ed|ing)(?:\s+in)?|liv(?:e|es|ed|ing)|be|been|being)\s+")
_NEGATED_PREP = r"((?:in|at|on|from|for|with|of|near|by)\s+)?"
_SPAN_END = (r"(?=\s*(?:[,;.!?()]|$)|\s+(?:who|whom|whose|that|which|with|and|or|but|in|at|from"
             r"|based|working|located|on|for|near)\b)")
_NEGATION = re.compile(
    r"\b" + _NEGATOR + r"\s+(?!" + _NOT_A_NEGATION + r")(?:" + _NEGATED_VERB + r")?"
    + _NEGATED_PREP + r"([^\s,;.!?()]+(?:\s+[^\s,;.!?()]+)*?)" + _SPAN_END,
    re.I)


def _negations(session: Session, text: str, result: "NLQuery") -> str:
    """Take negated phrases out of TEXT onto RESULT.exclusions.

    Each phrase is parsed on its own, like a query, so "not at Google" is an
    organization, "not in India" a country and "without deep learning" a
    subject. Its related subjects are not excluded: "robotics but not computer
    vision" leaves out computer vision people, not everyone near image
    processing.
    """
    spans = []

    def take(m):
        spans.append(((m.group(1) or "") + m.group(2), m.group(0).strip()))
        return " "

    text = _NEGATION.sub(take, text)
    for phrase, typed in spans:
        sub = parse(session, phrase, _nested=True)
        clauses = []
        for clause in sub.clause_order:
            payload = clause["payload"]
            # ...unless related subjects are all the corpus holds for it: with
            # no topic called "deep learning", its neighbours are what it means
            if clause["kind"] == "skill_groups" and (
                    payload.get("values") or payload.get("contained_values")
                    or payload.get("pattern")):
                payload = {k: v for k, v in payload.items() if k != "related_values"}
                clause = {**clause, "payload": payload}
            clauses.append(clause)
        result.exclusions.append({"term": typed, "clauses": clauses})
        # asking to leave people out by a protected attribute selects by it too
        result.protected_terms.extend(sub.protected_terms)
        result.corrections.extend(sub.corrections)
    return text


def subjects_asked(parsed: "NLQuery") -> list[str]:
    """One name per subject the question asked for, as the user said it.

    `skills` holds the topic VALUES a subject resolved to — nine of them for
    "machine learning", and none at all for "physicists", whose evidence is
    entirely related subjects and a free-text pattern. Neither is what the
    person asked for, and the answer has to be able to say what it searched.
    """
    return list(dict.fromkeys(
        (g.get("term") or g.get("pattern") or "") for g in parsed.skill_groups
        if (g.get("term") or g.get("pattern"))))


def query_understanding(parsed: "NLQuery") -> dict:
    """The parts of a question that are not filters on a subject, for the
    response: what was searched in place of what was typed, what was
    excluded, "both", and output thresholds."""
    return {
        "rewrites": parsed.rewrites,
        "exclusions": [{"term": e["term"], "as": [c["label"] for c in e["clauses"]]}
                       for e in parsed.exclusions],
        "require_all_orgs": parsed.require_all_orgs,
        "min_publications": parsed.min_publications,
        "min_citations": parsed.min_citations,
    }


# Words that may stand between "at" and the employer: "at both Google and
# Microsoft", "at the University of Tokyo".
_AFTER_AT = frozenset({"both", "the", "either"})


def _names_an_employer(tokens: list[str], first: int) -> bool:
    """Does an employer belong at position FIRST — "at X", "from X"?

    Reading past "both" and "the" matters: with them in the way, "at both
    Google and Microsoft" saw no "at" before Google and read it as a subject.
    """
    i = first
    while i > 0 and tokens[i - 1].lower() in _AFTER_AT:
        i -= 1
    return i > 0 and tokens[i - 1].lower() in ("at", "from", "@")


def _employer_position(tokens: list[str], span: set, gram: str) -> bool:
    """Is this one capitalised word sitting where an employer goes?

    "at Google" is an employer, "at scale" is not, which is why the word has
    to be capitalised as typed. Only single words: a longer phrase carries
    enough of itself to mean something ("at computer vision research").
    """
    if len(gram.split()) != 1 or not gram[:1].isupper() or gram.isupper():
        return False
    return _names_an_employer(tokens, min(span))


def _meaningful(term: str, min_len: int) -> bool:
    """Long enough to search on. Two characters of Chinese or Japanese carry
    as much meaning as a whole English word, so a letter-count threshold
    written for alphabets would silently drop every such query."""
    return len(term) >= min_len or (is_unspaced(term) and len(term) >= 2)


def _text_evidence_exists(session: Session, term: str) -> bool:
    """Does any bio or job title mention this term (as whole words)?"""
    if si.is_ready(session):
        return si.any_match(session, si.phrase_alt("t", term))
    return session.execute(
        select(Evidence.id).where(
            Evidence.attribute_type.in_(("bio", "role")),
            func.lower(Evidence.value).like(f"%{like_escape(term)}%", escape="\\"),
        ).limit(1)
    ).first() is not None


# Acronyms carry the whole meaning of a query ("ML engineers at OpenAI") but are
# too short to survive the name/length guards, so they get dropped and the query
# silently becomes "engineers at OpenAI". Expand them to what the corpus calls
# them; if the expansion has no vocabulary match either, the term still drops.
ACRONYMS = {
    "ai": "artificial intelligence",
    "ml": "machine learning",
    "nlp": "natural language",
    "cv": "computer vision",
    "llm": "large language model",
    "llms": "large language model",
    "ux": "user experience",
    "ui": "user interface",
    "hci": "human-computer interaction",
    "iot": "internet of things",
    "ar": "augmented reality",
    "vr": "virtual reality",
    "genai": "artificial intelligence",
    "rl": "reinforcement learning",
    "k8s": "kubernetes",
    "js": "javascript",
    "ts": "typescript",
    "ds": "data science",
    "bi": "business intelligence",
    "nlu": "natural language",
    "asr": "speech recognition",
    "cybersec": "cybersecurity",
    "infosec": "information security",
    "sre": "site reliability",
    "qa": "quality assurance",
    "dl": "deep learning",
    "gnn": "graph neural network",
    "gnns": "graph neural network",
    "cnn": "convolutional neural network",
    "cnns": "convolutional neural network",
    "rnn": "recurrent neural network",
    "ner": "named entity recognition",
    "nlg": "natural language generation",
    "rag": "retrieval-augmented generation",
    "xai": "explainable",
    "hpc": "high performance computing",
    "ocr": "optical character recognition",
    "ehr": "electronic health record",
    "bci": "brain-computer interface",
    "vlm": "vision-language model",
    "vlms": "vision-language model",
}

# Short forms that ARE the subject, not a way into neighbouring ones: "NLP"
# and "natural language processing" must find the same people. One person
# stating "NLP" made it a vocabulary word, after which "NLP" matched that
# literal value -- 26 people where the full name found 30, and the full name
# missed the person who wrote "NLP". Either spelling now searches the full
# subject and also accepts the short form as stated. Looked up against the
# vocabulary in singular and plural.
ABBREVIATIONS = {
    "nlp": "natural language processing",
    "ml": "machine learning",
    "ai": "artificial intelligence",
    "cv": "computer vision",
    "dl": "deep learning",
    "rl": "reinforcement learning",
    "llm": "large language model",
    "llms": "large language model",
    "hci": "human-computer interaction",
    "iot": "internet of things",
    "gnn": "graph neural network",
    "gnns": "graph neural network",
    "cnn": "convolutional neural network",
    "ner": "named entity recognition",
    "nlg": "natural language generation",
    "xai": "explainable artificial intelligence",
    "hpc": "high performance computing",
    "ocr": "optical character recognition",
    "ar": "augmented reality",
    "vr": "virtual reality",
}


def _full_form(short: str, skills: dict) -> str | None:
    """The vocabulary key of the subject SHORT abbreviates, if the corpus holds it."""
    full = ABBREVIATIONS.get(short)
    if not full:
        return None
    for key in (full, full + "s", full.removesuffix("s")):
        if key in skills:
            return key
    return None


def _short_forms(full: str, skills: dict) -> list[str]:
    """Stated values that abbreviate FULL ("NLP" for natural language processing)."""
    return [skills[s] for s, f in ABBREVIATIONS.items()
            if s in skills and f in (full, full.removesuffix("s"))]

# Full phrases that mean the same specialization in different words, where
# no acronym or substring relationship connects them. Unlike ACRONYMS (a
# short form expanding to a longer spelling of the SAME words), both sides
# here can be complete phrases that share no words at all — "LLM inference"
# and "model serving" describe the same work, but neither is a substring or
# abbreviation of the other, so containment matching can never bridge them.
# A curated, explainable list rather than embeddings/similarity search: every
# match here can be pointed to and read, matching this file's whole approach
# to ranking (see relevance_scores' docstring) — a similarity score nobody
# can point at a reason for is a worse fit for a tool that exists to show its
# work. Necessarily incomplete; extend as real query patterns turn up empty
# that plainly should not have. Many-to-one: several phrasings can point at
# the one the corpus is more likely to actually use.
SYNONYMS = {
    "llm inference": "model serving",
    "model inference": "model serving",
    "model deployment": "model serving",
    "client-side development": "frontend engineering",
    "client side development": "frontend engineering",
    "server-side development": "backend engineering",
    "server side development": "backend engineering",
    "distributed computing": "distributed systems",
    "large-scale systems": "distributed systems",
    "large scale systems": "distributed systems",
    "big data": "data engineering",
    "devops": "site reliability",
}

# Words that name a language, tool or stack — never a person. Once a live
# search stores someone surnamed Python, leftover matching would otherwise
# treat "python" as a name filter forever.
TECH_SKILLS = {
    "python", "rust", "ruby", "java", "kotlin", "scala", "haskell", "perl",
    "julia", "matlab", "fortran", "erlang", "elixir", "clojure", "dart",
    "swift", "golang", "javascript", "typescript", "react", "vue", "angular",
    "django", "flask", "rails", "spring", "pytorch", "tensorflow", "keras",
    "pandas", "numpy", "docker", "kubernetes", "linux", "android", "ios",
    "mongodb", "redis", "postgres", "postgresql", "mysql", "sqlite",
    "hadoop", "spark", "kafka", "airflow", "huggingface",
    "langchain", "openai", "node", "nodejs", "nextjs", "graphql", "html",
    "css", "sass", "bitcoin", "ethereum", "solidity", "cuda", "llvm",
    "c++", "cpp", "c#", "csharp",
}


def _token_frequency(skills: dict) -> dict:
    """How many vocabulary values contain each word — a genericness measure.

    Keyed by stem, like every other word comparison here, so "network" and
    "networks" are one word rather than two half-counted ones.
    """
    from collections import Counter

    df: Counter = Counter()
    for key in skills:
        for word in {singular(w) for w in re.findall(r"[^\W_]+", key)}:
            df[word] += 1
    return df


def _is_generic(term: str, df: dict[str, int], vocab_size: int) -> bool:
    """Would matching this term sweep in unrelated topics?"""
    words = term.split()
    if len(words) > 1:
        return False  # a phrase is specific enough to match on
    if singular(term) in GENERIC_ACADEMIC_WORDS or term in WEAK_WORDS:
        return True
    n = df.get(singular(term), 0)
    return n > max(GENERIC_DF_ABSOLUTE, vocab_size * GENERIC_DF_RATIO)


# The vocabulary is four SELECT DISTINCTs over the whole corpus. Rebuilding it
# per query costs more than everything else in a request put together, so it is
# cached and rebuilt only when the corpus actually changed. Ingest runs in a
# separate process, so a short TTL covers writes we never hear about; anything
# that adds people inside this process calls invalidate_vocab() directly.
# The cache used to expire on a timer alone, and rebuilt whether or not
# anything had changed. Measured on a synthetic corpus, the rebuild is not
# cheap and does not stay cheap:
#
#     distinct terms   _build_vocab   _build_aux     total
#          2,726          188 ms        329 ms       0.5 s
#         24,898          398 ms        692 ms       1.1 s
#         99,897        1,369 ms      5,158 ms       6.5 s
#        299,894        5,998 ms     19,193 ms      25.0 s
#
# At roughly 3.5 distinct terms per person that is 85,000 people spending 25
# seconds out of every 60 rebuilding a vocabulary nobody changed -- per worker
# process -- and past about 285,000 people the rebuild takes longer than the
# window, so the cache can never be warm at all.
#
# So the timer is no longer what decides. A fingerprint does: the largest id
# in each table the vocabulary reads, which is 0.4 ms at half a million
# evidence rows where COUNT(*) is 32 ms. Unchanged means nothing was ADDED and
# the cache stands however old it is. Changed means rebuild now rather than up
# to a minute later, which is also more correct than before.
#
# A maximum id cannot see the one thing left: a DELETED row taking the last
# use of a term with it. So the timer stays, but what it triggers is a COUNT,
# not a rebuild -- 32 ms against 25 seconds, and affordable once every ten
# minutes where it is not affordable per query. A corpus nobody is writing to
# rebuilds NEVER now, which is the whole point; a term that briefly outlives
# its last holder matches nobody and costs no wrong answers.
VOCAB_TTL_SECONDS = float(os.environ.get("RIP_VOCAB_TTL", "600"))
# Never rebuild more often than this, however busy a writer is. Bulk ingest
# changes evidence constantly, and without a floor every query during one
# would pay for its own rebuild.
VOCAB_MIN_INTERVAL = float(os.environ.get("RIP_VOCAB_MIN_INTERVAL", "5"))
# Keyed by the engine the session is bound to, never process-global: one
# process can legitimately talk to more than one database (the test suite
# builds a fresh in-memory engine per test), and a shared entry would hand
# one database's vocabulary to another.
# {bind: (stamp, (skills, orgs, locations), aux, fingerprint, census)}
_vocab_cache: "weakref.WeakKeyDictionary[Any, tuple[float, tuple[dict, dict, dict], VocabAux, Any, Any]]" = (
    weakref.WeakKeyDictionary())


@dataclass
class VocabAux:
    """Everything parse() derives from the vocabulary, built once per cache
    entry instead of once per query. At 100k vocabulary values rebuilding
    these per request cost more than the whole search."""

    df: dict
    skill_blocks: dict
    org_blocks: dict
    loc_blocks: dict
    # word stem -> skill keys containing it: containment checks touch only the
    # keys that could match instead of regex-scanning every key
    word_index: dict
    # skill key -> its words as stems, so a phrase matches a value whose words
    # differ only in number ("distributed system" / "Distributed Systems")
    key_stems: dict
    # values that exist only as a broad research field (see _field_only_keys)
    field_only: frozenset
    # organization name words (distinctive ones only) and name word sequences,
    # and abbreviations derived from names: "mit", "iit bombay", "aiims"
    org_words: dict
    org_sequences: dict
    org_acronyms: dict
    # every word the vocabulary uses, for repairing a typo inside a phrase,
    # and the same words bucketed by first letter so a repair compares against
    # a handful rather than the whole vocabulary
    vocab_words: list
    word_blocks: dict
    ordinal: dict
    skill_keys: list
    org_keys: list
    loc_keys: list


def _build_aux(skills: dict, orgs: dict, locations: dict,
               field_only: frozenset = frozenset()) -> VocabAux:
    word_index: dict[str, set] = {}
    key_stems: dict[str, tuple] = {}
    for key in skills:
        # textnorm.stems, so Chinese/Japanese/Thai keys are indexed by
        # character pairs and a shorter phrase can find the longer value
        key_stems[key] = tuple(stems(key))
        for w in set(key_stems[key]):
            word_index.setdefault(w, set()).add(key)
    return VocabAux(
        df=_token_frequency(skills),
        skill_blocks=_prefix_blocks(skills), org_blocks=_prefix_blocks(orgs),
        loc_blocks=_prefix_blocks(locations),
        word_index=word_index, key_stems=key_stems, vocab_words=list(word_index),
        field_only=field_only,
        **_org_index(orgs),
        word_blocks=_letter_blocks(word_index),
        ordinal={k: i for i, k in enumerate(skills)},
        skill_keys=list(skills), org_keys=list(orgs), loc_keys=list(locations),
    )


def _vocab_aux(session: Session) -> VocabAux:
    _vocab(session)
    return _vocab_cache[si._bind_key(session)][2]


def _contained(phrase: str, skills: dict, aux: VocabAux) -> list[str]:
    """Skill values containing PHRASE as consecutive whole words, in
    vocabulary order.

    Compared by stem, so number does not decide whether a query works:
    "distributed system" and "distributed systems" both reach "Distributed
    Systems", and "wireless sensor network" reaches "Energy Efficient Wireless
    Sensor Networks". Before this, the singular forms found nothing at all.

    A BROAD FILING CATEGORY answers only when the query names what it LEADS
    with. OpenAlex files people under compound labels -- "Radiology, Nuclear
    Medicine and Imaging", "Pharmacology, Toxicology and Pharmaceutics" -- and
    those belong in the vocabulary, because "radiologists" should reach the
    first of them. But plain containment let any word in the label answer, so
    "toxicology" matched the second one and "Health, Toxicology and
    Mutagenesis" and returned nine people, none of them toxicologists: nDCG
    0.000, the worst query in the set.

    The comma is what separates the two. A label that ENUMERATES subjects --
    "Pharmacology, Toxicology and Pharmaceutics" -- is a shelf holding several
    of them, and somebody on that shelf may be doing any one, so matching a
    subject it merely lists tells you nothing. A label without a comma is one
    subject with modifiers -- "Cellular and Molecular Neuroscience" -- and
    everybody under it is doing that subject, so containment is right there.

    For an enumerating label the query has to name what it LEADS with, which
    is its principal subject: "radiologists" reaches "Radiology, Nuclear
    Medicine and Imaging" and "toxicology" no longer reaches either shelf that
    merely lists it. Two blunter rules were tried and measured first --
    requiring the whole label cost "radiologists" 0.915 -> 0.000, and requiring
    the front of EVERY field label cost "neuroscience" 0.498 -> 0.284.
    """
    pieces = stems(phrase)
    if not pieces:
        return []
    candidates: set[str] | None = None
    for piece in pieces:
        keys = aux.word_index.get(piece, set())
        candidates = keys if candidates is None else candidates & keys
        if not candidates:
            return []
    if candidates is None:
        return []
    if is_unspaced(phrase):
        # no word boundaries to respect in a script without spaces
        hits = [k for k in candidates if phrase in k]
    else:
        n = len(pieces)
        hits = [
            k for k in candidates
            if any(aux.key_stems.get(k, ())[i:i + n] == tuple(pieces)
                   for i in range(len(aux.key_stems.get(k, ())) - n + 1))
        ]
    if aux.field_only:
        want = tuple(pieces)
        hits = [k for k in hits
                if k not in aux.field_only
                or "," not in skills.get(k, "")
                or aux.key_stems.get(k, ())[:len(want)] == want]
    hits.sort(key=lambda k: aux.ordinal.get(k, 0))
    return [skills[k] for k in hits]


# Words every kind of institution shares: on their own they name none of them.
ORG_GENERIC_WORDS = frozenset({
    "university", "universidad", "universite", "universitat", "universita", "universidade",
    "institute", "institut", "instituto", "college", "school", "center", "centre", "hospital",
    "department", "laboratory", "laboratories", "lab", "labs", "national", "international",
    "research", "foundation", "academy", "technology", "technological", "sciences",
    "science", "medical", "medicine", "state", "federal", "group", "inc", "ltd", "llc",
    "corporation", "company", "co", "limited", "private", "pvt", "united", "states",
    "kingdom", "republic", "of", "the", "and", "for", "at", "in", "de", "la", "le", "du",
    "des", "di", "da", "der", "und", "health", "clinic", "council", "ministry", "agency",
    "office", "service", "services", "systems", "global", "new", "north", "south", "east",
    "west", "central", "government", "public", "society", "association", "network",
})
_ACRONYM_SKIP = frozenset({"of", "the", "and", "for", "at", "in", "de", "la", "le", "du",
                           "des", "di", "da", "der", "und", "&"})
# Well-known forms no initials rule produces.
_ORG_NICKNAMES = {
    "iisc": "indian institute of science", "caltech": "california institute of technology",
    "eth": "eth zurich", "kaist": "korea advanced institute of science and technology",
    "cern": "european organization for nuclear research",
}


def _org_index(orgs: dict) -> dict:
    """Word, sequence and abbreviation lookups over organization names."""
    import re as _re

    names = set(orgs.values())
    org_words: dict[str, set] = {}
    org_sequences: dict[str, tuple] = {}
    org_acronyms: dict[str, set] = {}
    for name in names:
        # "Google (United States)": the country in brackets is not the name
        bare = _re.sub(r"\(.*?\)", " ", name)
        ws = words(bare)
        if not ws:
            continue
        org_sequences[name] = tuple(ws)
        for w in set(ws):
            if w not in ORG_GENERIC_WORDS and len(w) > 2 and w not in COUNTRIES:
                org_words.setdefault(w, set()).add(name)
        initials = [w[0] for w in ws if w not in _ACRONYM_SKIP and w[0].isalpha()]
        if 2 <= len(initials) <= 6:
            org_acronyms.setdefault("".join(initials), set()).add(name)
            # "Indian Institute of Technology Bombay" is typed "IIT Bombay"
            if len(initials) >= 3:
                org_acronyms.setdefault("".join(initials[:-1]) + " " + ws[-1], set()).add(name)
        for nick, phrase in _ORG_NICKNAMES.items():
            target = tuple(words(phrase))
            n = len(target)
            if any(tuple(ws[i:i + n]) == target for i in range(len(ws) - n + 1)):
                org_acronyms.setdefault(nick, set()).add(name)
    return {"org_words": org_words, "org_sequences": org_sequences, "org_acronyms": org_acronyms}


def _follows_at(tokens: list[str], span: set) -> bool:
    """Was this phrase typed as an employer -- "researchers at Oxford"?

    "at" names where someone works; "in" names where they are. A word that is
    both a place and part of an organization's name reads as the place unless
    "at" says otherwise.
    """
    before = min(span) - 1 if span else -1
    return before >= 0 and tokens[before].lower() == "at"


def _org_matches(gram: str, gram_l: str, parts: list[str], span: set, tokens: list[str],
                 aux: "VocabAux", df: dict) -> list[str]:
    """Stored organizations a typed term names, when the term is about one.

    "Stanford" is Stanford University, "Google DeepMind" is Google DeepMind
    (United Kingdom), "MIT" and "IIT Bombay" are what they abbreviate. The
    exact-name lookup found none of these: "researchers at Stanford" returned
    nobody while four Stanford researchers were stored.

    A word that is also a subject in the topic vocabulary ("vision", "energy")
    is read as an organization only after "at" or "from" — otherwise the
    Robotics Institute would answer every question about robotics.
    """
    after_at = _names_an_employer(tokens, min(span))
    content = [w for w in parts if w not in STOPWORDS]
    if not content:
        return []
    typed = " ".join(tokens[i] for i in sorted(span))

    hits: set = set()
    if gram_l in aux.org_acronyms and gram_l not in NOISE_WORDS and (
            typed.replace(" ", "")[:len(content[0])].isupper() or len(gram_l.replace(" ", "")) >= 4
            or after_at):
        hits |= aux.org_acronyms[gram_l]
    if not hits:
        target = tuple(words(gram_l))
        n = len(target)
        distinctive = [w for w in target if w not in ORG_GENERIC_WORDS]
        if distinctive and (n > 1 or target[0] in aux.org_words):
            for name, seq in aux.org_sequences.items():
                if any(seq[i:i + n] == target for i in range(len(seq) - n + 1)):
                    hits.add(name)
    if not hits:
        return []
    # A phrase is a subject only when every word of it is one: "computer
    # vision" is, "Google DeepMind" is not. Any one word used to be enough,
    # and "google" is a word of Stack Overflow tags (google-chrome), so the
    # phrase never matched; "DeepMind" alone answered for it until the corpus
    # gained an organization called plain "Google", and then "Google
    # DeepMind researchers" returned Google's engineers ahead of DeepMind's.
    topical = all(df.get(singular(w), 0) > 0 for w in content) if len(content) > 1 \
        else df.get(singular(content[0]), 0) > 0
    if topical and not after_at:
        return []
    return sorted(hits)[:20]


def _letter_blocks(word_index: dict) -> dict:
    """Vocabulary words bucketed by first letter, for repair candidates."""
    blocks: dict[str, list] = {}
    for word in word_index:
        blocks.setdefault(word[:1], []).append(word)
    return blocks


def _repair(parts: list[str], skills: dict, aux: VocabAux):
    """One misspelled word in a phrase, replaced by the word it meant.

    The per-token typo pass compares a word against whole vocabulary VALUES,
    so it fixes "bangalor" but can never fix "distributed sytems" — there the
    slip is one word inside a phrase, and the phrase scores nothing against
    any single value. Comparing against the vocabulary's WORDS finds it. Only
    a repair that makes the phrase match something real is accepted, so this
    cannot invent a filter: it returns (words, typed, values) or None, where
    TYPED is the misspelled word, or the whole phrase when two were repaired.

    Two slips are repaired together in a phrase of three words or more, so a
    word spelled right still pins down what was meant: "natual language
    procesing" fixed neither alone, fell back to "natual language", and
    dropped "procesing" without a word. In a two-word phrase nothing is
    spelled right, so the pair must land EXACTLY on a stored topic: "machin
    lerning" is machine learning, and two guesses agreeing on a whole topic by
    chance is not a risk worth naming. Containment alone is not enough there.
    """
    from rapidfuzz import process
    from rapidfuzz.distance import DamerauLevenshtein

    best = None
    guessed: dict[int, list[tuple[str, float]]] = {}
    for i, word in enumerate(parts):
        if len(word) < 4 or singular(word) in aux.word_index:
            continue                    # too short to judge, or spelled fine
        # The typo may sit in the plural ending itself: "robotcis" keeps its
        # "s" through singularisation (the rule that protects "analysis")
        # while the vocabulary's "Robotics" has lost it, leaving the two two
        # edits apart until the bare stem is tried as well.
        probes = {singular(word)}
        if word.endswith("s") and len(word) > 4:
            probes.add(word[:-1])
        guesses = []
        for probe in probes:
            hit = process.extractOne(probe, aux.vocab_words, scorer=fuzz.ratio,
                                     score_cutoff=FUZZY_VOCAB_THRESHOLD)
            if hit:
                guesses.append((hit[0], hit[1]))
            if len(probe) < 5:
                continue
            # One transposition — "learnign", "netowrks", "robotcis", the
            # commonest slip of all — is two edits to Levenshtein and one to
            # Damerau. Only words that start alike and are about as long are
            # compared, so this stays a small batch on a large vocabulary.
            # Tried even when the ratio found something, because that
            # something may not make the phrase match anything.
            # Two slips are allowed in a long word whose neighbours in the
            # phrase are spelled right: "partical physics" is two edits from
            # "particle", and "physics" pins down which word was meant.
            others_known = len(parts) > 1 and all(
                singular(w) in aux.word_index for j, w in enumerate(parts) if j != i)
            limit = 2 if len(probe) >= 7 and others_known else 1
            near = process.extractOne(
                probe,
                [w for w in aux.word_blocks.get(probe[:1], ())
                 if abs(len(w) - len(probe)) <= 2],
                scorer=DamerauLevenshtein.distance, score_cutoff=limit)
            if near:
                guesses.append((near[0], FUZZY_VOCAB_THRESHOLD - 5 * near[1]))
        if guesses:
            guessed[i] = sorted(guesses, key=lambda g: -g[1])[:3]
        for guess, score in guesses:
            candidate = list(parts)
            candidate[i] = guess
            values = _contained(" ".join(candidate), skills, aux)
            if values and (best is None or score > best[3]):
                best = (candidate, word, values, score)
    if best is None and len(guessed) == 2:
        (i, gi), (j, gj) = sorted(guessed.items())
        for a, sa in gi:
            for b, sb in gj:
                candidate = list(parts)
                candidate[i], candidate[j] = a, b
                values = _contained(" ".join(candidate), skills, aux)
                if len(parts) < 3 and not any(stems(v) == stems(" ".join(candidate))
                                              for v in values):
                    continue
                if values and (best is None or min(sa, sb) > best[3]):
                    best = (candidate, " ".join(parts), values, min(sa, sb))
    return None if best is None else (best[0], best[1], best[2])


def _rescue_phrase(parts: list[str], skills: dict, aux: VocabAux, df: dict, vocab_size: int):
    """A phrase the vocabulary does not hold as written, found another way.

    Returns (values, what was searched, how) or None. Tried in order, each
    only if the one before found nothing:

    - abbreviations spelled out inside it: "ai in healthcare" is "artificial
      intelligence in healthcare", which is a stored topic
    - every content word present in one value, not side by side: "wildlife
      conservation" is "Wildlife Ecology and Conservation"
    - a generic head dropped: "climate science" is a question about climate,
      because "science" names no subject (GENERIC_ACADEMIC_WORDS). Only from
      the end: in "systems programming" the generic word is a modifier, and
      dropping it left "programming", which reached functional-programming.
    """
    content = [w for w in parts if w not in STOPWORDS]
    if not content:
        return None

    if len(content) == 1:
        # One word built on a generic suffix — "nanotechnology",
        # "biotechnology", "neuroengineering" — is a question about its stem,
        # and the vocabulary files that stem under many words: nanomaterials,
        # nanofibers, nanostructures. Stems shorter than four letters ("bio")
        # would reach half the vocabulary, so they are left alone.
        word = content[0]
        for suffix in GLUED_GENERIC_SUFFIXES:
            stem = word[: -len(suffix)] if word.endswith(suffix) else ""
            if len(stem) >= 4:
                hits = [w for w in aux.word_blocks.get(stem[:1], ()) if w.startswith(stem)]
                keys: set = set()
                for w in hits:
                    keys |= aux.word_index.get(w, set())
                ordered = sorted(keys, key=lambda k: aux.ordinal.get(k, 0))
                if 0 < len(ordered) <= 12:
                    return [skills[k] for k in ordered], f"{stem}*", "word stem"

    expanded = " ".join(ACRONYMS.get(w, w) for w in parts)
    if expanded != " ".join(parts):
        values = _contained(expanded, skills, aux)
        if values and len(values) <= 12:
            return values, expanded, "abbreviation"

    if len(content) >= 2:
        shared: set[str] | None = None
        for piece in {singular(w) for w in content}:
            found = aux.word_index.get(piece, set())
            shared = found if shared is None else shared & found
            if not shared:
                break
        if shared:
            ordered = sorted(shared, key=lambda k: aux.ordinal.get(k, 0))
            if len(ordered) <= 12:
                return [skills[k] for k in ordered], " + ".join(content), "all words"

    core = list(content)
    while core and (singular(core[-1]) in GENERIC_ACADEMIC_WORDS or core[-1] in WEAK_WORDS):
        core.pop()
    if core and len(core) < len(content):
        phrase = " ".join(core)
        if len(core) > 1 or not _is_generic(phrase, df, vocab_size):
            values = _contained(phrase, skills, aux)
            if values and len(values) <= 12:
                return values, phrase, "generic words dropped"
    return None


def _related_values(term: str, skills: dict, aux: VocabAux, exclude: set) -> list[str]:
    """Vocabulary values of the subjects concepts.CONCEPTS relates to TERM —
    topics only, never a broad research field (see _field_only_keys)."""
    from .concepts import related_subjects

    out: list[str] = []
    for subject in related_subjects(term):
        for value in _contained(subject, skills, aux):
            if fold(value) in aux.field_only:
                continue
            if value not in exclude and value not in out:
                out.append(value)
            if len(out) >= MAX_RELATED_VALUES:
                return out
    return out


# Words the parser always drops. Not WEAK_WORDS: "software" is weak but can
# still be the subject, and its typo must stay a visible correction.
_LONG_NOISE = sorted(w for w in STOPWORDS if len(w) >= 6)


def _misspelt_noise(token: str, aux: VocabAux) -> str | None:
    """The filler or job word TOKEN misspells, if it is one: "resarchers"
    is "researchers". Long words only, never a word the vocabulary uses, and
    only words that are always dropped (see _LONG_NOISE).

    Spelled right, such a word sets no filter and is dropped. Misspelt, it
    made the phrase around it unknown: "cosmolgy resarchers" found nothing,
    because the pair was reported unknown and "cosmolgy" alone was then never
    tried; "computr vison resarchers" was reported dropped while its first two
    words were answered."""
    t = fold(token)
    if len(t) < 6 or t in NOISE_WORDS or singular(t) in aux.word_index:
        return None
    from rapidfuzz import process

    hit = process.extractOne(t, _LONG_NOISE, scorer=fuzz.ratio,
                             score_cutoff=FUZZY_VOCAB_THRESHOLD)
    return hit[0] if hit else None


def without_related(parsed: NLQuery) -> NLQuery:
    """PARSED answering with what each subject is, not what neighbours it.

    "NLP" also reaches Topic Modeling and Speech Recognition through the
    concept map. With related subjects off, a subject the corpus holds is
    matched as itself only; one reached ONLY through related subjects keeps
    them, since without them it would ask for nothing.
    """
    dropped = set()
    for group in parsed.skill_groups:
        own = group.get("values") or group.get("contained_values") or group.get("pattern")
        if own and group.pop("related_values", None):
            dropped.add(group.get("term"))
    parsed.rewrites = [r for r in parsed.rewrites
                       if not (r.get("how") == "related subjects" and r.get("typed") in dropped)]
    return parsed


def _whole_names(tokens: list[str], result: NLQuery, df: dict) -> None:
    """A name is asked for whole.

    "satya nadella" kept "satya" as a name and set "nadella" aside because
    nobody here is called that, so every Satya answered; "sundar pichai" did
    the same before anyone named Pichai was stored. Half a name is not the
    person. A name-shaped word beside a name word is part of that name,
    whether or not anyone here carries it yet -- the search then finds nobody
    rather than somebody else, and live search looks for the whole name.
    """
    if not result.name_terms:
        return
    named = {fold(w) for term in result.name_terms for w in term.split()}
    named |= {fold(c["typed"]) for c in result.corrections if c.get("matched") in result.name_terms}
    grew = True
    while grew:
        grew = False
        for i, token in enumerate(tokens):
            if (token not in result.unmatched_terms or not _could_be_a_name(token)
                    or df.get(singular(fold(token)), 0) > 0):
                continue
            beside = {fold(tokens[j]) for j in (i - 1, i + 1) if 0 <= j < len(tokens)}
            if beside & named:
                result.unmatched_terms.remove(token)
                result.name_terms.append(token)
                named.add(fold(token))
                grew = True


def _search_term(group: dict) -> str:
    """The words a concept is matched by: the repaired phrase where a typo
    was repaired, else the term as typed. Display keeps the typed term.

    "cosmolgy" was repaired to its topic and then matched bios, papers and
    the concept map as "cosmolgy" -- so it found the one person whose topic
    says cosmology and none of the dark-matter, black-hole or paper-only
    people the right spelling reaches.
    """
    return group.get("searched") or group.get("term") or ""


def _expand_concepts(result: NLQuery, skills: dict, aux: VocabAux) -> None:
    """Add related subjects to broad concepts, and rescue broad terms the
    vocabulary does not hold at all ("cybersecurity", "web")."""
    from .concepts import related_subjects

    for group in result.skill_groups:
        term = _search_term(group)
        have = list(group.get("related_values") or [])
        known = set(group.get("values") or []) | set(group.get("contained_values") or [])
        # The concept map is keyed by the subject, and a misspelt term is not
        # one: "natual language processing" resolved to the NLP topics and
        # then found no related subjects, so a typo cost the expansion the
        # right spelling gets. Fall back to a subject the term resolved to --
        # only when that subject is the term respelled: "computational"
        # resolves to "Computational Biology" too, and borrowing ITS related
        # subjects sent "computational pathology" to genomics.
        lookup = term
        if not related_subjects(term):
            lookup = next((v for v in group.get("values") or []
                           if related_subjects(v)
                           and fuzz.ratio(fold(term), fold(v)) >= FUZZY_VOCAB_THRESHOLD), term)
        extra = [v for v in _related_values(lookup, skills, aux, known) if v not in have]
        if extra:
            group["related_values"] = (have + extra)[:MAX_RELATED_VALUES]
            # typed as the user wrote it, searched as repaired: a typo read
            # "Searched natural language processing ... for natural language
            # processing" once the search term was the repair
            result.rewrites.append({"typed": group.get("term") or term,
                                    "searched": f"{term} + related subjects",
                                    "how": "related subjects"})
    for term in list(result.unmatched_terms):
        extra = _related_values(term, skills, aux, set())
        if extra:
            result.skill_groups.append({"term": term, "values": [], "related_values": extra})
            result.unmatched_terms.remove(term)
            result.rewrites.append({"typed": term, "searched": "related subjects",
                                    "how": "related subjects"})


def invalidate_vocab(session: Session | None = None) -> None:
    """Drop the cached vocabulary — call after storing people mid-request.

    Live discovery writes new people and then re-parses the same query so the
    terms it just learned become real filters. Without this the re-parse would
    read a stale vocabulary and the people we just fetched stay invisible to
    the very query that fetched them.

    Pass a session to clear just that database, or nothing to clear all.
    """
    if session is None:
        _vocab_cache.clear()
    else:
        _vocab_cache.pop(si._bind_key(session), None)


def _vocab_fingerprint(session: Session) -> tuple:
    """Cheap proof that the vocabulary cannot have gained anything.

    The largest id in each table it reads, plus this process's own index
    generation. Primary keys are indexed, so each is a constant-time lookup --
    0.4 ms at half a million evidence rows, against 32 ms for COUNT(*) and
    13 ms for MAX(updated_at), neither of which is affordable per query.

    Person contributes locations and has no integer key, so it is not
    fingerprinted: a person arriving essentially always brings evidence with
    them, and a lone location edit from another process waits for the backstop
    timer. That is the honest limit of this being cheap.
    """
    from .models import Organization

    # One statement per table: selecting both maxima together is a cartesian
    # product between two tables with nothing to join on, which SQLAlchemy
    # warns about and the database would have to plan around. Two indexed
    # lookups are 0.4 ms each.
    return (
        session.execute(select(func.max(Evidence.id))).scalar(),
        session.execute(select(func.max(Organization.id))).scalar(),
        si.generation(session),
    )


def _vocab_census(session: Session) -> tuple:
    """The count a maximum id cannot stand in for, because deletions lower it.

    32 ms at half a million evidence rows -- too much per query, nothing at
    all once every VOCAB_TTL_SECONDS.
    """
    from .models import Organization

    return (
        session.execute(select(func.count(Evidence.id))).scalar(),
        session.execute(select(func.count(Organization.id))).scalar(),
    )


def _vocab(session: Session) -> tuple[dict, dict, dict]:
    """(skills, orgs, locations) lowercase -> canonical value, from live data."""
    import time

    key = si._bind_key(session)
    cached = _vocab_cache.get(key)
    now = time.monotonic()
    if cached is not None:
        stamp, built, _aux, fingerprint, census = cached
        age = now - stamp
        try:
            grew = fingerprint != _vocab_fingerprint(session)
        except Exception:      # a database that cannot answer the cheap
            grew = True        # question is no reason to trust the cache
        if grew:
            if age < VOCAB_MIN_INTERVAL:
                return built   # a writer is busy; do not rebuild per query
        elif age < VOCAB_TTL_SECONDS:
            return built
        else:
            # nothing was added, but something may have been deleted. Ask the
            # question a maximum id cannot answer, and keep the cache if the
            # answer is no.
            try:
                shrank = census != _vocab_census(session)
            except Exception:
                shrank = True
            if not shrank:
                _vocab_cache[key] = (now, built, _aux, fingerprint, census)
                return built
    built = _build_vocab(session)
    _vocab_cache[key] = (now, built,
                         _build_aux(*built, field_only=_field_only_keys(session)),
                         _vocab_fingerprint(session), _vocab_census(session))
    return built


def _field_only_keys(session: Session) -> frozenset:
    """Vocabulary values that exist only as a research field ("Medicine",
    "Pharmacology"), never as anyone's stated topic. Broad filing categories:
    they answer a broad question asked directly, and must never be pulled in
    as a *related* subject — "drug discovery" reaching the field Pharmacology
    brought in migraine researchers."""
    fields = {fold(v) for (v,) in session.execute(
        select(Evidence.value).where(Evidence.attribute_type == "research_field").distinct())}
    if not fields:
        return frozenset()
    stated = {fold(v) for (v,) in session.execute(
        select(Evidence.value).where(
            Evidence.attribute_type.in_(tuple(a for a in SKILL_ATTRS if a != "research_field"))
        ).distinct())}
    return frozenset(fields - stated)


def _build_vocab(session: Session) -> tuple[dict, dict, dict]:
    """The uncached scan. Call _vocab() instead unless you need fresh data."""
    # Keys are folded (case- and accent-free) so "zurich" reaches "Zürich".
    skills = {
        fold(v): v
        for (v,) in session.execute(
            select(Evidence.value).where(Evidence.attribute_type.in_(SKILL_ATTRS)).distinct()
        )
    }
    org_names = session.execute(select(Organization.name).distinct()).scalars().all()
    orgs = {fold(v): v for v in org_names}
    # "deccan.ai" typed by a user must reach the record spelled "Deccan AI"
    from .models import normalize_org_name

    for v in org_names:
        orgs.setdefault(normalize_org_name(v), v)
        orgs.setdefault(org_key(v), v)
    locations: dict[str, str] = {}
    for (loc,) in session.execute(
        select(Person.location).where(Person.location.isnot(None)).distinct()
    ):
        locations[fold(loc)] = loc
        # each comma part is matchable ("Berlin" from "Berlin, Germany")
        for part in loc.split(","):
            part = part.strip()
            if len(part) > 2:
                locations.setdefault(fold(part), loc)
    return skills, orgs, locations


# Four, not three: "maternal and child health" is one subject, and read as
# "maternal and child" AND "health" it demanded two separate matches.
def _ngrams(tokens: list[str], max_n: int = 4):
    """Longest-first n-grams with their token spans."""
    for n in range(min(max_n, len(tokens)), 0, -1):
        for i in range(len(tokens) - n + 1):
            yield " ".join(tokens[i : i + n]), set(range(i, i + n))


# a query word in any script; + and # keep "c++" and "c#" whole
_QUERY_TOKEN = re.compile(r"[^\W_][\w+#.'’-]*")


# Words that put a following "us" in place: "in the US", "across US".
_BEFORE_A_PLACE = frozenset({"in", "the", "from", "across", "within", "outside", "throughout",
                             "based", "near", "of"})


def _country_codes(tokens: list[str]) -> list[str]:
    """The United States, written as the pronoun it is spelled like.

    "us" is a stopword ("find us people"), so "machine learning researchers in
    the US" silently lost its country. It is the country when typed as "US" or
    "U.S", or after a word that introduces a place.
    """
    out = []
    for i, token in enumerate(tokens):
        folded = token.lower()
        if (token in ("US", "U.S", "U.S.") or folded in ("u.s", "u.s.")
                or (folded == "us" and i > 0 and tokens[i - 1].lower() in _BEFORE_A_PLACE)):
            out.append("USA")
        else:
            out.append(token)
    return out


def parse(session: Session, query: str | None, _nested: bool = False) -> NLQuery:
    query = query or ""
    result = NLQuery(raw=query)

    # limit requires an explicit prefix — a bare number is never a limit
    limit_match = re.search(r"\b(?:top|first|show|list)\s+(\d{1,3})\b", query, re.I)
    if limit_match:
        result.limit = max(1, min(MAX_LIMIT, int(limit_match.group(1))))
    cleaned = re.sub(r"\b(?:top|first|show|list)\s+\d{1,3}\b", " ", query, flags=re.I)
    if not _nested:
        cleaned = _count_filters(cleaned, result)
        cleaned = _negations(session, cleaned, result)
    # Capitals carry meaning only beside lower case: "SaaS" is a product and
    # "AI" an acronym. Typed all in capitals they say nothing, and "GEOFFREY
    # HINTON" was read as two acronyms -- nobody found, where "geoffrey
    # hinton" found him. Short all-capital queries ("NLP") are left alone.
    if (any(ch.isupper() for ch in cleaned) and not any(ch.islower() for ch in cleaned)
            and re.search(r"[^\W\d_]{5,}", cleaned)):
        cleaned = cleaned.lower()

    skills, orgs, locations = _vocab(session)
    aux = _vocab_aux(session)
    df = aux.df
    vocab_size = max(1, len(skills))
    # Any script, not just ASCII: "São Paulo", "München", "北京" are words.
    # Trailing sentence punctuation is not part of a word ("Toronto.").
    tokens = [t.rstrip(".'’-") for t in _QUERY_TOKEN.findall(cleaned)]
    tokens = _country_codes([t for t in tokens if t][:40])
    consumed: set[int] = set()
    # tokens belonging to a multi-word phrase the corpus does not know, and the
    # spans already reported as dropped (longest first, so a sub-phrase of one
    # is not reported again)
    unknown_phrases: set[int] = set()
    reported_misses: list[set[int]] = []

    # People nouns become their subjects: nobody states "physicists" as a
    # topic, they state physics. See concepts.rewrite_agents.
    from .concepts import rewrite_agents

    tokens, agent_rewrites = rewrite_agents(tokens)
    result.rewrites.extend(agent_rewrites)
    # a misspelt filler or job word is that word: it sets no filter either way
    tokens = [_misspelt_noise(t, aux) or t for t in tokens]

    # Words selecting people by a protected attribute are taken out before
    # anything can match them — see compliance.protected_in_query.
    from .compliance import protected_in_query

    for index, word, attribute in protected_in_query(tokens):
        result.protected_terms.append({"term": word, "attribute": attribute})
        consumed.add(index)

    for gram, span in _ngrams(tokens):
        if span & consumed:
            continue
        gram_l = fold(gram)
        parts = gram_l.split()
        if all(t in STOPWORDS for t in parts):
            continue
        # A job title, checked before the filler rule below: role nouns are
        # themselves stopwords, so "community managers" would otherwise be
        # skipped and leave the bare word "community" to match Microbial
        # Community Ecology. A title belongs against role evidence.
        # Exactly two words. A longer phrase would swallow real vocabulary:
        # "rust program managers" is a Rust person with a job title, not a
        # title called "rust program manager".
        if (len(parts) == 2 and parts[-1] in ROLE_NOUNS
                and parts[0] not in STOPWORDS and parts[0] not in ROLE_NOUNS):
            title = _singular_role(gram_l)
            if _role_exists(session, title):
                result.roles.append(title)
            else:
                # Nobody here holds that title, so filtering on it would just
                # return nothing. Report it instead — the same rule the rest
                # of the parser follows — and let live search go looking.
                result.unmatched_terms.append(gram)
            consumed |= span
            continue
        # A phrase that opens or closes on a filler word ("in India",
        # "learning in") is not a real term — it would match a skill that
        # merely contains the preposition. The tighter n-gram covers the
        # meaningful part, so skip this one.
        if len(parts) > 1 and (parts[0] in STOPWORDS or parts[-1] in STOPWORDS):
            continue
        full = _full_form(gram_l, skills)
        if full:
            # "NLP" is searched as natural language processing, widening and
            # all, and still accepts anyone who wrote "NLP"
            also = [skills[gram_l]] if gram_l in skills else []
            gram_l, parts = full, full.split()
        else:
            also = _short_forms(gram_l, skills) if gram_l in skills else []
        if gram_l in TECH_SKILLS or gram_l in skills:
            group = {"term": gram}
            if full:
                # bios, papers, the concept map and live sources see the
                # subject's name, not three letters
                group["searched"] = full
            if gram_l in skills:
                values = [skills[gram_l]]
                # An exact hit used to be read as the narrowest possible
                # reading: "distributed systems engineers" returned the one
                # value spelled exactly that way and found a single person,
                # while the singular "distributed system engineers" went
                # through containment and found six. How a user types number or
                # wording should not decide who is findable, so values whose
                # words contain the phrase count too — the exact one first.
                #
                # Phrases only. A one-word value is a precise vocabulary term,
                # and widening it changes what depth in a concept means:
                # "robotics" would also count "Robotics 1".."Robotics 8" as
                # robotics evidence, so someone deep in one concept outranks
                # someone solid in both.
                if len(parts) > 1 and not _is_generic(gram_l, df, vocab_size):
                    wider = _contained(gram_l, skills, aux)
                    if len(wider) <= 12:
                        values += [v for v in wider if v != skills[gram_l]]
                elif len(parts) == 1 and not _is_generic(gram_l, df, vocab_size):
                    # One word widens too, in two ways. The narrower research
                    # FIELDS named after it count in full: "chemistry" is
                    # Organic, Inorganic and Materials Chemistry. Topics that
                    # contain it count as partial evidence, capped like a
                    # related subject: "cryptography" found one person who
                    # states exactly "Cryptography" and missed everyone in
                    # "Cryptography and Data Security" — while counting those
                    # topics in full let "Robotics 1".."Robotics 8" read as
                    # eight kinds of depth and outrank balanced people.
                    wider = [v for v in _contained(gram_l, skills, aux) if v != skills[gram_l]]
                    values += [v for v in wider if fold(v) in aux.field_only][:12]
                    partial = [v for v in wider if fold(v) not in aux.field_only]
                    if partial:
                        group["contained_values"] = partial[:MAX_RELATED_VALUES]
                group["values"] = values + [v for v in also if v not in values]
            else:
                # not in the corpus yet, but it is a skill — match bios/topics
                # by pattern rather than inventing a name filter
                group["pattern"] = gram_l
            result.skill_groups.append(group)
            consumed |= span
        elif gram_l in orgs:
            # the exact name, and the other stored spellings of the same one:
            # "google" is also Google (United States) and Google (United Kingdom)
            names = list(dict.fromkeys(
                [orgs[gram_l], *_org_matches(gram, gram_l, parts, span, tokens, aux, df)]))
            result.organizations.extend(names)
            result.org_terms.append({"term": gram, "orgs": names})
            consumed |= span
        elif gram_l in COUNTRIES:
            # Before the looser organization matches: "UK" is the United
            # Kingdom, not the acronym of the University of Karachi.
            result.countries.append(COUNTRIES[gram_l])
            consumed |= span
        elif (gram_l in locations or gram_l in PLACES) and not _follows_at(tokens, span):
            # A place, before the organizations that merely contain it -- unless
            # it was typed as an employer: "researchers at Oxford". After
            # them, "people in bangalore" became "people at IISc Bangalore or
            # Bangalore Medical College" and missed all four people whose
            # stated location is Bangalore; "rust london" asked for ten London
            # colleges. Nothing is lost by the order: a location filter also
            # matches an affiliation whose name holds the place
            # (location_anywhere, and the index's "p" field).
            #
            # Corpus spellings win: "Toronto" -> "Toronto, Canada" when that
            # is what we stored; the gazetteer label when it is not.
            result.locations.append(locations.get(gram_l) or PLACES[gram_l])
            consumed |= span
        elif found_orgs := _org_matches(gram, gram_l, parts, span, tokens, aux, df):
            result.organizations.extend(found_orgs)
            result.org_terms.append({"term": gram, "orgs": found_orgs})
            consumed |= span
        elif gram_l in DEMONYMS:
            # "Indian researchers" is a place, not the topic "Indian History"
            result.countries.append(DEMONYMS[gram_l])
            consumed |= span
        elif (
            len(parts) > 1
            and not any(
                p in TECH_SKILLS or p in PLACES or p in COUNTRIES or p in DEMONYMS
                for p in parts
            )
            and _full_name_exists(session, gram_l)
        ):
            # "geoffrey hinton" is a person, not an unknown topic phrase
            result.name_terms.append(gram)
            consumed |= span
        elif gram_l in ACRONYMS and ACRONYMS[gram_l] in skills:
            result.skill_groups.append({"term": gram, "values": [skills[ACRONYMS[gram_l]]],
                                        "searched": ABBREVIATIONS.get(gram_l) or ACRONYMS[gram_l]})
            consumed |= span
        elif gram_l in ACRONYMS:
            contained = _contained(ACRONYMS[gram_l], skills, aux)
            if contained:
                # bios and papers are read for the words, as for the full name:
                # "GNN" found 6 people and "graph neural networks" 17
                result.skill_groups.append({"term": gram, "values": contained,
                                            "searched": ABBREVIATIONS.get(gram_l) or ACRONYMS[gram_l]})
                consumed |= span
        elif gram_l in SYNONYMS and SYNONYMS[gram_l] in skills:
            # An exact vocabulary hit on the OTHER phrasing: "model serving"
            # is a real skill value, so "llm inference" should mean it too.
            result.skill_groups.append({"term": gram, "values": [skills[SYNONYMS[gram_l]]]})
            consumed |= span
        elif gram_l in SYNONYMS:
            # No exact hit, but the canonical phrasing might still appear
            # inside a longer skill value — same fallback ACRONYMS uses.
            contained = _contained(SYNONYMS[gram_l], skills, aux)
            if contained:
                result.skill_groups.append({"term": gram, "values": contained})
                consumed |= span
            # No vocabulary hit at all yet: still worth scoring as free text
            # against bios/roles under the SAME term the user typed, exactly
            # like the generic pattern-fallback branch below does for an
            # unrecognized term — a phrase like "devops" with nobody's
            # skill spelled that way should still catch "I do devops work"
            # in a bio, and it should not be reported as a dropped/
            # unmatched term when it plainly named a real specialization.
            elif _text_evidence_exists(session, gram_l):
                result.skill_groups.append({"term": gram})
                consumed |= span
        elif _meaningful(gram_l, 5) and not _is_generic(gram_l, df, vocab_size):
            # One word out of a phrase the corpus does not know does not stand
            # in for the phrase. "graph neural networks" has no match, and the
            # bare word "networks" reached wireless sensor networks and
            # VANETs — a confident answer to a question nobody asked. Longer
            # sub-phrases are still tried (and "neural networks" does match),
            # because they carry enough of the phrase to mean it.
            if len(parts) == 1 and span & unknown_phrases:
                continue
            # An employer nobody here works for is reported, not answered
            # with a subject: "at both Google and Stanford" reached
            # google-chrome and google-app-engine through containment below,
            # so a question about who works at Google came back as a list of
            # Chrome DevTools users. Organizations the corpus does hold are
            # matched before this (including by acronym).
            if _employer_position(tokens, span, gram):
                result.unmatched_terms.append(gram)
                reported_misses.append(span)
                consumed |= span
                continue
            # Word-boundary containment: "computer vision" should reach
            # "Computer Vision and Image Processing". Generic words are
            # excluded above, or "building" would match building materials.
            contained = _contained(gram_l, skills, aux)
            group = {"term": gram}
            if not contained:
                # A word people here are called is a name before it is a typo:
                # "Virat" -- seven people carry it -- was respelled "viral" and
                # answered with virologists.
                if (len(parts) == 1 and _could_be_a_name(gram)
                        and _name_exists(session, gram)):
                    result.name_terms.append(gram)
                    consumed |= span
                    continue
                # A typo one word into a phrase: "distributed sytems" used to
                # apply the bare word "distributed" (reaching Distributed
                # Processing) and report the rest as dropped.
                repaired = _repair(parts, skills, aux)
                if repaired:
                    fixed, typed, values = repaired
                    result.skill_groups.append({"term": gram, "values": values,
                                                "searched": " ".join(fixed)})
                    result.corrections.append({"typed": typed, "matched": values[0]})
                    consumed |= span
                    continue
                rescued = _rescue_phrase(parts, skills, aux, df, vocab_size)
                if rescued:
                    values, searched, how = rescued
                    result.skill_groups.append({"term": gram, "values": values})
                    result.rewrites.append({"typed": gram, "searched": searched, "how": how})
                    consumed |= span
                    continue
                # A subject the concept map knows, as a whole phrase. Checked
                # before the phrase can be split: "air pollution" otherwise
                # became the field "Pollution" — its exact word — and the
                # air-quality researchers it means were never asked about.
                if len(parts) > 1:
                    related = _related_values(gram, skills, aux, set())
                    if related:
                        result.skill_groups.append(
                            {"term": gram, "values": [], "related_values": related})
                        result.rewrites.append({"typed": gram, "searched": "related subjects",
                                                "how": "related subjects"})
                        consumed |= span
                        continue
                # A near-miss for a real vocabulary entry is a typo, not a
                # free-text hit: "bangalor" appears inside bios that say
                # Bangalore, which made the city look like a skill. The
                # per-token typo pass below gets a chance at it; either way its
                # own words must not answer for the phrase.
                if _near_vocabulary(gram_l, skills, orgs, locations, aux):
                    if len(parts) > 1:
                        unknown_phrases |= span
                    continue
                # A real person's name is a name filter, not a bio keyword.
                # "Rahul" appears on a portfolio page and used to be swallowed
                # as a skill, so every Rahul Satija in the graph was hidden.
                if (
                    len(parts) == 1
                    and _could_be_a_name(gram)
                    and _name_exists(session, gram)
                ):
                    result.name_terms.append(gram)
                    consumed |= span
                    continue
                # no topic matches, but a bio or job title might say it
                if _text_evidence_exists(session, gram_l):
                    result.skill_groups.append(group)
                    consumed |= span
                    continue
                # a phrase containing a real surname is a name search, and must
                # not be swallowed as an unknown topic. A word the topic
                # vocabulary uses is a subject, not a surname — the same rule
                # the leftover-token pass below follows: a GitHub account
                # display-named "Graph" made "graph neural networks" a name
                # search, which dropped the phrase and let the bare word
                # "graph" answer it with graph theory.
                if any(df.get(singular(t), 0) == 0 and _name_exists(session, t)
                       for t in parts):
                    continue
                # a part that means something on its own — a topic, an
                # organization, a place, a country word, an acronym — keeps the
                # phrase from swallowing it ("Indian AI" is India plus AI)
                exact = any(
                    t in skills or t in orgs or t in locations
                    or t in COUNTRIES or t in DEMONYMS or t in ACRONYMS
                    or t in TECH_SKILLS or t in PLACES
                    for t in parts
                )
                # A phrase joined by a connective — "women in robotics",
                # "Google and Microsoft" — is two things side by side, not one
                # unknown term. Treating it as one blocked the real term inside
                # it: "robotics" disappeared from "women in robotics" while
                # "people in robotics" worked, only because "people" is a
                # stopword and kept the phrase from being considered at all.
                connective = any(t in STOPWORDS for t in parts[1:-1])
                if not exact and not connective:
                    # The corpus does not know this phrase. Record the span so
                    # its individual words cannot stand in for it, but do NOT
                    # consume it: consuming blocked the overlapping phrase that
                    # did match — "graph neural" missing hid "neural networks"
                    # one token to the right. Words that are exact vocabulary
                    # on their own ("Python" in "Python developers") are spared
                    # by the check above.
                    unknown_phrases |= span
                    # tell the caller the phrase was dropped, once: a sub-phrase
                    # of an already-reported miss adds nothing
                    if not any(span <= wider for wider in reported_misses):
                        result.unmatched_terms.append(gram)
                        reported_misses.append(span)
                continue
            if len(contained) <= 12:
                group["values"] = contained
            else:
                group["pattern"] = gram_l
            result.skill_groups.append(group)
            consumed |= span

    # Typos. Checked against every vocabulary at once and the closest wins,
    # so "bangalor" becomes the city rather than whichever skill happened to
    # look nearest — it used to match a skill because only skills were tried.
    #
    # Comparing a token against every vocabulary entry is O(vocab_size) per
    # token; blocked on the first two characters, it is O(bucket_size) —
    # candidates share the token's own opening letters and there are usually
    # only a handful. Same tradeoff _nearest_name() already makes for names
    # (blocked on three letters there): a typo landing in the very first
    # character or two is missed, which is rare next to catching the far more
    # common "one slip further in" case, and cheap in-memory dict passes make
    # this free to build once per parse() call rather than something that
    # needs its own cache.
    skill_blocks, org_blocks, loc_blocks = aux.skill_blocks, aux.org_blocks, aux.loc_blocks
    for i, token in enumerate(tokens):
        if i in consumed or fold(token) in NOISE_WORDS or len(token) < 4:
            continue
        t = fold(token)
        best_kind: str | None = None
        best_value: Any = None
        best_score = 0.0
        for kind, blocks in (("skill", skill_blocks), ("org", org_blocks), ("location", loc_blocks)):
            candidates = blocks.get(t[:2], ())
            hit = max(candidates, key=lambda kv: _typo_score(t, kv[0]), default=None)
            if not hit:
                continue
            score = _typo_score(t, hit[0])
            if score > best_score:
                best_kind, best_value, best_score = kind, hit[1], score
        if best_score >= FUZZY_VOCAB_THRESHOLD:
            if best_kind == "skill":
                result.skill_groups.append({"term": token, "values": [best_value]})
            elif best_kind == "org":
                result.organizations.append(best_value)
                result.org_terms.append({"term": token, "orgs": [best_value]})
            else:
                result.locations.append(best_value)
            result.corrections.append({"typed": token, "matched": str(best_value)})
            consumed.add(i)
            continue

        # A misspelled name reaches nothing at all otherwise, because name
        # matching is exact: "hintonn" simply disappears. Never into a word
        # the topic vocabulary uses, for the reason the leftovers below give:
        # "machin" became the name "machine", an alias two entity records
        # carry, and "machin lerning" answered with a robot.
        if _could_be_a_name(token):
            near = _nearest_name_beside(session, tokens, i, t) or _nearest_name(session, t)
            if near and df.get(singular(fold(near)), 0) > 0:
                near = None
            if near:
                result.name_terms.append(near)
                result.corrections.append({"typed": token, "matched": near})
                consumed.add(i)
                # reported as unknown on the way here; it is not any more
                if token in result.unmatched_terms:
                    result.unmatched_terms.remove(token)

    # Leftovers. A capitalised word is NOT automatically a person's name:
    # treating "Hyderabad" as one silently guarantees zero results. Only apply
    # a name filter when somebody in the corpus actually has that name;
    # otherwise report the term as unapplied and let the caller see why.
    for i, token in enumerate(tokens):
        if i in consumed or fold(token) in NOISE_WORDS:
            continue
        if i in unknown_phrases:
            # Already reported as part of the phrase it came from — unless the
            # phrase was a misspelled name: in "Rahul Agarwl" the surname was
            # corrected, and the first name must not be lost with the phrase.
            if (result.name_terms and _could_be_a_name(token)
                    and _name_exists(session, token)):
                result.name_terms.append(token)
                for phrase in list(result.unmatched_terms):
                    if token.lower() in phrase.lower().split():
                        result.unmatched_terms.remove(phrase)
            continue
        # A word the topic vocabulary uses is a subject, not a surname:
        # "data" matched entity records like "G. DATA CyberDefense AG".
        if df.get(singular(fold(token)), 0) > 0:
            result.unmatched_terms.append(token)
            continue
        if fold(token) in PLACES:
            result.locations.append(PLACES[fold(token)])
            consumed.add(i)
            continue
        # not gated on capitalisation: people type "sricharan", not "Sricharan"
        if _could_be_a_name(token) and _name_exists(session, token):
            result.name_terms.append(token)
        else:
            result.unmatched_terms.append(token)

    _whole_names(tokens, result, df)
    _expand_concepts(result, skills, aux)

    # flat views, kept so the API response and existing callers stay simple
    result.skills = list(dict.fromkeys(
        v for g in result.skill_groups for v in g.get("values", [])))
    result.skill_patterns = list(dict.fromkeys(
        g["pattern"] for g in result.skill_groups if g.get("pattern")))
    result.organizations = list(dict.fromkeys(result.organizations))
    result.locations = list(dict.fromkeys(result.locations))
    result.countries = list(dict.fromkeys(result.countries))
    # "worked at both Google and Microsoft": all of them. Without "both", a
    # list of organizations stays a choice ("at MIT and Stanford").
    if len(result.org_terms) > 1 and re.search(r"\bboth\b", cleaned, re.I):
        result.require_all_orgs = True
    _fill_clause_order(tokens, result)
    return result


# Characters that end a word in the values we store: "Go, Python", "C/C++",
# "Machine Learning (Applied)", "Bangalore - Karnataka".
_WORD_EDGES = ",;/|()[]{}\"'-\u2013\u2014:.\t\n"


def like_escape(text: str) -> str:
    """TEXT with LIKE's own wildcards made literal, for use with escape="\\".

    Without it a location filter of "%" matched everyone with a location and
    "_erlin" matched Berlin: what the user typed was read as a pattern.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _word_match(column, value: str):
    """Match VALUE as a whole word or phrase inside COLUMN.

    A plain substring filter is close to useless at this scale: skill=r
    matched 49,239 of 50,733 people because nearly every skill contains the
    letter r, and skill=go reached Hinton through "Cognitive".

    Every separator is rewritten to a space and both sides are padded, so one
    LIKE can ask for " go " and mean the word. A trailing * asks for the loose
    behaviour back: skill=go* still matches "Golang". LIKE rather than a regex
    so the same clause runs on SQLite and Postgres.

    NOTE — tried and reverted: an inverted word-token index (EvidenceToken)
    was built to let this seek an index instead of scanning Evidence.value.
    It measured SLOWER in practice, not faster, and was removed. Every call
    site here is wrapped in sa_exists().where(and_(Evidence.person_id ==
    Person.id, ...)) — a correlated EXISTS evaluated once per outer Person
    row, where the person_id index already narrows that per-row scan to the
    ~3 evidence rows an average person has (measured on a 10k-person, 30k-
    evidence corpus) before this LIKE ever runs. Adding an index-backed
    pre-filter added a whole extra join per row for a scan that was already
    over a handful of short strings — pure overhead, confirmed by a real
    before/after benchmark, not assumed. Left as a comment rather than a
    silent revert so the next person doesn't reach for the same idea without
    the number that already answered it.
    """
    v = " ".join((value or "").strip().lower().split())
    if not v:
        return column.isnot(None)
    if v.endswith("*"):
        stem = v[:-1].strip()
        if not stem:
            return column.isnot(None)
        return func.lower(column).like(f"%{like_escape(stem)}%", escape="\\")

    normalized = func.lower(column)
    for sep in _WORD_EDGES:
        normalized = func.replace(normalized, sep, " ")
    padded = literal(" ").concat(normalized).concat(literal(" "))
    return padded.like(f"% {like_escape(v)} %", escape="\\")


LOCATION_TEXT_ATTRS = ("bio", "role", "location", "education")


def location_needles(loc: str) -> list[str]:
    """Spellings that mean the same place as LOC."""
    raw = fold((loc or "").split(",")[0].strip())
    if not raw:
        return []
    return list(dict.fromkeys(PLACE_SYNONYMS.get(raw, (raw,))))


def location_anywhere(loc: str):
    """Match a place on the person record, in a bio, or on an affiliation.

    GitHub often leaves `location` blank and writes the city into the bio
    ("NMIT, Bangalore"). Restricting the filter to Person.location then
    reports the city as not found for people who clearly have it.
    """
    from sqlalchemy import exists as sa_exists

    from .models import Affiliation

    clauses = []
    for needle in location_needles(loc):
        clauses.extend([
            _word_match(Person.location, needle),
            _word_match(Person.summary, needle),
            _word_match(Person.current_organization, needle),
            sa_exists().where(and_(
                Evidence.person_id == Person.id,
                Evidence.attribute_type.in_(LOCATION_TEXT_ATTRS),
                _word_match(Evidence.value, needle),
            )).correlate(Person),
            sa_exists().where(and_(
                Affiliation.person_id == Person.id,
                Organization.id == Affiliation.organization_id,
                _word_match(Organization.name, needle),
            )).correlate(Person),
        ])
    if not clauses:
        return Person.id.is_(None)
    return or_(*clauses)


def place_mentioned(text: str | None) -> str | None:
    """The gazetteer place named in free text, if any."""
    ws = words(text)
    if not ws:
        return None
    # longest phrase first, so "new delhi" wins over "delhi"
    for n in (3, 2, 1):
        for i in range(len(ws) - n + 1):
            gram = " ".join(ws[i:i + n])
            if gram in PLACES:
                return PLACES[gram]
    return None


def _name_clauses(column, token: str):
    """Word-boundary name match, since SQLite has no regex.

    A substring test makes "AI" match "A. Aijaz" and "Suaide" — 2,027 people
    in this corpus — so a query about AI silently becomes a query about names.
    """
    t = token.lower()
    return (
        func.lower(column) == t,
        func.lower(column).like(f"{like_escape(t)} %", escape="\\"),
        func.lower(column).like(f"% {like_escape(t)}", escape="\\"),
        func.lower(column).like(f"% {like_escape(t)} %", escape="\\"),
        func.lower(column).like(f"{like_escape(t)}, %", escape="\\"),
        func.lower(column).like(f"% {like_escape(t)}, %", escape="\\"),
        # Rahul's | Portfolio Website — apostrophe is a word edge
        func.lower(column).like(f"{like_escape(t)}'%", escape="\\"),
        func.lower(column).like(f"% {like_escape(t)}'%", escape="\\"),
    )


_PLURAL_ROLE = re.compile(r"(s|es)$")


def _role_exists(session: Session, title: str) -> bool:
    """Does anyone in the corpus actually hold this job title?"""
    from .models import Affiliation

    if si.is_ready(session):
        return si.any_match(session, si.phrase_alt("r", title))
    if session.execute(
        select(Person.id).where(_word_match(Person.current_role, title)).limit(1)
    ).first():
        return True
    return session.execute(
        select(Affiliation.id).where(_word_match(Affiliation.role, title)).limit(1)
    ).first() is not None


def _singular_role(phrase: str) -> str:
    """"community managers" -> "community manager", so it matches a job title."""
    words = phrase.split()
    if words and words[-1] not in ("bus", "ops"):
        words[-1] = _PLURAL_ROLE.sub("", words[-1]) or words[-1]
    return " ".join(words)


def _near_vocabulary(
    term: str, skills: dict, orgs: dict, locations: dict, aux: "VocabAux | None" = None,
) -> bool:
    """Is this term a near-miss for something the corpus actually knows?

    Same rule as _typo_score (ratio >= threshold, or one edit in a word of
    five or more letters), evaluated by rapidfuzz's C batch matcher with an
    early cutoff instead of a Python loop over the whole vocabulary.
    """
    from rapidfuzz import process
    from rapidfuzz.distance import Levenshtein

    keylists = (
        (aux.skill_keys, aux.org_keys, aux.loc_keys) if aux is not None
        else (list(skills), list(orgs), list(locations))
    )
    for keys in keylists:
        if not keys:
            continue
        if process.extractOne(term, keys, scorer=fuzz.ratio,
                              score_cutoff=FUZZY_VOCAB_THRESHOLD):
            return True
        if len(term) >= 5 and process.extractOne(
            term, keys, scorer=Levenshtein.distance, score_cutoff=1
        ):
            return True
    return False


def _nearest_name_beside(session: Session, tokens: list[str], i: int, token: str) -> str | None:
    """A misspelled name corrected against the people its neighbour names.

    "Dhruv Dixt" is four letters from anything on its own, too short for the
    general typo rule — and it returned 34 people called Dhruv. Beside a real
    first name, though, the candidates are the other names those people carry,
    and "dixit" is one edit away. Only names that sit next to the neighbour in
    the graph are considered, so this cannot turn a surname into someone else's.
    """
    from rapidfuzz.distance import DamerauLevenshtein

    if len(token) < 4 or not si.is_ready(session):
        return None
    for j in (i - 1, i + 1):
        if not 0 <= j < len(tokens):
            continue
        neighbour = tokens[j]
        if not _could_be_a_name(neighbour) or not _name_exists(session, neighbour):
            continue
        ids = si.people_with(session, si.phrase_alt("n", neighbour), limit=200)
        if not ids:
            continue
        near_neighbour = fold(neighbour)
        candidates: set[str] = set()
        for name in session.execute(
            select(Person.canonical_name).where(Person.id.in_(ids))
        ).scalars():
            candidates.update(w for w in words(name) if w != near_neighbour and len(w) > 2)
        limit = 2 if len(token) >= 7 else 1
        scored = sorted((DamerauLevenshtein.distance(token, c), c) for c in candidates
                        if c[:1] == token[:1] and c != token)
        if scored and scored[0][0] <= limit:
            return scored[0][1]
    return None


def _nearest_name(session: Session, token: str) -> str | None:
    """The closest real surname to a typo, or None if nothing is close.

    Blocked on the first three letters so this is a small comparison rather
    than a scan of every name in the graph.
    """
    from .models import PersonNameToken

    if len(token) < 4:
        return None
    # A range, not LIKE 'abc%': SQLite cannot serve LIKE from a BINARY index,
    # so the prefix form read the whole token table.
    prefix = token[:3]
    upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    candidates = session.execute(
        select(PersonNameToken.token)
        .where(PersonNameToken.token >= prefix, PersonNameToken.token < upper)
        .distinct()
        .limit(400)
    ).scalars().all()
    best = max(candidates, key=lambda c: _typo_score(token, c), default=None)
    if best and best != token and _typo_score(token, best) >= FUZZY_NAME_THRESHOLD:
        return best
    return None


def names_for_country(code: str) -> list[str]:
    """Every way a location string might spell this country."""
    code = (code or "").upper()
    return sorted(
        {name for name, iso in COUNTRIES.items() if iso == code}
        | {name for name, iso in DEMONYMS.items() if iso == code}
    )


def country_from_location(text: str | None) -> str | None:
    """The ISO-2 country named in a free-text location, if any.

    "Bangalore, India" and "Reading, United Kingdom" state their country
    plainly; without this they are unreachable by a country filter, which is
    why searching Go developers in IN returned nobody while the Go developers
    were sitting there with location "Bangalore, India". Bare city names are
    deliberately NOT guessed — "Stanford" is a university, not a place we can
    resolve to a country on our own.
    """
    if not text:
        return None
    parts = [w.strip(" .,()") for w in re.split(r"[,/|]| - ", fold(text))]
    for part in reversed(parts):          # the country is usually written last
        part = part.strip()
        if part in COUNTRIES:
            return COUNTRIES[part]
        if part in DEMONYMS:
            return DEMONYMS[part]
    return None


def _could_be_a_name(token: str) -> bool:
    """Could this word be part of a person's name, rather than a technology?

    Rejects acronyms and product-style spellings — SaaS, PostgreSQL, GraphQL,
    k8s — which name search would happily match against real surnames.
    """
    t = token.strip().lower()
    if len(t) < 3 or t in ACRONYMS or t in NOISE_WORDS or t in TECH_SKILLS or t in PLACES:
        return False
    if any(ch.isdigit() for ch in t):
        return False
    # CamelCase or an inner capital is product branding, not a surname
    inner = token.strip()[1:]
    if any(ch.isupper() for ch in inner):
        return False
    return token.strip().isalpha()


def _looks_like_a_name(token: str) -> bool:
    """Acronyms are technologies, not surnames: AI, ML, UX, API, SQL."""
    return len(token) >= 4 and not token.isupper()


def _full_name_exists(session: Session, phrase: str) -> bool:
    """Does a real person's name contain this whole phrase?

    Checked against the matched name, not just the row: index entities that
    slipped in ("HUA Computer Vision Group") would otherwise make "computer
    vision" look like somebody's name.
    """
    if si.is_ready(session):
        from .names import name_phrase_forms

        for form in name_phrase_forms(phrase):
            want = " ".join(words(form))
            ids = si.people_with(session, si.phrase_alt("n", form), limit=20)
            names = session.execute(
                select(Person.canonical_name).where(Person.id.in_(ids))
            ).scalars().all() if ids else []
            # the phrase has to open or close the name (see below)
            if any(
                _looks_like_a_person(n)
                and (" ".join(words(n)).startswith(want) or " ".join(words(n)).endswith(want))
                for n in names
            ):
                return True
        return False
    rows = session.execute(
        select(Person.canonical_name).where(
            func.lower(Person.canonical_name).like(f"%{like_escape(phrase)}%", escape="\\"),
            Person.merged_into.is_(None),
        ).limit(20)
    ).scalars().all()
    # A name is written "Geoffrey Hinton", so the phrase has to open or close
    # it. Buried in the middle it is a description, not a name: the index holds
    # author records like "PhD Computer Vision Mariano Cabezas".
    return any(
        _looks_like_a_person(n)
        and (n.lower().startswith(phrase) or n.lower().endswith(phrase))
        for n in rows
    )


def _name_word_keys(text: str) -> list[set[str]]:
    """Per word of TEXT, the name keys any of its search forms is stored as."""
    from .names import search_forms
    from .resolution import name_tokens

    out = []
    for word in words(text):
        keys = set()
        for form in search_forms(word):
            keys |= {k for k in name_tokens(form) if not k.startswith("k:")}
        if keys:
            out.append(keys)
    return out


def _carries_name_words(text: str):
    """SQL: the person's own names carry every word of TEXT."""
    from sqlalchemy import exists as sa_exists

    from .models import PersonNameToken

    per_word = _name_word_keys(text)
    if not per_word:
        return false()
    return and_(*[sa_exists().where(PersonNameToken.person_id == Person.id,
                                    PersonNameToken.token.in_(sorted(keys)))
                  for keys in per_word])


def _name_exists(session: Session, token: str) -> bool:
    """Does anyone in the corpus actually carry this word as part of a name?"""
    from sqlalchemy import or_

    if not _looks_like_a_name(token):
        return False
    if si.is_ready(session):
        from .names import search_forms

        ids = []
        # canonical names, then the person's own other names -- the index
        # keeps only those (names.alias_fits): "Leandros Maglaras" is
        # Λέανδρος Μαγλαράς, and was nobody's name to a Latin-script search
        for field in ("n", "na"):
            for form in sorted(search_forms(token)):
                ids += si.people_with(session, si.phrase_alt(field, form), limit=20)
            if ids:
                break
        if not ids:
            return False
        rows = session.execute(
            select(Person.canonical_name).where(Person.id.in_(ids))
        ).scalars().all()
        return any(_looks_like_a_person(n) for n in rows)
    rows = session.execute(
        select(Person.canonical_name).where(
            or_(*_name_clauses(Person.canonical_name, token), _carries_name_words(token)),
            Person.merged_into.is_(None),
        ).limit(20)
    ).scalars().all()
    # validated against the matched name: index entities like "Computer Vision
    # Center" would otherwise make "computer" look like somebody's surname
    return any(_looks_like_a_person(n) for n in rows)


def has_filters(parsed: NLQuery) -> bool:
    return bool(
        parsed.skill_groups or parsed.organizations
        or parsed.locations or parsed.name_terms or parsed.countries
        or parsed.roles or parsed.min_publications or parsed.min_citations
    )


def _papers_naming(term: str):
    """How many of the person's papers name TERM in the title or the paper's
    own topics: the "w" postings, for a database whose index is not built."""
    from sqlalchemy import String, cast

    from .models import Authorship, Publication

    needle = f"%{like_escape(term.lower())}%"
    return (
        select(func.count(Authorship.id))
        .join(Publication, Publication.id == Authorship.publication_id)
        .where(Authorship.person_id == Person.id,
               func.lower(Publication.title).like(needle, escape="\\")
               | func.lower(cast(Publication.topics, String)).like(needle, escape="\\"))
        .correlate(Person)
        .scalar_subquery()
    )


def _filtered_stmt(parsed: NLQuery, session: Session | None = None):
    """The filter query, without paging — shared by the count and the page."""
    from sqlalchemy import exists as sa_exists
    from sqlalchemy import or_

    from .models import Affiliation  # noqa: F401  (used below)

    stmt = select(Person).where(Person.merged_into.is_(None))
    # One EXISTS per concept: a person must satisfy EVERY concept asked for,
    # while any of that concept's matching topics will do. ORing everything
    # together instead would turn "robotics and computer vision" into "either".
    for group in parsed.skill_groups:
        clauses = []
        every_value = [*(group.get("values") or []), *(group.get("contained_values") or []),
                       *(group.get("related_values") or [])]
        if every_value:
            clauses.append(and_(
                Evidence.attribute_type.in_(SKILL_ATTRS),
                func.lower(Evidence.value).in_([v.lower() for v in every_value]),
            ))
        if group.get("pattern"):
            clauses.append(and_(
                Evidence.attribute_type.in_(SKILL_ATTRS),
                func.lower(Evidence.value).like(f"%{like_escape(group['pattern'].lower())}%", escape="\\"),
            ))
        # the raw term as written, against free text (bio, job title)
        term = _search_term(group).lower()
        if _meaningful(term, 4):
            clauses.append(and_(
                Evidence.attribute_type.in_(("bio", "role")),
                func.lower(Evidence.value).like(f"%{like_escape(term)}%", escape="\\"),
            ))
        if not clauses:
            continue
        stated = sa_exists().where(and_(Evidence.person_id == Person.id, or_(*clauses)))
        if _meaningful(term, 4):
            stmt = stmt.where(stated | (_papers_naming(term) >= si.WORK_MENTIONS_TO_MATCH))
        else:
            stmt = stmt.where(stated)
    if parsed.organizations:
        from sqlalchemy.orm import aliased

        from .models import normalize_org_name

        for names in _org_groups(parsed):
            wanted = {o.lower() for o in names}
            norms = {normalize_org_name(o) for o in names if o}
            aff, org = aliased(Affiliation), aliased(Organization)
            stmt = stmt.where(sa_exists().where(
                aff.person_id == Person.id,
                org.id == aff.organization_id,
                func.lower(org.name).in_(wanted)
                # the same company under another spelling
                | org.norm_name.in_(norms),
            ).correlate(Person))
    if parsed.countries:
        # The country a source stated, or one the stated location names:
        # "Pune, India" is India whether or not anyone filled the field in.
        # The index reads it that way (search_index adds a "c" posting from
        # the location), and before the index is built this has to agree —
        # a Postgres run found "machine learning researchers in India"
        # answering with nobody here.
        where = [func.upper(Person.country).in_(parsed.countries)]
        where += [location_anywhere(name) for code in parsed.countries
                  for name in names_for_country(code)]
        if session is None:
            stmt = stmt.where(or_(*where))
        else:
            # the index's own rule, person by person: a stated country wins,
            # and only current workplaces place someone who has one. Matching
            # any Indian institution ever named put an IIT Delhi alumnus now
            # in Norway "in India" here and not in the index.
            stated_country = func.coalesce(func.trim(Person.country), "")
            pool = session.execute(select(Person).where(
                Person.merged_into.is_(None),
                func.upper(Person.country).in_(parsed.countries) | (stated_country == ""),
            )).scalars().all()
            jobs = si.employers_of(session, [p.id for p in pool])
            codes = {c.upper() for c in parsed.countries}
            placed = [p.id for p in pool
                      if si.person_country(p, *jobs.get(p.id, ([], []))).upper() in codes]
            stmt = stmt.where(Person.id.in_(placed))
    if parsed.locations:
        stmt = stmt.where(or_(*[
            location_anywhere(loc) for loc in parsed.locations
        ]))
    for title in parsed.roles:
        # A job title, checked against what sources say someone's role is.
        # correlate(Person) keeps Affiliation in the subquery's FROM: the outer
        # query may already join it, and SQLAlchemy would otherwise correlate
        # every table away and leave the EXISTS with nothing to select from.
        stmt = stmt.where(
            _word_match(Person.current_role, title)
            | sa_exists().where(and_(
                Affiliation.person_id == Person.id,
                _word_match(Affiliation.role, title),
            )).correlate(Person)
        )
    for term in parsed.name_terms:
        # the canonical name, or the person's own other names through their
        # name keys -- which hold only aliases that are that person's name
        # (resolution.sync_name_tokens). Matching the raw alias list let
        # "Aman Sharma" filed on Poonam Sharma find her here, where the index
        # did not.
        stmt = stmt.where(or_(*_name_clauses(Person.canonical_name, term),
                              _carries_name_words(term)))
    for sub in _excluded_queries(parsed):
        stmt = stmt.where(Person.id.not_in(
            _filtered_stmt(sub, session).with_only_columns(Person.id)))
    if parsed.min_publications or parsed.min_citations:
        from .models import Authorship, Publication

        # stored works only: without the index there are no source totals
        if parsed.min_publications:
            stmt = stmt.where(
                select(func.count(Authorship.id)).where(Authorship.person_id == Person.id)
                .correlate(Person).scalar_subquery() >= parsed.min_publications)
        if parsed.min_citations:
            stmt = stmt.where(
                select(func.coalesce(func.sum(Publication.citations), 0))
                .join(Authorship, Authorship.publication_id == Publication.id)
                .where(Authorship.person_id == Person.id)
                .correlate(Person).scalar_subquery() >= parsed.min_citations)
    return stmt


def _org_groups(parsed: NLQuery) -> list[list[str]]:
    """Organizations as AND-ed groups of alternative spellings: one group
    normally, one per organization typed when all of them are required."""
    if parsed.require_all_orgs and len(parsed.org_terms) > 1:
        groups = [list(e["orgs"]) for e in parsed.org_terms if e["orgs"]]
        if groups:
            return groups
    return [parsed.organizations] if parsed.organizations else []


def _excluded_queries(parsed: NLQuery) -> list[NLQuery]:
    """One query per excluded clause; matching ANY of them excludes."""
    out = []
    for entry in parsed.exclusions:
        for clause in entry.get("clauses") or []:
            sub = _parsed_from_clauses(NLQuery(raw=""), [clause])
            if has_filters(sub):
                out.append(sub)
    return out


def _restrict(parsed: NLQuery):
    exclude = [c for sub in _excluded_queries(parsed) for c in _constraints(sub)]
    restrict = si.Restrict(exclude, parsed.min_publications, parsed.min_citations)
    return restrict or None


def _group_alts(group: dict) -> list:
    """Index alternatives for one skill concept — the same four routes the
    SQL filter takes: an exact topic value, a phrase inside a longer topic,
    the term as typed in a bio or job title, and the term in enough of their
    papers."""
    alts = [si.exact_alt("sv", value_key(v))
            for v in [*(group.get("values") or []), *(group.get("contained_values") or []),
                      *(group.get("related_values") or [])]]
    if group.get("pattern"):
        alts.append(si.phrase_alt("s", group["pattern"]))
    term = _search_term(group)
    if _meaningful(term, 4):
        alts.append(si.phrase_alt("t", term))
        # a subject of their papers they never state as a topic
        alts.append(si.work_alt(term))
    return [a for a in alts if a is not None]


def _constraints(parsed: NLQuery) -> list:
    """The parsed query as index constraints: ANY alternative satisfies a
    constraint, EVERY constraint must hold. Mirrors _filtered_stmt exactly in
    what is ANDed and what is ORed."""
    out = []
    for group in parsed.skill_groups:
        alts = _group_alts(group)
        if alts:
            out.append(si.Constraint(f"skill:{group.get('term')}", alts))
    for i, names in enumerate(_org_groups(parsed)):
        alts = [si.exact_alt("o", org_key(o)) for o in names]
        out.append(si.Constraint(f"org:{i}", [a for a in alts if a]))
    if parsed.countries:
        alts = [si.exact_alt("c", c.lower()) for c in parsed.countries]
        out.append(si.Constraint("country", [a for a in alts if a]))
    if parsed.locations:
        alts = [si.phrase_alt("p", needle)
                for loc in parsed.locations for needle in location_needles(loc)]
        out.append(si.Constraint("location", [a for a in alts if a]))
    for title in parsed.roles:
        alt = si.phrase_alt("r", title)
        out.append(si.Constraint(f"role:{title}", [alt] if alt else []))
    from .names import name_phrase_forms

    for term in parsed.name_terms:
        # "Bill Gates" is also William Gates; "Agarwal" is also Aggarwal. The
        # words of a name, not the words side by side: "karan singh" missed
        # Karan P. Singh and Karan Pratap Singh, whom the SQL path found.
        alts = [alt for form in name_phrase_forms(term)
                for alt in (_name_words_alt("n", form), _name_words_alt("na", form))]
        out.append(si.Constraint(f"name:{term}", [a for a in alts if a]))
    return out


def _name_words_alt(fld: str, text: str):
    """Every word of TEXT in field FLD, in any order and any distance apart."""
    terms = tuple(dict.fromkeys(w for w in words(text) if len(w) <= MAX_TERM_LEN))
    return si.Alt(fld, terms) if terms else None


def _topical_alts(parsed: NLQuery) -> list:
    return [a for g in parsed.skill_groups for a in _group_alts(g) if len(a.terms) == 1]


def count_matches(session: Session, parsed: NLQuery) -> int:
    """How many people match in total — not just how many this page returns."""
    if not has_filters(parsed):
        return 0
    if si.is_ready(session):
        return si.count(session, _constraints(parsed), _restrict(parsed))
    inner = _filtered_stmt(parsed, session).with_only_columns(Person.id).distinct().subquery()
    return session.execute(select(func.count()).select_from(inner)).scalar_one()


def satisfying(session: Session, parsed: NLQuery, ids, use_index: bool = True) -> set[str]:
    """Which of IDS meet every constraint PARSED applies.

    For people a live source has just returned: they are shown only when
    they answer the question. A search for "Sundar Pichai" showed Michael
    Bauer and a GitHub account called JACKSPARROWbts, because a source had
    matched the words somewhere in their papers or profile. A query whose
    every term is unknown applies no constraint, and keeps everyone.
    """
    ids = list(ids)
    if not ids or not has_filters(parsed):
        return set(ids)
    # the index learns a person at commit; one still being decided on is only
    # flushed, and the SQL filter -- the index's mirror -- can see them
    if use_index and si.is_ready(session):
        stmt = si.match_select(session, _constraints(parsed), _restrict(parsed))
        if stmt is None:
            return set()
        sub = stmt.subquery()
        return set(session.execute(
            select(sub.c.person_id).where(sub.c.person_id.in_(ids))).scalars())
    return set(session.execute(
        _filtered_stmt(parsed, session).with_only_columns(Person.id)
        .where(Person.id.in_(ids)).distinct()).scalars())


FILTER_GROUPS = ("skill_groups", "organizations", "locations", "countries", "name_terms")


def diagnose_empty(session: Session, parsed: NLQuery) -> dict | None:
    """When filters combine to nothing, say which one is responsible.

    Every filter can be individually reasonable while the intersection is
    empty — "growth" matches an economics topic, "Zomato" matches employers,
    and nobody is both. Reporting a bare 0 makes that look like a fault.
    """
    from dataclasses import replace

    active = [g for g in FILTER_GROUPS if getattr(parsed, g)]
    if len(active) < 2:
        return None
    for group in active:
        relaxed = replace(parsed)
        setattr(relaxed, group, [])
        if not has_filters(relaxed):
            continue
        n = count_matches(session, relaxed)
        if n:
            dropped = getattr(parsed, group)
            if group == "skill_groups":
                dropped = [g["term"] for g in dropped]
            return {
                "filter": group,
                "values": list(dropped),
                "would_match": n,
                "message": (
                    f"No one matches every filter at once. Dropping "
                    f"{group.replace('_', ' ')} ({', '.join(map(str, dropped))}) "
                    f"would return {n:,}."
                ),
            }
    return None


# How many filter matches to score before paging. Filtering says who is
# eligible; ranking says who is best, and it cannot say that over a page it
# has already been handed. Deep paging past this is not a meaningful request
# of a ranked list — total_matches still reports the true filter count.
#
# 250, down from 500, because the pool is now cut by a prior that mirrors the
# final score (search_index.SearchDoc.prior). Measured on a 10k corpus against
# exhaustive scoring of every match: pool 150 already returned the true top 50
# for all 30 broad benchmark queries (recall 1.000); 250 keeps margin for
# messier real data at ~20% less scoring work than 500. The old evidence-count
# cut recovered only 65% of the true top 10 even at 500.
CANDIDATE_POOL = int(os.environ.get("RIP_CANDIDATE_POOL", "250"))

# What "better profile" means, as weights that sum to 1. Deliberately a plain
# linear blend rather than a learned model: every result can explain itself,
# and match_feedback has to accumulate real judgements before anything can be
# trained on them.
WEIGHTS = {
    "depth": 0.25,          # how much evidence backs the thing you asked for
    "output": 0.25,         # work they actually shipped, and how much it landed
    "confidence": 0.15,     # how strongly the source stated it
    "recency": 0.15,        # how recent the evidence is, where dated
    "corroboration": 0.10,  # independent sources agreeing
    "breadth": 0.10,        # how many sources know this person at all
}
# When the query is just a name, exact identity matters more than output.
NAME_ONLY_WEIGHTS = {
    "name_fit": 0.45,
    "depth": 0.10,
    "output": 0.10,
    "confidence": 0.10,
    "recency": 0.10,
    "corroboration": 0.075,
    "breadth": 0.075,
}
# Saturation points: past this many rows the signal stops distinguishing
# people, so it is scaled logarithmically rather than left unbounded.
DEPTH_SATURATION = 12.0
BREADTH_SATURATION = 4.0
# How fast "how recent is this" decays, per KIND of dated evidence — a
# dormant repository and an old paper do not say the same thing about
# someone's current standing. A maintainer who stopped pushing a year ago
# has very plausibly moved on; a researcher whose most-cited paper is four
# years old has not thereby stopped being an expert in it — citations
# accumulate for years after publication, and co-authorship on real
# research is a durable credential in a way "last commit" is not. Applied
# in _recency_score() below: each dated source decays on its own timescale,
# and the least-decayed one wins, rather than picking one global rate and
# either overstating repo staleness or understating paper staleness.
PROJECT_RECENCY_HALF_LIFE_DAYS = 365.0
PUBLICATION_RECENCY_HALF_LIFE_DAYS = 1460.0
# Evidence.published_at (when a bio/skill/role claim's SOURCE was published,
# not project or publication activity specifically) keeps the original,
# unchanged rate — a deliberately conservative middle ground for a date
# whose subject matter varies too much to assign a sharper category.
EVIDENCE_RECENCY_HALF_LIFE_DAYS = 730.0
# Combined stars + citations at which output stops distinguishing people.
# Log-scaled, so a 240k-star repository does not flatten everyone else. Tuned
# against a real sample: at 500 a widely-followed maintainer and someone with a
# single 500-star repo both scored a flat 1.0, which is exactly the tie this
# signal exists to break. 5,000 is genuinely notable for stars and for
# citations alike, and keeps the field spread.
OUTPUT_SATURATION = 5000.0
# Work that is real but not what you asked about. It still says this person
# ships things, so it counts — at a quarter, so an unrelated famous repo never
# outranks on-topic work.
OFF_TOPIC_WEIGHT = 0.25
# A fork is a weaker signal of impact than a star: it costs one click and is
# routinely done to read code rather than to endorse it.
FORK_WEIGHT = 0.5
# Most-cited and most-recent publications read per person when scoring.
PUBLICATIONS_PER_PERSON = 50


def _log_scale(value: float, saturation: float) -> float:
    """Bounded 0..1 growth — the 20th repo matters less than the 2nd."""
    import math

    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(saturation))


def _recency_score(now, when, half_life_days: float) -> float | None:
    """0..1 exponential-decay score for one dated signal, or None if WHEN is
    unknown — the caller decides what "no date at all" should mean (a
    neutral default, typically), rather than this function silently
    inventing one."""
    if when is None:
        return None
    age_days = max(0.0, (now - when).total_seconds() / 86400.0)
    return float(0.5 ** (age_days / half_life_days))


# A bio saying "I work on machine learning" is real evidence, but it is a
# self-description, not a body of work — it should separate someone from
# nobody without ever outranking a decade of commits.
TEXT_EVIDENCE_WEIGHT = 0.25
# A field is a filing category, not a claim about the person's own work: it
# counts for half of a topic they actually work on.
FIELD_EVIDENCE_WEIGHT = 0.5
# A subject related to the one asked for (concepts.CONCEPTS) is partial
# evidence: someone who states "neural networks" is a candidate for "deep
# learning", behind someone who states deep learning itself.
RELATED_SUBJECT_WEIGHT = 0.5
# A stated topic that contains the word asked for ("Global Public Health
# Policies and Epidemiology" for "epidemiology") is the person's own claim, less
# specific than the exact subject but more than a filing category or a
# neighbouring subject. Scored as a related subject, it tied with someone merely
# filed under the Epidemiology subfield, and ranked no higher.
CONTAINED_TOPIC_WEIGHT = 0.75
# Most related values one concept may add: a broad subject must not turn one
# query into a scan of half the vocabulary.
MAX_RELATED_VALUES = 40


def _matched_evidence_clause(parsed: NLQuery):
    """Evidence rows that back what the query asked for, and how they count.

    Returns (clause, weight_case): the clause selects rows to score, and the
    CASE assigns each row its weight — full for a skill the source stated
    outright, a fraction for a passing mention in free text.
    """

    clauses = []
    for group in parsed.skill_groups:
        every_value = [*(group.get("values") or []), *(group.get("contained_values") or []),
                       *(group.get("related_values") or [])]
        if every_value:
            clauses.append(and_(
                Evidence.attribute_type.in_(SKILL_ATTRS),
                func.lower(Evidence.value).in_([v.lower() for v in every_value]),
            ))
        if group.get("pattern"):
            clauses.append(and_(
                Evidence.attribute_type.in_(SKILL_ATTRS),
                func.lower(Evidence.value).like(f"%{like_escape(group['pattern'].lower())}%", escape="\\"),
            ))
        # The raw term against free text — the same clause the filter uses, so
        # anything that passed the filter can also be scored. Without this a
        # bio-only match scores a flat zero and every such person ties.
        term = _search_term(group).lower()
        if _meaningful(term, 4):
            clauses.append(and_(
                Evidence.attribute_type.in_(("bio", "role")),
                func.lower(Evidence.value).like(f"%{like_escape(term)}%", escape="\\"),
            ))
    if not clauses:
        return None, None
    weight = case(
        (Evidence.attribute_type == "research_field", FIELD_EVIDENCE_WEIGHT),
        (Evidence.attribute_type.in_(SKILL_ATTRS), 1.0),
        else_=TEXT_EVIDENCE_WEIGHT,
    )
    return or_(*clauses), weight


def _score_papers_only(session, parsed, ids, per_group, related_depth, contained_depth,
                       depth) -> set[str]:
    """Depth for people the filter let in through their papers alone, and
    who they are.

    Worth one bio mention (TEXT_EVIDENCE_WEIGHT) and given only where nothing
    stated matched that concept, so nobody already matched moves. Depth alone
    does not keep them behind the people who state the subject: their papers
    count in output too, and a breast pathologist with cited
    computational-pathology papers came second for "computational pathology"
    ahead of the people who work on it. _rank_and_page orders them after
    everyone else instead.
    """
    from .models import Authorship, Publication

    phrases = [(gi, stems(_search_term(g))) for gi, g in enumerate(parsed.skill_groups)
               if _meaningful(_search_term(g), 4)]
    phrases = [(gi, p) for gi, p in phrases if p]
    if not phrases:
        return set()
    n_groups = len(parsed.skill_groups)

    def stated(pid: str, gi: int) -> bool:
        return any(d.get(pid, [0.0] * n_groups)[gi]
                   for d in (per_group, related_depth, contained_depth))

    # only people missing stated evidence for some concept can be credited,
    # so only their papers are read -- usually a handful of the pool
    unstated = [pid for pid in ids if any(not stated(pid, gi) for gi, _ in phrases)]
    counts: dict[tuple[str, int], int] = {}
    credited: set[str] = set()
    for start in range(0, len(unstated), 900):
        for pid, title, topics in session.execute(
            select(Authorship.person_id, Publication.title, Publication.topics)
            .join(Publication, Publication.id == Authorship.publication_id)
            .where(Authorship.person_id.in_(unstated[start:start + 900]))
        ).all():
            texts = [stems(title or ""), *(stems(str(t)) for t in topics or [])]
            for gi, phrase in phrases:
                if any(contains_phrase(t, phrase) for t in texts):
                    counts[(pid, gi)] = counts.get((pid, gi), 0) + 1
    for (pid, gi), n in counts.items():
        if n < si.WORK_MENTIONS_TO_MATCH or stated(pid, gi):
            continue
        per_group.setdefault(pid, [0.0] * n_groups)[gi] += TEXT_EVIDENCE_WEIGHT
        depth[pid] = depth.get(pid, 0.0) + TEXT_EVIDENCE_WEIGHT
        credited.add(pid)
    return credited


def _score_evidence(session, parsed, ids, depth, best_conf, corroborated, latest,
                    papers_only: set[str] | None = None):
    """Per-concept evidence depth, plus confidence/corroboration/recency, for
    the candidate pool. Fills the passed dicts; returns {pid: [depth per
    concept]}.

    Matching happens here in Python over the pool's own evidence rows (a few
    per person) with the same whole-word rules the index uses, instead of a
    LIKE '%term%' scan: faster, and "java" no longer counts as evidence for
    someone whose bio says JavaScript.
    """
    matchers = []
    for g in parsed.skill_groups:
        values = {value_key(v) for v in g.get("values") or []}
        contained = {value_key(v) for v in g.get("contained_values") or []} - values
        related = {value_key(v) for v in g.get("related_values") or []} - values - contained
        pattern = words(g["pattern"]) if g.get("pattern") else None
        term = _search_term(g)
        term_words = words(term) if _meaningful(term, 4) else None
        matchers.append((values, contained, related, pattern, term_words))

    n_groups = len(matchers)
    per_group: dict[str, list[float]] = {}
    # related-subject evidence, kept apart so it can be capped below
    related_depth: dict[str, list[float]] = {}
    contained_depth: dict[str, list[float]] = {}
    sources: dict[str, set] = {}
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        rows = session.execute(
            select(Evidence.person_id, Evidence.attribute_type, Evidence.value,
                   Evidence.confidence, Evidence.verification_state, Evidence.source,
                   Evidence.published_at)
            .where(Evidence.person_id.in_(chunk), Evidence.attribute_type.in_(TEXT_ATTRS))
        ).all()
        for pid, attr, value, conf, state, source, published in rows:
            is_skill = attr in SKILL_ATTRS
            weight = ((FIELD_EVIDENCE_WEIGHT if attr == "research_field" else 1.0)
                      if is_skill else TEXT_EVIDENCE_WEIGHT)
            vkey = value_key(value) if is_skill else None
            vwords = None
            hit_any = False
            for gi, (values, contained, related, pattern, term_words) in enumerate(matchers):
                share = 1.0
                if is_skill:
                    hit = vkey in values
                    if not hit and pattern:
                        vwords = vwords if vwords is not None else words(value)
                        hit = contains_phrase(vwords, pattern)
                    if not hit and vkey in contained:
                        hit, share = True, CONTAINED_TOPIC_WEIGHT
                    elif not hit and vkey in related:
                        hit, share = True, RELATED_SUBJECT_WEIGHT
                else:
                    if not term_words:
                        continue
                    vwords = vwords if vwords is not None else words(value)
                    hit = contains_phrase(vwords, term_words)
                if hit:
                    if share == CONTAINED_TOPIC_WEIGHT:
                        contained_depth.setdefault(pid, [0.0] * n_groups)[gi] += weight
                    elif share < 1.0:
                        related_depth.setdefault(pid, [0.0] * n_groups)[gi] += weight
                    else:
                        per_group.setdefault(pid, [0.0] * n_groups)[gi] += weight
                    hit_any = True
            if not hit_any:
                continue
            depth[pid] = depth.get(pid, 0.0) + weight
            best_conf[pid] = max(best_conf.get(pid, 0.0), float(conf or 0.0) * weight)
            if state == "corroborated" and source:
                sources.setdefault(pid, set()).add(source)
            if published and (pid not in latest or published > latest[pid]):
                latest[pid] = published
    for pid, srcs in sources.items():
        corroborated[pid] = len(srcs)
    credited = _score_papers_only(session, parsed, ids, per_group, related_depth,
                                  contained_depth, depth)
    if papers_only is not None:
        papers_only.update(credited)
    # However many related subjects someone has, together they are worth at
    # most half of one subject they state: three neural-network topics made a
    # person outrank someone who states "deep learning" outright.
    for pid, parts in related_depth.items():
        row = per_group.setdefault(pid, [0.0] * n_groups)
        for gi, amount in enumerate(parts):
            row[gi] += min(amount, 1.0) * RELATED_SUBJECT_WEIGHT
    # Topics containing the word count up to one topic's worth, at three
    # quarters: eight "Robotics ..." values are one claim restated, not eight.
    for pid, parts in contained_depth.items():
        row = per_group.setdefault(pid, [0.0] * n_groups)
        for gi, amount in enumerate(parts):
            row[gi] += min(amount, 1.0) * CONTAINED_TOPIC_WEIGHT
    return per_group


def _concept_weights(session: Session, parsed: NLQuery) -> list[float] | None:
    """How much each concept of a multi-concept query counts: its rarity.

    Being deep in "reinforcement learning" says more than being deep in
    "python" when the query asks for both. Inverse document frequency over the
    index, the classic measure: log(1 + N / (1 + people carrying it)).
    """
    if not si.is_ready(session):
        return None
    n = max(1, si.corpus_size(session))
    weights = []
    for g in parsed.skill_groups:
        alts = _group_alts(g)
        counts = si.term_counts(session, [(a.field, t) for a in alts for t in a.terms])
        df = si.estimate(si.Constraint("g", alts), counts)
        weights.append(math.log1p(n / (1 + df)))
    return weights


def _depth_component(per_group: list[float] | None, total: float,
                     weights: list[float] | None) -> float:
    """Depth for the whole query.

    One concept: the log-scaled evidence behind it, exactly as before.
    Several: a rarity-weighted MEAN of each concept's own depth, so someone
    with twelve rows on one concept and nothing on the other no longer
    outranks someone solidly evidenced on both — a sum could not tell them
    apart.
    """
    if not per_group or len(per_group) == 1:
        return _log_scale(total if not per_group else per_group[0], DEPTH_SATURATION)
    scaled = [_log_scale(d, DEPTH_SATURATION) for d in per_group]
    w = weights if weights and len(weights) == len(scaled) and sum(weights) > 0 else [1.0] * len(scaled)
    return sum(a * b for a, b in zip(scaled, w, strict=True)) / sum(w)


def _query_terms(parsed: NLQuery) -> set[str]:
    """Every phrasing of what the query asked for, lowercased."""
    terms: set[str] = set()
    for group in parsed.skill_groups:
        for value in group.get("values") or []:
            terms.add(value.lower())
        if group.get("pattern"):
            terms.add(str(group["pattern"]).lower())
        for key in ("term", "searched"):
            if group.get(key):
                terms |= _subject_spellings(str(group[key]))
    return {t for t in terms if t}


def _subject_spellings(term: str) -> set[str]:
    """TERM and, for an abbreviated subject, its other spellings: a paper on
    "LLM agents" is on topic for "large language models", and the reverse."""
    t = fold(term)
    stem = (ABBREVIATIONS.get(t) or t).removesuffix("s")
    shorts = {s for s, f in ABBREVIATIONS.items() if f.removesuffix("s") == stem}
    return {t, stem, stem + "s", *shorts} if shorts else {t}


@lru_cache(maxsize=200_000)
def _parse_loose_date(text: str | None):
    """Dates arrive as 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD' depending on source.

    Cached with a digit fast path: strptime was the single largest Python cost
    of scoring (tens of thousands of calls per query batch, mostly repeats).
    """
    from datetime import datetime

    text = (text or "").strip()[:10]
    y, m, d = text[0:4], text[5:7], text[8:10]
    if y.isdigit() and (len(text) == 4 or (text[4:5] == "-" and m.isdigit())):
        try:
            return datetime(int(y), int(m) if m else 1, int(d) if d.isdigit() else 1)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _output_signals(
    session: Session, parsed: NLQuery, ids: list[str]
) -> tuple[dict[str, float], dict[str, float], dict[str, object], dict[str, object]]:
    """Work shipped, and when it was last touched — kept separate BY KIND.

    Evidence says someone claims a skill; this says what they built with it.
    Projects and publications are scored on one axis on purpose — stars and
    citations are the same kind of signal, and ranking them apart would mean a
    developer always outranks a researcher for reasons of source, not merit.

    Dates are NOT merged the same way: a project's last-active date and a
    publication's date decay on different timescales (see
    PROJECT_RECENCY_HALF_LIFE_DAYS / PUBLICATION_RECENCY_HALF_LIFE_DAYS), so
    the caller needs to know which kind of date it is looking at, not just
    the single most recent one across both.

    On-topic and off-topic work come back APART rather than summed with the
    off-topic half already discounted. The caller scales this through a log
    that saturates at OUTPUT_SATURATION, and a discount applied before a
    ceiling stops existing above it: 0.25 x anything over 20,000 is still
    past 5,000. That left 97 people — 12.6% of the corpus — scoring a flat
    output of 1.000 for every query whatever it asked about, and put Geoffrey
    Hinton first for "information retrieval" on 195,452 citations of which
    none were on topic. Scaling each half first and discounting the scaled
    one bounds unrelated fame at OFF_TOPIC_WEIGHT of the component, which is
    what that weight has always been documented to do.

    Returns ({person_id: on-topic impact}, {person_id: off-topic impact},
    {person_id: latest project date}, {person_id: latest publication date}).
    """
    from .models import Authorship, Contribution, Project, Publication

    on_project: dict[str, float] = {}
    off_project: dict[str, float] = {}
    on_publication: dict[str, float] = {}
    off_publication: dict[str, float] = {}
    project_latest: dict[str, object] = {}
    publication_latest: dict[str, object] = {}
    terms = _query_terms(parsed)

    def record(pid, value, matched, when, latest: dict):
        # No subject in the query means nothing can be off topic.
        relevant = bool(matched or not terms)
        if latest is publication_latest:
            into = on_publication if relevant else off_publication
        else:
            into = on_project if relevant else off_project
        into[pid] = into.get(pid, 0.0) + max(0.0, value)
        # An off-topic project should not set someone's recency — otherwise a
        # side repo makes a decade-dormant specialism look current.
        if not relevant:
            return
        parsed_when = _parse_loose_date(when)
        if parsed_when and (pid not in latest or parsed_when > latest[pid]):
            latest[pid] = parsed_when

    rows = session.execute(
        select(
            Contribution.person_id, Project.technologies, Project.name,
            Project.activity, Project.last_active_at,
        )
        .join(Project, Project.id == Contribution.project_id)
        .where(Contribution.person_id.in_(ids))
    ).all()
    # Whole words, not substrings: "java" is not on-topic for a JavaScript
    # repo and "rust" is not on-topic for a paper on "trust".
    phrases = [p for p in (words(t) for t in terms) if p]
    # Arranged once, here, rather than per text. A one-word phrase is a set
    # membership test. A longer one has to be looked for in order, but only if
    # its FIRST word is in the text at all, so they are grouped under it.
    single = {p[0] for p in phrases if len(p) == 1}
    multi: dict[str, list] = {}
    for phrase in phrases:
        if len(phrase) > 1:
            multi.setdefault(phrase[0], []).append(phrase)

    def on_topic(texts) -> bool:
        """Does any of TEXTS contain any of the query's phrases?

        This was 70% of the time a query spent, and it was arranged to be. The
        generator it replaces read `for text in texts ... for phrase in
        phrases`, so words(text) was recomputed once per PHRASE rather than
        once per text, and every multi-word phrase was scanned through every
        text whether or not its first word appeared there. Over ten queries on
        a corpus of 767 people that was 605,390 calls to words() and 594,760
        to contains_phrase.

        Tokenise each text once; answer one-word phrases from a set; reach a
        longer phrase only through its first word. 165 ms a query to 51, with
        the ranking eval returning identical numbers.
        """
        for text in texts:
            if not text:
                continue
            haystack = words(str(text))
            present = set(haystack)
            if single and not single.isdisjoint(present):
                return True
            for word in present:
                for phrase in multi.get(word, ()):
                    if contains_phrase(haystack, phrase):
                        return True
        return False

    for pid, techs, name, activity, last_active in rows:
        activity = activity or {}
        try:
            stars = float(activity.get("stars") or 0)
            forks = float(activity.get("forks") or 0)
        except (TypeError, ValueError, AttributeError):
            stars = forks = 0.0
        matched = bool(phrases) and on_topic([*(techs or []), name])
        record(pid, stars + forks * FORK_WEIGHT, matched, last_active, project_latest)

    # A prolific author can have thousands of papers, and loading all of them
    # for 500 candidates was the single largest cost of scoring. The most-cited
    # and the most recent papers carry the signal (output is log-scaled and
    # saturates at OUTPUT_SATURATION; recency wants the newest), so each person
    # contributes at most PUBLICATIONS_PER_PERSON of each.
    by_cites = func.row_number().over(
        partition_by=Authorship.person_id,
        order_by=(desc(func.coalesce(Publication.citations, 0)), Publication.id),
    ).label("by_cites")
    by_date = func.row_number().over(
        partition_by=Authorship.person_id,
        order_by=(desc(func.coalesce(Publication.published_date, "")), Publication.id),
    ).label("by_date")
    ranked = (
        select(
            Authorship.person_id, Publication.topics, Publication.title,
            Publication.citations, Publication.published_date, by_cites, by_date,
        )
        .join(Publication, Publication.id == Authorship.publication_id)
        .where(Authorship.person_id.in_(ids))
        .subquery()
    )
    pubs = session.execute(
        select(ranked.c.person_id, ranked.c.topics, ranked.c.title,
               ranked.c.citations, ranked.c.published_date)
        .where((ranked.c.by_cites <= PUBLICATIONS_PER_PERSON)
               | (ranked.c.by_date <= PUBLICATIONS_PER_PERSON))
    ).all()
    for pid, topics, title, citations, published in pubs:
        matched = bool(phrases) and on_topic([*(topics or []), title])
        record(pid, float(citations or 0), matched, published, publication_latest)

    # A field's citation norms are a property of the person, not of which
    # half their papers landed in, so the same factor scales both.
    factors = _field_citation_factors(
        session, list({*on_publication, *off_publication}))
    on_impact, off_impact = dict(on_project), dict(off_project)
    for cited, into in ((on_publication, on_impact), (off_publication, off_impact)):
        for pid, cites in cited.items():
            into[pid] = into.get(pid, 0.0) + cites * factors.get(pid, 1.0)
    return on_impact, off_impact, project_latest, publication_latest


def _output_component(on_topic: float, off_topic: float) -> float:
    """Work shipped, with unrelated fame bounded rather than discounted.

    Each half is scaled on its own before OFF_TOPIC_WEIGHT applies, so the
    discount outlives saturation. Someone with no on-topic output scores at
    most OFF_TOPIC_WEIGHT here however cited they are elsewhere, and nobody's
    on-topic work is diluted by work they did on something else.
    """
    return min(1.0, _log_scale(on_topic, OUTPUT_SATURATION)
               + OFF_TOPIC_WEIGHT * _log_scale(off_topic, OUTPUT_SATURATION))


# How far a field's citation norms may move someone's output. Damped (square
# root of the ratio) and bounded: at full strength the norms swung a person's
# output sixteen-fold between Medicine and Engineering, enough to reorder
# people who were equally relevant on the evidence — measured, it made eight of
# 69 judged queries slightly worse. Norms should break ties between
# disciplines, not overrule relevance.
FIELD_FACTOR_BOUNDS = (0.5, 2.0)
FIELD_FACTOR_DAMPING = 0.5
# Pseudo-people at the global median added to every field's sample, so a field
# with three researchers in the graph does not set its own norm.
FIELD_BASELINE_PRIOR = 10
_field_baseline_cache: "weakref.WeakKeyDictionary[Any, tuple[float, tuple[dict[str, float], float]]]" = (
    weakref.WeakKeyDictionary())


def _field_citation_factors(session: Session, ids: list[str]) -> dict[str, float]:
    """Per person: how much their citations count, given their field's norms.

    Citation counts are not comparable across fields — a typical biomedical
    researcher is cited many times as often as a typical mathematician — so an
    unnormalized "output" signal ranked people partly by discipline. Each
    person's primary field (the OpenAlex field most of their topics are filed
    under) sets a baseline, the median citation total of that field's people in
    the graph, shrunk toward the global median; citations count by the ratio of
    the global baseline to it. Field-weighted citation impact, in short. People
    with no field on record are left as they are.
    """
    if not ids:
        return {}
    baselines, global_median = _field_baselines(session)
    if not baselines or not global_median:
        return {}
    primary = _primary_fields(session, ids)
    low, high = FIELD_FACTOR_BOUNDS
    out = {}
    for pid, field_name in primary.items():
        base = baselines.get(field_name)
        if base:
            out[pid] = max(low, min(high, (global_median / base) ** FIELD_FACTOR_DAMPING))
    return out


def _primary_fields(session: Session, ids: list[str]) -> dict[str, str]:
    """Each person's most frequent OpenAlex field (not subfield)."""
    from collections import Counter

    counts: dict[str, Counter] = {}
    for start in range(0, len(ids), 900):
        for pid, value in session.execute(
            select(Evidence.person_id, Evidence.value).where(
                Evidence.person_id.in_(ids[start:start + 900]),
                Evidence.attribute_type == "research_field",
                Evidence.extracted_info.like("OpenAlex field of%"),
            )
        ).all():
            counts.setdefault(pid, Counter())[value] += 1
    return {pid: c.most_common(1)[0][0] for pid, c in counts.items()}


def _field_baselines(session: Session) -> tuple[dict[str, float], float]:
    """({field: shrunk median citation total}, global median), cached like the
    vocabulary."""
    import statistics
    import time

    from .models import Authorship, Publication

    key = si._bind_key(session)
    cached = _field_baseline_cache.get(key)
    if cached is not None and (time.monotonic() - cached[0]) < VOCAB_TTL_SECONDS:
        return cached[1]
    totals: dict[str, int] = dict(session.execute(
        select(Authorship.person_id, func.sum(func.coalesce(Publication.citations, 0)))
        .join(Publication, Publication.id == Authorship.publication_id)
        .group_by(Authorship.person_id)
    ).tuples().all())
    result: tuple[dict, float] = ({}, 0.0)
    if totals:
        primary = _primary_fields(session, list(totals))
        global_median = float(statistics.median(totals.values())) or 1.0
        by_field: dict[str, list[float]] = {}
        for pid, field_name in primary.items():
            by_field.setdefault(field_name, []).append(float(totals[pid]))
        baselines = {}
        for field_name, values in by_field.items():
            n = len(values)
            median = float(statistics.median(values))
            k = FIELD_BASELINE_PRIOR
            baselines[field_name] = max(1.0, (n * median + k * global_median) / (n + k))
        result = (baselines, global_median)
    _field_baseline_cache[key] = (time.monotonic(), result)
    return result


def _name_only_intent(parsed: NLQuery) -> bool:
    """Is this query primarily looking for a person by name?"""
    return bool(
        parsed.name_terms
        and not parsed.skill_groups
        and not parsed.organizations
        and not parsed.locations
        and not parsed.countries
        and not parsed.roles
    )


# Named, inspectable tiers for how closely a candidate's name matches the
# query — pulled out of inline literals so the values can be seen, discussed,
# and tuned in one place, the same way WEIGHTS/NAME_ONLY_WEIGHTS already are.
#
# NOT a one-hot categorical score suited to the same logistic-regression
# fitting scripts/fit_weights.py uses for WEIGHTS: NAME_FIT_EXACT through
# NAME_FIT_ANY_WORD are mutually exclusive (a strict priority chain, first
# match wins), but NAME_FIT_ALIAS_FLOOR and NAME_FIT_HANDLE_FLOOR are
# independent "at least this much" floors applied afterward — a person can
# land on the ANY_WORD tier AND separately clear the handle-match floor, and
# the floors are checked and applied in sequence, each only when the score
# so far is still below it (see _name_fit_scores for the exact order this
# matters). Fitting that structure the way WEIGHTS is fit would need either
# a less faithful one-hot approximation or a restructuring of the underlying
# mechanism into a clean additive form — a bigger, separate decision, not
# bundled into this constant-extraction.
NAME_FIT_EXACT = 1.0
NAME_FIT_PREFIX_SUFFIX = 0.95
NAME_FIT_ALL_WORDS = 0.85
NAME_FIT_ANY_WORD = 0.65
NAME_FIT_ALIAS_FLOOR = 0.8
NAME_FIT_HANDLE_FLOOR = 0.7


def _name_fit_scores(
    session: Session, parsed: NLQuery, ids: list[str]
) -> dict[str, float]:
    """How closely each person matches the name the user typed."""
    if not parsed.name_terms or not ids:
        return {}

    # Compared as normalised words, like the rest of search: "Karan P. Singh"
    # typed with or without the dot, or "José" typed as "jose", is the same
    # exact name — raw lowercase strings ranked the exact person below a
    # different spelling of the name.
    def norm(text):
        return " ".join(words(text))

    query_phrase = norm(" ".join(parsed.name_terms))
    terms = [norm(t) for t in parsed.name_terms if norm(t)]
    raw_terms = [t.lower() for t in parsed.name_terms]

    def _name_word_in(name_l: str, term: str) -> bool:
        t = term
        padded = f" {name_l} "
        return (
            name_l == t
            or name_l.startswith(t + " ")
            or name_l.endswith(" " + t)
            or f" {t} " in padded
            or name_l.startswith(t + ", ")
            or f" {t}, " in padded
        )

    rows = session.execute(
        select(Person.id, Person.canonical_name, Person.aliases).where(
            Person.id.in_(ids)
        )
    ).all()
    from .models import SourceRecord

    handles: dict[str, list[str]] = {pid: [] for pid in ids}
    for pid, handle in session.execute(
        select(IdentityLink.person_id, SourceRecord.external_id)
        .join(SourceRecord, SourceRecord.id == IdentityLink.source_record_id)
        .where(IdentityLink.person_id.in_(ids))
    ).all():
        if handle:
            handles.setdefault(pid, []).append(str(handle).lower())

    from .names import fitting_aliases, search_forms

    def same(typed: list[str], stored: list[str]) -> bool:
        # word by word, where a word also matches its nicknames and spellings:
        # "bill gates" is "william gates", but "samuel" is not "samantha"
        return len(typed) == len(stored) and all(
            b in search_forms(a) for a, b in zip(typed, stored, strict=True))

    def contains(stored: list[str], typed: list[str]) -> bool:
        n = len(typed)
        return any(same(typed, stored[i:i + n]) for i in range(len(stored) - n + 1))

    query_words = query_phrase.split()
    term_words = [t.split() for t in terms]
    out: dict[str, float] = {}
    for pid, name, aliases in rows:
        name_l = norm(name)
        name_words = name_l.split()
        n = len(query_words)
        score = 0.0
        if same(query_words, name_words):
            score = NAME_FIT_EXACT
        elif n < len(name_words) and (same(query_words, name_words[:n])
                                      or same(query_words, name_words[-n:])):
            score = NAME_FIT_PREFIX_SUFFIX
        elif term_words and all(contains(name_words, t) for t in term_words):
            score = NAME_FIT_ALL_WORDS
        elif any(contains(name_words, t) for t in term_words):
            score = NAME_FIT_ANY_WORD
        alias_words = [norm(a).split() for a in fitting_aliases(name, aliases)]
        if score < NAME_FIT_ALIAS_FLOOR and any(
                same(t, a) for t in term_words for a in alias_words):
            score = max(score, NAME_FIT_ALIAS_FLOOR)
        if score < NAME_FIT_HANDLE_FLOOR:
            for h in handles.get(pid, []):
                if any(t in h for t in raw_terms):
                    score = max(score, NAME_FIT_HANDLE_FLOOR)
                    break
        out[pid] = score
    return out


def relevance_scores(
    session: Session, parsed: NLQuery, ids: list[str]
) -> dict[str, dict]:
    """Score each candidate, and record why. Returns {person_id: {...}}.

    The breakdown travels with the score on purpose: a ranked list nobody can
    interrogate is exactly the thing this codebase spent its whole design
    avoiding. Every component here traces back to evidence rows.
    """
    from datetime import datetime, timezone

    if not ids:
        return {}

    depth: dict[str, float] = {}
    best_conf: dict[str, float] = {}
    corroborated: dict[str, int] = {}
    latest: dict[str, datetime] = {}

    group_depth: dict[str, list[float]] = {}
    papers_only: set[str] = set()
    if parsed.skill_groups:
        group_depth = _score_evidence(session, parsed, ids, depth, best_conf, corroborated, latest,
                                      papers_only)
    matched, weight = (None, None) if parsed.skill_groups else _matched_evidence_clause(parsed)
    if matched is not None:
        stmt = (
            select(
                Evidence.person_id,
                func.sum(weight),
                # A stated skill sets the confidence ceiling; a bio mention is
                # scaled down so free text cannot present as a strong claim.
                func.max(Evidence.confidence * weight),
                # DISTINCT sources, not corroborated ROWS: WEIGHTS' own
                # comment calls this "independent sources agreeing", but two
                # rows from the same source (e.g. a person re-observed under
                # two SourceRecords from one provider) is not two
                # independent agreements — it is one source, seen twice.
                # COUNT(DISTINCT CASE WHEN ... THEN source END) counts each
                # source once regardless of how many of its rows landed in
                # "corroborated" state, so three GitHub-sourced rows score
                # the same as one — correctly weaker than three DIFFERENT
                # sources agreeing, which is the actual claim this
                # component is supposed to be measuring. Standard SQL,
                # portable across SQLite and Postgres — verified directly
                # before relying on it here.
                func.count(func.distinct(
                    case((Evidence.verification_state == "corroborated", Evidence.source))
                )),
                func.max(Evidence.published_at),
            )
            .where(Evidence.person_id.in_(ids), matched)
            .group_by(Evidence.person_id)
        )
        for pid, n, conf, corr, pub in session.execute(stmt).all():
            depth[pid] = float(n or 0.0)
            best_conf[pid] = float(conf or 0.0)
            corroborated[pid] = int(corr or 0)
            if pub:
                latest[pid] = pub

    # How many independent sources know this person at all. Someone confirmed
    # across GitHub, ORCID and dblp is a more solid record than a lone profile,
    # regardless of what was asked for.
    breadth: dict[str, int] = {}
    for pid, n in session.execute(
        select(IdentityLink.person_id, func.count(IdentityLink.id))
        .where(
            IdentityLink.person_id.in_(ids),
            IdentityLink.review_state != "split",
        )
        .group_by(IdentityLink.person_id)
    ).all():
        breadth[pid] = n or 0

    on_impact, off_impact, project_latest, publication_latest = _output_signals(
        session, parsed, ids)

    name_only = _name_only_intent(parsed)
    name_fit = _name_fit_scores(session, parsed, ids) if name_only else {}
    weights = NAME_ONLY_WEIGHTS if name_only else WEIGHTS

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    out: dict[str, dict] = {}
    concept_weights = _concept_weights(session, parsed) if len(parsed.skill_groups) > 1 else None
    for pid in ids:
        parts = {
            "depth": _depth_component(group_depth.get(pid), depth.get(pid, 0.0), concept_weights),
            "output": _output_component(on_impact.get(pid, 0.0),
                                        off_impact.get(pid, 0.0)),
            "confidence": best_conf.get(pid, 0.0),
            "corroboration": _log_scale(corroborated.get(pid, 0), 3.0),
            "breadth": _log_scale(breadth.get(pid, 0), BREADTH_SATURATION),
        }
        if name_only:
            parts["name_fit"] = name_fit.get(pid, 0.0)
        # The freshest signal we have, judged on ITS OWN timescale: a
        # dormant repository and an old paper do not say the same thing (see
        # the PROJECT_/PUBLICATION_/EVIDENCE_RECENCY_HALF_LIFE_DAYS
        # comments), so each dated source is decayed at its own rate FIRST,
        # then the best resulting score wins — not the single most recent
        # raw date decayed at one blanket rate, which either overstated how
        # stale an old paper is or understated how stale an old repo is.
        # Undated scores a neutral 0.5 — most sources never state a date,
        # and treating "unknown" as "ancient" would rank the whole GitHub
        # corpus below anyone with one dated paper.
        recency_candidates = [
            s for s in (
                _recency_score(now, latest.get(pid), EVIDENCE_RECENCY_HALF_LIFE_DAYS),
                _recency_score(now, project_latest.get(pid), PROJECT_RECENCY_HALF_LIFE_DAYS),
                _recency_score(now, publication_latest.get(pid), PUBLICATION_RECENCY_HALF_LIFE_DAYS),
            )
            if s is not None
        ]
        parts["recency"] = max(recency_candidates) if recency_candidates else 0.5
        score = sum(weights.get(k, WEIGHTS.get(k, 0.0)) * v for k, v in parts.items())
        out[pid] = {
            "score": round(score, 4),
            "components": {k: round(v, 3) for k, v in parts.items()},
            "matched_evidence": round(depth.get(pid, 0.0), 2),
            "sources": breadth.get(pid, 0),
            # Reported apart for the same reason they are scored apart: a
            # breakdown that summed them could not show WHY someone very
            # cited scored low on output for this particular query.
            "impact": round(on_impact.get(pid, 0.0), 1),
            "impact_off_topic": round(off_impact.get(pid, 0.0), 1),
        }
        if pid in papers_only:
            # matched a concept only through their papers: ranked after
            # everyone who states it, and the reason travels with the score
            out[pid]["papers_only"] = True
    return out


def _fill_clause_order(tokens: list[str], result: NLQuery) -> None:
    """Record constraints left-to-right as the user typed them."""
    order: list[dict[str, Any]] = []
    used: set[tuple] = set()
    i = 0
    low = [t.lower() for t in tokens]
    while i < len(low):
        hit: dict[str, Any] | None = None
        width = 1
        for n in range(min(3, len(low) - i), 0, -1):
            gram = " ".join(low[i : i + n])
            raw = " ".join(tokens[i : i + n])
            for name in result.name_terms:
                if name.lower() == gram and ("name", name.lower()) not in used:
                    hit = {"kind": "name_terms", "payload": name, "token": raw, "label": "name"}
                    used.add(("name", name.lower()))
                    width = n
                    break
            if hit:
                break
            for loc in result.locations:
                loc_l = loc.lower()
                loc_parts = [p.strip() for p in loc_l.split(",") if p.strip()]
                loc_first = loc_parts[0] if loc_parts else loc_l
                place_hit = gram in PLACES and PLACES[gram].lower() in loc_parts + [loc_l]
                if loc_l == gram or gram in loc_parts or loc_first == gram or place_hit:
                    if ("loc", loc_l) not in used:
                        hit = {"kind": "locations", "payload": loc, "token": raw, "label": "location"}
                        used.add(("loc", loc_l))
                        width = n
                    break
            if hit:
                break
            for g in result.skill_groups:
                term = (g.get("term") or g.get("pattern") or "").lower()
                if term == gram and ("skill", term) not in used:
                    hit = {"kind": "skill_groups", "payload": g, "token": raw, "label": "skill"}
                    used.add(("skill", term))
                    width = n
                    break
            if hit:
                break
            for entry in result.org_terms:
                term = entry["term"].lower()
                if term == gram and ("org", term) not in used:
                    hit = {"kind": "organizations", "payload": entry["orgs"], "token": raw,
                           "label": "org"}
                    used.add(("org", term))
                    width = n
                    break
            if hit:
                break
            for role in result.roles:
                if role.lower() == gram and ("role", role.lower()) not in used:
                    hit = {"kind": "roles", "payload": role, "token": raw, "label": "role"}
                    used.add(("role", role.lower()))
                    width = n
                    break
            if hit:
                break
            code = COUNTRIES.get(gram) or DEMONYMS.get(gram)
            if code and code in result.countries and ("country", code) not in used:
                hit = {"kind": "countries", "payload": code, "token": raw, "label": "country"}
                used.add(("country", code))
                width = n
        if hit:
            hit["at"] = i
            order.append(hit)
            i += width
        else:
            i += 1
    # A constraint no typed words map back to must still be a clause, or
    # relaxing the query silently drops it: "Indian" is a country filter, and
    # "Founders of Indian AI startups" lost it the moment a clause was removed.
    end = len(tokens)
    for code in result.countries:
        if ("country", code) not in used:
            order.append({"kind": "countries", "payload": code, "token": code,
                          "label": "country", "at": end})
    for loc in result.locations:
        if ("loc", loc.lower()) not in used:
            order.append({"kind": "locations", "payload": loc, "token": loc,
                          "label": "location", "at": end})
    for g in result.skill_groups:
        term = (g.get("term") or g.get("pattern") or "").lower()
        if ("skill", term) not in used:
            order.append({"kind": "skill_groups", "payload": g, "token": g.get("term") or term,
                          "label": "skill", "at": end})
    for entry in result.org_terms:
        if ("org", entry["term"].lower()) not in used:
            order.append({"kind": "organizations", "payload": entry["orgs"],
                          "token": entry["term"], "label": "org", "at": end})
    for name in result.name_terms:
        if ("name", name.lower()) not in used:
            order.append({"kind": "name_terms", "payload": name, "token": name,
                          "label": "name", "at": end})
    for role in result.roles:
        if ("role", role.lower()) not in used:
            order.append({"kind": "roles", "payload": role, "token": role,
                          "label": "role", "at": end})
    result.clause_order = sorted(order, key=lambda c: c["at"])


def _parsed_from_clauses(base: NLQuery, clauses: list) -> NLQuery:
    from dataclasses import replace

    p = replace(
        base,
        skill_groups=[], skills=[], skill_patterns=[],
        name_terms=[], locations=[], countries=[], organizations=[], roles=[],
        org_terms=[], clause_order=list(clauses),
    )
    for c in clauses:
        kind, payload = c["kind"], c["payload"]
        if kind == "skill_groups":
            p.skill_groups.append(payload)
        elif kind == "name_terms":
            p.name_terms.append(payload)
        elif kind == "locations":
            p.locations.append(payload)
        elif kind == "countries":
            p.countries.append(payload)
        elif kind == "organizations":
            names = payload if isinstance(payload, list) else [payload]
            p.organizations.extend(names)
            p.org_terms.append({"term": c.get("token") or names[0], "orgs": list(names)})
        elif kind == "roles":
            p.roles.append(payload)
    p.skills = list(dict.fromkeys(
        v for g in p.skill_groups for v in g.get("values", [])))
    p.skill_patterns = list(dict.fromkeys(
        g["pattern"] for g in p.skill_groups if g.get("pattern")))
    p.locations = list(dict.fromkeys(p.locations))
    p.name_terms = list(dict.fromkeys(p.name_terms))
    p.organizations = list(dict.fromkeys(p.organizations))
    p.countries = list(dict.fromkeys(p.countries))
    p.roles = list(dict.fromkeys(p.roles))
    return p


# Which constraints give way first when a query matches too few people. The
# subject asked about is the question; where someone is and who employs them
# are the negotiable parts of it. Among clauses of one kind, the last typed
# goes first. Names go last: a name search relaxed into a topic search answers
# something else entirely.
RELAX_ORDER = ("countries", "locations", "organizations", "roles", "skill_groups", "name_terms")
# Below this many full matches, the page is topped up with partial ones.
PARTIAL_FILL_BELOW = 10


def _half_a_name(parsed: NLQuery) -> list[dict]:
    """The name words nobody here is called, when the query is a name.

    "Katherine Jones" found Kate Tilling: Katherine matched by nickname and
    Jones matched nobody, so half a name answered a whole one and the row read
    as a full match. The people are still worth showing — a surname the corpus
    has never seen is exactly what live search is for — but each one has to say
    which part of the name it does not carry.

    A name-shaped word after "at" is an employer, not a name, and is labelled
    as one: "Dhruv Dixit at Zzyzx" is missing the employer.
    """
    if not parsed.name_terms:
        return []
    tokens = [t.lower().rstrip(".'’-") for t in _QUERY_TOKEN.findall(parsed.raw or "")]
    out = []
    for term in parsed.unmatched_terms:
        if not _could_be_a_name(term):
            continue
        first = term.split()[0].lower()
        index = tokens.index(first) if first in tokens else -1
        employer = index > 0 and tokens[index - 1] in ("at", "from", "@")
        out.append({"term": term, "as": "org" if employer else "name"})
    return out


def _modifies_the_next_subject(clause: dict, clauses: list) -> bool:
    """Is this subject the first half of a two-word subject, as typed?

    "computational pathology" is not held as one topic, so it parses as two
    subjects side by side. Nobody is filed under both, and dropping the last
    one typed kept "computational" and threw away pathology — computational
    mechanics answering a question about pathology. The head of an English
    noun phrase is its last word, so the modifier gives way instead.
    """
    if clause["kind"] != "skill_groups":
        return False
    after = clause.get("at", 0) + len((clause.get("token") or "").split())
    return any(other is not clause and other["kind"] == "skill_groups"
               and other.get("at") == after for other in clauses)


def _drop_sequence(clauses: list) -> list:
    """Clauses in the order they are dropped."""
    rank = {kind: i for i, kind in enumerate(RELAX_ORDER)}

    def order(clause):
        at = clause.get("at", 0)
        # modifiers first, left to right; everything else last typed first
        if _modifies_the_next_subject(clause, clauses):
            return (rank.get(clause["kind"], 0), 0, at)
        return (rank.get(clause["kind"], 0), 1, -at)

    return sorted(clauses, key=order)


def execute_progressive(session: Session, parsed: NLQuery) -> tuple[list, NLQuery, list]:
    """AND every constraint; when that finds too few, relax — and say so.

    Two things changed from dropping clauses right to left when nothing
    matched at all:

    - **What gives way.** A place or an employer is dropped before the
      subject, whatever order they were typed in. "deep learning researchers
      at Oxford" with nobody at Oxford returns deep learning researchers, not
      everyone at Oxford.
    - **When.** A page with only a couple of full matches is topped up with
      partial ones behind them. "drug discovery researchers in India" found
      two people and stopped, with six more drug discovery researchers stored.

    Every partial row carries `partial_match = {"missing": [...]}`, naming the
    constraints it does not meet; full matches never move behind partial ones.
    The common case — enough full matches — costs exactly one execute().
    """
    try:
        clauses = list(parsed.clause_order or [])
        rows = execute(session, parsed)
        for person in rows:
            # instances are shared within a session: a flag from an earlier
            # query must not survive into this one
            person.__dict__.pop("partial_match", None)
        # half a name is not a whole one, however the rest of the page fills
        half = _half_a_name(parsed)
        if half:
            for person in rows:
                # a transient flag on the instance, read back by api.py
                person.partial_match = {"missing": half}  # type: ignore[attr-defined]
        if not clauses or len(clauses) < 2 and rows:
            return rows, parsed, []
        wanted = parsed.limit
        # Paging never relaxes. A page past the last one is empty because the
        # answer ran out, not because the question was too narrow: page two of
        # eight Google DeepMind people dropped the employer, called it "not
        # found", and offered to go live for it.
        if parsed.offset:
            return rows, parsed, []
        if rows and len(rows) >= min(PARTIAL_FILL_BELOW, wanted):
            return rows, parsed, []

        # Relaxing never loosens a name. "Dhruv Dixit" found Dhruv Dixit, and
        # filling the page with every other Dhruv is noise; and when nobody
        # here has the whole name, people with half of it are not an answer
        # either -- "sundar pichai" listed every Sundar. Nobody is the honest
        # answer, and the one live search exists for.
        sequence = [c for c in _drop_sequence(clauses) if c["kind"] != "name_terms"]
        for n_dropped in range(1, len(sequence) + 1):
            dropped_clauses = sequence[:n_dropped]
            kept = [c for c in clauses if c not in dropped_clauses]
            if not kept:
                break
            trial = _parsed_from_clauses(parsed, kept)
            if not has_filters(trial) or count_matches(session, trial) <= len(rows):
                continue
            missing = [{"term": c["token"], "as": c["label"]} for c in dropped_clauses]
            if rows:
                have = {p.id for p in rows}
                trial.offset = 0
                trial.limit = wanted - len(rows) + len(have)
                extra = [p for p in execute(session, trial) if p.id not in have]
                for person in extra:
                    person.partial_match = {"missing": missing}  # type: ignore[attr-defined]
                return rows + extra[: wanted - len(rows)], parsed, []
            relaxed = execute(session, trial)
            for person in relaxed:
                person.partial_match = {"missing": missing}  # type: ignore[attr-defined]
            return relaxed, trial, missing
        if rows:
            return rows, parsed, []
        return [], parsed, [{"term": c["token"], "as": c["label"]} for c in clauses]
    except Exception:
        logger.exception("progressive search failed; returning no rows")
        return [], parsed, []


def execute(session: Session, parsed: NLQuery) -> list[Person]:
    """Filter, then rank: strongest evidence for what was asked comes first.

    Filtering decides who is eligible; ranking decides who leads. Returning
    filter matches in insertion order made person #4 indistinguishable from
    person #400, and truncating that at LIMIT discarded good candidates before
    anyone saw them.

    If NOTHING in the query matched the corpus vocabulary, return nothing.
    An unfiltered SELECT would hand back arbitrary people that look like
    answers to a question we could not actually answer.

    Each returned Person carries `relevance` (score + component breakdown) as
    a plain attribute — it is a property of this query, not of the person, so
    it is deliberately not persisted.
    """
    if not has_filters(parsed):
        return []
    pool_size = max(CANDIDATE_POOL, parsed.offset + parsed.limit)
    if si.is_ready(session):
        ids = si.candidates(session, _constraints(parsed), pool_size, _topical_alts(parsed),
                            _restrict(parsed))
        return _rank_and_page(session, parsed, ids)
    stmt = _filtered_stmt(parsed, session)
    # de-duplicate on id, not whole rows: Postgres cannot DISTINCT a JSON column
    # Deterministic, quality-biased ordering BEFORE the pool is capped.
    # Without an ORDER BY here, which rows survive `LIMIT pool_size` is
    # whatever the engine's scan happens to produce — not guaranteed stable
    # across identical requests, and when total matches exceed pool_size,
    # arbitrary rather than the strongest candidates. Evidence-row count is a
    # cheap, already-indexed (Evidence.person_id) proxy for "richer profile";
    # it is only a pre-cut, not the final order — relevance_scores() below
    # still ranks precisely.
    #
    # A scalar correlated subquery, not a direct JOIN to Evidence: `stmt` may
    # already join Affiliation + Organization (for an organization filter),
    # and a second direct join onto the same statement would fan out — two
    # independent one-to-many joins on the same parent multiply rows, so
    # COUNT(Evidence.id) would count each evidence row once per matching
    # affiliation instead of once. A correlated subquery is evaluated
    # independently per outer row and is immune to that, regardless of
    # whatever else `stmt` joins. (An earlier version of this fix tried a
    # two-step derived-table JOIN instead, on the theory that SQLite would
    # run it as a single aggregate pass rather than a per-row subquery;
    # benchmarked against a 10k-person corpus it measured slower, not
    # faster, so it was reverted — worth knowing this was checked, not
    # assumed.) `GROUP BY` rather than `DISTINCT` because Postgres rejects
    # an ORDER BY expression that isn't in the SELECT list when DISTINCT is
    # used, which this scalar subquery would violate; GROUP BY has no such
    # restriction and dedupes the same join-fanout rows just as well.
    evidence_richness = (
        select(func.count(Evidence.id))
        .where(Evidence.person_id == Person.id)
        .correlate(Person)
        .scalar_subquery()
    )
    pool = session.execute(
        stmt.with_only_columns(Person.id)
        .group_by(Person.id)
        .order_by(desc(evidence_richness), Person.id)
        .limit(pool_size)
    ).scalars().all()
    return _rank_and_page(session, parsed, list(pool))


def _rank_and_page(session: Session, parsed: NLQuery, ids: list[str]) -> list[Person]:
    """Score the candidate pool, order it, and load one page of people."""
    if not ids:
        return []

    scored = relevance_scores(session, parsed, list(ids))
    # Ties keep their filter order, so equal-evidence results stay stable
    # across requests instead of shuffling between pages.
    order = {pid: i for i, pid in enumerate(ids)}
    # People who match only through their papers come after everyone who
    # states the subject, whatever their citations (see _score_papers_only).
    ranked = sorted(ids, key=lambda p: (scored.get(p, {}).get("papers_only", False),
                                        -scored.get(p, {}).get("score", 0.0), order[p]))
    page = ranked[parsed.offset : parsed.offset + parsed.limit]
    if not page:
        return []

    rows = {p.id: p for p in session.execute(
        select(Person).where(Person.id.in_(page))).scalars()}
    out = []
    for pid in page:
        person = rows.get(pid)
        if person is None:
            continue
        # transient, like partial_match: the score behind this page position
        person.relevance = scored.get(pid, {"score": 0.0, "components": {}})  # type: ignore[attr-defined]
        out.append(person)
    return out


# Scholarly indexes carry entity records that are not people: conferences,
# labs, societies, even "Computer Vision Syndrome". Ingesting them as persons
# pollutes the graph, so they are rejected before any fetch.
NOT_A_PERSON = re.compile(
    r"\b(foundation|conference|workshop|symposium|proceedings|society|institute"
    r"|laborator(y|ies)|university|college|committee|association|consortium"
    r"|group|centre|center|department|journal|press|syndrome|corporation|inc"
    r"|ltd|llc|gmbh|team|proj(ect)?|proceedings"
    # degree and title prefixes, and bare discipline names: the scholarly
    # indexes carry "PhD Computer Vision Mariano Cabezas" and "Computer
    # Engineering" as author records
    r"|phd|ph\.d|prof|professor|coordinator|engineering|sciences?|studies"
    r"|editor|editorial|anonymous|unknown|staff|admin"
    r"|community|collective|network|alliance|federation|council|forum|club"
    r"|hub|labs?|studios?|systems|tech|technologies|solutions|official|bot"
    r"|software|digital|media|ventures|partners|holdings|pvt|private|limited)\b",
    re.IGNORECASE,
)


def _looks_like_a_person(name: str | None) -> TypeGuard[str]:
    """Filter index entities out of author search results."""
    if not name or len(name) > 60:
        return False
    if NOT_A_PERSON.search(name):
        return False
    # "Deep Learning Türkiye" is a community, not someone surnamed Türkiye:
    # a topic phrase or entity word inside a name is the same signal ingest
    # refuses on (rip/personhood.py). Checked here too, so a record stored
    # before that gate existed cannot turn "deep learning" into a name filter.
    from .personhood import has_entity_signal

    if has_entity_signal(name):
        return False
    # A person's name is not a sentence. Initials are cheap ("André C. P. L.
    # F. de Carvalho"), so count only the words that are not single letters.
    name_words = [w for w in name.split() if len(w.strip(".")) > 1]
    return 1 <= len(name_words) <= 5
