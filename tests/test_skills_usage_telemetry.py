"""Tests for SkillUsage telemetry, privacy sanitization, async journal consolidation,
retention pruning with daily aggregation, Bayesian router weighting, Sterling cost reports,
and distiller evidence-based proposals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.catalog.skills_engine import DirectorSkillsEngine
from src.database.models import (
    Agent,
    Base,
    DirectorSkill,
    SkillCatalog,
    SkillUsage,
    SkillUsageDaily,
)
from src.database.usage import (
    consolidate_skill_usage,
    cost_by_skill,
    prune_skill_usage,
    record_skill_usage,
    sanitize_note,
)
from src.evolution.distiller import propose_skill_improvements
from src.governance.limits import get_sterling_cost_report


@pytest.fixture
def temp_db(tmp_path: Path):
    """Create an isolated SQLite database."""
    db_path = tmp_path / "test_telemetry.db"
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    return {"engine": engine, "db_url": db_url, "db_path": db_path}


def test_sanitizer_removes_sensitive_data_and_truncates():
    """Test 2: Sanitizer removes emails, phones, CPF/CNPJ, tokens, query URLs, and truncates to 280 chars."""
    raw_note = (
        "User contact: user.test@enterprise.com.br, phone (11) 98765-4321, CPF 123.456.789-00, "
        "CNPJ 12.345.678/0001-90, API key sk-proj-1234567890abcdef1234567890, "
        "docs at https://api.service.internal/docs?token=secret123&env=prod. "
        + "Extra detailed explanation "
        * 20
    )

    clean = sanitize_note(raw_note)

    assert "user.test@enterprise.com.br" not in clean
    assert "98765-4321" not in clean
    assert "123.456.789-00" not in clean
    assert "12.345.678/0001-90" not in clean
    assert "sk-proj-1234567890abcdef1234567890" not in clean
    assert "token=secret123" not in clean
    assert "[redacted]" in clean
    assert len(clean) <= 280


def test_record_skill_usage_async_journal_and_consolidation(temp_db, tmp_path: Path):
    """Test 1 & 3: Fast journal append, non-blocking, and idempotent batch consolidation into SQLite."""
    journal_file = tmp_path / "journal.jsonl"
    engine = temp_db["engine"]

    # Record 3 events to journal (fast path, no direct db commit)
    res1 = record_skill_usage(
        director_id="vulcan",
        task_id="task-001",
        outcome="SUCCESS",
        skill_id="k8s-deploy",
        adherence_score=0.88,
        gates_passed=True,
        prompt_tokens=150,
        completion_tokens=50,
        cost_usd=0.0012,
        duration_ms=450.0,
        note="Kubernetes deployment succeeded on staging",
        journal_path=journal_file,
    )
    res2 = record_skill_usage(
        director_id="vulcan",
        task_id="task-002",
        outcome="FAILURE",
        skill_id="k8s-deploy",
        adherence_score=0.75,
        gates_passed=False,
        gates_failed=["GATE2_LINT"],
        prompt_tokens=120,
        completion_tokens=30,
        cost_usd=0.0008,
        duration_ms=320.0,
        note="Lint checks failed with syntax errors",
        journal_path=journal_file,
    )

    assert res1 is not None
    assert res2 is not None
    assert journal_file.exists()

    # Consolidate into SQLite
    with Session(engine) as session:
        initial_count = len(session.scalars(select(SkillUsage)).all())
        assert initial_count == 0

        added_first = consolidate_skill_usage(
            journal_path=journal_file, session=session
        )
        assert added_first == 2

        rows = list(session.scalars(select(SkillUsage)).all())
        assert len(rows) == 2
        assert rows[0].task_id in ("task-001", "task-002")

        # Second consolidation run must be idempotent (0 new rows)
        added_second = consolidate_skill_usage(
            journal_path=journal_file, session=session
        )
        assert added_second == 0
        assert len(session.scalars(select(SkillUsage)).all()) == 2


def test_io_failure_in_journal_does_not_raise(monkeypatch: pytest.MonkeyPatch):
    """Test 3: An I/O error while writing to journal logs a warning and does not crash."""

    def fail_telemetry(*args, **kwargs):
        raise OSError("Disk full or permission denied")

    monkeypatch.setattr("src.database.usage.record_telemetry", fail_telemetry)

    # Should not raise exception
    res = record_skill_usage(
        director_id="vulcan",
        task_id="task-io-fail",
        outcome="SUCCESS",
        skill_id="playwright-runner",
    )
    assert res is None


def test_retention_pruning_aggregates_into_daily_and_is_idempotent(temp_db):
    """Test 4: prune_skill_usage aggregates detailed records into SkillUsageDaily before deleting."""
    engine = temp_db["engine"]
    now = datetime.now(UTC)
    old_date = now - timedelta(days=70)
    day_str = old_date.strftime("%Y-%m-%d")

    with Session(engine) as session:
        # Seed 3 old records (older than 60 days retention)
        r1 = SkillUsage(
            id="old-001",
            created_at=old_date,
            skill_id="sec-audit",
            director_id="sterling",
            task_id="t1",
            outcome="SUCCESS",
            gates_passed=True,
            adherence_score=0.90,
            prompt_tokens=200,
            completion_tokens=100,
            cost_usd=0.0030,
            duration_ms=500.0,
        )
        r2 = SkillUsage(
            id="old-002",
            created_at=old_date,
            skill_id="sec-audit",
            director_id="sterling",
            task_id="t2",
            outcome="FAILURE",
            gates_passed=False,
            gates_failed=["GATE1"],
            adherence_score=0.80,
            prompt_tokens=150,
            completion_tokens=50,
            cost_usd=0.0020,
            duration_ms=400.0,
        )
        # Recent record (10 days old, should NOT be pruned)
        r3 = SkillUsage(
            id="recent-001",
            created_at=now - timedelta(days=10),
            skill_id="sec-audit",
            director_id="sterling",
            task_id="t3",
            outcome="SUCCESS",
            gates_passed=True,
            adherence_score=0.95,
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.0010,
            duration_ms=250.0,
        )
        session.add_all([r1, r2, r3])
        session.commit()

        # Prune with 60 days retention
        prune_res = prune_skill_usage(days=60, session=session)
        assert prune_res["pruned_count"] == 2
        assert prune_res["daily_aggregated"] == 1

        # Check remaining detailed rows
        remaining = list(session.scalars(select(SkillUsage)).all())
        assert len(remaining) == 1
        assert remaining[0].id == "recent-001"

        # Check SkillUsageDaily aggregate
        daily = session.scalar(
            select(SkillUsageDaily).where(SkillUsageDaily.skill_id == "sec-audit")
        )
        assert daily is not None
        assert daily.day == day_str
        assert daily.runs == 2
        assert daily.successes == 1
        assert daily.failures == 1
        assert daily.total_tokens == 500
        assert daily.total_cost_usd == 0.0050
        assert daily.gate_pass_rate == 0.50
        assert daily.avg_adherence == 0.85

        # Second prune run must be idempotent
        second_prune = prune_skill_usage(days=60, session=session)
        assert second_prune["pruned_count"] == 0
        assert len(session.scalars(select(SkillUsage)).all()) == 1


def test_router_bayesian_smoothed_success_rate(
    temp_db, monkeypatch: pytest.MonkeyPatch
):
    """Test 5: Router prefers a skill with good history over slightly higher similarity, and uses similarity for < 3 runs."""
    engine = temp_db["engine"]
    monkeypatch.setenv("ROUTER_SUCCESS_WEIGHT", "0.3")

    with Session(engine) as session:
        # Create director and skills
        agent = Agent(
            agent_id="vulcan",
            macro_domain="engineering",
            name="Vulcan",
            system_prompt_path="/tmp/vulcan.md",
            trigger_hooks=[],
        )
        s1 = SkillCatalog(
            id="skill-reliable",
            name="Docker Container Optimizer",
            description="Optimize and package Docker images efficiently",
            content_md="# Docker",
        )
        s2 = SkillCatalog(
            id="skill-unreliable",
            name="Container Docker Builder",
            description="Build and push Docker containers",
            content_md="# Docker Builder",
        )
        s3 = SkillCatalog(
            id="skill-new",
            name="Kubernetes Operator Scaffold",
            description="Scaffold K8s operators",
            content_md="# K8s Scaffold",
        )
        session.add_all([agent, s1, s2, s3])
        session.flush()

        session.add(
            DirectorSkill(
                director_id="vulcan",
                skill_id="skill-reliable",
                is_core=True,
                load_priority=1,
            )
        )
        session.add(
            DirectorSkill(
                director_id="vulcan",
                skill_id="skill-unreliable",
                is_core=True,
                load_priority=1,
            )
        )
        session.add(
            DirectorSkill(
                director_id="vulcan",
                skill_id="skill-new",
                is_core=True,
                load_priority=1,
            )
        )

        # Seed history:
        # skill-reliable: 10 runs, 10 successes -> smoothed rate = (10 + 2.5)/15 = 0.8333
        # skill-unreliable: 10 runs, 1 success  -> smoothed rate = (1 + 2.5)/15 = 0.2333
        for i in range(10):
            session.add(
                SkillUsage(
                    id=f"rel-{i}",
                    created_at=datetime.now(UTC),
                    skill_id="skill-reliable",
                    director_id="vulcan",
                    task_id=f"t-rel-{i}",
                    outcome="SUCCESS",
                    gates_passed=True,
                )
            )
            session.add(
                SkillUsage(
                    id=f"unrel-{i}",
                    created_at=datetime.now(UTC),
                    skill_id="skill-unreliable",
                    director_id="vulcan",
                    task_id=f"t-unrel-{i}",
                    outcome="SUCCESS" if i == 0 else "FAILURE",
                    gates_passed=i == 0,
                )
            )
        session.commit()

        # Mock embedder with controlled similarities
        def mock_embedder(texts):
            # Query prompt
            if len(texts) == 1:
                return [[1.0, 0.0, 0.0]]
            # 3 authorized skills
            # skill-reliable: sim = 0.80
            # skill-unreliable: sim = 0.82 (slightly higher raw similarity)
            # skill-new: sim = 0.75 (only 0 runs -> uses raw similarity 0.75)
            return [
                [0.80, 0.60, 0.0],  # skill-reliable -> cos = 0.80
                [0.82, 0.5723, 0.0],  # skill-unreliable -> cos = 0.82
                [0.75, 0.6614, 0.0],  # skill-new -> cos = 0.75
            ]

        engine_router = DirectorSkillsEngine(
            db_session=session, embedder=mock_embedder, threshold=0.70
        )
        match = engine_router.resolve_skill("vulcan", "Build Docker image")

        # skill-reliable final score = 0.80 * 0.7 + 0.8333 * 0.3 = 0.56 + 0.25 = 0.81
        # skill-unreliable final score = 0.82 * 0.7 + 0.2333 * 0.3 = 0.574 + 0.07 = 0.644
        assert match.matched is True
        assert match.candidate_id == "skill-reliable"
        assert match.score > 0.80


def test_distiller_proposes_improvements_with_evidence_and_rejects_without_evidence(
    temp_db,
):
    """Test 6: Distiller proposes changes when >= 3 failures share pattern, and nothing without evidence."""
    engine = temp_db["engine"]
    now = datetime.now(UTC)

    with Session(engine) as session:
        # Case 1: No failures -> Empty proposal list
        proposals_empty = propose_skill_improvements(session=session, min_failures=3)
        assert proposals_empty == []

        # Case 2: 3 failures for skill-db-migrator with the same gate failure ("GATE_SCHEMA_CHECK")
        u1 = SkillUsage(
            id="ev-001",
            created_at=now - timedelta(days=2),
            skill_id="skill-db-migrator",
            director_id="vulcan",
            task_id="t1",
            outcome="FAILURE",
            gates_passed=False,
            gates_failed=["GATE_SCHEMA_CHECK"],
            note="Migration halted: schema check failed",
        )
        u2 = SkillUsage(
            id="ev-002",
            created_at=now - timedelta(days=3),
            skill_id="skill-db-migrator",
            director_id="vulcan",
            task_id="t2",
            outcome="FAILURE",
            gates_passed=False,
            gates_failed=["GATE_SCHEMA_CHECK"],
            note="Foreign key schema mismatch",
        )
        u3 = SkillUsage(
            id="ev-003",
            created_at=now - timedelta(days=5),
            skill_id="skill-db-migrator",
            director_id="vulcan",
            task_id="t3",
            outcome="FAILURE",
            gates_passed=False,
            gates_failed=["GATE_SCHEMA_CHECK"],
            note="Column type inconsistency on target schema",
        )
        # 1 isolated failure for another skill (should NOT trigger proposal)
        u4 = SkillUsage(
            id="ev-004",
            created_at=now - timedelta(days=1),
            skill_id="skill-isolated",
            director_id="aura",
            task_id="t4",
            outcome="FAILURE",
            gates_passed=False,
            gates_failed=["GATE_UI"],
            note="UI element missing",
        )
        session.add_all([u1, u2, u3, u4])
        session.commit()

        proposals = propose_skill_improvements(session=session, min_failures=3)
        assert len(proposals) == 1
        p = proposals[0]
        assert p.skill_id == "skill-db-migrator"
        assert p.director_id == "vulcan"
        assert p.failure_count == 3
        assert p.failed_gate == "GATE_SCHEMA_CHECK"
        assert sorted(p.evidence_usage_ids) == ["ev-001", "ev-002", "ev-003"]
        assert "ev-004" not in p.evidence_usage_ids


def test_sterling_cost_by_skill_aggregation(temp_db):
    """Test 7: Sterling cost_by_skill accurately sums detailed and daily records."""
    engine = temp_db["engine"]

    with Session(engine) as session:
        # Detailed rows
        session.add(
            SkillUsage(
                id="c-001",
                created_at=datetime.now(UTC),
                skill_id="data-pipeline",
                director_id="sterling",
                task_id="t1",
                outcome="SUCCESS",
                prompt_tokens=1000,
                completion_tokens=500,
                cost_usd=0.0150,
            )
        )
        # Daily aggregated row
        session.add(
            SkillUsageDaily(
                id="d-001",
                skill_id="data-pipeline",
                director_id="sterling",
                day="2026-09-01",
                runs=4,
                successes=4,
                failures=0,
                total_tokens=4500,
                total_cost_usd=0.0450,
            )
        )
        session.commit()

        costs = cost_by_skill(session=session)
        assert "data-pipeline" in costs
        dp = costs["data-pipeline"]
        assert dp["runs"] == 5
        assert dp["total_tokens"] == 6000
        assert dp["total_cost_usd"] == 0.0600
        assert dp["avg_cost_usd"] == 0.0120

        # Verify Sterling governance report integration
        report = get_sterling_cost_report(database_url=temp_db["db_url"])
        assert report["total_runs"] == 5
        assert report["total_tokens"] == 6000
        assert report["total_cost_usd"] == 0.0600
        assert "data-pipeline" in report["skills"]
