"""Natural-language query parsing for internal search.

Turns "distributed systems researchers at Google in London, top 10" into
structured filters. The parser still extracts constraints only — scoring is
applied after filtering, so unmatched terms stay unmatched rather than being
silently "understood" as weights.

The vocabulary is built from the live corpus (skill/interest values, org
names, locations) rather than hardcoded gazetteers, so it grows with the data.
"""

import logging
import os
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz
from sqlalchemy import func, literal, or_, select
from sqlalchemy.orm import Session

from .models import Evidence, Organization, Person

logger = logging.getLogger("rip.nlq")

# Words that describe what the searcher wants rather than what a person does.
# Without these, "strong in computer vision" matches the physics topic "Strong
# Light-Matter Interactions", and "at top conferences" matches "Conferences and
# Exhibitions Management". They are query grammar, not domain vocabulary.
STOPWORDS = {
    # articles, prepositions, conjunctions
    "a", "an", "and", "or", "the", "of", "in", "at", "on", "to", "from", "for",
    "with", "who", "whom", "whose", "that", "which", "both", "also", "than",
    "then", "into", "across", "about", "over", "under", "between", "not",
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
    # career/evidence talk
    "experience", "experienced", "expertise", "background", "worked", "works",
    "working", "work", "built", "build", "building", "contributed",
    "contributing", "contributions", "published", "publishing", "publication",
    "publications", "papers", "paper", "authored", "spoken", "speaking",
    "joined", "later", "previously", "currently", "current", "former",
    "years", "year", "public", "publicly", "presence", "evidence", "online",
    "portfolio", "profile", "show", "showing", "track", "record",
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
    "firm", "firms", "company", "companies", "startup", "startups",
    "organisation", "organisations", "organization", "organizations",
    "business", "businesses", "agency", "agencies", "vendor", "vendors",
    "client", "clients", "customer", "customers", "industry", "industries",
    "platforms", "application", "applications", "apps", "app", "solutions",
    "services", "service", "technology", "technologies", "tech", "software",
    "systems" if False else "__unused__",
}
STOPWORDS.discard("__unused__")

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
SKILL_ATTRS = ("skill", "research_interest", "specialization")
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
# A word occurring in more than this share of vocabulary values is too generic
# to match on: "systems" appears in 147 topics, "robotics" in 7.
GENERIC_DF_RATIO = 0.01
GENERIC_DF_ABSOLUTE = 25
FUZZY_VOCAB_THRESHOLD = 90.0
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


def _text_evidence_exists(session: Session, term: str) -> bool:
    """Does any bio or job title mention this term?"""
    return session.execute(
        select(Evidence.id).where(
            Evidence.attribute_type.in_(("bio", "role")),
            func.lower(Evidence.value).like(f"%{term}%"),
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
    "hadoop", "spark", "kafka", "airflow", "pytorch", "huggingface",
    "langchain", "openai", "node", "nodejs", "nextjs", "graphql", "html",
    "css", "sass", "bitcoin", "ethereum", "solidity", "cuda", "llvm",
    "c++", "cpp", "c#", "csharp",
}


def _token_frequency(skills: dict) -> dict:
    """How many vocabulary values contain each word — a genericness measure."""
    from collections import Counter

    df: Counter = Counter()
    for key in skills:
        for word in set(re.findall(r"[a-z0-9]+", key)):
            df[word] += 1
    return df


def _is_generic(term: str, df: dict, vocab_size: int) -> bool:
    """Would matching this term sweep in unrelated topics?"""
    words = term.split()
    if len(words) > 1:
        return False  # a phrase is specific enough to match on
    n = df.get(term, 0)
    return n > max(GENERIC_DF_ABSOLUTE, vocab_size * GENERIC_DF_RATIO)


def _vocab(session: Session) -> tuple[dict, dict, dict]:
    """(skills, orgs, locations) lowercase -> canonical value, from live data."""
    skills = {
        v.lower(): v
        for (v,) in session.execute(
            select(Evidence.value).where(Evidence.attribute_type.in_(SKILL_ATTRS)).distinct()
        )
    }
    orgs = {
        v.lower(): v
        for (v,) in session.execute(select(Organization.name).distinct())
    }
    # "deccan.ai" typed by a user must reach the record spelled "Deccan AI"
    from .models import normalize_org_name

    for (v,) in session.execute(select(Organization.name).distinct()):
        orgs.setdefault(normalize_org_name(v), v)
    locations: dict[str, str] = {}
    for (loc,) in session.execute(
        select(Person.location).where(Person.location.isnot(None)).distinct()
    ):
        locations[loc.lower()] = loc
        # each comma part is matchable ("Berlin" from "Berlin, Germany")
        for part in loc.split(","):
            part = part.strip()
            if len(part) > 2:
                locations.setdefault(part.lower(), loc)
    return skills, orgs, locations


def _ngrams(tokens: list[str], max_n: int = 3):
    """Longest-first n-grams with their token spans."""
    for n in range(min(max_n, len(tokens)), 0, -1):
        for i in range(len(tokens) - n + 1):
            yield " ".join(tokens[i : i + n]), set(range(i, i + n))


def parse(session: Session, query: str | None) -> NLQuery:
    query = query or ""
    result = NLQuery(raw=query)

    # limit requires an explicit prefix — a bare number is never a limit
    limit_match = re.search(r"\b(?:top|first|show|list)\s+(\d{1,3})\b", query, re.I)
    if limit_match:
        result.limit = max(1, min(MAX_LIMIT, int(limit_match.group(1))))
    cleaned = re.sub(r"\b(?:top|first|show|list)\s+\d{1,3}\b", " ", query, flags=re.I)

    skills, orgs, locations = _vocab(session)
    df = _token_frequency(skills)
    vocab_size = max(1, len(skills))
    tokens = re.findall(r"[a-zA-Z0-9+#.'-]+", cleaned)[:40]
    consumed: set[int] = set()

    for gram, span in _ngrams(tokens):
        if span & consumed:
            continue
        gram_l = gram.lower()
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
        if gram_l in TECH_SKILLS or gram_l in skills:
            group = {"term": gram}
            if gram_l in skills:
                group["values"] = [skills[gram_l]]
            else:
                # not in the corpus yet, but it is a skill — match bios/topics
                # by pattern rather than inventing a name filter
                group["pattern"] = gram_l
            result.skill_groups.append(group)
            consumed |= span
        elif gram_l in orgs:
            result.organizations.append(orgs[gram_l])
            consumed |= span
        elif gram_l in COUNTRIES:
            result.countries.append(COUNTRIES[gram_l])
            consumed |= span
        elif gram_l in DEMONYMS:
            # "Indian researchers" is a place, not the topic "Indian History"
            result.countries.append(DEMONYMS[gram_l])
            consumed |= span
        elif gram_l in PLACES:
            # Gazetteer wins over a city that happens to contain the word
            # ("california" must not become "San Francisco, California").
            result.locations.append(PLACES[gram_l])
            consumed |= span
        elif gram_l in locations:
            result.locations.append(locations[gram_l])
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
            result.skill_groups.append({"term": gram, "values": [skills[ACRONYMS[gram_l]]]})
            consumed |= span
        elif gram_l in ACRONYMS:
            expansion = ACRONYMS[gram_l]
            pattern = re.compile(rf"\b{re.escape(expansion)}\b")
            contained = [v for k, v in skills.items() if pattern.search(k)]
            if contained:
                result.skill_groups.append({"term": gram, "values": contained})
                consumed |= span
        elif len(gram_l) >= 5 and not _is_generic(gram_l, df, vocab_size):
            # Word-boundary containment: "computer vision" should reach
            # "Computer Vision and Image Processing". Generic words are
            # excluded above, or "building" would match building materials.
            pattern = re.compile(rf"\b{re.escape(gram_l)}\b")
            contained = [v for k, v in skills.items() if pattern.search(k)]
            group = {"term": gram}
            if not contained:
                # A near-miss for a real vocabulary entry is a typo, not a
                # free-text hit: "bangalor" appears inside bios that say
                # Bangalore, which made the city look like a skill.
                if _near_vocabulary(gram_l, skills, orgs, locations):
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
                # not be swallowed as an unknown topic
                if any(_name_exists(session, t) for t in parts):
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
                # only a two-word phrase blocks its words. A longer phrase that
                # missed must let its sub-phrases try: "content delivery
                # networks" is unknown, but "content delivery" is a real topic.
                if len(parts) == 2 and not exact:
                    # A phrase the corpus does not know must not be split into
                    # its words: "computer vision" has no matching topic, and
                    # the bare word "computer" reaches EEG and Brain-Computer
                    # Interfaces. Consume the span so the parts cannot match by
                    # mere containment. Words that are exact vocabulary in their
                    # own right ("Python" in "Python developers") are spared.
                    consumed |= span
                    # still tell the caller the phrase was dropped
                    result.unmatched_terms.append(gram)
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
    for i, token in enumerate(tokens):
        if i in consumed or token.lower() in STOPWORDS or len(token) < 4:
            continue
        t = token.lower()
        best_kind, best_value, best_score = None, None, 0.0
        for kind, vocab in (("skill", skills), ("org", orgs), ("location", locations)):
            hit = max(vocab.items(), key=lambda kv: _typo_score(t, kv[0]), default=None)
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
            else:
                result.locations.append(best_value)
            result.corrections.append({"typed": token, "matched": str(best_value)})
            consumed.add(i)
            continue

        # A misspelled name reaches nothing at all otherwise, because name
        # matching is exact: "hintonn" simply disappears.
        if _could_be_a_name(token):
            near = _nearest_name(session, t)
            if near:
                result.name_terms.append(near)
                result.corrections.append({"typed": token, "matched": near})
                consumed.add(i)

    # Leftovers. A capitalised word is NOT automatically a person's name:
    # treating "Hyderabad" as one silently guarantees zero results. Only apply
    # a name filter when somebody in the corpus actually has that name;
    # otherwise report the term as unapplied and let the caller see why.
    for i, token in enumerate(tokens):
        if i in consumed or token.lower() in STOPWORDS:
            continue
        # A word the topic vocabulary uses is a subject, not a surname:
        # "data" matched entity records like "G. DATA CyberDefense AG".
        if df.get(token.lower(), 0) > 0:
            result.unmatched_terms.append(token)
            continue
        if token.lower() in PLACES:
            result.locations.append(PLACES[token.lower()])
            consumed.add(i)
            continue
        # not gated on capitalisation: people type "sricharan", not "Sricharan"
        if _could_be_a_name(token) and _name_exists(session, token):
            result.name_terms.append(token)
        else:
            result.unmatched_terms.append(token)

    # flat views, kept so the API response and existing callers stay simple
    result.skills = list(dict.fromkeys(
        v for g in result.skill_groups for v in g.get("values", [])))
    result.skill_patterns = list(dict.fromkeys(
        g["pattern"] for g in result.skill_groups if g.get("pattern")))
    result.organizations = list(dict.fromkeys(result.organizations))
    result.locations = list(dict.fromkeys(result.locations))
    result.countries = list(dict.fromkeys(result.countries))
    _fill_clause_order(tokens, result)
    return result


# Characters that end a word in the values we store: "Go, Python", "C/C++",
# "Machine Learning (Applied)", "Bangalore - Karnataka".
_WORD_EDGES = ",;/|()[]{}\"'-\u2013\u2014:.\t\n"


def _word_match(column, value: str):
    """Match VALUE as a whole word or phrase inside COLUMN.

    A plain substring filter is close to useless at this scale: skill=r
    matched 49,239 of 50,733 people because nearly every skill contains the
    letter r, and skill=go reached Hinton through "Cognitive".

    Every separator is rewritten to a space and both sides are padded, so one
    LIKE can ask for " go " and mean the word. A trailing * asks for the loose
    behaviour back: skill=go* still matches "Golang". LIKE rather than a regex
    so the same clause runs on SQLite and Postgres.
    """
    v = " ".join((value or "").strip().lower().split())
    if not v:
        return column.isnot(None)
    if v.endswith("*"):
        stem = v[:-1].strip()
        if not stem:
            return column.isnot(None)
        return func.lower(column).like(f"%{_like_escape(stem)}%", escape="\\")

    normalized = func.lower(column)
    for sep in _WORD_EDGES:
        normalized = func.replace(normalized, sep, " ")
    padded = literal(" ").concat(normalized).concat(literal(" "))
    return padded.like(f"% {_like_escape(v)} %", escape="\\")


def _like_escape(value: str) -> str:
    """Keep user text from becoming a LIKE wildcard (% or _)."""
    return (value or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


LOCATION_TEXT_ATTRS = ("bio", "role", "location", "education")


def location_needles(loc: str) -> list[str]:
    """Spellings that mean the same place as LOC."""
    raw = (loc or "").split(",")[0].strip().lower()
    if not raw:
        return []
    return list(dict.fromkeys(PLACE_SYNONYMS.get(raw, (raw,))))


def location_anywhere(loc: str):
    """Match a place on the person record, in a bio, or on an affiliation.

    GitHub often leaves `location` blank and writes the city into the bio
    ("NMIT, Bangalore"). Restricting the filter to Person.location then
    reports the city as not found for people who clearly have it.
    """
    from sqlalchemy import and_, exists as sa_exists

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
    if not text:
        return None
    low = text.lower()
    for sep in _WORD_EDGES:
        low = low.replace(sep, " ")
    padded = f" {low} "
    for gram in sorted(PLACES, key=len, reverse=True):
        if f" {gram} " in padded:
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
        func.lower(column).like(f"{t} %"),
        func.lower(column).like(f"% {t}"),
        func.lower(column).like(f"% {t} %"),
        func.lower(column).like(f"{t}, %"),
        func.lower(column).like(f"% {t}, %"),
        # Rahul's | Portfolio Website — apostrophe is a word edge
        func.lower(column).like(f"{t}'%"),
        func.lower(column).like(f"% {t}'%"),
    )


def _name_like_query(query: str) -> bool:
    """Is this worth asking an author-NAME index about?

    dblp and Semantic Scholar match names and nothing else, so a technology
    or a whole question produces surname collisions rather than answers.
    """
    words = query.split()
    return 1 <= len(words) <= 3 and all(_could_be_a_name(w) for w in words)


_PLURAL_ROLE = re.compile(r"(s|es)$")


def _role_exists(session: Session, title: str) -> bool:
    """Does anyone in the corpus actually hold this job title?"""
    from .models import Affiliation

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


def _near_vocabulary(term: str, skills: dict, orgs: dict, locations: dict) -> bool:
    """Is this term a near-miss for something the corpus actually knows?"""
    for vocab in (skills, orgs, locations):
        hit = max(vocab, key=lambda k: _typo_score(term, k), default=None)
        if hit and _typo_score(term, hit) >= FUZZY_VOCAB_THRESHOLD:
            return True
    return False


def _nearest_name(session: Session, token: str) -> str | None:
    """The closest real surname to a typo, or None if nothing is close.

    Blocked on the first three letters so this is a small comparison rather
    than a scan of every name in the graph.
    """
    from .models import PersonNameToken

    if len(token) < 4:
        return None
    candidates = session.execute(
        select(PersonNameToken.token)
        .where(PersonNameToken.token.like(f"{token[:3]}%"))
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
    words = [w.strip(" .,()") for w in re.split(r"[,/|]| - ", text.lower())]
    for part in reversed(words):          # the country is usually written last
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
    if len(t) < 3 or t in ACRONYMS or t in STOPWORDS or t in TECH_SKILLS or t in PLACES:
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
    rows = session.execute(
        select(Person.canonical_name).where(
            func.lower(Person.canonical_name).like(f"%{phrase}%"),
            Person.merged_into.is_(None),
        ).limit(20)
    ).scalars().all()
    phrase_words = phrase.lower().split()
    return any(_looks_like_a_person(n) and _phrase_is_name_affix(n, phrase_words) for n in rows)


def _phrase_is_name_affix(name: str, phrase_words: list[str]) -> bool:
    """True when the phrase is the first or last words of the name, not a stem.

    startswith("andrew python") would fire on "Andrew Pythonista" and swallow
    a skill into a name filter.
    """
    words = re.findall(r"[a-z0-9]+", name.lower())
    n = len(phrase_words)
    if not n or n > len(words):
        return False
    return words[:n] == phrase_words or words[-n:] == phrase_words


def _name_exists(session: Session, token: str) -> bool:
    """Does anyone in the corpus actually carry this word as part of a name?"""
    from sqlalchemy import or_

    if not _looks_like_a_name(token):
        return False
    rows = session.execute(
        select(Person.canonical_name).where(
            or_(*_name_clauses(Person.canonical_name, token)),
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
        or parsed.roles
    )


def _filtered_stmt(parsed: NLQuery):
    """The filter query, without paging — shared by the count and the page."""
    from .models import Affiliation  # noqa: F401  (used below)

    from sqlalchemy import or_

    from sqlalchemy import and_, exists as sa_exists

    stmt = select(Person).where(Person.merged_into.is_(None))
    # One EXISTS per concept: a person must satisfy EVERY concept asked for,
    # while any of that concept's matching topics will do. ORing everything
    # together instead would turn "robotics and computer vision" into "either".
    for group in parsed.skill_groups:
        clauses = []
        if group.get("values"):
            clauses.append(and_(
                Evidence.attribute_type.in_(SKILL_ATTRS),
                func.lower(Evidence.value).in_([v.lower() for v in group["values"]]),
            ))
        if group.get("pattern"):
            clauses.append(and_(
                Evidence.attribute_type.in_(SKILL_ATTRS),
                func.lower(Evidence.value).like(f"%{group['pattern'].lower()}%"),
            ))
        # the raw term as written, against free text (bio, job title)
        term = (group.get("term") or "").lower()
        if len(term) >= 4:
            clauses.append(and_(
                Evidence.attribute_type.in_(("bio", "role")),
                func.lower(Evidence.value).like(f"%{term}%"),
            ))
        if not clauses:
            continue
        stmt = stmt.where(sa_exists().where(and_(
            Evidence.person_id == Person.id, or_(*clauses),
        )))
    if parsed.organizations:
        from .models import normalize_org_name

        wanted = {o.lower() for o in parsed.organizations}
        norms = {normalize_org_name(o) for o in parsed.organizations if o}
        stmt = (
            stmt.join(Affiliation, Affiliation.person_id == Person.id)
            .join(Organization, Organization.id == Affiliation.organization_id)
            .where(
                func.lower(Organization.name).in_(wanted)
                # the same company under another spelling
                | Organization.norm_name.in_(norms)
            )
        )
    if parsed.countries:
        stmt = stmt.where(func.upper(Person.country).in_(parsed.countries))
    if parsed.locations:
        stmt = stmt.where(or_(*[location_anywhere(loc) for loc in parsed.locations]))
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
    if parsed.name_terms:
        from sqlalchemy import String as SAString  # noqa: F401  (used below)

        for term in parsed.name_terms:
            stmt = stmt.where(or_(
                *_name_clauses(Person.canonical_name, term),
                func.lower(func.cast(Person.aliases, SAString)).like(f"%\"{term.lower()}%"),
            ))
    return stmt


def count_matches(session: Session, parsed: NLQuery) -> int:
    """How many people match in total — not just how many this page returns."""
    if not has_filters(parsed):
        return 0
    try:
        inner = _filtered_stmt(parsed).with_only_columns(Person.id).distinct().subquery()
        return session.execute(select(func.count()).select_from(inner)).scalar_one()
    except Exception:
        logger.exception("count_matches failed")
        return 0


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


def _fill_clause_order(tokens: list[str], result: NLQuery) -> None:
    """Record constraints left-to-right as the user typed them."""
    order = []
    used: set[tuple] = set()
    i = 0
    low = [t.lower() for t in tokens]
    while i < len(low):
        hit = None
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
            for org in result.organizations:
                if org.lower() == gram and ("org", org.lower()) not in used:
                    hit = {"kind": "organizations", "payload": org, "token": raw, "label": "org"}
                    used.add(("org", org.lower()))
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
            if gram in COUNTRIES and ("country", COUNTRIES[gram]) not in used:
                hit = {
                    "kind": "countries", "payload": COUNTRIES[gram],
                    "token": raw, "label": "country",
                }
                used.add(("country", COUNTRIES[gram]))
                width = n
        if hit:
            hit["at"] = i
            order.append(hit)
            i += width
        else:
            i += 1
    result.clause_order = order


def _parsed_from_clauses(base: NLQuery, clauses: list) -> NLQuery:
    from dataclasses import replace

    p = replace(
        base,
        skill_groups=[], skills=[], skill_patterns=[],
        name_terms=[], locations=[], countries=[], organizations=[], roles=[],
        clause_order=list(clauses),
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
            p.organizations.append(payload)
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


def execute_progressive(session: Session, parsed: NLQuery) -> tuple[list, NLQuery, list]:
    """AND every constraint; if that is empty, drop terms from the right.

    "Andrew python california" tries all three, then Andrew+Python, then Andrew.
    Dropped terms are returned so the UI can say which word was not found.
    """
    try:
        clauses = list(parsed.clause_order or [])
        if not clauses:
            return execute(session, parsed), parsed, []
        for n in range(len(clauses), 0, -1):
            trial = _parsed_from_clauses(parsed, clauses[:n])
            rows = execute(session, trial)
            if rows:
                dropped = [
                    {"term": c["token"], "as": c["label"]}
                    for c in clauses[n:]
                ]
                return rows, trial, dropped
        return [], parsed, [{"term": c["token"], "as": c["label"]} for c in clauses]
    except Exception:
        logger.exception("progressive search failed; returning no rows")
        return [], parsed, []


def execute(session: Session, parsed: NLQuery) -> list[Person]:
    """Apply the parsed filters, then order by evidence-backed relevance.

    If NOTHING in the query matched the corpus vocabulary, return nothing.
    An unfiltered SELECT would hand back arbitrary people that look like
    answers to a question we could not actually answer.
    """
    if not has_filters(parsed):
        return []
    from .rank import relevance_order_by

    try:
        stmt = _filtered_stmt(parsed)
        # de-duplicate on id, not whole rows: Postgres cannot DISTINCT a JSON column
        inner = stmt.with_only_columns(Person.id).distinct().subquery()
        ids = session.execute(
            select(inner.c.id)
            .select_from(inner.join(Person, Person.id == inner.c.id))
            .order_by(*relevance_order_by(parsed))
            .limit(parsed.limit)
            .offset(parsed.offset)
        ).scalars().all()
    except Exception:
        logger.exception("search execute failed")
        return []
    if not ids:
        return []
    rows = {p.id: p for p in session.execute(
        select(Person).where(Person.id.in_(ids))).scalars()}
    return [rows[i] for i in ids if i in rows]


def _search_openalex(query: str, limit: int) -> list[dict]:
    from .connectors import get_connector

    conn = get_connector("openalex")
    # A one- or two-word query is a person's name far more often than a
    # subject, and works search answers it with whoever wrote a paper
    # mentioning the word — "sricharan" returned Justine S. Ko.
    # Author-NAME search is only ever right for something that could BE a
    # name. Asking it about "SaaS" returns people surnamed Saas, and about
    # "Rust" people surnamed Rust — a whole page of confident nonsense.
    name_ok = all(_could_be_a_name(t) for t in query.split()) and len(query.split()) <= 3
    order = (conn.search_authors, conn.search_authors_by_topic) if name_ok \
        else (conn.search_authors_by_topic,)
    candidates = []
    for search in order:
        candidates = [c for c in search(query, limit=limit)
                      if _looks_like_a_person(c.get("name"))]
        if candidates:
            break
    return [
        {
            "source": "openalex",
            "external_id": c["id"],
            "name": c.get("name"),
            "affiliation": c.get("affiliation"),
            "works_count": c.get("works_count"),
        }
        for c in candidates
    ]


def _old_search_openalex(query: str, limit: int) -> list[dict]:
    from .connectors import get_connector

    return [
        {
            "source": "openalex",
            "external_id": c["id"],
            "name": c.get("name"),
            "affiliation": c.get("affiliation"),
            "works_count": c.get("works_count"),
        }
        for c in get_connector("openalex").search_authors(query, limit=limit)
    ]


def _search_semanticscholar(query: str, limit: int) -> list[dict]:
    from .connectors import get_connector

    # an author-name index: asking it about "SaaS" returns people named Saas
    if not _name_like_query(query):
        return []
    return [
        {
            "source": "semanticscholar",
            "external_id": c["id"],
            "name": c.get("name"),
            "affiliation": (c.get("affiliations") or [None])[0],
            "works_count": c.get("papers"),
        }
        for c in get_connector("semanticscholar").search_authors(query, limit=limit)
    ]


def _search_exa(query: str, limit: int) -> list[dict]:
    """People search over professional profiles. Costs money per call, so it
    runs only when the free scholarly sources found nothing."""
    import os

    from .connectors import get_connector

    if not os.environ.get("EXA_API_KEY"):
        return []
    return [
        {
            "source": "exa",
            "external_id": c["id"],
            "name": c.get("name"),
            "affiliation": c.get("affiliation"),
            "role": c.get("role"),
            "location": c.get("location"),
            "works_count": None,
            # the full person payload we already paid for — ingesting it costs
            # nothing more, and means this query is answered locally next time
            "_raw": c.get("raw"),
            "_connector": "exa",
        }
        for c in get_connector("exa").search_people(query, limit=limit)
    ]


def _search_github(query: str, limit: int, parsed: "NLQuery | None" = None) -> list[dict]:
    """Working engineers — the population the scholarly indexes cannot see.

    Nobody publishes a paper about maintaining Kubernetes, so queries about
    tools and open-source work come back empty from OpenAlex and dblp while
    GitHub answers them directly.
    """
    from .connectors import get_connector

    conn = get_connector("github")
    # GitHub matches the literal string against login, name and bio, so a whole
    # question finds nobody. Try the phrase, then its individual words, most
    # distinctive first — "Kubernetes" is what identifies these people.
    # Query grammar must not become the search: "designers" matched the users
    # designerSejinOH and DESIGNERSWITHOUTBORDERS. Only distinctive words are
    # worth asking about, and a question with none of them has no answer here.
    terms = [
        t for t in re.split(r"[^A-Za-z0-9.+#-]+", query)
        if len(t) > 2 and t.lower() not in STOPWORDS and t.lower() not in ROLE_MODIFIERS
    ]
    if not terms:
        return []
    attempts = [query] if len(terms) > 1 else []
    attempts += sorted(set(terms), key=len, reverse=True)
    # GitHub can filter by location itself, so "community managers in
    # hyderabad" stops returning people anywhere in the world.
    place = None
    if parsed is not None and parsed.locations:
        place = str(parsed.locations[0]).split(",")[0].strip()

    logins: list[str] = []
    # Do not fall back to a worldwide search when the user named a place:
    # that turned "Andrew python california" into anyone named Andrew.
    loc_tries = [place] if place else [None]
    for attempt in attempts[:3]:
        for loc in loc_tries:
            try:
                logins, _total = conn.search_users(
                    location=loc, query=attempt, per_page=limit
                )
            except Exception:
                continue    # a throttled search is not a query failure
            if logins:
                break
        if logins:
            break
    return [
        {"source": "github", "external_id": login, "name": login,
         "affiliation": None, "works_count": None}
        for login in logins
    ]


def _search_dblp(query: str, limit: int) -> list[dict]:
    from .connectors import get_connector

    # same as Semantic Scholar: dblp searches author names, nothing else
    if not _name_like_query(query):
        return []
    return [
        {
            "source": "dblp",
            "external_id": c["pid"],
            "name": c.get("name"),
            "affiliation": None,
            "works_count": None,
        }
        for c in get_connector("dblp").search_authors(query, limit=limit)
        if c.get("pid")
    ]


def _search_tinyfish(query: str, limit: int, parsed: "NLQuery | None" = None) -> list[dict]:
    """Public web people-search via TinyFish Search (free). Fetch is separate.

    Scholarly indexes answer researchers. GitHub answers people whose handle
    matches the query text. TinyFish answers everyone else — a designer with
    a portfolio, an engineer whose homepage lists their stack — by finding
    pages and routing each URL to the connector that should read it.
    """
    from .tinyfish import configured, route_hit, search as tf_search

    if not configured():
        return []
    bits: list[str] = []
    place = None
    if parsed is not None:
        bits.extend(parsed.roles[:2])
        bits.extend(parsed.skills[:2])
        bits.extend(parsed.name_terms[:2])
        if parsed.organizations:
            bits.append(f'"{parsed.organizations[0]}"')
        if parsed.locations:
            place = str(parsed.locations[0]).split(",")[0].strip()
            bits.append(place)
        if parsed.countries:
            place = place or parsed.countries[0]
    text = " ".join(b for b in bits if b).strip() or query
    if not text:
        return []
    name_only = (
        parsed is not None
        and parsed.name_terms
        and not parsed.roles
        and not parsed.skills
        and not parsed.organizations
    )
    q = (
        f'{text} (github.com OR researcher OR engineer OR "personal website")'
        if name_only
        else (
            f'{text} (portfolio OR "personal website" OR "about me" '
            f'OR github.io OR "I work as" OR researcher OR engineer)'
        )
    )
    hits = tf_search(q, limit=max(limit * 2, 8), location=place)
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for hit in hits:
        item = route_hit(hit["url"], hit.get("title"), hit.get("snippet"))
        if item is None:
            continue
        key = (item["source"], item["external_id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


# tried in order; later sources only run if earlier ones found nothing useful
# (name, fn, uses_full_query). Author-name lookups want just the leftover
# terms; a semantic people search wants the whole question, because stripping
# "engineers at ... in Bengaluru" throws away the very context it matches on.
SUGGESTION_SEARCHERS = (
    ("openalex", _search_openalex, True),
    ("semanticscholar", _search_semanticscholar, False),
    ("dblp", _search_dblp, False),
    # the leftover words, not the whole question: GitHub matches literal text
    ("github", _search_github, False),
    # free web search: people the scholarly indexes and GitHub login-search miss
    ("tinyfish", _search_tinyfish, True),
    # paid, and the only remaining source for closed professional graphs: last
    ("exa", _search_exa, True),
)

def _wants_parsed(searcher) -> bool:
    """Does this searcher take the parsed query as a third argument?

    Kept optional so a test can still patch in a plain (query, limit) function.
    """
    import inspect

    try:
        return len(inspect.signature(searcher).parameters) >= 3
    except (TypeError, ValueError):
        return False


def _always_run(source: str) -> bool:
    """Sources that run even when earlier ones already found enough.

    GitHub covers a population the scholarly indexes structurally cannot —
    nobody publishes a paper about maintaining Kubernetes — so a query about
    tools would otherwise be answered by whichever academic wrote about the
    word. Its search is one request, so it always runs; fetching the profiles
    is what the unauthenticated 60/hour budget cannot afford.
    """
    return source in ("github", "tinyfish")


# When GitHub's hourly budget runs out, every further fetch is a wasted
# round-trip. Remember that until the quota resets rather than rediscovering
# it once per person.
_GITHUB_BLOCKED_UNTIL = 0.0


def _github_throttled() -> bool:
    import time

    return time.time() < _GITHUB_BLOCKED_UNTIL


def _note_github_throttled(seconds: float = 900.0) -> None:
    global _GITHUB_BLOCKED_UNTIL
    import time

    _GITHUB_BLOCKED_UNTIL = time.time() + seconds
    logger.warning(
        "GitHub rate limit reached (60/hour unauthenticated); "
        "set GITHUB_TOKEN to raise it to 5000/hour"
    )


def _may_fetch_github(stored_so_far: int) -> bool:
    """With a token, always. Without one, only while the page is still thin."""
    import os

    if _github_throttled():
        return False
    return bool(os.environ.get("GITHUB_TOKEN")) or stored_so_far < 3
MIN_USEFUL_SUGGESTIONS = 3
# providers that bill per call
PAID_SOURCES = ("exa",)


# A cached live search is reused for this long before the provider is asked
# again. Short enough that job changes surface within a week; long enough that
# repeating a search costs nothing.
SEARCH_TTL_DAYS = int(os.environ.get("SEEKR_SEARCH_TTL_DAYS", "7"))


def _norm_query(q: str) -> str:
    return " ".join(sorted(re.findall(r"[a-z0-9]+", q.lower())))[:512]


def _cache_lookup(session: Session, provider: str, query: str):
    """The cache row for this query if it is still fresh, else None."""
    from datetime import datetime, timedelta, timezone

    from .models import SearchCache

    row = session.execute(
        select(SearchCache).where(
            SearchCache.provider == provider,
            SearchCache.query_norm == _norm_query(query),
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    age = datetime.now(timezone.utc) - row.ran_at.replace(tzinfo=timezone.utc)
    return row if age < timedelta(days=SEARCH_TTL_DAYS) else None


def _cache_record(
    session: Session, provider: str, query: str, found: int, stored: int,
    person_ids: list[str] | None = None,
) -> None:
    from datetime import datetime, timezone

    from .models import SearchCache

    row = session.execute(
        select(SearchCache).where(
            SearchCache.provider == provider,
            SearchCache.query_norm == _norm_query(query),
        )
    ).scalar_one_or_none()
    if row is None:
        row = SearchCache(provider=provider, query_norm=_norm_query(query), query_raw=query[:512])
        session.add(row)
    row.result_count, row.stored_count = found, stored
    row.person_ids = person_ids or []
    row.ran_at = datetime.now(timezone.utc)
    session.commit()


# Sources whose full-profile fetch is a free public API, so a live search can
# be turned into real stored people without spending anything.
FREE_FETCH_SOURCES = (
    "openalex", "semanticscholar", "dblp", "github",
    "huggingface", "orcid", "web", "stackoverflow",
)
# GitHub costs two requests per profile against a 60/hour unauthenticated
# budget, so it fetches fewer than the scholarly APIs do. Web pages are
# similarly bounded: each one is a live fetch of someone else's site.
PER_SOURCE_FETCH_LIMIT = {"github": 5, "web": 5, "huggingface": 5, "orcid": 5}

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


def _github_worth_keeping(profile, term: str) -> bool:
    """Keep a GitHub profile only when the query is actually about this person.

    Name searches: the display name must contain a query word (Rahul → Rahul
    Sharma). Skill or role searches: the word must appear in the bio, an
    employer, a place or a project — not only the login — so a handle like
    Intelligence08 is not an "AI researcher".
    """
    words = [w for w in re.split(r"[^A-Za-z0-9+#.-]+", (term or "").lower()) if len(w) > 2]
    if not words:
        return True
    name = (profile.name or "").strip().lower()
    if _name_like_query(term):
        return any(w in name for w in words)
    if name in words:
        return False
    return _corroborated(profile, term)


def _corroborated(profile, term: str) -> bool:
    """Did the search term turn up anywhere beyond the username?

    GitHub matches literal text against the login, so "AI researchers from
    Stanford" reached github.com/Intelligence-Manifesto and Intelligence247 —
    real people whose only connection to the query is the word in their
    handle. A match has to show up in the bio, an employer, a place, a skill
    or a project to count as evidence of anything.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9+#.-]+", term.lower()) if len(w) > 2]
    if not words:
        return True
    haystack = " ".join(
        str(x).lower() for x in (
            [profile.summary or "", profile.location or ""]
            + list(profile.organizations or [])
            + [e.value for e in (profile.evidence or [])]
            + [getattr(pr, "name", "") for pr in (profile.projects or [])]
            + [getattr(pr, "description", "") or "" for pr in (profile.projects or [])]
        )
    )
    return any(w in haystack for w in words)


def _looks_like_a_person(name: str | None) -> bool:
    """Filter index entities out of author search results."""
    if not name or len(name) > 60:
        return False
    if NOT_A_PERSON.search(name):
        return False
    # A person's name is not a sentence. Initials are cheap ("André C. P. L.
    # F. de Carvalho"), so count only the words that are not single letters.
    words = [w for w in name.split() if len(w.strip(".")) > 1]
    return 1 <= len(words) <= 5
MAX_FREE_FETCHES = 10
# concurrent profile fetches: enough to hide latency, gentle on the source
FETCH_WORKERS = 5


def persist_suggestions(session: Session, suggestions: list[dict]) -> int:
    """Ingest live results, so a query the corpus could not answer grows it.

    Two kinds of result arrive here. Exa returns the whole person record in the
    search response — already bought, so storing it costs nothing. The free
    scholarly sources return only an identifier, so the profile has to be
    fetched; that request is free, and bounded per query.

    Fetches run concurrently because they are independent HTTP calls and doing
    them one at a time put a live query near 30 seconds. Ingest stays on this
    thread: a SQLAlchemy session is not safe to share.
    """
    from concurrent.futures import ThreadPoolExecutor

    from .connectors import get_connector
    from .ingest import ingest_profile

    def _keep(item: dict, profile) -> None:
        nonlocal stored
        try:
            person = ingest_profile(session, profile)
            item["stored"] = True
            item["person_id"] = str(person.id)
            stored += 1
        except Exception as exc:
            # surfaced on the item so a caller can see the record was found
            # but not kept, rather than it vanishing silently
            item["stored"] = False
            item["store_error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("could not persist %s result: %s", item.get("source"), exc)
            session.rollback()

    stored = 0
    to_fetch: list[dict] = []
    for item in suggestions:
        raw, source = item.get("_raw"), item.get("_connector")
        if raw and source:
            try:
                _keep(item, get_connector(source).normalize(raw))
            except Exception as exc:
                item["stored"] = False
                item["store_error"] = f"{type(exc).__name__}: {exc}"
            continue
        if item.get("source") in FREE_FETCH_SOURCES and item.get("external_id"):
            src = item["source"]
            cap = PER_SOURCE_FETCH_LIMIT.get(src, MAX_FREE_FETCHES)
            if sum(1 for i in to_fetch if i["source"] == src) < cap \
                    and len(to_fetch) < MAX_FREE_FETCHES + sum(PER_SOURCE_FETCH_LIMIT.values()):
                to_fetch.append(item)

    if to_fetch:
        def _pull(item: dict):
            try:
                profile = get_connector(item["source"]).fetch(item["external_id"])
                if not (profile.name or "").strip():
                    raise ValueError("source returned a profile with no name")
                term = item.get("_term", "")
                term_words = {
                    w for w in re.split(r"[^A-Za-z0-9+#.-]+", term.lower())
                    if len(w) > 2
                }
                # A person named Rahul is the right answer to "Rahul". The
                # old check treated that as a username collision and dropped
                # every exact-name hit from OpenAlex and GitHub.
                if not _name_like_query(term) and (profile.name or "").strip().lower() in term_words:
                    raise ValueError(
                        f"{item['external_id']} is named after the search term, not a person"
                    )
                if item["source"] == "github" and not _github_worth_keeping(profile, term):
                    raise ValueError(
                        f"{item['external_id']} matches '{term}' only in "
                        "the username — no supporting evidence"
                    )
                return item, profile, None
            except Exception as exc:
                if item.get("source") == "github" and "rate limit" in str(exc).lower():
                    _note_github_throttled()
                return item, None, exc

        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            for item, profile, exc in pool.map(_pull, to_fetch):
                if exc is not None:
                    item["stored"] = False
                    item["store_error"] = f"{type(exc).__name__}: {exc}"
                    logger.warning("could not fetch %s result: %s", item.get("source"), exc)
                    continue
                _keep(item, profile)
    return stored


def discovery_suggestions(
    session: Session | None = None, parsed: NLQuery = None, limit: int = 10,
    allow_paid: bool = True,
) -> list[dict]:
    """Live author search across sources for terms the local corpus lacks.

    Returns SUGGESTIONS only — nothing is ingested here. Sources are tried in
    order and later ones are skipped once enough candidates are found, so the
    common case still costs a single upstream call. A failing source is
    skipped rather than failing the query.
    """
    terms = parsed.unmatched_terms + parsed.name_terms
    if not terms:
        terms = parsed.skills[:1]
    query = " ".join(terms).strip()
    # stable across vocabulary changes, unlike the derived `query`
    cache_key = (parsed.raw or query).strip()
    if not cache_key:
        return []

    out: list[dict] = []
    total_stored = 0
    # person ids from cached searches: found before, still the right answer
    replayed: list[str] = []
    for source, searcher, uses_full_query in SUGGESTION_SEARCHERS:
        if len(out) >= MIN_USEFUL_SUGGESTIONS and not _always_run(source):
            break
        if not allow_paid and source in PAID_SOURCES:
            continue
        # Already bought this answer recently? The people are in the graph, so
        # do not pay for it again. Keyed on the USER'S query, not the derived
        # search string: once new people are stored the residual string
        # changes, and keying on that would bill for the same request twice.
        cached = _cache_lookup(session, source, cache_key) if session is not None else None
        if cached:
            # Free, but not empty: replay the people this search already found,
            # so a repeat query returns the same answers it did the first time.
            cached.hits = (cached.hits or 0) + 1
            replayed.extend(cached.person_ids or [])
            session.commit()
            continue
        # A question made entirely of role words ("product designers who have
        # worked on developer tools") leaves no residue at all. Fall back to
        # what the user actually typed rather than searching for nothing.
        search_for = (cache_key if uses_full_query else query) or cache_key
        try:
            found = (
                searcher(search_for, limit, parsed)
                if _wants_parsed(searcher) else searcher(search_for, limit)
            )
        except Exception:
            continue  # a throttled or unavailable source is not a query failure
        keep = []
        for item in found:
            if not item.get("external_id"):
                continue
            item["reason"] = f"live {source} search for '{search_for}'"
            item["ingest_command"] = f"rip.cli ingest {source} {item['external_id']}"
            item["_term"] = search_for
            keep.append(item)
        if source == "github" and not _may_fetch_github(total_stored):
            # keep them as candidates to add, but do not spend the request
            # budget pulling full profiles
            for item in keep:
                item["fetch_skipped"] = "github rate limit — set GITHUB_TOKEN"
            stored = 0
        else:
            stored = persist_suggestions(session, keep) if session is not None else 0
        total_stored += stored
        # A free source that stored nobody is not worth remembering: the
        # identifiers may be real, but caching the miss locks out the retry
        # that a recovered fetch (or a name we used to reject) would answer.
        # Paid providers still cache their misses — that is where the money is.
        if session is not None and (stored or source in PAID_SOURCES):
            _cache_record(
                session, source, cache_key, len(keep), stored,
                [i["person_id"] for i in keep if i.get("person_id")],
            )
        out.extend(keep)
    result = out[: limit * 2]
    # Replayed people carry no suggestion payload — they are already in the
    # graph. They ride along as id-only entries so the caller can include them
    # in results, and are not shown as "live candidates" to add.
    seen_ids = {i.get("person_id") for i in result}
    result.extend(
        {"person_id": pid, "replayed": True}
        for pid in dict.fromkeys(replayed)
        if pid not in seen_ids
    )
    return result


def queue_suggestions(session: Session, suggestions: list[dict], query: str) -> int:
    """Add suggestions to the discovery-lead queue for a worker to ingest.

    Still no ingest inside the request: this only records intent, and the
    normal lead pipeline (rate limits, enrichment, resolution) applies when a
    worker picks it up.
    """
    from .discover import _add_lead
    from .models import DiscoveryLead, SourceRecord

    added = 0
    for item in suggestions:
        source, external_id = item.get("source"), item.get("external_id")
        if not source or not external_id:
            continue
        exists = session.execute(
            select(DiscoveryLead.id).where(
                DiscoveryLead.source == source, DiscoveryLead.identifier == external_id
            )
        ).first()
        already = session.execute(
            select(SourceRecord.id).where(
                SourceRecord.source == source, SourceRecord.external_id == external_id
            )
        ).first()
        if exists or already:
            continue
        session.add(
            DiscoveryLead(
                source=source,
                identifier=external_id,
                reason=f"queued from search '{query}': {item.get('name')}"[:1000],
            )
        )
        added += 1
    session.commit()
    return added
