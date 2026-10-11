"""Kanban + Journal CRUD tests for Agentic OS (v0.4.0)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Harness  # noqa: E402


class KanbanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.h.start()

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def test_create_and_get_task(self):
        status, data = self.h.api(
            "POST",
            "/api/kanban/tasks",
            {
                "title": "Test task",
                "body": "do it",
                "status": "todo",
                "priority": "high",
                "assignee": "tester",
            },
        )
        self.assertEqual(status, 200)
        tid = data["id"]
        self.assertTrue(tid)

        status, got = self.h.api("GET", f"/api/kanban/tasks/{tid}")
        self.assertEqual(status, 200)
        self.assertEqual(got["title"], "Test task")

    def test_update_task(self):
        _, created = self.h.api(
            "POST",
            "/api/kanban/tasks",
            {
                "title": "Update me",
                "body": "",
                "status": "todo",
                "priority": "low",
                "assignee": "",
            },
        )
        tid = created["id"]
        status, updated = self.h.api(
            "PATCH",
            f"/api/kanban/tasks/{tid}",
            {"title": "Updated title", "status": "in_progress"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["title"], "Updated title")
        self.assertEqual(updated["status"], "in_progress")

    def test_complete_task(self):
        _, created = self.h.api(
            "POST",
            "/api/kanban/tasks",
            {
                "title": "Complete me",
                "body": "",
                "status": "todo",
                "priority": "medium",
                "assignee": "",
            },
        )
        tid = created["id"]
        status, done = self.h.api(
            "POST", f"/api/kanban/tasks/{tid}/complete", {"summary": "finished"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(done["status"], "done")

    def test_block_and_unblock(self):
        _, created = self.h.api(
            "POST",
            "/api/kanban/tasks",
            {
                "title": "Block me",
                "body": "",
                "status": "todo",
                "priority": "medium",
                "assignee": "",
            },
        )
        tid = created["id"]
        _, blocked = self.h.api(
            "POST", f"/api/kanban/tasks/{tid}/block", {"reason": "blocked"}
        )
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["block_reason"], "blocked")
        _, unblocked = self.h.api("POST", f"/api/kanban/tasks/{tid}/unblock")
        self.assertEqual(unblocked["status"], "ready")

    def test_comment_roundtrip(self):
        _, created = self.h.api(
            "POST",
            "/api/kanban/tasks",
            {
                "title": "Comment me",
                "body": "",
                "status": "todo",
                "priority": "low",
                "assignee": "",
            },
        )
        tid = created["id"]
        _, with_comment = self.h.api(
            "POST", f"/api/kanban/tasks/{tid}/comments", {"message": "hello world"}
        )
        self.assertEqual(with_comment["comments"][-1]["message"], "hello world")

    def test_missing_task_404(self):
        status, _ = self.h.api("GET", "/api/kanban/tasks/nonexistent-id-xyz")
        self.assertEqual(status, 404)

    def test_board_contains_columns(self):
        status, data = self.h.api("GET", "/api/kanban/board")
        self.assertEqual(status, 200)
        self.assertIn("columns", data)


class JournalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.h.start()

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def test_save_and_get_entry(self):
        status, saved = self.h.api(
            "PUT",
            "/api/journal/entries/2026-08-11",
            {"content": "Today I learned testing."},
        )
        self.assertEqual(status, 200)
        self.assertEqual(saved["status"], "saved")

        status, got = self.h.api("GET", "/api/journal/entries/2026-08-11")
        self.assertEqual(status, 200)
        self.assertIn("Today I learned", got["content"])

    def test_entries_list_includes_new(self):
        self.h.api(
            "PUT", "/api/journal/entries/2026-08-10", {"content": "Journal test entry"}
        )
        status, data = self.h.api("GET", "/api/journal/entries")
        self.assertEqual(status, 200)
        dates = {e["date"] for e in data.get("entries", [])}
        self.assertIn("2026-08-10", dates)

    def test_search_finds_content(self):
        self.h.api(
            "PUT",
            "/api/journal/entries/2026-08-09",
            {"content": "Searchable unicorn keyword here"},
        )
        status, data = self.h.api("GET", "/api/journal/search?q=unicorn")
        self.assertEqual(status, 200)
        hits = data.get("results", [])
        self.assertTrue(any("unicorn" in r.get("preview", "") for r in hits))

    def test_search_empty_query_ok(self):
        status, data = self.h.api("GET", "/api/journal/search?q=")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("results"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
