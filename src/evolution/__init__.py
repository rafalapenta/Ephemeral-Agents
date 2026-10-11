"""Evolution and self-improvement package for Ephemeral-Agents.

Includes:
- hotpatch: Surgical patch of SOUL.md guardrails from human corrections.
- distiller: Autonomous distillation of completed tasks into standard SKILL.md.
"""

from __future__ import annotations

from .distiller import (
    SkillImprovementProposal,
    distill_task_to_skill,
    propose_skill_improvements,
)
from .hotpatch import apply_human_correction

__all__ = [
    "SkillImprovementProposal",
    "apply_human_correction",
    "distill_task_to_skill",
    "propose_skill_improvements",
]
