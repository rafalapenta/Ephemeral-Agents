"""LiteLLM Handoff module with Ephemeral subagent tool-calling loop."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol, runtime_checkable

import litellm

from src.database.usage import record_skill_usage
from src.gateway.models import estimate_cost, resolve_model
from src.governance.limits import EphemeralLimits, record_task_cost
from src.macro_agents.ephemeral import SPAWN_EPHEMERAL_TOOL, execute_ephemeral_task
from src.memory.obsidian import ObsidianMemory
from src.orchestration.context import compress_context
from src.orchestration.kanban import KanbanBoard, KanbanStatus, KanbanTask
from src.router.semantic import RouteAgentResult
from src.state.manager import StateManager

logger = logging.getLogger(__name__)


@runtime_checkable
class HandoffFn(Protocol):
    """Standard protocol for handoff execution functions.
    
    All handoff implementations must accept (task, route, context).
    """

    def __call__(
        self,
        task: KanbanTask,
        route: RouteAgentResult,
        context: dict[str, Any],
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        ...


def _safe_record_skill_usage(**kwargs) -> None:
    try:
        record_skill_usage(**kwargs)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Non-fatal failure recording skill usage: %s", exc)



def litellm_handoff(
    task: KanbanTask,
    route: RouteAgentResult,
    context: dict[str, Any] | None = None,
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
    logger.info("Initiating LiteLLM handoff for task %s to agent %s", task.task_id, route.agent_id)

    # Extract helper services from context if provided
    active_board = board or (context.get("_board") if context else None)
    db_url = database_url or (context.get("_db_url") if context else None)
    active_limits = limits or (context.get("_limits") if context else None) or EphemeralLimits()
    active_state = state_manager or (context.get("_state_manager") if context else None)

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
        user_context = {
            k: v for k, v in context.items()
            if not k.startswith("_") and k != "system_prompt"
        }
    else:
        handoff_context = {
            "task_id": task.task_id,
            "title": task.title,
            "body": task.body,
            "priority": task.priority,
            "macro_domain": route.macro_domain,
            "target_agent": route.agent_id,
        }
        user_context = compress_context(handoff_context)

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
                logger.warning("Governance limit hit for task %s: %s", task.task_id, limit_reason)
                if active_board is not None:
                    try:
                        active_board.transition(
                            task.task_id,
                            KanbanStatus.BLOCKED,
                            actor="governance",
                            reason="limit_exceeded",
                        )
                    except Exception:
                        pass
                    task.blocked_reason = "limit_exceeded"

                # Record skill usage with limit_exceeded
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
                    adherence_score=context.get("adherence_score", 1.0) if context else 1.0,
                    outcome="limit_exceeded",
                    gates_passed=False,
                    model=used_model,
                    prompt_tokens=total_prompt_tokens,
                    completion_tokens=total_completion_tokens,
                    cost_usd=total_cost_usd,
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

            # Call LLM with fallback candidates
            candidates = [resolved.model, *resolved.fallbacks]
            response = None

            for index, candidate in enumerate(candidates):
                try:
                    response = litellm.completion(
                        model=candidate,
                        messages=messages,
                        **completion_kwargs,
                    )
                    used_model = candidate
                    if index > 0:
                        logger.warning("Primary model unavailable; served by fallback %s", candidate)
                    break
                except Exception as attempt_error:  # noqa: BLE001
                    last_error = attempt_error
                    logger.warning(
                        "Model %s failed (%s: %s)%s",
                        candidate,
                        type(attempt_error).__name__,
                        attempt_error,
                        "; trying next fallback" if index + 1 < len(candidates) else "",
                    )

            if response is None:
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
                    except Exception:
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
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": "spawn_ephemeral",
                        "content": eph_res.get("output", ""),
                    })

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
            adherence_score=context.get("adherence_score", 1.0) if context else 1.0,
            outcome="success",
            gates_passed=True,
            model=used_model,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            cost_usd=total_cost_usd,
            duration_ms=duration * 1000.0,
            note=f"Director execution completed ({tool_calls_executed} ephemerals)"[:280],
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

    except Exception as exc:
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
            adherence_score=context.get("adherence_score", 0.0) if context else 0.0,
            outcome="failure",
            gates_passed=False,
            model=used_model,
            prompt_tokens=total_prompt_tokens,
            completion_tokens=total_completion_tokens,
            cost_usd=total_cost_usd,
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
