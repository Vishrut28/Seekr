"""Hugging Face Hub connector — public Hub API (no key).

Identifier: HF username.
Extracts: full name, org memberships, public models/datasets as projects
(likes/downloads as activity), and pipeline tags + libraries as ML skill
evidence.
"""

from collections import Counter

from ..normalize import EvidenceItem, NormalizedProfile, OrgAffiliation, ProjectData
from .base import BaseConnector

API = "https://huggingface.co/api"
MAX_MODELS = 30
MAX_DATASETS = 10


class HuggingFaceConnector(BaseConnector):
    source = "huggingface"
    source_type = "ml_hub"
    min_request_interval = 0.5

    def search_users(self, name: str, limit: int = 10) -> list[dict]:
        """Hub user search. Falls back to model authors if quicksearch is empty."""
        q = (name or "").strip()
        if len(q) < 2:
            return []
        try:
            rows = self.get_json(f"{API}/quicksearch", params={"q": q, "type": "user"})
        except Exception:
            rows = []
        if not isinstance(rows, list):
            rows = (rows or {}).get("users") or (rows or {}).get("results") or []
        out = []
        seen: set[str] = set()
        for row in rows:
            user = (row.get("id") or row.get("user") or row.get("name") or "").strip()
            if not user or user in seen:
                continue
            seen.add(user)
            out.append({"id": user, "name": row.get("fullname") or row.get("name") or user})
            if len(out) >= limit:
                return out
        if out:
            return out
        try:
            models = self.get_json(
                f"{API}/models",
                params={"search": q, "limit": max(limit * 3, 12), "sort": "likes", "direction": -1},
            )
        except Exception:
            return []
        if not isinstance(models, list):
            models = []
        for m in models:
            user = ((m.get("id") or "").split("/")[0] if "/" in (m.get("id") or "") else "") or ""
            if not user or user in seen:
                continue
            seen.add(user)
            out.append({"id": user, "name": user})
            if len(out) >= limit:
                break
        return out

    def fetch(self, identifier: str) -> NormalizedProfile:
        username = identifier.rstrip("/").rsplit("/", 1)[-1]
        overview = self.get_json(f"{API}/users/{username}/overview")
        models = self.get_json(
            f"{API}/models",
            params={"author": username, "sort": "likes", "direction": -1, "limit": MAX_MODELS},
        )
        datasets = self.get_json(
            f"{API}/datasets",
            params={"author": username, "sort": "likes", "direction": -1, "limit": MAX_DATASETS},
        )
        return self.normalize(username, overview, models, datasets)

    def renormalize(self, external_id: str, raw: dict) -> NormalizedProfile:
        return self.normalize(external_id, raw["overview"], raw["models"], raw["datasets"])

    def normalize(
        self, username: str, overview: dict, models: list[dict], datasets: list[dict]
    ) -> NormalizedProfile:
        profile_url = f"https://huggingface.co/{username}"

        organizations = [
            OrgAffiliation(
                name=org["fullname"],
                relation="worked_at",
                is_current=True,
                url=f"https://huggingface.co/{org.get('name', '')}",
            )
            for org in overview.get("orgs") or []
            if org.get("fullname")
        ]

        evidence: list[EvidenceItem] = []
        task_counts = Counter(m["pipeline_tag"] for m in models if m.get("pipeline_tag"))
        for task, count in task_counts.most_common():
            evidence.append(
                EvidenceItem(
                    attribute_type="skill",
                    value=task,
                    extracted_info=f"{count} public Hugging Face models for {task}",
                    url=profile_url,
                    confidence=min(0.85, 0.4 + 0.08 * count),
                )
            )
        lib_counts = Counter(m["library_name"] for m in models if m.get("library_name"))
        for lib, count in lib_counts.most_common(5):
            evidence.append(
                EvidenceItem(
                    attribute_type="skill",
                    value=lib,
                    extracted_info=f"{count} public Hugging Face models built with {lib}",
                    url=profile_url,
                    confidence=min(0.8, 0.4 + 0.05 * count),
                )
            )

        projects: list[ProjectData] = []
        for m in models:
            model_id = m.get("modelId") or m.get("id")
            if not model_id:
                continue
            projects.append(
                ProjectData(
                    name=model_id.split("/")[-1],
                    description=f"Hugging Face model ({m.get('pipeline_tag') or 'unspecified task'})",
                    url=f"https://huggingface.co/{model_id}",
                    technologies=[t for t in (m.get("library_name"), m.get("pipeline_tag")) if t],
                    activity={"likes": m.get("likes", 0), "downloads": m.get("downloads", 0)},
                    started_at=(m.get("createdAt") or "")[:10] or None,
                    role="owner",
                )
            )
        for d in datasets:
            dataset_id = d.get("id")
            if not dataset_id:
                continue
            projects.append(
                ProjectData(
                    name=dataset_id.split("/")[-1],
                    description="Hugging Face dataset",
                    url=f"https://huggingface.co/datasets/{dataset_id}",
                    technologies=["dataset"],
                    activity={"likes": d.get("likes", 0), "downloads": d.get("downloads", 0)},
                    started_at=(d.get("createdAt") or "")[:10] or None,
                    role="owner",
                )
            )

        return NormalizedProfile(
            source=self.source,
            source_type=self.source_type,
            external_id=username,
            url=profile_url,
            raw={"overview": overview, "models": models, "datasets": datasets},
            name=overview.get("fullname"),
            aliases=[username],
            usernames=[f"huggingface:{username.lower()}"],
            organizations=organizations,
            evidence=evidence,
            projects=projects,
        )
