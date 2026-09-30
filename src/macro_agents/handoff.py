import json
import logging
import os
import time
from typing import Any

import litellm

from src.orchestration.context import compress_context
from src.orchestration.kanban import KanbanTask
from src.router.semantic import RouteAgentResult
from src.memory.obsidian import ObsidianMemory
from src.gateway.models import resolve_model

logger = logging.getLogger(__name__)

def litellm_handoff(task: KanbanTask, route: RouteAgentResult) -> dict[str, Any]:
    """Execute the domain agent handoff using a direct LiteLLM call.
    
    This constructs a conversation using the agent's system prompt (from the 
    SOUL.md file) and the task context, and sends it to the configured LLM.
    """
    logger.info("Initiating LiteLLM handoff for task %s to agent %s", task.task_id, route.agent_id)
    
    # 1. Compress the context to avoid sending unnecessary bloat to the LLM
    handoff_context = {
        "task_id": task.task_id,
        "title": task.title,
        "body": task.body,
        "priority": task.priority,
        "macro_domain": route.macro_domain,
        "target_agent": route.agent_id,
    }
    compressed = compress_context(handoff_context)
    
    # 2. Fetch memory from Obsidian
    obsidian = ObsidianMemory()
    memory_context = obsidian.get_agent_context(route.agent_id)
    
    # 3. Build the messages payload
    sys_prompt = route.system_prompt or "You are a helpful AI assistant."
    if memory_context:
        sys_prompt = f"{sys_prompt}\n\n{memory_context}"
        
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": f"Please execute the following task.\n\nContext:\n{json.dumps(compressed, indent=2)}"}
    ]
    
    # 3. Resolve the per-agent model target.
    # The gateway is OpenAI-compatible, so routing goes through an explicit
    # api_base instead of vendor endpoints. See src/gateway/models.py for the
    # agent→model matrix and the fallback chains.
    resolved = resolve_model(route.agent_id)

    logger.info(
        "Handoff %s -> %s (tier=%s, api_base=%s, fallbacks=%d)",
        route.agent_id,
        resolved.model,
        resolved.tier,
        resolved.api_base,
        len(resolved.fallbacks),
    )
    if not resolved.api_key:
        logger.warning(
            "No OMNIROUTE_API_KEY set; relying on the gateway accepting anonymous requests."
        )

    # LiteLLM reads api_key/api_base from the call, not from .env, so a stale
    # OPENAI_API_KEY in the environment cannot hijack the request.
    completion_kwargs: dict[str, Any] = {
        "messages": messages,
        "temperature": resolved.temperature,
        "max_tokens": resolved.max_tokens,
        "api_base": resolved.api_base,
        "timeout": resolved.timeout,
    }
    if resolved.api_key:
        completion_kwargs["api_key"] = resolved.api_key

    try:
        start_time = time.time()
        # Try the primary model, then each fallback in order. A rate-limited or
        # unreachable provider must not abort the handoff outright.
        candidates = [resolved.model, *resolved.fallbacks]
        last_error: Exception | None = None
        response = None
        used_model = resolved.model

        for index, candidate in enumerate(candidates):
            try:
                response = litellm.completion(model=candidate, **completion_kwargs)
                used_model = candidate
                if index > 0:
                    logger.warning(
                        "Primary model unavailable; served by fallback %s", candidate
                    )
                break
            except Exception as attempt_error:  # noqa: BLE001 - provider errors vary wildly
                last_error = attempt_error
                logger.warning(
                    "Model %s failed (%s: %s)%s",
                    candidate,
                    type(attempt_error).__name__,
                    attempt_error,
                    "; trying next fallback" if index + 1 < len(candidates) else "",
                )

        if response is None:
            raise last_error or RuntimeError("no model candidates available")

        duration = time.time() - start_time

        reply = response.choices[0].message.content
        usage = response.usage.model_dump() if response.usage else {}
        
        # Log the task result back to Obsidian memory
        obsidian.log_task_completion(route.agent_id, task.title, reply)
        
        return {
            "status": "success",
            "agent_id": route.agent_id,
            "model": used_model,
            "reply": reply,
            "usage": usage,
            "duration_seconds": round(duration, 2),
            "timestamp": time.time(),
        }
        
    except Exception as exc:
        logger.error("LiteLLM handoff failed: %s", exc)
        return {
            "status": "error",
            "agent_id": route.agent_id,
            "error": str(exc),
            "timestamp": time.time(),
        }
