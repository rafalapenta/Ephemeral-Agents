"""E2E smoke test for Phase 2 macro orchestration.

Validates the full lifecycle:
  1. Task creation    (GATE1)
  2. Routing          (GATE2)
  3. Context compress (GATE3)
  4. State invariant  (GATE4)
  + STATE_INVARIANT — version > 0 and phase == "completed"

Uses a fake router and dry-run handoff (no real DB/ChromaDB needed).
Exit code 0 means all gates passed.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.macro_agents.orchestrator import MacroOrchestrator
from src.router.semantic import RouteAgentResult


def _fake_route_agent(
    query: str,
    threshold: float = 0.30,
    *,
    database_url=None,
    chroma_path=None,
    source_root=None,
    embedding_function=None,
    **kwargs,
) -> RouteAgentResult:
    """Deterministic fake router for E2E testing."""
    return RouteAgentResult(
        matched=True,
        score=0.85,
        agent_id="engineering-frontend-developer",
        name="Frontend Developer",
        macro_domain="engineering",
        system_prompt="You are a frontend engineering specialist.",
        tools=[],
        reason="matched candidate above threshold",
    )


def main() -> int:
    results: dict[str, int] = {}

    with tempfile.TemporaryDirectory(prefix="aggency-e2e-") as tmp:
        state_dir = Path(tmp) / "state"

        # Patch route_agent globally so MacroOrchestrator uses the fake
        with patch(
            "src.macro_agents.orchestrator.route_agent",
            side_effect=_fake_route_agent,
        ):
            orch = MacroOrchestrator(state_dir=state_dir)
            result = orch.orchestrate(
                title="E2E smoke test task",
                body="Validate the full orchestration lifecycle",
                query="React frontend accessibility web performance",
                priority="high",
                assignee="e2e-tester",
                source_agent="smoke-test",
            )

        # ── Evaluate gates ─────────────────────────────────
        for gate in result.gates:
            exit_code = 0 if gate.passed else 1
            results[gate.gate_id] = exit_code

        # ── State invariant ────────────────────────────────
        state = orch.state.get()
        state_ok = (
            state.get("state_version", 0) > 0 and state.get("phase") == "completed"
        )
        results["STATE_INVARIANT"] = 0 if state_ok else 1

        # ── Overall update check ───────────────────────────
        results["UPDATE"] = 0 if result.status == "completed" else 1

    # ── Print results ──────────────────────────────────────
    for key, val in sorted(results.items()):
        print(f"{key}={val}")

    overall = max(results.values()) if results else 1
    if overall == 0:
        print("\n✅ All gates passed.")
    else:
        print("\n❌ Some gates failed.")

    return overall


if __name__ == "__main__":
    sys.exit(main())
