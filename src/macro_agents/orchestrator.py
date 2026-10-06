"""Macro orchestration loop.

Executes the full lifecycle of a task across the AGgency pipeline:

    1. Create task  → KanbanBoard
    2. Persist state → StateManager
    3. Route         → SemanticRouter
    4. Compress      → ContextCompressor
    5. Handoff       → Domain agent dispatch
    6. Update state  → StateManager
    7. Quality Gates → Validation

Each step is idempotent and auditable via the journal.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.catalog.skills_engine import DirectorSkillsEngine, SkillMatchResult
from src.database.models import Base, DirectorSkill, SkillCatalog
from src.macro_agents.handoff import litellm_handoff
from src.orchestration.context import compress_context, inject_ephemeral_skill
from src.orchestration.kanban import (
    MISSING_SKILL,
    KanbanBoard,
    KanbanStatus,
    KanbanTask,
    TransitionError,
)
from src.orchestration.linear_client import LinearClient
from src.router.semantic import DEFAULT_DB_URL, RouteAgentResult, route_agent
from src.state import StateManager

logger = logging.getLogger(__name__)


# ── Quality Gate definitions ───────────────────────────────────

@dataclass
class GateResult:
    """Result of a single quality gate evaluation."""

    gate_id: str
    passed: bool
    message: str = ""


@dataclass
class OrchestrationResult:
    """Full result of a macro orchestration run."""

    task_id: str
    status: str
    route: RouteAgentResult | None = None
    skill_match: SkillMatchResult | None = None
    subtask_id: str | None = None
    gates: list[GateResult] = field(default_factory=list)
    state_version: int = 0
    error: str | None = None

    @property
    def all_gates_passed(self) -> bool:
        return all(g.passed for g in self.gates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "route": self.route.model_dump(mode="json") if self.route else None,
            "skill_match": (
                {
                    "matched": self.skill_match.matched,
                    "score": self.skill_match.score,
                    "candidate_id": self.skill_match.candidate_id,
                    "reason": self.skill_match.reason,
                }
                if self.skill_match
                else None
            ),
            "subtask_id": self.subtask_id,
            "gates": [
                {"gate_id": g.gate_id, "passed": g.passed, "message": g.message}
                for g in self.gates
            ],
            "state_version": self.state_version,
            "all_gates_passed": self.all_gates_passed,
            "error": self.error,
        }


# ── Orchestrator ──────────────────────────────────────────────

class MacroOrchestrator:
    """Coordinates the full task lifecycle across the AGgency pipeline.

    Parameters
    ----------
    state_dir:
        Directory for persistent state (passed to ``StateManager``).
    database_url:
        SQLite URL for the agent catalog.
    chroma_path:
        Path to the ChromaDB persistent store.
    source_root:
        Root path for agent prompt Markdown files.
    threshold:
        Minimum routing score to consider a match.
    handoff_fn:
        Optional callback to perform the actual domain agent dispatch.
        Signature: ``(task: KanbanTask, route: RouteAgentResult) -> dict``.
        If ``None``, handoff is simulated (dry-run mode).
    """

    def __init__(
        self,
        *,
        state_dir: Path | str = "state_data",
        database_url: str | None = None,
        chroma_path: Path | str | None = None,
        source_root: Path | str | None = None,
        threshold: float = 0.30,
        handoff_fn: Any | None = litellm_handoff,
    ) -> None:
        self.state = StateManager(state_dir=state_dir)
        self.board = KanbanBoard()
        self.threshold = threshold
        self.handoff_fn = handoff_fn

        # Router config (passthrough to route_agent)
        self._db_url = database_url
        self._chroma = Path(chroma_path) if chroma_path else None
        self._source = Path(source_root) if source_root else None

    def orchestrate_task(
        self,
        task: KanbanTask,
        source_agent: str = "system",
        query: str | None = None,
    ) -> OrchestrationResult:
        """Execute orchestration for an existing task on the board."""
        result = OrchestrationResult(task_id=task.task_id, status="pending")
        query = query or task.title

        try:
            # Gate 1: Task exists
            result.gates.append(
                GateResult(gate_id="GATE1", passed=True, message="task exists")
            )

            # Persist initial state
            self.state.apply(
                {
                    "current_task": task.model_dump(mode="json"),
                    "phase": "routing",
                },
                source=source_agent,
            )

            # Route
            route_result = route_agent(
                query=query,
                threshold=self.threshold,
                database_url=self._db_url,
                chroma_path=self._chroma,
                source_root=self._source,
            )
            result.route = route_result

            result.gates.append(
                GateResult(
                    gate_id="GATE2",
                    passed=route_result.matched,
                    message=(
                        f"routed to {route_result.agent_id} (score={route_result.score})"
                        if route_result.matched
                        else f"no match: {route_result.reason}"
                    ),
                )
            )

            if not route_result.matched:
                self.board.transition(
                    task.task_id,
                    KanbanStatus.BLOCKED,
                    actor="orchestrator",
                    reason="no agent matched query",
                )
                result.status = "blocked"
                self.state.apply(
                    {"phase": "blocked", "block_reason": route_result.reason},
                    source="orchestrator",
                )
                result.state_version = self.state.version
                return result

            # Resolve Director Skill Adherence
            director_id = route_result.agent_id
            task_parts = [task.title]
            if task.body and task.body != task.title:
                task_parts.append(task.body)
            if query and query not in (task.title, task.body):
                task_parts.append(query)
            task_description = " ".join(task_parts).strip()

            db_url = self._db_url or DEFAULT_DB_URL
            engine = create_engine(db_url)
            with Session(engine) as session:
                skills_engine = DirectorSkillsEngine(
                    db_session=session,
                    threshold=0.72,
                )
                skill_match = skills_engine.resolve_skill(director_id, task_description)
            result.skill_match = skill_match

            # Case B: Adherence < 0.72 -> Block task and escalate to Lyra for research on skills.sh
            if not skill_match.matched or skill_match.skill is None:
                if task.status != KanbanStatus.BLOCKED:
                    self.board.transition(
                        task.task_id,
                        KanbanStatus.BLOCKED,
                        actor="orchestrator",
                        reason=MISSING_SKILL,
                    )
                task.blocked_reason = MISSING_SKILL

                # Create sub-task linked to Lyra
                subtask_title = f"Pesquisar no skills.sh especificação homologada para: {task.title}"
                subtask = KanbanTask(
                    title=subtask_title,
                    body=(
                        f"Carência de skill detectada para o Diretor '{director_id}' na tarefa '{task.task_id}'. "
                        f"Candidato mais próximo: {skill_match.candidate_id} (score: {skill_match.score:.4f}). "
                        f"Objetivo: Pesquisar no skills.sh especificação homologada para: {task.title}"
                    ),
                    priority=task.priority,
                    assignee="lyra",
                    macro_domain="research",
                    parent_task_id=task.task_id,
                )
                self.board.add(subtask)
                result.subtask_id = subtask.task_id

                self.state.apply(
                    {
                        "phase": "blocked",
                        "blocked_reason": MISSING_SKILL,
                        "target_agent": director_id,
                        "missing_skill_query": task.title,
                        "subtask_id": subtask.task_id,
                        "candidate_skill_id": skill_match.candidate_id,
                        "candidate_score": skill_match.score,
                    },
                    source="orchestrator",
                )
                result.status = "blocked"
                result.error = MISSING_SKILL
                result.state_version = self.state.version
                return result

            # Case A: Adherence >= 0.72 -> Fast Path
            if task.status == KanbanStatus.TODO:
                self.board.transition(task.task_id, KanbanStatus.READY, actor="orchestrator")
            if task.status == KanbanStatus.READY:
                self.board.transition(task.task_id, KanbanStatus.IN_PROGRESS, actor="orchestrator")

            # Compress context and inject ephemeral skill
            handoff_context = {
                "task_id": task.task_id,
                "title": task.title,
                "body": task.body,
                "priority": task.priority,
                "macro_domain": route_result.macro_domain,
                "source_agent": source_agent,
                "target_agent": route_result.agent_id,
                "system_prompt": route_result.system_prompt,
                "state_version": self.state.version,
            }
            handoff_context = inject_ephemeral_skill(handoff_context, skill_match.skill)
            compressed = compress_context(handoff_context)
            original_size = len(str(handoff_context))
            compressed_size = len(str(compressed))
            result.gates.append(
                GateResult(
                    gate_id="GATE3",
                    passed=True,
                    message=f"compressed {original_size}→{compressed_size} chars",
                )
            )

            # Handoff
            if self.handoff_fn is not None:
                handoff_result = self.handoff_fn(task, route_result)
            else:
                handoff_result = {
                    "status": "simulated",
                    "agent_id": route_result.agent_id,
                    "timestamp": time.time(),
                }

            self.state.apply(
                {
                    "phase": "completed",
                    "handoff_result": handoff_result,
                    "route_score": route_result.score,
                    "target_agent": route_result.agent_id,
                },
                source="orchestrator",
            )

            self.board.transition(task.task_id, KanbanStatus.REVIEW, actor="orchestrator")
            
            # Post comment to Linear
            try:
                linear = LinearClient()
                if handoff_result.get("status") == "success":
                    comment_body = (
                        f"**Task executed by {route_result.agent_id}**\n\n"
                        f"{handoff_result.get('reply', 'No reply content.')}"
                    )
                else:
                    error_msg = handoff_result.get('error', 'Unknown error')
                    comment_body = f"⚠️ **Task execution failed:** {error_msg}\n\nPlease check the agent logs and `.env` credentials."
                linear.add_comment(task.task_id, comment_body)
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to post comment to Linear: {e}")

            self.board.transition(task.task_id, KanbanStatus.DONE, actor="orchestrator", reason="handoff completed")

            current_state = self.state.get()
            version_ok = current_state.get("state_version", 0) > 0
            phase_ok = current_state.get("phase") == "completed"
            result.gates.append(
                GateResult(
                    gate_id="GATE4",
                    passed=version_ok and phase_ok,
                    message=f"state_version={current_state.get('state_version')}, phase={current_state.get('phase')}",
                )
            )

            result.status = "completed"
            result.state_version = self.state.version

        except TransitionError as exc:
            result.status = "error"
            result.error = f"transition error: {exc}"
            logger.error("Orchestration transition error: %s", exc)
        except Exception as exc:
            result.status = "error"
            result.error = str(exc)
            logger.exception("Orchestration error")

        return result

    def on_skill_discovered(
        self,
        director_id: str,
        skill_md: str,
        task_id: str | None = None,
        *,
        skill_id: str | None = None,
        skill_name: str | None = None,
        description: str | None = None,
        token_budget: int = 800,
    ) -> SkillCatalog:
        """Persist discovered skill from skills.sh, link to director, and unblock the original task to READY."""
        parsed_id = skill_id
        parsed_name = skill_name
        parsed_desc = description

        # Extract name and description from frontmatter if present
        if "---" in skill_md:
            try:
                import yaml
                parts = skill_md.split("---", 2)
                if len(parts) >= 3:
                    meta = yaml.safe_load(parts[1]) or {}
                    if isinstance(meta, dict):
                        parsed_id = parsed_id or meta.get("name") or meta.get("id")
                        parsed_name = parsed_name or meta.get("name")
                        parsed_desc = parsed_desc or meta.get("description")
            except (yaml.YAMLError, ValueError, TypeError):
                pass

        final_id = parsed_id or f"skill-{int(time.time())}"
        final_name = parsed_name or final_id.replace("-", " ").title()
        final_desc = parsed_desc or f"Skill discovered via skills.sh for {director_id}"

        # Persist in database & associate to director
        db_url = self._db_url or DEFAULT_DB_URL
        engine = create_engine(db_url)
        Base.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as session:
            existing_skill = session.scalar(select(SkillCatalog).where(SkillCatalog.id == final_id))
            if existing_skill:
                existing_skill.name = final_name
                existing_skill.description = final_desc
                existing_skill.content_md = skill_md
                existing_skill.token_budget = token_budget
                existing_skill.source = "skills.sh"
                saved_skill = existing_skill
            else:
                saved_skill = SkillCatalog(
                    id=final_id,
                    name=final_name,
                    description=final_desc,
                    content_md=skill_md,
                    token_budget=token_budget,
                    source="skills.sh",
                )
                session.add(saved_skill)
            session.flush()

            existing_link = session.scalar(
                select(DirectorSkill).where(
                    DirectorSkill.director_id == director_id,
                    DirectorSkill.skill_id == final_id,
                )
            )
            if not existing_link:
                new_link = DirectorSkill(
                    director_id=director_id,
                    skill_id=final_id,
                    is_core=False,
                    load_priority=1,
                )
                session.add(new_link)
            session.commit()

        # Reactivate original task to READY on the Kanban board
        target_tasks: list[KanbanTask] = []
        if task_id:
            t = self.board.get(task_id)
            if t and t.status == KanbanStatus.BLOCKED:
                target_tasks.append(t)
        else:
            for t in self.board.tasks.values():
                if t.status == KanbanStatus.BLOCKED and (
                    t.blocked_reason == MISSING_SKILL or t.block_reason == MISSING_SKILL
                ):
                    target_tasks.append(t)

        for t in target_tasks:
            self.board.unblock(t.task_id, actor="lyra")

        self.state.apply(
            {
                "phase": "skill_discovered",
                "director_id": director_id,
                "skill_id": final_id,
                "unblocked_tasks": [t.task_id for t in target_tasks],
            },
            source="lyra",
        )

        return saved_skill

    def orchestrate(
        self,
        task_data: dict[str, Any] | KanbanTask | None = None,
        *,
        title: str | None = None,
        body: str = "",
        query: str | None = None,
        priority: str = "medium",
        assignee: str = "",
        source_agent: str = "system",
        **kwargs: Any,
    ) -> OrchestrationResult:
        """Run the full macro orchestration loop for a task."""
        if isinstance(task_data, KanbanTask):
            task = task_data
            if task.task_id not in self.board.tasks:
                self.board.add(task)
            return self.orchestrate_task(task, source_agent=source_agent, query=query)

        if isinstance(task_data, dict):
            task_title = task_data.get("title") or title or "Untitled Task"
            task_body = task_data.get("body", body)
            task_priority = task_data.get("priority", priority)
            task_assignee = task_data.get("assignee", assignee)
            task_query = task_data.get("query") or task_data.get("prompt") or query
            task_source = task_data.get("source_agent", source_agent)
            task = KanbanTask(
                title=task_title,
                body=task_body,
                priority=task_priority,
                assignee=task_assignee,
            )
            self.board.add(task)
            return self.orchestrate_task(task, source_agent=task_source, query=task_query)

        task_title = title or "Untitled Task"
        task = KanbanTask(
            title=task_title,
            body=body,
            priority=priority,
            assignee=assignee,
        )
        self.board.add(task)
        return self.orchestrate_task(task, source_agent=source_agent, query=query)

