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
                idx = (
                    int(hashlib.sha256(token.encode("utf-8")).hexdigest(), 16)
                    % self.dimensions
                )
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


def _compute_adjusted_score(
    db_session: Session,
    skill_id: str,
    director_id: str,
    similarity: float,
) -> float:
    """Calculate final routing score with Bayesian smoothed success rate adjustment.

    score_final = similarity * (1 - w) + smoothed_success_rate * w
    where w = ROUTER_SUCCESS_WEIGHT (default 0.3).
    Bayesian smoothing: prior 0.5 with 5 runs weight: (successes + 2.5) / (total_runs + 5).
    If total_runs < 3, uses only similarity.
    """
    import os

    from sqlalchemy import case, func, select

    from src.database.models import SkillUsage, SkillUsageDaily

    total_runs = 0
    total_successes = 0

    try:
        # 1. Query SkillUsage
        d_stmt = select(
            func.count(SkillUsage.id),
            func.sum(case((func.upper(SkillUsage.outcome) == "SUCCESS", 1), else_=0)),
        ).where(
            SkillUsage.skill_id == skill_id,
            SkillUsage.director_id == director_id,
        )
        d_row = db_session.execute(d_stmt).one()
        d_runs = d_row[0] or 0
        d_succ = d_row[1] or 0
        total_runs += int(d_runs)
        total_successes += int(d_succ)
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        pass

    try:
        # 2. Query SkillUsageDaily
        day_stmt = select(
            func.sum(SkillUsageDaily.runs),
            func.sum(SkillUsageDaily.successes),
        ).where(
            SkillUsageDaily.skill_id == skill_id,
            SkillUsageDaily.director_id == director_id,
        )
        day_row = db_session.execute(day_stmt).one()
        day_runs = day_row[0] or 0
        day_succ = day_row[1] or 0
        total_runs += int(day_runs)
        total_successes += int(day_succ)
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        pass

    if total_runs < 3:
        return float(similarity)

    w_env = os.getenv("ROUTER_SUCCESS_WEIGHT", "0.3")
    try:
        w = float(w_env)
        w = max(0.0, min(1.0, w))
    except ValueError:
        w = 0.3

    smoothed_success_rate = (total_successes + 2.5) / (total_runs + 5.0)
    score_final = similarity * (1.0 - w) + smoothed_success_rate * w
    return max(0.0, min(1.0, float(score_final)))


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

        if (
            chroma_client is not None
            and callable(chroma_client)
            and not hasattr(chroma_client, "get_collection")
        ):
            self.embedder = chroma_client
            self.chroma_client = None
        else:
            self.chroma_client = chroma_client
            self.embedder = (
                embedder if embedder is not None else DefaultSkillsEmbedder()
            )

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

        best_candidate_id: str | None = None
        best_score: float = -1.0
        best_skill: SkillCatalog | None = None

        query_vec = self.embedder([prompt])[0]
        docs = [f"{s.name}. {s.description}\n{s.content_md}" for s in authorized_skills]
        doc_vecs = self.embedder(docs)

        for skill, doc_vec in zip(authorized_skills, doc_vecs, strict=False):
            raw_sim = _cosine_similarity(query_vec, doc_vec)
            final_score = _compute_adjusted_score(
                self.db_session, skill.id, director_id, raw_sim
            )
            if final_score > best_score:
                best_score = final_score
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
