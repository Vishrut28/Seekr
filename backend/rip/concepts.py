"""What a query means beyond the words typed: people nouns and related subjects.

Two gaps the evaluation measured at zero:

- **People nouns.** "physicists", "epidemiologists", "ecologists" found nobody,
  because nobody's stated topic is a person noun — it is "physics",
  "epidemiology", "ecology". The noun is rewritten to its subject before
  parsing, and the rewrite is reported, so the answer says what it searched.

- **Related subjects.** "deep learning" did not reach people whose topics say
  "neural network", and "cybersecurity" did not reach "network security" or
  "intrusion detection". CONCEPTS names, for common broad subjects, the
  narrower or neighbouring subjects that count as partial evidence. Matches
  through it are weighted below the subject asked for (see nlq), so someone
  who states the subject still ranks first.

Research subjects also get OpenAlex's own hierarchy — every topic filed under
a subfield and a field — as evidence at ingest (connectors/openalex.py). This
map covers what that hierarchy does not: industry and engineering skills, and
subjects whose subfield name does not contain the word people search for.
"""

from __future__ import annotations

from .textnorm import stems

# Whole words whose subject does not follow a suffix rule, or follows it wrongly
# ("physician" is medicine, not physics).
_AGENT_EXCEPTIONS = {
    "physician": "medicine", "clinician": "clinical", "surgeon": "surgery",
    "dentist": "dentistry", "pharmacist": "pharmacy", "nurse": "nursing",
    "economist": "economics", "linguist": "linguistics", "chemist": "chemistry",
    "biochemist": "biochemistry", "statistician": "statistics",
    "mathematician": "mathematics", "roboticist": "robotics",
    "geneticist": "genetics", "psychiatrist": "psychiatry",
    "pediatrician": "pediatrics", "paediatrician": "paediatrics",
    "astronomer": "astronomy", "agronomist": "agronomy",
    "neuroscientist": "neuroscience", "epidemiologist": "epidemiology",
    "technician": None, "electrician": None, "musician": None, "magician": None,
    "politician": None, "optician": "optometry", "cryptographer": "cryptography",
}
# (suffix of the singular, subject suffix, minimum length of the word)
_AGENT_SUFFIXES = (
    ("ologist", "ology", 7),        # cardiologist -> cardiology
    ("icist", "ics", 7),            # physicist -> physics
    ("ician", "ics", 8),            # mathematician handled above; logician -> logics
    ("ographer", "ography", 9),     # oceanographer -> oceanography
    ("onomist", "onomy", 8),        # taxonomist -> taxonomy
    ("iatrist", "iatry", 8),        # psychiatrist -> psychiatry
)
# "data scientists" is data science; "scientists" alone is nothing to search.
_SCIENCE_PREFIXES = {
    "data", "computer", "political", "social", "cognitive", "materials", "climate",
    "soil", "food", "plant", "earth", "environmental", "atmospheric", "marine",
    "animal", "sports", "exercise", "information", "behavioral", "behavioural",
}


def _singular_person(word: str) -> str:
    return word[:-1] if word.endswith("s") and not word.endswith("ss") else word


def agent_subject(word: str) -> str | None:
    """The subject a person noun names: "cardiologists" -> "cardiology"."""
    w = _singular_person(word.lower())
    if w in _AGENT_EXCEPTIONS:
        return _AGENT_EXCEPTIONS[w]
    for suffix, subject, minimum in _AGENT_SUFFIXES:
        if w.endswith(suffix) and len(w) >= minimum:
            return w[: -len(suffix)] + subject
    return None


def rewrite_agents(tokens: list[str]) -> tuple[list[str], list[dict]]:
    """Tokens with person nouns replaced by their subjects, and what changed."""
    out: list[str] = []
    rewrites: list[dict] = []
    for i, token in enumerate(tokens):
        low = token.lower()
        if low in ("scientist", "scientists") and out and out[-1].lower() in _SCIENCE_PREFIXES:
            rewrites.append({"typed": f"{out[-1]} {token}", "searched": f"{out[-1]} science",
                             "how": "people noun"})
            out.append("science")
            continue
        subject = agent_subject(token) if len(token) > 5 else None
        if subject:
            rewrites.append({"typed": token, "searched": subject, "how": "people noun"})
            out.append(subject)
        else:
            out.append(token)
    return out, rewrites


# Broad subject -> subjects that are partial evidence for it. Keys and values
# are matched by stems against the corpus vocabulary; entries the corpus does
# not hold simply contribute nothing.
_CONCEPTS: dict[str, list[str]] = {
    "deep learning": ["neural network", "convolutional", "transformer", "representation learning",
                      "generative adversarial", "recurrent neural", "deep neural"],
    "machine learning": ["deep learning", "neural network", "reinforcement learning",
                         "statistical learning", "pattern recognition", "data mining",
                         "few shot learning", "domain adaptation"],
    "artificial intelligence": ["machine learning", "deep learning", "natural language processing",
                                "computer vision", "knowledge representation", "multi agent",
                                "reinforcement learning"],
    "computer vision": ["image processing", "image recognition", "object detection",
                        "image segmentation", "video analysis", "image retrieval", "visual tracking",
                        "face recognition", "video surveillance", "vision and imaging"],
    "natural language processing": ["topic modeling", "language model", "text mining",
                                    "machine translation", "sentiment analysis", "speech recognition",
                                    "information extraction", "text classification",
                                    "text analysis"],
    "cybersecurity": ["network security", "intrusion detection", "cryptography", "malware",
                      "privacy preserving", "digital forensics", "cybercrime", "vulnerability",
                      "cyber forensics", "information security", "spam and phishing"],
    "information security": ["network security", "cryptography", "malware", "intrusion detection"],
    "web development": ["html", "css", "javascript", "typescript", "reactjs", "angular", "vue.js",
                        "jquery", "node.js", "php", "web services", "semantic web"],
    "frontend": ["html", "css", "javascript", "typescript", "reactjs", "angular", "vue.js"],
    "backend": ["node.js", "django", "flask", "spring", "microservices", "postgresql", "mysql",
                "rest api"],
    "mobile development": ["android", "ios", "swift", "kotlin", "flutter", "react native", "iphone"],
    "devops": ["kubernetes", "docker", "terraform", "jenkins", "ansible", "cloud computing"],
    "cloud computing": ["aws", "azure", "google cloud", "kubernetes", "serverless",
                        "resource management"],
    "data engineering": ["apache spark", "hadoop", "etl", "data pipeline", "kafka",
                         "data warehouse", "big data", "data stream"],
    "data science": ["machine learning", "statistics", "data mining", "data analysis", "pandas",
                     "data visualization"],
    "climate": ["atmospheric", "aerosol", "meteorological", "weather", "global warming",
                "carbon emission", "greenhouse gas", "ocean circulation", "paleoclimate",
                "planetary change"],
    "public health": ["epidemiology", "health policy", "global health", "health disparities",
                      "maternal and child health", "infectious disease", "health systems",
                      "nutrition"],
    "infectious disease": ["tuberculosis", "hiv", "malaria", "covid", "influenza",
                           "fungal infection", "antimicrobial resistance", "vaccine"],
    "oncology": ["cancer", "tumor", "tumour", "carcinoma", "glioma", "leukemia", "lymphoma",
                 "metastases"],
    "neuroscience": ["brain", "neural circuit", "neuron", "cognitive neuroscience", "neuroimaging",
                     "neurodegenerative", "dementia", "alzheimer", "parkinson"],
    "astrophysics": ["cosmology", "galaxy", "black hole", "dark matter", "stellar",
                     "gravitational wave", "exoplanet", "supernova", "cosmic"],
    "astronomy": ["astrophysics", "cosmology", "galaxy", "stellar", "exoplanet", "planetary science"],
    "cosmology": ["dark matter", "dark energy", "cosmic microwave", "gravitation", "black hole"],
    "particle physics": ["particle collision", "quantum chromodynamics", "particle detector",
                         "collider", "hadron", "neutrino", "higgs"],
    "high energy physics": ["particle physics", "particle collision", "quantum chromodynamics",
                            "particle detector", "collider"],
    "physics": ["particle physics", "quantum", "condensed matter", "cosmology", "optics",
                "superconductivity", "gravitation", "plasma", "theoretical physics"],
    "materials science": ["graphene", "2d material", "nanomaterial", "thin film", "crystallography",
                          "polymer", "alloy", "composite", "nanofiber"],
    "ecology": ["wildlife", "biodiversity", "species distribution", "conservation", "ecosystem",
                "population dynamics"],
    "genomics": ["gene expression", "genome", "sequencing", "microrna", "genetic variation"],
    "bioinformatics": ["genomics", "protein structure", "sequence analysis", "systems biology",
                       "computational biology"],
    "robotics": ["robot", "autonomous vehicle", "motion planning", "manipulation", "localization"],
    "blockchain": ["cryptocurrency", "smart contract", "distributed ledger", "bitcoin", "ethereum"],
    "medical imaging": ["radiomics", "mri", "radiology", "image segmentation", "medical image"],
    "drug discovery": ["medicinal chemistry", "drug design", "molecular docking", "pharmacology",
                       "computational drug"],
    "economics": ["econometrics", "macroeconomics", "microeconomics", "finance", "monetary policy"],
    "quantum computing": ["quantum algorithm", "quantum information", "qubit"],
    "distributed systems": ["cloud computing", "fault tolerance", "consensus", "parallel computing",
                            "peer to peer"],
    "human computer interaction": ["user experience", "usability", "interaction design",
                                   "human technology interaction"],
    "energy": ["renewable energy", "solar", "battery", "wind energy", "energy storage", "fuel cell"],
    "agriculture": ["crop", "soil", "plant science", "agronomy", "irrigation"],
    "mental health": ["depression", "anxiety", "psychiatry", "psychology"],
    "cardiology": ["cardiovascular", "heart failure", "hypertension", "coronary", "arrhythmia"],
    "information retrieval": ["search engine", "recommender system", "image retrieval", "ranking",
                              "question answering", "topic modeling", "semantic web"],
    "air pollution": ["air quality", "aerosol", "atmospheric chemistry", "particulate matter",
                      "emission"],
    "water resources": ["hydrology", "groundwater", "water quality", "irrigation", "flood"],
    "software engineering": ["software testing", "software architecture", "programming language",
                             "code review", "requirements engineering"],
    "statistics": ["statistical method", "bayesian", "causal inference", "econometrics",
                   "probability"],
    "immunology": ["immune response", "vaccine", "antibody", "inflammation", "autoimmune"],
    "medical ai": ["medical imaging", "radiomics", "clinical decision support", "health informatics",
                   "ai in cancer detection", "computer aided diagnosis", "electronic health record",
                   "artificial intelligence in healthcare"],
    "cryptography": ["encryption", "cryptographic", "quantum cryptography", "data security",
                     "blockchain"],
    "dementia": ["alzheimer", "cognitive impairment", "neurodegenerative", "cognitive aging"],
}
# Other ways people type the same subject.
_ALIASES = {
    "cyber security": "cybersecurity", "security": "cybersecurity", "infosec": "cybersecurity",
    "web": "web development", "web dev": "web development", "front end": "frontend",
    "back end": "backend", "mobile": "mobile development", "ai": "artificial intelligence",
    "ml": "machine learning", "nlp": "natural language processing", "cv": "computer vision",
    "climate science": "climate", "climate change": "climate", "cancer research": "oncology",
    "neurology": "neuroscience", "hep": "high energy physics", "genetics": "genomics",
    "computational biology": "bioinformatics", "hci": "human computer interaction",
    "cloud": "cloud computing", "healthcare ai": "medical ai",
    "infectious diseases": "infectious disease",
    "ai in healthcare": "medical ai", "ai in medicine": "medical ai",
    "clinical ai": "medical ai", "alzheimer": "dementia", "alzheimers": "dementia",
}


def _key(text: str) -> str:
    return " ".join(stems(text))


CONCEPTS = {_key(k): v for k, v in _CONCEPTS.items()}
CONCEPTS.update({_key(alias): CONCEPTS[_key(target)] for alias, target in _ALIASES.items()})


def related_subjects(term: str) -> list[str]:
    """Subjects that count as partial evidence for TERM, or []."""
    return CONCEPTS.get(_key(term), [])
