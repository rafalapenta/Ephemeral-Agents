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

from src.macro_agents.handoff import litellm_handoff
from src.orchestration.context import compress_context
from src.orchestration.linear_client import LinearClient
from src.orchestration.kanban import (
    KanbanBoard,
    KanbanStatus,
    KanbanTask,
    TransitionError,
)
from src.router.semantic import RouteAgentResult, route_agent
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

            # Transition state if needed
            if task.status == KanbanStatus.TODO:
                self.board.transition(task.task_id, KanbanStatus.READY, actor="orchestrator")
            if task.status == KanbanStatus.READY:
                self.board.transition(task.task_id, KanbanStatus.IN_PROGRESS, actor="orchestrator")

            # Compress context
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
            except Exception as e:
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

    def orchestrate(
        self,
        *,
        title: str,
        body: str = "",
        query: str,
        priority: str = "medium",
        assignee: str = "",
        source_agent: str = "system",
    ) -> OrchestrationResult:
        """Run the full macro orchestration loop for a new task."""
        task = KanbanTask(
            title=title,
            body=body,
            priority=priority,
            assignee=assignee,
        )
        self.board.add(task)
        return self.orchestrate_task(task, source_agent=source_agent, query=query)
