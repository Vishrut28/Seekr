"""Wikidata connector — free open API, no key.

Identifier: a Wikidata Q-id (e.g. "Q92894"), or use `search_people`.

Wikidata is unusual and valuable: it is an *identity hub*. A single record
often carries a person's ORCID, GitHub handle, Google Scholar id, dblp PID,
Semantic Scholar id and homepage together. Those become strong resolution
keys, letting us link profiles that would otherwise never match on name —
including Google Scholar identifiers, which have no API we may fetch.
"""

from typing import cast

from ..normalize import (
    EvidenceItem,
    NormalizedProfile,
    OrgAffiliation,
)
from .base import BaseConnector

API = "https://www.wikidata.org/w/api.php"
HUMAN = "Q5"

# property -> (identity kind, url template)
IDENTITY_PROPS = {
    "P496": ("orcid", "https://orcid.org/{}"),
    "P2037": ("github", "https://github.com/{}"),
    "P1960": ("scholar", "https://scholar.google.com/citations?user={}"),
    "P2456": ("dblp", "https://dblp.org/pid/{}"),
    "P4012": ("semanticscholar", "https://www.semanticscholar.org/author/{}"),
    "P6178": ("dimensions", None),
    "P856": ("homepage", None),  # official website (a bare URL, not an id)
}
EMPLOYER = "P108"
EDUCATED_AT = "P69"
OCCUPATION = "P106"
FIELD_OF_WORK = "P101"
AWARD = "P166"
MAX_LABEL_LOOKUPS = 40


class WikidataConnector(BaseConnector):
    source = "wikidata"
    source_type = "knowledge_base"
    min_request_interval = 1.0

    def _get(self, params: dict) -> dict:
        data: dict = self.get_json(API, params={"format": "json", **params})
        return data

    def search_people(self, name: str, limit: int = 10) -> list[dict]:
        data = self._get(
            {"action": "wbsearchentities", "search": name, "language": "en",
             "type": "item", "limit": limit}
        )
        return [
            {"id": r["id"], "name": r.get("label"), "description": r.get("description")}
            for r in data.get("search", [])
        ]

    def _labels(self, qids: list[str]) -> dict[str, str]:
        """Resolve referenced entities (employers, awards) to English labels."""
        out: dict[str, str] = {}
        qids = [q for q in dict.fromkeys(qids) if q][:MAX_LABEL_LOOKUPS]
        for i in range(0, len(qids), 50):
            batch = qids[i : i + 50]
            data = self._get(
                {"action": "wbgetentities", "ids": "|".join(batch),
                 "props": "labels", "languages": "en"}
            )
            for qid, entity in (data.get("entities") or {}).items():
                label = ((entity.get("labels") or {}).get("en") or {}).get("value")
                if label:
                    out[qid] = label
        return out

    def fetch(self, identifier: str) -> NormalizedProfile:
        qid = identifier.strip().rsplit("/", 1)[-1].upper()
        data = self._get(
            {"action": "wbgetentities", "ids": qid,
             "props": "labels|aliases|descriptions|claims", "languages": "en"}
        )
        entity = (data.get("entities") or {}).get(qid)
        if not entity or "missing" in entity:
            raise ValueError(f"wikidata entity {qid} not found")
        claims = entity.get("claims") or {}

        # only ingest humans; Wikidata items are mostly not people
        instance_of = [
            (c.get("mainsnak", {}).get("datavalue", {}).get("value") or {}).get("id")
            for c in claims.get("P31", [])
        ]
        if HUMAN not in instance_of:
            raise ValueError(f"wikidata entity {qid} is not a person (P31={instance_of})")

        referenced = []
        for prop in (EMPLOYER, EDUCATED_AT, OCCUPATION, FIELD_OF_WORK, AWARD):
            for claim in claims.get(prop, []):
                value = (claim.get("mainsnak", {}).get("datavalue", {}).get("value") or {})
                if isinstance(value, dict) and value.get("id"):
                    referenced.append(value["id"])
        return self.normalize(qid, entity, self._labels(referenced))

    def renormalize(self, external_id: str, raw: dict) -> NormalizedProfile:
        return self.normalize(external_id, raw["entity"], raw.get("labels") or {})

    def normalize(self, qid: str, entity: dict, labels: dict[str, str]) -> NormalizedProfile:
        claims = entity.get("claims") or {}
        url = f"https://www.wikidata.org/entity/{qid}"

        def string_values(prop: str) -> list[str]:
            out = []
            for claim in claims.get(prop, []):
                value = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
                if isinstance(value, str):
                    out.append(value)
            return out

        def entity_labels(prop: str) -> list[str]:
            out = []
            for claim in claims.get(prop, []):
                value = (claim.get("mainsnak", {}).get("datavalue", {}).get("value") or {})
                # labels are keyed by Q-id, so "" finds nothing, as a missing id did
                if isinstance(value, dict) and labels.get(value.get("id", "")):
                    out.append(labels[value["id"]])
            return out

        name = ((entity.get("labels") or {}).get("en") or {}).get("value")
        aliases = [
            a["value"] for a in ((entity.get("aliases") or {}).get("en") or []) if a.get("value")
        ]
        summary = ((entity.get("descriptions") or {}).get("en") or {}).get("value")

        orcid = None
        linked: list[str] = []
        usernames: list[str] = []
        websites: list[str] = []
        for prop, (kind, template) in IDENTITY_PROPS.items():
            for value in string_values(prop):
                if kind == "orcid":
                    orcid = value
                elif kind == "homepage":
                    websites.append(value)
                elif kind == "github":
                    usernames.append(f"github:{value.lower()}")
                    linked.append(cast(str, template).format(value))  # P2037 has one
                elif template:
                    # scholar/dblp/semanticscholar profile URLs become strong keys,
                    # which is how a Wikidata record links sources we cannot crawl
                    linked.append(template.format(value))

        organizations = [
            OrgAffiliation(name=org, relation="worked_at", is_current=True, url=url)
            for org in entity_labels(EMPLOYER)
        ] + [
            OrgAffiliation(name=school, relation="studied_at", url=url)
            for school in entity_labels(EDUCATED_AT)
        ]

        evidence: list[EvidenceItem] = []
        for value in entity_labels(FIELD_OF_WORK):
            evidence.append(
                EvidenceItem(attribute_type="research_interest", value=value,
                             extracted_info="Wikidata field of work (P101)",
                             url=url, confidence=0.7)
            )
        for value in entity_labels(OCCUPATION):
            evidence.append(
                EvidenceItem(attribute_type="specialization", value=value,
                             extracted_info="Wikidata occupation (P106)",
                             url=url, confidence=0.6)
            )
        for value in entity_labels(AWARD):
            evidence.append(
                EvidenceItem(attribute_type="award", value=value,
                             extracted_info="Wikidata award received (P166)",
                             url=url, confidence=0.85)
            )
        if summary:
            evidence.append(
                EvidenceItem(attribute_type="bio", value=summary[:512], url=url, confidence=0.6)
            )

        return NormalizedProfile(
            source=self.source,
            source_type=self.source_type,
            external_id=qid,
            url=url,
            raw={"entity": entity, "labels": labels},
            name=name,
            aliases=aliases,
            summary=summary,
            orcid=orcid,
            usernames=usernames,
            websites=websites,
            linked_urls=linked,
            organizations=organizations,
            evidence=evidence,
        )
