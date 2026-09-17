"""Stack Overflow connector — official Stack Exchange API v2.3.

Identifier: numeric Stack Overflow user ID (e.g. "22656").
Set STACKEXCHANGE_KEY for a higher daily quota (300/day without, 10k with).
Extracts: display name, location, website (strong resolution link), and
top answer tags as skill evidence weighted by answer volume/score.
"""

import os

from ..normalize import EvidenceItem, NormalizedProfile
from .base import BaseConnector

API = "https://api.stackexchange.com/2.3"
MAX_TAGS = 20


class StackOverflowConnector(BaseConnector):
    source = "stackoverflow"
    source_type = "qa_community"
    min_request_interval = 1.0  # small daily quota; be conservative

    def _params(self, extra: dict | None = None) -> dict:
        params = {"site": "stackoverflow", **(extra or {})}
        key = os.environ.get("STACKEXCHANGE_KEY")
        if key:
            params["key"] = key
        return params

    def search_users(self, name: str, limit: int = 10) -> list[dict]:
        """Display-name search. Empty when the name is too short to be useful."""
        q = (name or "").strip()
        if len(q) < 3:
            return []
        data = self.get_json(
            f"{API}/users",
            params=self._params({
                "inname": q,
                "sort": "reputation",
                "order": "desc",
                "pagesize": max(1, min(int(limit), 30)),
            }),
        )
        out = []
        for row in data.get("items") or []:
            uid = row.get("user_id")
            if not uid:
                continue
            out.append({
                "id": str(uid),
                "name": row.get("display_name"),
                "affiliation": None,
                "location": row.get("location"),
            })
            if len(out) >= limit:
                break
        return out

    def fetch(self, identifier: str) -> NormalizedProfile:
        user_id = identifier.rstrip("/").rsplit("/", 1)[-1]
        users = self.get_json(f"{API}/users/{user_id}", params=self._params())
        items = users.get("items") or []
        if not items:
            raise ValueError(f"stackoverflow user {user_id} not found")
        tags = self.get_json(
            f"{API}/users/{user_id}/top-tags", params=self._params({"pagesize": MAX_TAGS})
        )
        return self.normalize(items[0], tags.get("items") or [])

    def renormalize(self, external_id: str, raw: dict) -> NormalizedProfile:
        return self.normalize(raw["user"], raw["top_tags"])

    def normalize(self, user: dict, top_tags: list[dict]) -> NormalizedProfile:
        user_id = str(user["user_id"])
        link = user.get("link") or f"https://stackoverflow.com/users/{user_id}"

        evidence = []
        for tag in top_tags:
            name = tag.get("tag_name")
            answers = tag.get("answer_count") or 0
            score = tag.get("answer_score") or 0
            if not name or answers == 0:
                continue
            evidence.append(
                EvidenceItem(
                    attribute_type="skill",
                    value=name,
                    extracted_info=f"{answers} answers, total score {score} on Stack Overflow",
                    url=link,
                    confidence=min(0.9, 0.4 + 0.01 * answers),
                )
            )

        websites = [user["website_url"]] if user.get("website_url") else []

        return NormalizedProfile(
            source=self.source,
            source_type=self.source_type,
            external_id=user_id,
            url=link,
            raw={"user": user, "top_tags": top_tags},
            name=user.get("display_name"),
            location=user.get("location"),
            usernames=[f"stackoverflow:{user_id}"],
            websites=websites,
            evidence=evidence,
        )
