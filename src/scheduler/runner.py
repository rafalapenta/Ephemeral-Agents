"""Lightweight task scheduler wrapper around MacroOrchestrator."""
from __future__ import annotations

import logging
import time
from typing import Any

from src.macro_agents.orchestrator import MacroOrchestrator, OrchestrationResult
from src.orchestration.kanban import KanbanBoard, KanbanStatus

logger = logging.getLogger(__name__)


class OrchestratorScheduler:
    """Polls ready tasks on the Kanban board and executes them via MacroOrchestrator."""

    def __init__(
        self,
        orchestrator: MacroOrchestrator,
        poll_interval: float = 1.0,
    ) -> None:
        self.orchestrator = orchestrator
        self.poll_interval = poll_interval

    def process_ready_tasks(self) -> list[OrchestrationResult]:
        """Process all tasks currently in READY state."""
        results = []
        ready_tasks = self.orchestrator.board.columns().get(KanbanStatus.READY.value, [])
        for task in ready_tasks:
            logger.info("Scheduler picking up ready task %s: %s", task.task_id, task.title)
            res = self.orchestrator.orchestrate_task(task, query=task.title)
            results.append(res)
        return results

    def run_once(self) -> list[OrchestrationResult]:
        """Run a single polling cycle over ready tasks."""
        return self.process_ready_tasks()


def main() -> None:
    """CLI entrypoint for running the scheduler once or in a loop."""
    import argparse
    from dotenv import load_dotenv
    
    load_dotenv()
    
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="AGency Orchestrator Scheduler")
    parser.add_argument("--poll-once", action="store_true", help="Run a single polling cycle and exit")
    args = parser.parse_args()

    orchestrator = MacroOrchestrator()
    scheduler = OrchestratorScheduler(orchestrator)
    if args.poll_once:
        results = scheduler.run_once()
        print(f"Processed {len(results)} ready tasks.")
    else:
        print("Scheduler running... (Ctrl+C to stop)")
        try:
            while True:
                results = scheduler.run_once()
                if results:
                    print(f"Processed {len(results)} ready tasks.")
                time.sleep(scheduler.poll_interval)
        except KeyboardInterrupt:
            print("Scheduler stopped.")


if __name__ == "__main__":
    main()

