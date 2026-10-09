"""Skill usage recording, telemetry consolidation, and retention pruning.

Privacy policy:
- NEVER records task body, prompts, completions, or customer data.
- Records ONLY execution metrics and a short, truncated summary note (<= 280 chars).
"""
from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from src.database.models import Base, SkillUsage
from src.router.semantic import DEFAULT_DB_URL
from src.state.manager import StateManager

logger = logging.getLogger(__name__)


def record_skill_usage(
    *,
    director_id: str,
    task_id: str,
    outcome: str,
    skill_id: str | None = None,
    parent_task_id: str | None = None,
    is_ephemeral: bool = False,
    adherence_score: float = 0.0,
    gates_passed: bool = True,
    model: str = "unknown",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
    duration_ms: float = 0.0,
    note: str = "",
    state_manager: StateManager | None = None,
    database_url: str | None = None,
    db_session: Session | None = None,
) -> dict[str, Any] | None:
    """Record a skill usage event to journal and persist to SQLite.
    
    This function is non-blocking and resilient: errors are logged as warnings
    and never bubble up to interrupt the orchestration pipeline.
    """
    try:
        usage_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        truncated_note = (note or "")[:280].strip()

        # 1. Privacy-safe payload (strictly metadata & metrics)
        payload = {
            "id": usage_id,
            "created_at": now.isoformat(),
            "skill_id": skill_id,
            "director_id": director_id,
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "is_ephemeral": is_ephemeral,
            "adherence_score": round(float(adherence_score), 4),
            "outcome": str(outcome),
            "gates_passed": bool(gates_passed),
            "model": str(model),
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "cost_usd": round(float(cost_usd), 6),
            "duration_ms": round(float(duration_ms), 2),
            "note": truncated_note,
        }

        # 2. Append to StateManager journal as 'skill_usage' event
        if state_manager is not None:
            try:
                state_manager.apply(
                    {"last_skill_usage": payload, "event_type": "skill_usage"},
                    source="skill_usage_tracker",
                )
            except Exception as j_err:  # noqa: BLE001
                logger.warning("Failed to record skill_usage in StateManager journal: %s", j_err)

        # 3. Consolidate to SQLite
        record = SkillUsage(
            id=usage_id,
            created_at=now,
            skill_id=skill_id,
            director_id=director_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            is_ephemeral=is_ephemeral,
            adherence_score=float(adherence_score),
            outcome=outcome,
            gates_passed=gates_passed,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            note=truncated_note,
        )

        if db_session is not None:
            db_session.add(record)
            db_session.commit()
        else:
            db_url = database_url or DEFAULT_DB_URL
            engine = create_engine(db_url)
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                session.add(record)
                session.commit()

        return payload

    except Exception as exc:  # noqa: BLE001
        logger.warning("Non-fatal error recording skill usage for task %s: %s", task_id, exc)
        return None


def prune_skill_usage(
    days: int = 90,
    *,
    database_url: str | None = None,
    session: Session | None = None,
) -> dict[str, dict[str, Any]]:
    """Prune SkillUsage records older than `days` and return aggregated summaries per skill.
    
    Aggregates old records into total uses, success rate, and average cost,
    then deletes the detailed rows.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    db_url = database_url or DEFAULT_DB_URL

    def _do_prune(sess: Session) -> dict[str, dict[str, Any]]:
        # Fetch all records and filter in Python for cross-database timezone compatibility
        all_records = list(sess.scalars(select(SkillUsage)).all())
        old_records: list[SkillUsage] = []

        for rec in all_records:
            rec_created = rec.created_at
            if rec_created is not None:
                if rec_created.tzinfo is None:
                    rec_created = rec_created.replace(tzinfo=UTC)
                if rec_created < cutoff:
                    old_records.append(rec)

        if not old_records:
            return {}

        aggregations: dict[str, dict[str, Any]] = {}
        for rec in old_records:
            sk_id = rec.skill_id or "unassigned"
            if sk_id not in aggregations:
                aggregations[sk_id] = {
                    "total_uses": 0,
                    "successful_uses": 0,
                    "total_cost_usd": 0.0,
                    "total_prompt_tokens": 0,
                    "total_completion_tokens": 0,
                }
            agg = aggregations[sk_id]
            agg["total_uses"] += 1
            if rec.outcome == "success":
                agg["successful_uses"] += 1
            agg["total_cost_usd"] += rec.cost_usd
            agg["total_prompt_tokens"] += rec.prompt_tokens
            agg["total_completion_tokens"] += rec.completion_tokens

        # Calculate final averages
        summary: dict[str, dict[str, Any]] = {}
        for sk_id, agg in aggregations.items():
            uses = agg["total_uses"]
            successes = agg["successful_uses"]
            summary[sk_id] = {
                "total_uses": uses,
                "successful_uses": successes,
                "success_rate": round(successes / uses, 4) if uses > 0 else 0.0,
                "total_cost_usd": round(agg["total_cost_usd"], 6),
                "avg_cost_usd": round(agg["total_cost_usd"] / uses, 6) if uses > 0 else 0.0,
                "total_tokens": agg["total_prompt_tokens"] + agg["total_completion_tokens"],
            }

        # Delete pruned details by ID
        prune_ids = [r.id for r in old_records]
        del_stmt = delete(SkillUsage).where(SkillUsage.id.in_(prune_ids))
        sess.execute(del_stmt)
        sess.commit()
        return summary

    if session is not None:
        return _do_prune(session)

    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    with Session(engine) as sess:
        return _do_prune(sess)
