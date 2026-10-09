"""Ephemeral Agent execution engine.

Ephemeral agents are short-lived, single-purpose subagents spawned to execute
a specialized subtask using a specific skill from the catalog.

Lifecycle:
1. Create a child subtask on the Kanban board (with parent_task_id) and transition to IN_PROGRESS.
2. Load ONLY the specified skill and subtask:
   - NO director SOUL.md
   - NO conversation history
   - NO Obsidian long-term memory
3. Perform a single LLM call without tools (enforcing max_depth=1).
4. Transition subtask to DONE (or BLOCKED on error), return output to director,
   and discard all ephemeral state.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import litellm
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from src.database.models import SkillCatalog
from src.gateway.models import estimate_cost, resolve_model
from src.orchestration.kanban import (
    MISSING_SKILL,
    KanbanBoard,
    KanbanStatus,
    KanbanTask,
)
from src.router.semantic import DEFAULT_DB_URL

logger = logging.getLogger(__name__)


SPAWN_EPHEMERAL_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "spawn_ephemeral",
        "description": (
            "Spawn an ephemeral subagent to execute a specialized subtask "
            "using a specific skill from the catalog."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": "The ID of the skill from the skills catalog to be loaded for the ephemeral agent.",
                },
                "subtask_title": {
                    "type": "string",
                    "description": "Title of the subtask to execute.",
                },
                "subtask_body": {
                    "type": "string",
                    "description": "Detailed instructions and requirements for the subtask.",
                },
                "expected_output": {
                    "type": "string",
                    "description": "Description of the expected output format and deliverables.",
                },
            },
            "required": ["skill_id", "subtask_title", "subtask_body", "expected_output"],
        },
    },
}


def execute_ephemeral_task(
    *,
    skill_id: str,
    subtask_title: str,
    subtask_body: str,
    expected_output: str,
    parent_task: KanbanTask,
    board: KanbanBoard | None = None,
    db_session: Session | None = None,
    database_url: str | None = None,
    model_override: str | None = None,
    director_id: str | None = None,
    limits: Any | None = None,
    usage_recorder: Any | None = None,
) -> dict[str, Any]:
    """Execute a single ephemeral subtask in complete isolation."""
    start_time = time.time()
    logger.info("Spawning ephemeral agent for skill '%s' (parent=%s)", skill_id, parent_task.task_id)

    # 1. Create child subtask on Kanban
    subtask = KanbanTask(
        title=subtask_title,
        body=f"{subtask_body}\n\nExpected Output:\n{expected_output}".strip(),
        priority=parent_task.priority,
        assignee="ephemeral",
        macro_domain=parent_task.macro_domain,
        parent_task_id=parent_task.task_id,
    )
    if board is not None:
        board.add(subtask)
        if subtask.status == KanbanStatus.TODO:
            board.transition(subtask.task_id, KanbanStatus.READY, actor="director", reason="spawned ephemeral")
        if subtask.status == KanbanStatus.READY:
            board.transition(subtask.task_id, KanbanStatus.IN_PROGRESS, actor="director", reason="ephemeral executing")

    # 2. Check limits if tracker/limits is passed
    if limits is not None:
        can_spawn, limit_reason = limits.can_spawn_ephemeral()
        if not can_spawn:
            logger.warning("Ephemeral spawn limit reached: %s", limit_reason)
            if board is not None:
                board.transition(subtask.task_id, KanbanStatus.BLOCKED, actor="governance", reason="limit_exceeded")
                subtask.blocked_reason = "limit_exceeded"
            
            error_output = f"limit_exceeded: Ephemeral execution aborted ({limit_reason})."
            if usage_recorder:
                usage_recorder(
                    skill_id=skill_id,
                    director_id=director_id or parent_task.assignee or "unknown",
                    task_id=subtask.task_id,
                    parent_task_id=parent_task.task_id,
                    is_ephemeral=True,
                    outcome="limit_exceeded",
                    model=model_override or "unknown",
                    prompt_tokens=0,
                    completion_tokens=0,
                    cost_usd=0.0,
                    duration_ms=(time.time() - start_time) * 1000.0,
                    note="Execution aborted: limit_exceeded",
                )
            return {
                "status": "limit_exceeded",
                "subtask_id": subtask.task_id,
                "error": error_output,
                "output": error_output,
            }

    # 3. Lookup skill in catalog
    db_url = database_url or DEFAULT_DB_URL
    skill: SkillCatalog | None = None

    if db_session is not None:
        skill = db_session.scalar(select(SkillCatalog).where(SkillCatalog.id == skill_id))
    else:
        engine = create_engine(db_url)
        with Session(engine) as session:
            skill = session.scalar(select(SkillCatalog).where(SkillCatalog.id == skill_id))

    if skill is None:
        logger.warning("Skill '%s' not found in catalog during ephemeral execution", skill_id)
        if board is not None:
            board.transition(subtask.task_id, KanbanStatus.BLOCKED, actor="orchestrator", reason=MISSING_SKILL)
            subtask.blocked_reason = MISSING_SKILL
            
            # Create Lyra escalation subtask
            lyra_subtask = KanbanTask(
                title=f"Pesquisar no skills.sh especificação homologada para: {subtask_title}",
                body=(
                    f"Carência de skill '{skill_id}' detectada durante execução efêmera da tarefa '{parent_task.task_id}'. "
                    f"Sub-tarefa: {subtask.task_id}. Objetivo: Pesquisar no skills.sh."
                ),
                priority=subtask.priority,
                assignee="lyra",
                macro_domain="research",
                parent_task_id=parent_task.task_id,
            )
            board.add(lyra_subtask)

        error_msg = f"Error: Skill '{skill_id}' not found in catalog."
        if usage_recorder:
            usage_recorder(
                skill_id=skill_id,
                director_id=director_id or parent_task.assignee or "unknown",
                task_id=subtask.task_id,
                parent_task_id=parent_task.task_id,
                is_ephemeral=True,
                outcome="missing_skill",
                model=model_override or "unknown",
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
                duration_ms=(time.time() - start_time) * 1000.0,
                note=f"Skill '{skill_id}' not found in catalog",
            )
        return {
            "status": "missing_skill",
            "subtask_id": subtask.task_id,
            "error": error_msg,
            "output": error_msg,
        }

    # 4. Prepare isolated ephemeral prompts (NO SOUL, NO history, NO Obsidian)
    system_prompt = (
        f"You are a specialized ephemeral agent executing an isolated subtask.\n\n"
        f"## Assigned Skill: {skill.name} ({skill.id})\n"
        f"{skill.content_md}"
    )
    user_prompt = (
        f"Subtask Title: {subtask_title}\n\n"
        f"Subtask Instructions:\n{subtask_body}\n\n"
        f"Expected Output:\n{expected_output}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    # Resolve model for ephemeral execution (always uses the cheapest/free ephemeral tier)
    assigned_director = director_id or parent_task.assignee or "ephemeral"
    resolved = resolve_model("ephemeral")
    model_name = model_override or resolved.model

    completion_kwargs: dict[str, Any] = {
        "messages": messages,
        "temperature": 0.2,  # deterministic execution for skills
        "max_tokens": min(resolved.max_tokens, 2048),
        "api_base": resolved.api_base,
        "timeout": resolved.timeout,
    }
    if resolved.api_key:
        completion_kwargs["api_key"] = resolved.api_key

    # Ephemerals NEVER receive tools (max_depth=1)
    try:
        call_start = time.time()
        candidates = [model_name, *resolved.fallbacks] if model_name == resolved.model else [model_name]
        response = None
        used_model = model_name
        last_error = None

        for candidate in candidates:
            try:
                response = litellm.completion(model=candidate, **completion_kwargs)
                used_model = candidate
                break
            except Exception as attempt_err:  # noqa: BLE001
                last_error = attempt_err
                logger.warning("Ephemeral model attempt failed with %s: %s", candidate, attempt_err)

        if response is None:
            raise last_error or RuntimeError("Ephemeral completion failed: no response")

        duration_ms = (time.time() - call_start) * 1000.0
        reply_text = response.choices[0].message.content or ""
        usage = response.usage.model_dump() if response.usage else {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

        # Calculate cost
        cost_usd, cost_source = estimate_cost(
            model=used_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            completion_response=response,
        )

        if limits is not None:
            limits.record_call(
                tokens=total_tokens,
                cost_usd=cost_usd,
                is_ephemeral=True,
                cost_source=cost_source,
            )

        # Transition subtask to DONE
        if board is not None:
            board.transition(
                subtask.task_id,
                KanbanStatus.DONE,
                actor="ephemeral",
                reason="ephemeral execution completed successfully",
            )

        if usage_recorder:
            note = f"Ephemeral execution of {skill_id} succeeded"[:280]
            usage_recorder(
                skill_id=skill_id,
                director_id=assigned_director,
                task_id=subtask.task_id,
                parent_task_id=parent_task.task_id,
                is_ephemeral=True,
                outcome="success",
                model=used_model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                note=note,
            )

        return {
            "status": "success",
            "subtask_id": subtask.task_id,
            "output": reply_text,
            "model": used_model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": cost_usd,
            "duration_ms": duration_ms,
        }

    except Exception as exc:
        logger.error("Ephemeral execution failed for subtask %s: %s", subtask.task_id, exc)
        if board is not None:
            try:
                board.transition(
                    subtask.task_id,
                    KanbanStatus.BLOCKED,
                    actor="ephemeral",
                    reason=f"execution failure: {exc}",
                )
            except Exception:
                pass
            subtask.blocked_reason = str(exc)

        duration_ms = (time.time() - start_time) * 1000.0
        if usage_recorder:
            usage_recorder(
                skill_id=skill_id,
                director_id=assigned_director,
                task_id=subtask.task_id,
                parent_task_id=parent_task.task_id,
                is_ephemeral=True,
                outcome="failure",
                model=model_name,
                prompt_tokens=0,
                completion_tokens=0,
                cost_usd=0.0,
                duration_ms=duration_ms,
                note=f"Ephemeral error: {exc}"[:280],
            )

        return {
            "status": "failure",
            "subtask_id": subtask.task_id,
            "error": str(exc),
            "output": f"Error executing ephemeral subtask: {exc}",
        }
