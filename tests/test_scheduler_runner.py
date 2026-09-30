"""Unit tests for OrchestratorScheduler (PR 10)."""
import unittest
from pathlib import Path
import tempfile
import shutil

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.catalog.indexer import run_indexing
from src.macro_agents.orchestrator import MacroOrchestrator
from src.scheduler.runner import OrchestratorScheduler
from src.orchestration.kanban import KanbanTask, KanbanStatus


class TestOrchestratorScheduler(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scheduler-test-"))
        db_path = self.tmp / "catalog.db"
        self.db_url = f"sqlite:///{db_path.as_posix()}"
        self.chroma_path = self.tmp / "chroma"
        self.source_root = self.tmp / "agency-agents"

        # Write a test agent markdown with valid frontmatter
        agent_path = self.source_root / "engineering" / "engineering-frontend-developer.md"
        agent_path.parent.mkdir(parents=True, exist_ok=True)
        agent_path.write_text(
            """---
name: Frontend Developer
description: Expert frontend developer for React, Vue, accessibility, and performance.
vibe: Builds responsive web apps with pixel-perfect precision.
---
# Frontend Developer Agent Personality
- **Role**: Modern web application and UI implementation specialist
""",
            encoding="utf-8",
        )

        # Index the catalog so routing works
        run_indexing(
            source_root=self.source_root,
            database_url=self.db_url,
            chroma_path=self.chroma_path,
            dry_run=False,
            reindex=True,
        )

        self.orchestrator = MacroOrchestrator(
            state_dir=self.tmp / "state",
            database_url=self.db_url,
            chroma_path=self.chroma_path,
            source_root=self.source_root,
            threshold=0.0,  # accept any match for test
            handoff_fn=lambda t, r: {"status": "ok", "agent": r.agent_id}
        )
        self.scheduler = OrchestratorScheduler(self.orchestrator)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scheduler_processes_ready_task(self):
        # Add a task and transition to READY
        task = KanbanTask(
            title="Frontend Developer Task",
            body="Build React component",
            priority="high",
        )
        self.orchestrator.board.add(task)
        self.orchestrator.board.transition(task.task_id, KanbanStatus.READY, actor="test")

        results = self.scheduler.run_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "completed")
        self.assertEqual(results[0].task_id, task.task_id)


if __name__ == "__main__":
    unittest.main()
