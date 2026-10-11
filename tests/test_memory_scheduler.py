"""Memory (FTS5 search) + Scheduler tests for Agentic OS (v0.4.0)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Harness  # noqa: E402


class MemoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.h.start()

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def test_search_endpoint_ok(self):
        status, data = self.h.api("GET", "/api/memory/search?q=system")
        self.assertEqual(status, 200)
        self.assertIn("results", data)
        self.assertIn("entities", data)
        self.assertEqual(data["query"], "system")

    def test_search_indexes_brain_file(self):
        # Seed a brain file in temp state, reindex, then search
        (self.h.tmp / "brain" / "test-brain-note.md").write_text(
            "zephyrquadrant cloud infrastructure notes"
        )
        status, _ = self.h.api("POST", "/api/memory/reindex")
        self.assertEqual(status, 200)
        status, data = self.h.api("GET", "/api/memory/search?q=zephyrquadrant")
        self.assertEqual(status, 200)
        self.assertTrue(
            data["results"],
            "FTS5 search should find the indexed brain file",
        )

    def test_reindex_endpoint(self):
        status, data = self.h.api("POST", "/api/memory/reindex")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "reindexed")

    def test_entities_endpoint(self):
        status, data = self.h.api("GET", "/api/memory/entities")
        self.assertEqual(status, 200)
        self.assertIn("entities", data)

    def test_search_without_query_empty(self):
        status, data = self.h.api("GET", "/api/memory/search?q=")
        self.assertEqual(status, 200)
        self.assertEqual(data["results"], [])

    def test_entity_extraction(self):
        status, data = self.h.api(
            "GET", "/api/memory/search?q=Contact%20test@example.com"
        )
        self.assertEqual(status, 200)
        types = {e["type"] for e in data["entities"]}
        self.assertIn("email", types)


class SchedulerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.h.start()

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def test_events_endpoint(self):
        status, data = self.h.api("GET", "/api/scheduler/events")
        self.assertEqual(status, 200)
        self.assertIn("events", data)

    def test_trigger_unknown_job_404(self):
        status, _ = self.h.api("POST", "/api/scheduler/trigger/does-not-exist")
        self.assertEqual(status, 404)

    def test_webhook_received(self):
        status, data = self.h.api(
            "POST", "/api/webhook/generic", {"source": "test", "event": "ping"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["event"], "ping")


class SkillGenerateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.h = Harness()
        cls.h.start()

    @classmethod
    def tearDownClass(cls):
        cls.h.stop()

    def test_generate_skill(self):
        status, data = self.h.api(
            "POST",
            "/api/skills/generate",
            {"name": "Test Skill V4", "description": "A test skill"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "created")
        self.assertEqual(data["name"], "test-skill-v4")
        # verify it appears in skills list, then clean up
        (self.h.tmp / "skills" / "test-skill-v4").mkdir(exist_ok=True)
        status, skills = self.h.api("GET", "/api/skills")
        self.assertEqual(status, 200)
        self.assertIn("test-skill-v4", [s["name"] for s in skills])

    def test_generate_invalid_name(self):
        status, _ = self.h.api(
            "POST",
            "/api/skills/generate",
            {"name": "bad name/../../x", "description": "bad"},
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
