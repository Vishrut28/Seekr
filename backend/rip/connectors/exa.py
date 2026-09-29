"""Exa connector — semantic web + people search (commercial API, key required).

Identifier: an Exa person-library id (`ybx9zz9d72n`) or its full URL.
Set EXA_API_KEY. Use `search_people(query)` to find candidates first.

Why this connector exists: every other Seekr source indexes scholarly or
open-source output, so people without publications or public code are
invisible. Exa indexes professional profiles, which is the only route we have
to non-academic roles.

PROVENANCE WARNING: Exa's person records are largely derived from LinkedIn
profiles. Seekr does not crawl LinkedIn — Exa is queried under a commercial
agreement and bears responsibility for how it built its index — but the
resulting rows are personal data about identifiable people, and the original
`url` (usually a LinkedIn profile) is preserved on every record so the
provenance is never hidden. Treat this source differently from the open
scholarly ones when deciding retention and lawful basis.
"""

import os
import re

from ..normalize import (
    EvidenceItem,
    NormalizedProfile,
    OrgAffiliation,
)
from .base import BaseConnector

API = "https://api.exa.ai"
# "Gurugram, Haryana, India (IN)" -> country code
COUNTRY_RE = re.compile(r"\(([A-Z]{2})\)\s*$")
SKILLS_RE = re.compile(r"##\s*Skills\s*\n+(.+?)(?:\n##|\Z)", re.S | re.I)


class ExaConnector(BaseConnector):
    source = "exa"
    source_type = "professional_profile"
    min_request_interval = 0.4

    def default_headers(self) -> dict:
        headers = super().default_headers()
        key = os.environ.get("EXA_API_KEY")
        if key:
            headers["x-api-key"] = key
        headers["Content-Type"] = "application/json"
        return headers

    def _post(self, path: str, payload: dict) -> dict:
        for attempt in range(self.max_retries + 1):
            resp = self._client.post(f"{API}{path}", json=payload)
            if resp.status_code == 429 and attempt < self.max_retries:
                import time

                time.sleep(2**attempt)
                continue
            resp.raise_for_status()
            data: dict = resp.json()
            return data
        raise RuntimeError("exa: retries exhausted")

    def search_people(self, query: str, limit: int = 10) -> list[dict]:
        """Find people matching a natural-language description.

        Each call costs money (roughly $0.007 per search at current pricing),
        so callers should treat results as suggestions to be reviewed rather
        than something to run in a loop.
        """
        if not os.environ.get("EXA_API_KEY"):
            raise RuntimeError("EXA_API_KEY is not set")
        data = self._post(
            "/search",
            {
                "query": query,
                "numResults": limit,
                "type": "auto",
                "contents": {"text": {"maxCharacters": 3000}},
            },
        )
        out = []
        for r in data.get("results") or []:
            entity = (r.get("entities") or [{}])[0]
            if entity.get("type") != "person":
                continue
            props = entity.get("properties") or {}
            work = (props.get("workHistory") or [{}])[0]
            out.append(
                {
                    "id": self._person_id(r.get("id") or ""),
                    "name": props.get("name") or r.get("title"),
                    "affiliation": (work.get("company") or {}).get("name"),
                    "role": work.get("title"),
                    "location": props.get("location"),
                    "profile_url": r.get("url"),
                    "raw": r,
                }
            )
        return out

    @staticmethod
    def _person_id(value: str) -> str:
        return value.rstrip("/").rsplit("/", 1)[-1]

    def fetch(self, identifier: str) -> NormalizedProfile:
        """Re-fetch one person by their Exa library id."""
        person_id = self._person_id(identifier)
        url = (
            identifier
            if identifier.startswith("http")
            else f"https://exa.ai/library/person/{person_id}"
        )
        data = self._post(
            "/contents", {"urls": [url], "text": {"maxCharacters": 3000}}
        )
        results = data.get("results") or []
        if not results:
            raise ValueError(f"exa person {person_id} not found")
        return self.normalize(results[0])

    def renormalize(self, external_id: str, raw: dict) -> NormalizedProfile:
        return self.normalize(raw)

    def normalize(self, result: dict) -> NormalizedProfile:
        entity = (result.get("entities") or [{}])[0]
        props = entity.get("properties") or {}
        text = result.get("text") or ""
        person_id = self._person_id(entity.get("id") or result.get("id") or "")
        exa_url = f"https://exa.ai/library/person/{person_id}"
        # the underlying profile (usually LinkedIn) — kept so provenance is explicit
        profile_url = result.get("url")

        name = props.get("name") or result.get("title")
        location = props.get("location")
        country = None
        if location:
            match = COUNTRY_RE.search(location)
            if match:
                country = match.group(1)
                location = COUNTRY_RE.sub("", location).strip()
        if not country:
            match = COUNTRY_RE.search(text.split("\n\n")[2] if text.count("\n\n") > 2 else "")
            country = match.group(1) if match else None

        organizations: list[OrgAffiliation] = []
        for i, job in enumerate(props.get("workHistory") or []):
            company = (job.get("company") or {}).get("name")
            if not company:
                continue
            organizations.append(
                OrgAffiliation(
                    name=company,
                    relation="worked_at",
                    role=job.get("title"),
                    org_type="company",
                    is_current=i == 0,
                    url=profile_url,
                )
            )
        for school in props.get("educationHistory") or []:
            institution = (school.get("school") or school.get("company") or {})
            institution_name = (
                institution.get("name") if isinstance(institution, dict) else institution
            )
            if institution_name:
                organizations.append(
                    OrgAffiliation(
                        name=institution_name, relation="studied_at", url=profile_url
                    )
                )

        evidence: list[EvidenceItem] = []
        skills_match = SKILLS_RE.search(text)
        if skills_match:
            for skill in skills_match.group(1).split("•"):
                skill = skill.strip()
                if 1 < len(skill) <= 120:
                    evidence.append(
                        EvidenceItem(
                            attribute_type="skill",
                            value=skill,
                            extracted_info="listed on the person's professional profile",
                            url=profile_url or exa_url,
                            confidence=0.6,
                        )
                    )
        current = next((o for o in organizations if o.is_current), None)
        if current and current.role:
            evidence.append(
                EvidenceItem(
                    attribute_type="role",
                    value=current.role,
                    extracted_info=f"{current.role} at {current.name}",
                    url=profile_url or exa_url,
                    confidence=0.7,
                )
            )

        return NormalizedProfile(
            source=self.source,
            source_type=self.source_type,
            external_id=person_id,
            url=exa_url,
            raw=result,
            name=name,
            location=location or None,
            country=country,
            # the underlying profile URL is a strong identity key AND the
            # audit trail for where this record ultimately came from
            linked_urls=[profile_url] if profile_url else [],
            organizations=organizations,
            evidence=evidence,
        )
