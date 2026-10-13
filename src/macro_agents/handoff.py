"""LiteLLM Handoff module with Ephemeral subagent tool-calling loop."""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import litellm

from src.database.models import DEFAULT_DB_URL
from src.database.usage import record_skill_usage
from src.gateway.models import estimate_cost, resolve_model
from src.governance.limits import EphemeralLimits, record_task_cost
from src.macro_agents.ephemeral import SPAWN_EPHEMERAL_TOOL, execute_ephemeral_task
from src.memory.obsidian import ObsidianMemory
from src.orchestration.context import compress_context, sanitize_context
from src.orchestration.kanban import KanbanBoard, KanbanStatus, KanbanTask
from src.router.semantic import RouteAgentResult
from src.state.manager import StateManager

logger = logging.getLogger(__name__)


@dataclass
class HandoffRuntime:
    """Runtime services and governance objects passed outside prompt context."""

    state_manager: StateManager | None = None
    db_url: str | None = None
    limits: EphemeralLimits | None = None
    adherence_score: float = 1.0
    board: KanbanBoard | None = None


@runtime_checkable
class HandoffFn(Protocol):
    """Standard protocol for handoff execution functions.

    All handoff implementations must accept (task, route, context, runtime).
    """

    def __call__(
        self,
        task: KanbanTask,
        route: RouteAgentResult,
        context: dict[str, Any],
        runtime: HandoffRuntime | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]: ...


def _safe_record_skill_usage(**kwargs) -> None:
    try:
        record_skill_usage(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Non-fatal failure recording skill usage: %s", exc)


def litellm_handoff(
    task: KanbanTask,
    route: RouteAgentResult,
    context: dict[str, Any] | None = None,
    runtime: HandoffRuntime | None = None,
    *,
    board: KanbanBoard | None = None,
    database_url: str | None = None,
    limits: EphemeralLimits | None = None,
    state_manager: StateManager | None = None,
    tools_enabled: bool = True,
) -> dict[str, Any]:
    """Execute the domain agent handoff using LiteLLM with tool-calling loop.

    This constructs a conversation using the agent's system prompt (from SOUL.md
    and any injected ephemeral skills) and task context. Directors receive the
    `spawn_ephemeral` tool, allowing multi-turn orchestration with isolated subagents.
    """
    start_time = time.time()
    logger.info(
        "Initiating LiteLLM handoff for task %s to agent %s",
        task.task_id,
        route.agent_id,
    )

    # Extract helper services strictly from runtime or kwargs, never from context
    active_board = (runtime.board if runtime else None) or board
    db_url = (
        (runtime.db_url if runtime else None)
        or database_url
        or os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    )
    active_limits = (runtime.limits if runtime else None) or limits or EphemeralLimits()
    active_state = (runtime.state_manager if runtime else None) or state_manager
    adherence = (runtime.adherence_score if runtime else None) or (
        context.get("adherence_score") if context else 1.0
    )

    # 1. Fetch memory from Obsidian
    obsidian = ObsidianMemory()
    memory_context = obsidian.get_agent_context(route.agent_id)

    # 2. Build the messages payload
    sys_prompt = (
        (context.get("system_prompt") if context else None)
        or route.system_prompt
        or "You are a helpful AI assistant."
    )
    if memory_context:
        sys_prompt = f"{sys_prompt}\n\n{memory_context}"

    if context:
        sanitized = sanitize_context(context)
        user_context = {k: v for k, v in sanitized.items() if k != "system_prompt"}
    else:
        handoff_context = {
            "task_id": task.task_id,
            "title": task.title,
            "body": task.body,
            "priority": task.priority,
            "macro_domain": route.macro_domain,
            "target_agent": route.agent_id,
        }
        user_context = sanitize_context(compress_context(handoff_context))

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": sys_prompt},
        {
            "role": "user",
            "content": f"Please execute the following task.\n\nContext:\n{json.dumps(user_context, indent=2)}",
        },
    ]

    # 3. Resolve the per-agent model target
    resolved = resolve_model(route.agent_id)
    logger.info(
        "Handoff %s -> %s (tier=%s, api_base=%s, fallbacks=%d)",
        route.agent_id,
        resolved.model,
        resolved.tier,
        resolved.api_base,
        len(resolved.fallbacks),
    )

    completion_kwargs: dict[str, Any] = {
        "temperature": resolved.temperature,
        "max_tokens": resolved.max_tokens,
        "api_base": resolved.api_base,
        "timeout": resolved.timeout,
    }
    if resolved.api_key:
        completion_kwargs["api_key"] = resolved.api_key
    if tools_enabled:
        completion_kwargs["tools"] = [SPAWN_EPHEMERAL_TOOL]
        completion_kwargs["tool_choice"] = "auto"

    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_cost_usd = 0.0
    tool_calls_executed = 0
    final_reply = ""
    used_model = resolved.model
    last_error: Exception | None = None

    try:
        # Tool execution loop
        while True:
            # Check limits before next iteration
            can_iterate, limit_reason = active_limits.can_iterate()
            if not can_iterate:
                logger.warning(
                    "Governance limit hit for task %s: %s", task.task_id, limit_reason
                )
                if active_board is not None:
                    try:
                        active_board.transition(
                            task.task_id,
                            KanbanStatus.BLOCKED,
                            actor="governance",
                            reason="limit_exceeded",
                        )
                    except Exception:  # noqa: BLE001, S110
                        pass
                    task.blocked_reason = "limit_exceeded"

                # Record skill usage with LIMIT_EXCEEDED
                skill_id = (
                    context.get("ephemeral_skill", {}).get("id")
                    if context and isinstance(context.get("ephemeral_skill"), dict)
                    else None
                )
                _safe_record_skill_usage(
                    director_id=route.agent_id,
                    task_id=task.task_id,
                    skill_id=skill_id,
                    is_ephemeral=False,
                    adherence_score=adherence,
                    outcome="LIMIT_EXCEEDED",
                    gates_passed=False,
                    gates_failed=["LIMIT_EXCEEDED"],
                    model=used_model,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                    cost_usd=total_cost_usd,
                    cost_source=active_limits.cost_source,
                    duration_ms=(time.time() - start_time) * 1000.0,
                    note=f"Task halted: {limit_reason}"[:280],
                    state_manager=active_state,
                    database_url=db_url,
                )
                cost_rep = active_limits.to_report()
                record_task_cost(task.task_id, cost_rep)

                return {
                    "status": "limit_exceeded",
                    "agent_id": route.agent_id,
                    "model": used_model,
                    "error": f"limit_exceeded: {limit_reason}",
                    "reply": f"Execution halted: {limit_reason}",
                    "cost_report": cost_rep,
                    "timestamp": time.time(),
                }

            # Call LLM with fallback candidates and retry on 429 / rate limits
            candidates = [resolved.model, *resolved.fallbacks]
            response = None

            def _is_rate_limit(err: Exception) -> bool:
                err_str = str(err).lower()
                return (
                    isinstance(
                        err,
                        (
                            getattr(litellm, "RateLimitError", type(None)),
                            getattr(
                                getattr(litellm, "exceptions", None),
                                "RateLimitError",
                                type(None),
                            ),
                        ),
                    )
                    or "429" in err_str
                    or "rate limit" in err_str
                    or "rate_limit" in err_str
                )

            for index, candidate in enumerate(candidates):
                # 1 retry with backoff on 429 / rate limit
                for attempt in range(2):
                    try:
                        response = litellm.completion(
                            model=candidate,
                            messages=messages,
                            **completion_kwargs,
                        )
                        used_model = candidate
                        if index > 0:
                            logger.warning(
                                "Primary model unavailable; served by fallback %s",
                                candidate,
                            )
                        break
                    except Exception as attempt_error:  # noqa: BLE001
                        last_error = attempt_error
                        if _is_rate_limit(attempt_error) and attempt == 0:
                            backoff = float(
                                os.getenv("AGENCY_RETRY_BACKOFF_SECONDS", "1.0")
                            )
                            logger.warning(
                                "Rate limit (429) on %s; retrying with backoff (%.1fs)...",
                                candidate,
                                backoff,
                            )
                            time.sleep(backoff)
                            continue
                        logger.warning(
                            "Model %s failed (%s: %s)%s",
                            candidate,
                            type(attempt_error).__name__,
                            attempt_error,
                            "; trying next fallback"
                            if index + 1 < len(candidates)
                            else "",
                        )
                        break
                if response is not None:
                    break

            if response is None:
                if last_error and _is_rate_limit(last_error):
                    escalation_msg = f"Rate limit / 429 persistiu após retry e fallbacks: {last_error}"
                    logger.error("Escalating task %s: %s", task.task_id, escalation_msg)
                    if active_board is not None:
                        try:
                            active_board.transition(
                                task.task_id,
                                KanbanStatus.ESCALATED,
                                actor="gateway",
                                reason=escalation_msg[:200],
                            )
                        except Exception:
                            try:
                                active_board.transition(
                                    task.task_id,
                                    KanbanStatus.BLOCKED,
                                    actor="gateway",
                                    reason=escalation_msg[:200],
                                )
                            except Exception:
                                pass
                        task.blocked_reason = escalation_msg

                    duration = time.time() - start_time
                    cost_rep = active_limits.to_report()
                    record_task_cost(task.task_id, cost_rep)

                    skill_id = (
                        context.get("ephemeral_skill", {}).get("id")
                        if context and isinstance(context.get("ephemeral_skill"), dict)
                        else None
                    )
                    _safe_record_skill_usage(
                        director_id=route.agent_id,
                        task_id=task.task_id,
                        skill_id=skill_id,
                        is_ephemeral=False,
                        adherence_score=adherence,
                        outcome="ESCALATED",
                        gates_passed=False,
                        gates_failed=["RATE_LIMIT_429"],
                        model=used_model,
                        prompt_tokens=total_prompt_tokens,
                        completion_tokens=total_completion_tokens,
                        cost_usd=total_cost_usd,
                        cost_source=active_limits.cost_source,
                        duration_ms=duration * 1000.0,
                        note=f"Task escalated due to 429 rate limit: {last_error}"[
                            :280
                        ],
                        state_manager=active_state,
                        database_url=db_url,
                    )
                    return {
                        "status": "escalated",
                        "agent_id": route.agent_id,
                        "model": used_model,
                        "error": escalation_msg,
                        "reply": f"Task escalated: {escalation_msg}",
                        "cost_report": cost_rep,
                        "duration_seconds": round(duration, 2),
                        "timestamp": time.time(),
                    }

                raise last_error or RuntimeError("No model candidates available")

            # Accumulate usage & cost
            usage_data = response.usage.model_dump() if response.usage else {}
            p_tok = usage_data.get("prompt_tokens", 0)
            c_tok = usage_data.get("completion_tokens", 0)
            t_tok = usage_data.get("total_tokens", p_tok + c_tok)

            call_cost, cost_source = estimate_cost(
                model=used_model,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                completion_response=response,
            )

            active_limits.record_call(
                tokens=t_tok,
                cost_usd=call_cost,
                is_ephemeral=False,
                cost_source=cost_source,
            )
            total_prompt_tokens += p_tok
            total_completion_tokens += c_tok
            total_cost_usd += call_cost

            choice = response.choices[0]
            message_obj = choice.message
            tool_calls = getattr(message_obj, "tool_calls", None)

            # If director didn't request any tools, finish loop
            if not tool_calls:
                final_reply = message_obj.content or ""
                break

            # Process tool calls
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": message_obj.content or "",
            }
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ]
            messages.append(assistant_msg)

            for tc in tool_calls:
                if tc.function.name == "spawn_ephemeral":
                    tool_calls_executed += 1
                    try:
                        args = json.loads(tc.function.arguments)
                    except Exception:  # noqa: BLE001
                        args = {}

                    def _usage_sink(**kwargs):
                        _safe_record_skill_usage(
                            state_manager=active_state,
                            database_url=db_url,
                            **kwargs,
                        )

                    eph_res = execute_ephemeral_task(
                        skill_id=args.get("skill_id", ""),
                        subtask_title=args.get("subtask_title", "Ephemeral Subtask"),
                        subtask_body=args.get("subtask_body", ""),
                        expected_output=args.get("expected_output", ""),
                        parent_task=task,
                        board=active_board,
                        database_url=db_url,
                        director_id=route.agent_id,
                        limits=active_limits,
                        usage_recorder=_usage_sink,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": "spawn_ephemeral",
                            "content": eph_res.get("output", ""),
                        }
                    )

        duration = time.time() - start_time
        cost_report = active_limits.to_report()
        record_task_cost(task.task_id, cost_report)

        # Log task result to Obsidian memory
        obsidian.log_task_completion(route.agent_id, task.title, final_reply)

        # Record successful Director SkillUsage
        skill_id = (
            context.get("ephemeral_skill", {}).get("id")
            if context and isinstance(context.get("ephemeral_skill"), dict)
            else None
        )
        _safe_record_skill_usage(
            director_id=route.agent_id,
            task_id=task.task_id,
            skill_id=skill_id,
            is_ephemeral=False,
            adherence_score=adherence,
            outcome="SUCCESS",
            gates_passed=True,
            gates_failed=[],
            model=used_model,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            cost_usd=total_cost_usd,
            cost_source=active_limits.cost_source,
            duration_ms=duration * 1000.0,
            note=f"Director execution completed ({tool_calls_executed} ephemerals)"[
                :280
            ],
            state_manager=active_state,
            database_url=db_url,
        )

        return {
            "status": "success",
            "agent_id": route.agent_id,
            "model": used_model,
            "reply": final_reply,
            "usage": {
                "prompt_tokens": total_prompt_tokens,
                "completion_tokens": total_completion_tokens,
                "total_tokens": total_prompt_tokens + total_completion_tokens,
            },
            "cost_report": cost_report,
            "tool_calls_executed": tool_calls_executed,
            "duration_seconds": round(duration, 2),
            "timestamp": time.time(),
        }

    except Exception as exc:  # noqa: BLE001
        logger.error("LiteLLM handoff failed: %s", exc)
        duration = time.time() - start_time
        cost_report = active_limits.to_report()
        record_task_cost(task.task_id, cost_report)

        skill_id = (
            context.get("ephemeral_skill", {}).get("id")
            if context and isinstance(context.get("ephemeral_skill"), dict)
            else None
        )
        _safe_record_skill_usage(
            director_id=route.agent_id,
            task_id=task.task_id,
            skill_id=skill_id,
            is_ephemeral=False,
            adherence_score=adherence,
            outcome="FAILURE",
            gates_passed=False,
            gates_failed=["HANDOFF_ERROR"],
            model=used_model,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            cost_usd=total_cost_usd,
            cost_source=active_limits.cost_source,
            duration_ms=duration * 1000.0,
            note=f"Director failure: {exc}"[:280],
            state_manager=active_state,
            database_url=db_url,
        )

        return {
            "status": "error",
            "agent_id": route.agent_id,
            "error": str(exc),
            "cost_report": cost_report,
            "timestamp": time.time(),
        }


def dry_run_handoff(
    task: KanbanTask,
    route: RouteAgentResult,
    context: dict[str, Any] | None = None,
    runtime: HandoffRuntime | None = None,
    *,
    board: KanbanBoard | None = None,
    database_url: str | None = None,
    limits: EphemeralLimits | None = None,
    state_manager: StateManager | None = None,
    tools_enabled: bool = True,
) -> dict[str, Any]:
    """Execute a deterministic dry-run handoff that completes all stages without LLM calls.

    Creates subtasks, simulates ephemeral tool executions, registers SkillUsage,
    and returns a clean, structured reply with zero cost.
    """
    start_time = time.time()
    active_board = (runtime.board if runtime else None) or board
    db_url = (
        (runtime.db_url if runtime else None)
        or database_url
        or os.getenv("DATABASE_URL", DEFAULT_DB_URL)
    )
    active_limits = (runtime.limits if runtime else None) or limits or EphemeralLimits()
    active_state = (runtime.state_manager if runtime else None) or state_manager
    adherence = (runtime.adherence_score if runtime else None) or (
        context.get("adherence_score") if context else 1.0
    )

    skill_id = (
        context.get("ephemeral_skill", {}).get("id")
        if context and isinstance(context.get("ephemeral_skill"), dict)
        else (
            getattr(route.skill_match, "candidate_id", None)
            if hasattr(route, "skill_match")
            else None
        )
    )

    tool_calls_executed = 0
    if skill_id:
        tool_calls_executed = 1
        subtask = KanbanTask(
            title=f"Subtarefa efêmera: {skill_id}",
            body=f"Execução simulada da skill '{skill_id}' para a tarefa '{task.title}'",
            priority=task.priority,
            assignee="ephemeral",
            macro_domain=route.macro_domain,
            parent_task_id=task.task_id,
        )
        if active_board is not None:
            active_board.add(subtask)
            if subtask.status == KanbanStatus.TODO:
                active_board.transition(
                    subtask.task_id, KanbanStatus.READY, actor="director"
                )
            if subtask.status == KanbanStatus.READY:
                active_board.transition(
                    subtask.task_id, KanbanStatus.IN_PROGRESS, actor="director"
                )
            active_board.transition(
                subtask.task_id,
                KanbanStatus.DONE,
                actor="ephemeral",
                reason="dry-run complete",
            )

        active_limits.record_call(
            tokens=100, cost_usd=0.0, is_ephemeral=True, cost_source="free_tier"
        )

        _safe_record_skill_usage(
            director_id=route.agent_id,
            task_id=subtask.task_id,
            parent_task_id=task.task_id,
            skill_id=skill_id,
            is_ephemeral=True,
            adherence_score=adherence,
            outcome="SUCCESS",
            gates_passed=True,
            gates_failed=[],
            model="simulated/dry-run",
            prompt_tokens=50,
            completion_tokens=50,
            cost_usd=0.0,
            cost_source="free_tier",
            duration_ms=10.0,
            note=f"Dry-run ephemeral execution of {skill_id}",
            state_manager=active_state,
            database_url=db_url,
        )

    active_limits.record_call(
        tokens=150, cost_usd=0.0, is_ephemeral=False, cost_source="free_tier"
    )

    reply = (
        f"[DRY-RUN] Tarefa '{task.title}' processada com sucesso pelo diretor {route.agent_id}.\n"
        f"Skill utilizada: {skill_id or 'none'}.\n"
        f"Subagentes efêmeros executados: {tool_calls_executed}."
    )

    duration = time.time() - start_time
    cost_report = active_limits.to_report()
    record_task_cost(task.task_id, cost_report)

    _safe_record_skill_usage(
        director_id=route.agent_id,
        task_id=task.task_id,
        skill_id=skill_id,
        is_ephemeral=False,
        adherence_score=adherence,
        outcome="SUCCESS",
        gates_passed=True,
        gates_failed=[],
        model="simulated/dry-run",
        prompt_tokens=75,
        completion_tokens=75,
        cost_usd=0.0,
        cost_source="free_tier",
        duration_ms=duration * 1000.0,
        note="Dry-run director execution completed",
        state_manager=active_state,
        database_url=db_url,
    )

    return {
        "status": "success",
        "agent_id": route.agent_id,
        "model": "simulated/dry-run",
        "reply": reply,
        "usage": {
            "prompt_tokens": 125,
            "completion_tokens": 125,
            "total_tokens": 250,
        },
        "cost_report": cost_report,
        "tool_calls_executed": tool_calls_executed,
        "duration_seconds": round(duration, 3),
        "timestamp": time.time(),
    }
