"""Kanban state machine with audited transitions.

Models task lifecycle as a deterministic state machine:

    todo → ready → in_progress → review → done
                              ↕
                           blocked

Every transition is validated and recorded in the audit trail.
Now integrated with Linear GraphQL API for cloud state synchronization.
"""
from __future__ import annotations

import logging
import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

class KanbanStatus(str, Enum):
    """Valid Kanban column states."""

    TODO = "todo"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    BLOCKED = "blocked"
    DONE = "done"


# Allowed transitions: source → set of valid targets
_TRANSITIONS: dict[KanbanStatus, frozenset[KanbanStatus]] = {
    KanbanStatus.TODO: frozenset({KanbanStatus.READY, KanbanStatus.BLOCKED}),
    KanbanStatus.READY: frozenset({KanbanStatus.IN_PROGRESS, KanbanStatus.BLOCKED}),
    KanbanStatus.IN_PROGRESS: frozenset({
        KanbanStatus.REVIEW,
        KanbanStatus.BLOCKED,
        KanbanStatus.DONE,
    }),
    KanbanStatus.REVIEW: frozenset({
        KanbanStatus.IN_PROGRESS,
        KanbanStatus.DONE,
        KanbanStatus.BLOCKED,
    }),
    KanbanStatus.BLOCKED: frozenset({
        KanbanStatus.READY,
        KanbanStatus.TODO,
    }),
    KanbanStatus.DONE: frozenset(),  # terminal
}


class TransitionError(ValueError):
    """Raised when a Kanban transition is not allowed."""


class AuditEntry(BaseModel):
    """Single audit record for a Kanban transition."""

    model_config = ConfigDict(extra="ignore")

    timestamp: float = Field(default_factory=time.time)
    from_status: str
    to_status: str
    actor: str = "system"
    reason: str = ""


MISSING_SKILL = "MISSING_SKILL"


class KanbanTask(BaseModel):
    """A single task on the Kanban board."""

    model_config = ConfigDict(extra="ignore")

    task_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    body: str = ""
    status: KanbanStatus = KanbanStatus.TODO
    priority: str = "medium"
    assignee: str = ""
    macro_domain: str = ""
    block_reason: str = ""
    blocked_reason: str | None = None
    parent_task_id: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    audit_trail: list[AuditEntry] = Field(default_factory=list)


class KanbanBoard:
    """In-memory Kanban board with validated transitions.

    Thread-safety is not handled here — callers should use
    the StateManager lock when mutating shared board state.
    """

    def __init__(self, sync_linear: bool = True) -> None:
        self._tasks: dict[str, KanbanTask] = {}
        self._linear = None
        self._sync_linear = sync_linear

    @property
    def linear(self):
        if not self._sync_linear:
            return None
        if self._linear is None:
            from src.orchestration.linear_client import get_linear_client
            self._linear = get_linear_client()
        return self._linear

    @property
    def tasks(self) -> dict[str, KanbanTask]:
        return dict(self._tasks)

    def columns(self) -> dict[str, list[KanbanTask]]:
        """Return tasks grouped by status column. Pulls from Linear if configured."""
        if self.linear:
            try:
                ready_tasks = self.linear.fetch_ready_tasks()
                # Store them locally so we can retrieve them by ID later during transition
                for task in ready_tasks:
                    self._tasks[task.task_id] = task
                
                # If using Linear, we primarily care about the READY column for the scheduler
                return {KanbanStatus.READY.value: ready_tasks}
            except Exception as e:  # noqa: BLE001
                logger.error(f"Failed to fetch tasks from Linear: {e}")

        # Fallback local logic
        cols: dict[str, list[KanbanTask]] = {s.value: [] for s in KanbanStatus}
        for task in self._tasks.values():
            cols[task.status.value].append(task)
        return cols

    def add(self, task: KanbanTask) -> KanbanTask:
        """Add a new task to the board."""
        self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> KanbanTask | None:
        return self._tasks.get(task_id)

    def transition(
        self,
        task_id: str,
        to_status: KanbanStatus,
        *,
        actor: str = "system",
        reason: str = "",
    ) -> KanbanTask:
        """Move a task to *to_status*, recording an audit entry.

        Raises ``TransitionError`` if the move is not allowed.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(f"task {task_id!r} not found")

        from_status = task.status
        if to_status not in _TRANSITIONS.get(from_status, frozenset()):
            raise TransitionError(
                f"transition {from_status.value!r} → {to_status.value!r} "
                f"is not allowed"
            )

        entry = AuditEntry(
            from_status=from_status.value,
            to_status=to_status.value,
            actor=actor,
            reason=reason,
        )
        task.audit_trail.append(entry)
        task.status = to_status
        task.updated_at = time.time()

        if to_status == KanbanStatus.BLOCKED:
            task.block_reason = reason
            task.blocked_reason = reason
        elif from_status == KanbanStatus.BLOCKED:
            task.block_reason = ""
            task.blocked_reason = None

        # Update Linear if configured
        if self.linear:
            state_map = {
                KanbanStatus.TODO: "Todo",
                KanbanStatus.READY: "Todo",
                KanbanStatus.IN_PROGRESS: "In Progress",
                KanbanStatus.REVIEW: "In Review",
                KanbanStatus.DONE: "Done",
                KanbanStatus.BLOCKED: "Canceled"
            }
            target = state_map.get(to_status, "Todo")
            success = self.linear.update_issue_state(task_id, target)
            if success and to_status == KanbanStatus.DONE and reason == "handoff completed":
                self.linear.add_comment(task_id, "Agent execution completed successfully.")

        return task

    def complete(
        self,
        task_id: str,
        *,
        actor: str = "system",
        summary: str = "",
    ) -> KanbanTask:
        """Convenience: move a task to DONE from IN_PROGRESS or REVIEW."""
        return self.transition(
            task_id, KanbanStatus.DONE, actor=actor, reason=summary
        )

    def block(
        self,
        task_id: str,
        *,
        actor: str = "system",
        reason: str = "",
    ) -> KanbanTask:
        """Convenience: block a task."""
        return self.transition(
            task_id, KanbanStatus.BLOCKED, actor=actor, reason=reason
        )

    def unblock(
        self,
        task_id: str,
        *,
        actor: str = "system",
    ) -> KanbanTask:
        """Move a blocked task back to READY."""
        return self.transition(
            task_id, KanbanStatus.READY, actor=actor, reason="unblocked"
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialisable snapshot of the entire board."""
        return {
            "columns": {
                status: [t.model_dump(mode="json") for t in tasks]
                for status, tasks in self.columns().items()
            },
            "total": len(self._tasks),
        }
