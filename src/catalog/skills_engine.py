"""Director-scoped skills adherence and semantic resolution engine."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.database.models import DirectorSkill, SkillCatalog


@dataclass
class SkillMatchResult:
    """Result of director skill matching against a task prompt."""

    matched: bool
    score: float
    skill: SkillCatalog | None = None
    candidate_id: str | None = None
    reason: str = ""


class DefaultSkillsEmbedder:
    """Deterministic, zero-dependency token and character n-gram cosine embedder."""

    def __init__(self, dimensions: int = 1024) -> None:
        self.dimensions = dimensions

    def __call__(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dimensions
            words = re.findall(r"\w+", text.lower())
            if not words:
                results.append(vec)
                continue

            tokens = list(words)
            tokens.extend(f"{words[i]}_{words[i + 1]}" for i in range(len(words) - 1))
            for w in words:
                if len(w) >= 3:
                    tokens.extend(w[i : i + 3] for i in range(len(w) - 2))

            for token in tokens:
                idx = int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16) % self.dimensions
                vec[idx] += 1.0

            norm = math.sqrt(sum(x * x for x in vec))
            if norm > 0.0:
                vec = [x / norm for x in vec]
            results.append(vec)
        return results


def _cosine_similarity(vec1: list[float], vec2: list[float]) -> float:
    """Calculate cosine similarity between two numeric vectors in [0.0, 1.0]."""
    dot = sum(a * b for a, b in zip(vec1, vec2, strict=False))
    norm1 = math.sqrt(sum(a * a for a in vec1))
    norm2 = math.sqrt(sum(b * b for b in vec2))
    if norm1 == 0.0 or norm2 == 0.0:
        return 0.0
    return max(0.0, min(1.0, float(dot / (norm1 * norm2))))


class DirectorSkillsEngine:
    """Scoped skills engine providing department isolation and adherence calculation."""

    def __init__(
        self,
        db_session: Session,
        chroma_client: Any = None,
        embedder: Any = None,
        threshold: float = 0.72,
        collection_name: str = "skills_catalog",
    ) -> None:
        self.db_session = db_session
        self.threshold = threshold
        self.collection_name = collection_name

        if chroma_client is not None and callable(chroma_client) and not hasattr(chroma_client, "get_collection"):
            self.embedder = chroma_client
            self.chroma_client = None
        else:
            self.chroma_client = chroma_client
            self.embedder = embedder if embedder is not None else DefaultSkillsEmbedder()

    def _get_or_create_collection(self) -> Any:
        if self.chroma_client is None:
            return None
        try:
            return self.chroma_client.get_or_create_collection(
                name=self.collection_name,
                metadata={"hnsw:space": "cosine"},
            )
        except (AttributeError, RuntimeError, ValueError, TypeError):
            return None

    def get_authorized_skills(self, director_id: str) -> list[SkillCatalog]:
        """Fetch strictly the skills linked to the given director in director_skills."""
        stmt = (
            select(SkillCatalog)
            .join(DirectorSkill, SkillCatalog.id == DirectorSkill.skill_id)
            .where(DirectorSkill.director_id == director_id)
            .order_by(DirectorSkill.load_priority.asc(), SkillCatalog.id.asc())
        )
        return list(self.db_session.scalars(stmt).all())

    def resolve_skill(self, director_id: str, task_prompt: str) -> SkillMatchResult:
        """Resolve the best matching skill for a director ensuring strict department isolation."""
        prompt = task_prompt.strip()
        if not prompt:
            return SkillMatchResult(
                matched=False,
                score=0.0,
                skill=None,
                candidate_id=None,
                reason="task_prompt must not be empty",
            )

        authorized_skills = self.get_authorized_skills(director_id)
        if not authorized_skills:
            return SkillMatchResult(
                matched=False,
                score=0.0,
                skill=None,
                candidate_id=None,
                reason=f"No authorized skills found for director '{director_id}'",
            )

        skill_map = {s.id: s for s in authorized_skills}
        best_candidate_id: str | None = None
        best_score: float = -1.0
        best_skill: SkillCatalog | None = None

        collection = self._get_or_create_collection()
        if collection is not None:
            try:
                docs = [f"{s.name}. {s.description}\n{s.content_md}" for s in authorized_skills]
                ids = [s.id for s in authorized_skills]
                metadatas = [{"skill_id": s.id, "director_id": director_id} for s in authorized_skills]
                embeddings = self.embedder(docs)
                collection.upsert(ids=ids, documents=docs, metadatas=metadatas, embeddings=embeddings)

                where_filter = (
                    {"skill_id": ids[0]}
                    if len(ids) == 1
                    else {"skill_id": {"$in": ids}}
                )
                query_emb = self.embedder([prompt])
                query_res = collection.query(
                    query_embeddings=query_emb,
                    n_results=min(len(ids), 10),
                    where=where_filter,
                    include=["distances", "metadatas"],
                )
                retrieved_ids = query_res.get("ids", [[]])[0]
                distances = query_res.get("distances", [[]])[0]
                if retrieved_ids and distances:
                    best_candidate_id = retrieved_ids[0]
                    best_score = max(0.0, min(1.0, 1.0 - float(distances[0])))
                    best_skill = skill_map.get(best_candidate_id)
            except (AttributeError, RuntimeError, ValueError, KeyError, TypeError):
                best_candidate_id = None
                best_score = -1.0
                best_skill = None

        if best_score < 0.0:
            query_vec = self.embedder([prompt])[0]
            docs = [f"{s.name}. {s.description}\n{s.content_md}" for s in authorized_skills]
            doc_vecs = self.embedder(docs)

            for skill, doc_vec in zip(authorized_skills, doc_vecs, strict=False):
                sim = _cosine_similarity(query_vec, doc_vec)
                if sim > best_score:
                    best_score = sim
                    best_candidate_id = skill.id
                    best_skill = skill

        best_score = max(0.0, best_score)
        if best_score >= self.threshold and best_skill is not None:
            return SkillMatchResult(
                matched=True,
                score=round(best_score, 4),
                skill=best_skill,
                candidate_id=best_candidate_id,
                reason=f"Matched authorized skill '{best_candidate_id}' with score {best_score:.4f} >= threshold {self.threshold:.2f}",
            )

        return SkillMatchResult(
            matched=False,
            score=round(best_score, 4),
            skill=None,
            candidate_id=best_candidate_id,
            reason=f"Best candidate '{best_candidate_id}' score ({best_score:.4f}) below threshold ({self.threshold:.2f})",
        )
