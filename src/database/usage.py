"""Skill usage recording, telemetry consolidation, and retention pruning.

Privacy policy:
- NEVER records task body, prompts, completions, or customer data.
- Records ONLY execution metrics and a sanitized summary note (<= 280 chars).
- Strips emails, phones, CPF/CNPJ, URLs with query strings, and API tokens.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session

from src.database.models import DEFAULT_DB_URL, Base, SkillUsage, SkillUsageDaily
from src.state.manager import StateManager, record_telemetry

logger = logging.getLogger(__name__)

# ── Sanitizer Regex Patterns ──────────────────────────────────
RE_EMAIL = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
RE_PHONE = re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,3}\)?[-.\s]?\d{4,5}[-.\s]?\d{4}")
RE_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
RE_CNPJ = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/\d{4}-?\d{2}\b")
RE_URL_QUERY = re.compile(r"https?://[^\s]+\?[^\s]+")
RE_TOKEN = re.compile(
    r"\b(?:sk-[a-zA-Z0-9T_-]{12,}|ghp_[a-zA-Z0-9]{16,}|bearer\s+[a-zA-Z0-9._~+/-]{16,}=*|[a-fA-F0-9]{32,64}|[a-zA-Z0-9_-]{28,})\b",
    re.IGNORECASE,
)
RE_JWT = re.compile(r"\beyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b")


def sanitize_note(note: str | None) -> str:
    """Sanitize summary note: redacts sensitive info and truncates to 280 chars.

    Removes emails, phones, CPF/CNPJ, URLs with query strings, and API keys/tokens,
    replacing them with '[redacted]'.
    """
    if not note:
        return ""
    text = str(note)
    text = RE_JWT.sub("[redacted]", text)
    text = RE_URL_QUERY.sub("[redacted]", text)
    text = RE_EMAIL.sub("[redacted]", text)
    text = RE_TOKEN.sub("[redacted]", text)
    text = RE_CNPJ.sub("[redacted]", text)
    text = RE_CPF.sub("[redacted]", text)
    text = RE_PHONE.sub("[redacted]", text)
    return text[:280].strip()


def ensure_tables(engine: Any) -> None:
    """Ensure database schema and columns are initialized and compatible."""
    Base.metadata.create_all(engine)
    try:
        from sqlalchemy import inspect, text

        inspector = inspect(engine)
        if "skill_usage" in inspector.get_table_names():
            columns = {c["name"] for c in inspector.get_columns("skill_usage")}
            with engine.connect() as conn:
                if "gates_failed" not in columns:
                    conn.execute(
                        text(
                            "ALTER TABLE skill_usage ADD COLUMN gates_failed JSON DEFAULT '[]'"
                        )
                    )
                if "cost_source" not in columns:
                    conn.execute(
                        text(
                            "ALTER TABLE skill_usage ADD COLUMN cost_source VARCHAR DEFAULT 'litellm'"
                        )
                    )
                conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug("Schema verification notice: %s", exc)


def get_retention_days() -> int:
    """Get valid retention days from SKILL_USAGE_RETENTION_DAYS (default 60, range 30-90)."""
    val = os.getenv("SKILL_USAGE_RETENTION_DAYS")
    if val:
        try:
            days = int(val)
            return max(30, min(90, days))
        except ValueError:
            pass
    return 60


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
    gates_failed: list[str] | None = None,
    model: str = "unknown",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
    cost_source: str = "litellm",
    duration_ms: float = 0.0,
    note: str = "",
    journal_path: Path | str = Path("data/journal.jsonl"),
    state_manager: StateManager | None = None,
    database_url: str | None = None,
    db_session: Session | None = None,
) -> dict[str, Any] | None:
    """Record a skill usage event by appending a structured line to journal.jsonl.

    This function is fast, non-blocking, and resilient on the task execution path.
    Errors are logged as warnings and never fail the task.
    """
    try:
        usage_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        sanitized_note = sanitize_note(note)
        clean_gates_failed = list(gates_failed or [])

        # Privacy-safe payload (strictly metadata & metrics, zero task content)
        payload = {
            "type": "skill_usage",
            "id": usage_id,
            "created_at": now.isoformat(),
            "skill_id": skill_id,
            "director_id": director_id,
            "task_id": task_id,
            "parent_task_id": parent_task_id,
            "is_ephemeral": bool(is_ephemeral),
            "adherence_score": round(float(adherence_score), 4),
            "outcome": str(outcome).upper(),
            "gates_passed": bool(gates_passed),
            "gates_failed": clean_gates_failed,
            "model": str(model),
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "cost_usd": round(float(cost_usd), 6),
            "cost_source": str(cost_source),
            "duration_ms": round(float(duration_ms), 2),
            "note": sanitized_note,
        }

        # 1. Primary path: Append to journal.jsonl
        record_telemetry(payload, journal_path=journal_path)

        # 2. If StateManager is active, also register event in state journal
        if state_manager is not None:
            try:
                state_manager.apply(
                    {"last_skill_usage": payload, "event_type": "skill_usage"},
                    source="skill_usage_tracker",
                )
            except Exception as j_err:  # noqa: BLE001
                logger.warning(
                    "Failed to record skill_usage in StateManager: %s", j_err
                )

        # 3. If an active DB session or database_url is provided, persist directly to SQLite
        if db_session is not None:
            record = SkillUsage(
                id=usage_id,
                created_at=now,
                skill_id=skill_id,
                director_id=director_id,
                task_id=task_id,
                parent_task_id=parent_task_id,
                is_ephemeral=bool(is_ephemeral),
                adherence_score=float(adherence_score),
                outcome=str(outcome).upper(),
                gates_passed=bool(gates_passed),
                gates_failed=clean_gates_failed,
                model=str(model),
                prompt_tokens=int(prompt_tokens),
                completion_tokens=int(completion_tokens),
                cost_usd=float(cost_usd),
                cost_source=str(cost_source),
                duration_ms=float(duration_ms),
                note=sanitized_note,
            )
            db_session.add(record)
            db_session.commit()
        elif database_url:
            db_engine = create_engine(database_url)
            ensure_tables(db_engine)
            with Session(db_engine) as sess:
                record = SkillUsage(
                    id=usage_id,
                    created_at=now,
                    skill_id=skill_id,
                    director_id=director_id,
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    is_ephemeral=bool(is_ephemeral),
                    adherence_score=float(adherence_score),
                    outcome=str(outcome).upper(),
                    gates_passed=bool(gates_passed),
                    gates_failed=clean_gates_failed,
                    model=str(model),
                    prompt_tokens=int(prompt_tokens),
                    completion_tokens=int(completion_tokens),
                    cost_usd=float(cost_usd),
                    cost_source=str(cost_source),
                    duration_ms=float(duration_ms),
                    note=sanitized_note,
                )
                sess.add(record)
                sess.commit()

        return payload

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Non-fatal error recording skill usage for task %s: %s", task_id, exc
        )
        return None


def consolidate_skill_usage(
    journal_path: Path | str = Path("data/journal.jsonl"),
    *,
    database_url: str | None = None,
    session: Session | None = None,
) -> int:
    """Consolidate skill usage records from journal.jsonl into SQLite in batch.

    Idempotent: deduplicates by record ID, inserting only new records.
    Returns the count of newly inserted records.
    """
    target_file = Path(journal_path)
    if not target_file.exists():
        return 0

    entries: list[dict[str, Any]] = []
    try:
        with open(target_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                # Extract payload from direct entry or state patch
                if isinstance(data, dict):
                    if data.get("type") == "skill_usage" and "id" in data:
                        entries.append(data)
                    elif (
                        data.get("event_type") == "skill_usage"
                        and "last_skill_usage" in data
                    ):
                        entries.append(data["last_skill_usage"])
                    elif (
                        isinstance(data.get("patch"), dict)
                        and "last_skill_usage" in data["patch"]
                    ):
                        entries.append(data["patch"]["last_skill_usage"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read journal for consolidation: %s", exc)
        return 0

    if not entries:
        return 0

    def _do_consolidate(sess: Session) -> int:
        existing_ids = set(sess.scalars(select(SkillUsage.id)).all())
        new_records: list[SkillUsage] = []
        seen_in_batch: set[str] = set()

        for item in entries:
            rec_id = item.get("id")
            if not rec_id or rec_id in existing_ids or rec_id in seen_in_batch:
                continue
            seen_in_batch.add(rec_id)

            created_raw = item.get("created_at")
            if isinstance(created_raw, str):
                try:
                    created_at = datetime.fromisoformat(created_raw)
                except (ValueError, TypeError):
                    created_at = datetime.now(UTC)
            else:
                created_at = datetime.now(UTC)

            record = SkillUsage(
                id=rec_id,
                created_at=created_at,
                skill_id=item.get("skill_id"),
                director_id=item.get("director_id", "unknown"),
                task_id=item.get("task_id", "unknown"),
                parent_task_id=item.get("parent_task_id"),
                is_ephemeral=bool(item.get("is_ephemeral", False)),
                adherence_score=float(item.get("adherence_score", 0.0)),
                outcome=str(item.get("outcome", "SUCCESS")).upper(),
                gates_passed=bool(item.get("gates_passed", True)),
                gates_failed=list(item.get("gates_failed", [])),
                model=str(item.get("model", "unknown")),
                prompt_tokens=int(item.get("prompt_tokens", 0)),
                completion_tokens=int(item.get("completion_tokens", 0)),
                cost_usd=float(item.get("cost_usd", 0.0)),
                cost_source=str(item.get("cost_source", "litellm")),
                duration_ms=float(item.get("duration_ms", 0.0)),
                note=sanitize_note(item.get("note", "")),
            )
            new_records.append(record)

        if new_records:
            sess.add_all(new_records)
            sess.commit()
        return len(new_records)

    if session is not None:
        return _do_consolidate(session)

    db_url = database_url or DEFAULT_DB_URL
    engine = create_engine(db_url)
    ensure_tables(engine)
    with Session(engine) as sess:
        return _do_consolidate(sess)


def prune_skill_usage(
    days: int | None = None,
    *,
    database_url: str | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """Prune SkillUsage records older than retention days and aggregate into SkillUsageDaily.

    Idempotent: aggregates metrics into SkillUsageDaily per (skill_id, director_id, day)
    before deleting detailed rows.
    """
    retention_days = days if days is not None else get_retention_days()
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    db_url = database_url or DEFAULT_DB_URL

    def _do_prune(sess: Session) -> dict[str, Any]:
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
            return {"pruned_count": 0, "daily_aggregated": 0}

        # Group by (skill_id, director_id, day)
        groups: dict[tuple[str, str, str], list[SkillUsage]] = {}
        for rec in old_records:
            sk_id = rec.skill_id or "unassigned"
            dir_id = rec.director_id or "unassigned"
            day_str = (
                rec.created_at.strftime("%Y-%m-%d") if rec.created_at else "1970-01-01"
            )
            key = (sk_id, dir_id, day_str)
            if key not in groups:
                groups[key] = []
            groups[key].append(rec)

        daily_count = 0
        for (sk_id, dir_id, day_str), recs in groups.items():
            runs = len(recs)
            successes = sum(1 for r in recs if str(r.outcome).upper() == "SUCCESS")
            failures = sum(1 for r in recs if str(r.outcome).upper() == "FAILURE")
            escalations = sum(1 for r in recs if str(r.outcome).upper() == "ESCALATED")
            missing = sum(1 for r in recs if str(r.outcome).upper() == "MISSING_SKILL")
            gates_passed_cnt = sum(1 for r in recs if r.gates_passed)
            total_tokens = sum(r.prompt_tokens + r.completion_tokens for r in recs)
            total_cost_usd = sum(r.cost_usd for r in recs)
            sum_duration = sum(r.duration_ms for r in recs)
            sum_adherence = sum(r.adherence_score for r in recs)

            existing_daily = sess.scalar(
                select(SkillUsageDaily).where(
                    SkillUsageDaily.skill_id == sk_id,
                    SkillUsageDaily.director_id == dir_id,
                    SkillUsageDaily.day == day_str,
                )
            )

            if existing_daily:
                old_runs = existing_daily.runs
                new_runs = old_runs + runs
                existing_daily.runs = new_runs
                existing_daily.successes += successes
                existing_daily.failures += failures
                existing_daily.escalations += escalations
                existing_daily.missing += missing
                existing_daily.total_tokens += total_tokens
                existing_daily.total_cost_usd = round(
                    existing_daily.total_cost_usd + total_cost_usd, 6
                )
                if new_runs > 0:
                    existing_daily.gate_pass_rate = round(
                        (existing_daily.gate_pass_rate * old_runs + gates_passed_cnt)
                        / new_runs,
                        4,
                    )
                    existing_daily.avg_adherence = round(
                        (existing_daily.avg_adherence * old_runs + sum_adherence)
                        / new_runs,
                        4,
                    )
                    existing_daily.avg_duration_ms = round(
                        (existing_daily.avg_duration_ms * old_runs + sum_duration)
                        / new_runs,
                        2,
                    )
                existing_daily.updated_at = datetime.now(UTC)
            else:
                new_daily = SkillUsageDaily(
                    id=str(uuid.uuid4()),
                    skill_id=sk_id,
                    director_id=dir_id,
                    day=day_str,
                    runs=runs,
                    successes=successes,
                    failures=failures,
                    escalations=escalations,
                    missing=missing,
                    gate_pass_rate=round(gates_passed_cnt / runs, 4)
                    if runs > 0
                    else 0.0,
                    avg_adherence=round(sum_adherence / runs, 4) if runs > 0 else 0.0,
                    total_tokens=total_tokens,
                    total_cost_usd=round(total_cost_usd, 6),
                    avg_duration_ms=round(sum_duration / runs, 2) if runs > 0 else 0.0,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
                sess.add(new_daily)
            daily_count += 1

        # Build per-skill summary map for caller convenience and backwards compatibility
        skill_summaries: dict[str, dict[str, Any]] = {}
        for (sk_id, dir_id, day_str), recs in groups.items():
            if sk_id not in skill_summaries:
                skill_summaries[sk_id] = {
                    "total_uses": 0,
                    "successful_uses": 0,
                    "success_rate": 0.0,
                    "total_cost_usd": 0.0,
                }
            s_runs = len(recs)
            s_succ = sum(1 for r in recs if str(r.outcome).upper() == "SUCCESS")
            s_cost = sum(r.cost_usd for r in recs)
            skill_summaries[sk_id]["total_uses"] += s_runs
            skill_summaries[sk_id]["successful_uses"] += s_succ
            skill_summaries[sk_id]["total_cost_usd"] = round(
                skill_summaries[sk_id]["total_cost_usd"] + s_cost, 4
            )

        for sk_id, stats in skill_summaries.items():
            if stats["total_uses"] > 0:
                stats["success_rate"] = round(
                    stats["successful_uses"] / stats["total_uses"], 4
                )

        # Delete pruned detailed rows
        prune_ids = [r.id for r in old_records]
        sess.execute(delete(SkillUsage).where(SkillUsage.id.in_(prune_ids)))
        sess.commit()

        result: dict[str, Any] = {
            "pruned_count": len(prune_ids),
            "daily_aggregated": daily_count,
            "retention_days": retention_days,
        }
        result.update(skill_summaries)
        return result

    if session is not None:
        return _do_prune(session)

    engine = create_engine(db_url)
    ensure_tables(engine)
    with Session(engine) as sess:
        return _do_prune(sess)


def cost_by_skill(
    period_days: int | None = None,
    *,
    database_url: str | None = None,
    session: Session | None = None,
) -> dict[str, dict[str, Any]]:
    """Aggregate token and USD cost by skill across detailed and daily records.

    Returns:
        dict[str, dict[str, Any]]: {skill_id: {"runs": int, "total_cost_usd": float, "total_tokens": int, "avg_cost_usd": float}}
    """
    cutoff = (
        datetime.now(UTC) - timedelta(days=period_days)
        if period_days is not None
        else None
    )
    cutoff_day = cutoff.strftime("%Y-%m-%d") if cutoff is not None else None

    def _do_cost(sess: Session) -> dict[str, dict[str, Any]]:
        # 1. Aggregate from detailed SkillUsage
        usage_stmt = select(
            SkillUsage.skill_id,
            func.count(SkillUsage.id),
            func.sum(SkillUsage.prompt_tokens + SkillUsage.completion_tokens),
            func.sum(SkillUsage.cost_usd),
        ).group_by(SkillUsage.skill_id)

        if cutoff is not None:
            usage_stmt = usage_stmt.where(SkillUsage.created_at >= cutoff)

        usage_rows = sess.execute(usage_stmt).all()

        # 2. Aggregate from SkillUsageDaily
        daily_stmt = select(
            SkillUsageDaily.skill_id,
            func.sum(SkillUsageDaily.runs),
            func.sum(SkillUsageDaily.total_tokens),
            func.sum(SkillUsageDaily.total_cost_usd),
        ).group_by(SkillUsageDaily.skill_id)

        if cutoff_day is not None:
            daily_stmt = daily_stmt.where(SkillUsageDaily.day >= cutoff_day)

        daily_rows = sess.execute(daily_stmt).all()

        result: dict[str, dict[str, Any]] = {}

        for sk_id, runs, tokens, cost in usage_rows:
            key = sk_id or "unassigned"
            if key not in result:
                result[key] = {
                    "runs": 0,
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                    "avg_cost_usd": 0.0,
                }
            result[key]["runs"] += int(runs or 0)
            result[key]["total_tokens"] += int(tokens or 0)
            result[key]["total_cost_usd"] += float(cost or 0.0)

        for sk_id, runs, tokens, cost in daily_rows:
            key = sk_id or "unassigned"
            if key not in result:
                result[key] = {
                    "runs": 0,
                    "total_tokens": 0,
                    "total_cost_usd": 0.0,
                    "avg_cost_usd": 0.0,
                }
            result[key]["runs"] += int(runs or 0)
            result[key]["total_tokens"] += int(tokens or 0)
            result[key]["total_cost_usd"] += float(cost or 0.0)

        for stats in result.values():
            r = stats["runs"]
            c = stats["total_cost_usd"]
            stats["total_cost_usd"] = round(c, 6)
            stats["avg_cost_usd"] = round(c / r, 6) if r > 0 else 0.0

        return result

    if session is not None:
        return _do_cost(session)

    db_url = database_url or DEFAULT_DB_URL
    engine = create_engine(db_url)
    ensure_tables(engine)
    with Session(engine) as sess:
        return _do_cost(sess)
