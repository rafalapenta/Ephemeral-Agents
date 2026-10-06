"""Evolution and self-improvement package for Ephemeral-Agents.

Includes:
- hotpatch: Surgical patch of SOUL.md guardrails from human corrections.
- distiller: Autonomous distillation of completed tasks into standard SKILL.md.
"""
from __future__ import annotations

from .distiller import distill_task_to_skill
from .hotpatch import apply_human_correction

__all__ = [
    "apply_human_correction",
    "distill_task_to_skill",
]
