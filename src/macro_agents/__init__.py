"""Domain macro-orchestrators.

Public re-exports:
  - ``MacroOrchestrator`` — Full lifecycle orchestration loop.
  - ``OrchestrationResult`` — Result of a macro orchestration run.
  - ``GateResult`` — Single quality gate evaluation.
"""

from .orchestrator import GateResult, MacroOrchestrator, OrchestrationResult

__all__ = ["GateResult", "MacroOrchestrator", "OrchestrationResult"]
