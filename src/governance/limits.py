"""Governance limits and cost tracking for Ephemeral Agents."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# In-memory index of recent task cost reports
_TASK_COST_REPORTS: dict[str, dict[str, Any]] = {}


@dataclass
class EphemeralLimits:
    """Configurable execution and cost limits for task orchestration.
    
    Defaults are loaded from environment variables when available.
    """

    max_depth: int = field(
        default_factory=lambda: int(os.getenv("EPHEMERAL_MAX_DEPTH", "1"))
    )
    max_ephemerals_per_task: int = field(
        default_factory=lambda: int(os.getenv("EPHEMERAL_MAX_PER_TASK", "5"))
    )
    max_tool_iterations: int = field(
        default_factory=lambda: int(os.getenv("EPHEMERAL_MAX_TOOL_ITERATIONS", "6"))
    )
    max_tokens_per_task: int = field(
        default_factory=lambda: int(os.getenv("EPHEMERAL_MAX_TOKENS_PER_TASK", "20000"))
    )
    max_cost_usd_per_task: float = field(
        default_factory=lambda: float(os.getenv("EPHEMERAL_MAX_COST_USD_PER_TASK", "0.50"))
    )

    # Runtime accumulation per task
    current_tokens: int = 0
    current_cost_usd: float = 0.0
    ephemeral_count: int = 0
    tool_iterations: int = 0
    limits_hit: list[str] = field(default_factory=list)

    def can_iterate(self) -> tuple[bool, str | None]:
        """Check if another tool iteration is permitted before calling LLM."""
        if self.tool_iterations >= self.max_tool_iterations:
            reason = f"max_tool_iterations exceeded ({self.tool_iterations}/{self.max_tool_iterations})"
            self._record_hit(reason)
            return False, reason
        if self.current_tokens >= self.max_tokens_per_task:
            reason = f"max_tokens_per_task exceeded ({self.current_tokens}/{self.max_tokens_per_task})"
            self._record_hit(reason)
            return False, reason
        if self.current_cost_usd >= self.max_cost_usd_per_task:
            reason = f"max_cost_usd_per_task exceeded (${self.current_cost_usd:.4f}/${self.max_cost_usd_per_task:.2f})"
            self._record_hit(reason)
            return False, reason
        return True, None

    def can_spawn_ephemeral(self) -> tuple[bool, str | None]:
        """Check if spawning another ephemeral subagent is permitted."""
        if self.ephemeral_count >= self.max_ephemerals_per_task:
            reason = f"max_ephemerals_per_task exceeded ({self.ephemeral_count}/{self.max_ephemerals_per_task})"
            self._record_hit(reason)
            return False, reason
        if self.current_tokens >= self.max_tokens_per_task:
            reason = f"max_tokens_per_task exceeded ({self.current_tokens}/{self.max_tokens_per_task})"
            self._record_hit(reason)
            return False, reason
        if self.current_cost_usd >= self.max_cost_usd_per_task:
            reason = f"max_cost_usd_per_task exceeded (${self.current_cost_usd:.4f}/${self.max_cost_usd_per_task:.2f})"
            self._record_hit(reason)
            return False, reason
        return True, None

    def record_call(
        self,
        *,
        tokens: int = 0,
        cost_usd: float = 0.0,
        is_ephemeral: bool = False,
    ) -> None:
        """Record token and cost consumption of a single LLM call."""
        self.current_tokens += int(tokens)
        self.current_cost_usd += float(cost_usd)
        if is_ephemeral:
            self.ephemeral_count += 1
        else:
            self.tool_iterations += 1

    def _record_hit(self, reason: str) -> None:
        if reason not in self.limits_hit:
            self.limits_hit.append(reason)

    def to_report(self) -> dict[str, Any]:
        """Summary report for Sterling governance and audit."""
        return {
            "total_tokens": self.current_tokens,
            "total_cost_usd": round(self.current_cost_usd, 6),
            "ephemeral_count": self.ephemeral_count,
            "tool_iterations": self.tool_iterations,
            "limits_hit": list(self.limits_hit),
            "limit_exceeded": len(self.limits_hit) > 0,
        }


def record_task_cost(task_id: str, report: dict[str, Any]) -> None:
    """Store task cost report in memory."""
    _TASK_COST_REPORTS[task_id] = report


def get_cost_report(task_id: str, journal_path: Path | str = Path("data/journal.jsonl")) -> dict[str, Any]:
    """Retrieve cost and token consumption report for Sterling governance.
    
    Checks memory cache first, then falls back to querying data/journal.jsonl.
    """
    if task_id in _TASK_COST_REPORTS:
        return _TASK_COST_REPORTS[task_id]

    # Fallback to journal.jsonl
    target_file = Path(journal_path)
    if target_file.exists():
        try:
            with open(target_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if entry.get("task_id") == task_id or entry.get("patch", {}).get("task_id") == task_id:
                        cost_report = entry.get("cost_report") or entry.get("patch", {}).get("cost_report")
                        if cost_report:
                            return cost_report
        except Exception as exc:
            logger.warning("Failed to parse journal for task cost report: %s", exc)

    return {
        "task_id": task_id,
        "total_tokens": 0,
        "total_cost_usd": 0.0,
        "ephemeral_count": 0,
        "tool_iterations": 0,
        "limits_hit": [],
        "limit_exceeded": False,
    }
