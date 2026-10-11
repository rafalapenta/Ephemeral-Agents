"""Unit tests for Phase 2 modules.

Covers:
  - RFC 7396 merge_patch
  - StateManager (journal, versioning, persistence)
  - Context compression
  - Kanban state machine (transitions, audit)
  - Domain tie-breaking in router
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.orchestration.context import compress_context
from src.orchestration.kanban import (
    KanbanBoard,
    KanbanStatus,
    KanbanTask,
    TransitionError,
)
from src.state.manager import StateManager
from src.state.merge_patch import merge_patch

# ── RFC 7396 merge_patch ──────────────────────────────────────


class TestMergePatch:
    """RFC 7396 compliance tests."""

    def test_scalar_replacement(self):
        assert merge_patch({"a": 1}, {"a": 2}) == {"a": 2}

    def test_add_new_key(self):
        assert merge_patch({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_remove_via_null(self):
        result = merge_patch({"a": 1, "b": 2}, {"a": None})
        assert result == {"b": 2}
        assert "a" not in result

    def test_nested_merge(self):
        target = {"a": {"b": 1, "c": 2}}
        patch = {"a": {"b": 99}}
        assert merge_patch(target, patch) == {"a": {"b": 99, "c": 2}}

    def test_nested_remove(self):
        target = {"a": {"b": 1, "c": 2}}
        patch = {"a": {"c": None}}
        assert merge_patch(target, patch) == {"a": {"b": 1}}

    def test_array_atomic_replacement(self):
        target = {"a": [1, 2, 3]}
        patch = {"a": [4, 5]}
        assert merge_patch(target, patch) == {"a": [4, 5]}

    def test_non_dict_patch_replaces_entirely(self):
        assert merge_patch({"a": 1}, "string") == "string"
        assert merge_patch({"a": 1}, 42) == 42
        assert merge_patch({"a": 1}, [1, 2]) == [1, 2]

    def test_non_dict_target_becomes_dict(self):
        assert merge_patch("string", {"a": 1}) == {"a": 1}
        assert merge_patch(42, {"b": 2}) == {"b": 2}

    def test_empty_patch_returns_copy(self):
        target = {"a": 1}
        result = merge_patch(target, {})
        assert result == {"a": 1}
        assert result is not target  # must be a copy

    def test_immutability(self):
        target = {"a": {"b": 1}}
        patch = {"a": {"c": 2}}
        merge_patch(target, patch)
        assert target == {"a": {"b": 1}}  # original unchanged


# ── StateManager ──────────────────────────────────────────────


class TestStateManager:
    def test_initial_state_persisted(self, tmp_path: Path):
        sm = StateManager(state_dir=tmp_path, initial_state={"foo": "bar"})
        assert sm.version == 0
        assert sm.get("foo") == "bar"
        assert (tmp_path / "state.json").exists()

    def test_apply_bumps_version(self, tmp_path: Path):
        sm = StateManager(state_dir=tmp_path)
        sm.apply({"key": "value"}, source="test")
        assert sm.version == 1
        assert sm.get("key") == "value"

    def test_version_monotonic(self, tmp_path: Path):
        sm = StateManager(state_dir=tmp_path)
        for i in range(5):
            sm.apply({"step": i}, source="test")
        assert sm.version == 5

    def test_version_protected_from_patch(self, tmp_path: Path):
        sm = StateManager(state_dir=tmp_path)
        sm.apply({"state_version": 999}, source="test")
        # state_version should be 1, not 999
        assert sm.version == 1

    def test_null_removes_key(self, tmp_path: Path):
        sm = StateManager(state_dir=tmp_path, initial_state={"remove_me": True})
        sm.apply({"remove_me": None}, source="test")
        assert sm.get("remove_me") is None

    def test_journal_records_patches(self, tmp_path: Path):
        sm = StateManager(state_dir=tmp_path)
        sm.apply({"a": 1}, source="agent-1")
        sm.apply({"b": 2}, source="agent-2")
        entries = sm.journal()
        assert len(entries) == 2
        assert entries[0]["source"] == "agent-1"
        assert entries[1]["source"] == "agent-2"

    def test_journal_since_version(self, tmp_path: Path):
        sm = StateManager(state_dir=tmp_path)
        sm.apply({"a": 1}, source="test")
        sm.apply({"b": 2}, source="test")
        sm.apply({"c": 3}, source="test")
        entries = sm.journal(since_version=2)
        assert len(entries) == 1
        assert entries[0]["version"] == 3

    def test_persistence_survives_reload(self, tmp_path: Path):
        sm1 = StateManager(state_dir=tmp_path, initial_state={"x": 0})
        sm1.apply({"x": 42}, source="test")
        del sm1
        sm2 = StateManager(state_dir=tmp_path)
        assert sm2.version == 1
        assert sm2.get("x") == 42

    def test_atomic_write_no_partial(self, tmp_path: Path):
        sm = StateManager(state_dir=tmp_path)
        sm.apply({"data": "value"}, source="test")
        # .tmp file should not persist after apply
        assert not (tmp_path / "state.json.tmp").exists()
        # state.json should be valid JSON
        with open(tmp_path / "state.json") as f:
            data = json.load(f)
        assert data["data"] == "value"


# ── Context Compression ──────────────────────────────────────


class TestContextCompression:
    def test_drops_diagnostic_keys(self):
        ctx = {
            "task_id": "t-1",
            "title": "test",
            "full_log": "x" * 5000,
            "raw_response": {"big": "data"},
            "debug_trace": ["a", "b", "c"],
        }
        result = compress_context(ctx)
        assert "task_id" in result
        assert "title" in result
        assert "full_log" not in result
        assert "raw_response" not in result
        assert "debug_trace" not in result

    def test_truncates_long_strings(self):
        ctx = {"title": "short", "body": "x" * 5000}
        result = compress_context(ctx, max_string_len=100)
        assert len(result["body"]) < 200
        assert "truncated" in result["body"]

    def test_preserves_essential_keys(self):
        ctx = {
            "task_id": "t-1",
            "status": "pending",
            "priority": "high",
            "state_version": 3,
        }
        result = compress_context(ctx)
        assert result == ctx

    def test_recursive_compression(self):
        ctx = {
            "task_id": "t-1",
            "nested": {
                "debug_trace": ["a"],
                "important": "keep",
            },
        }
        result = compress_context(ctx)
        assert "debug_trace" not in result["nested"]
        assert result["nested"]["important"] == "keep"

    def test_reduction_ratio(self):
        ctx = {
            "task_id": "t-1",
            "title": "test",
            "full_log": "x" * 10000,
            "raw_response": "y" * 10000,
            "debug_trace": "z" * 10000,
            "intermediate_steps": "w" * 10000,
        }
        result = compress_context(ctx)
        original_size = len(str(ctx))
        compressed_size = len(str(result))
        reduction = 1 - compressed_size / original_size
        assert reduction > 0.40, f"Expected >40% reduction, got {reduction:.0%}"

    def test_does_not_mutate_input(self):
        ctx = {"task_id": "t-1", "full_log": "remove me"}
        original = dict(ctx)
        compress_context(ctx)
        assert ctx == original


# ── Kanban State Machine ─────────────────────────────────────


class TestKanbanBoard:
    def test_add_and_get(self):
        board = KanbanBoard()
        task = KanbanTask(title="Test task")
        board.add(task)
        assert board.get(task.task_id) is task

    def test_valid_transitions(self):
        board = KanbanBoard()
        task = KanbanTask(title="Flow test")
        board.add(task)
        # todo → ready → in_progress → review → done
        board.transition(task.task_id, KanbanStatus.READY)
        assert task.status == KanbanStatus.READY
        board.transition(task.task_id, KanbanStatus.IN_PROGRESS)
        assert task.status == KanbanStatus.IN_PROGRESS
        board.transition(task.task_id, KanbanStatus.REVIEW)
        assert task.status == KanbanStatus.REVIEW
        board.transition(task.task_id, KanbanStatus.DONE)
        assert task.status == KanbanStatus.DONE

    def test_invalid_transition_raises(self):
        board = KanbanBoard()
        task = KanbanTask(title="Bad transition")
        board.add(task)
        with pytest.raises(TransitionError):
            board.transition(task.task_id, KanbanStatus.DONE)  # todo → done not allowed

    def test_done_is_terminal(self):
        board = KanbanBoard()
        task = KanbanTask(title="Terminal")
        board.add(task)
        board.transition(task.task_id, KanbanStatus.READY)
        board.transition(task.task_id, KanbanStatus.IN_PROGRESS)
        board.transition(task.task_id, KanbanStatus.DONE)
        with pytest.raises(TransitionError):
            board.transition(task.task_id, KanbanStatus.IN_PROGRESS)

    def test_block_and_unblock(self):
        board = KanbanBoard()
        task = KanbanTask(title="Block test")
        board.add(task)
        board.transition(task.task_id, KanbanStatus.READY)
        board.block(task.task_id, reason="waiting on dependency")
        assert task.status == KanbanStatus.BLOCKED
        assert task.block_reason == "waiting on dependency"
        board.unblock(task.task_id)
        assert task.status == KanbanStatus.READY
        assert task.block_reason == ""

    def test_audit_trail_recorded(self):
        board = KanbanBoard()
        task = KanbanTask(title="Audit test")
        board.add(task)
        board.transition(task.task_id, KanbanStatus.READY, actor="bot-1")
        board.transition(task.task_id, KanbanStatus.IN_PROGRESS, actor="bot-2")
        assert len(task.audit_trail) == 2
        assert task.audit_trail[0].from_status == "todo"
        assert task.audit_trail[0].to_status == "ready"
        assert task.audit_trail[0].actor == "bot-1"
        assert task.audit_trail[1].actor == "bot-2"

    def test_complete_convenience(self):
        board = KanbanBoard()
        task = KanbanTask(title="Complete test")
        board.add(task)
        board.transition(task.task_id, KanbanStatus.READY)
        board.transition(task.task_id, KanbanStatus.IN_PROGRESS)
        board.complete(task.task_id, summary="all done")
        assert task.status == KanbanStatus.DONE

    def test_columns_grouping(self):
        board = KanbanBoard()
        t1 = KanbanTask(title="T1")
        t2 = KanbanTask(title="T2")
        board.add(t1)
        board.add(t2)
        board.transition(t1.task_id, KanbanStatus.READY)
        cols = board.columns()
        assert len(cols["ready"]) == 1
        assert len(cols["todo"]) == 1

    def test_missing_task_raises(self):
        board = KanbanBoard()
        with pytest.raises(KeyError):
            board.transition("nonexistent", KanbanStatus.READY)

    def test_to_dict_serializable(self):
        board = KanbanBoard()
        board.add(KanbanTask(title="Serialize me"))
        data = board.to_dict()
        assert "columns" in data
        assert "total" in data
        assert data["total"] == 1
        # Ensure it's JSON-serializable
        json.dumps(data)


# ── Domain Priority Tie-breaking ─────────────────────────────


class TestDomainPriority:
    def test_priority_order_defined(self):
        from src.router.semantic import DOMAIN_PRIORITY

        assert DOMAIN_PRIORITY["engineering"] < DOMAIN_PRIORITY["operations"]
        assert DOMAIN_PRIORITY["operations"] < DOMAIN_PRIORITY["business"]
        assert DOMAIN_PRIORITY["business"] < DOMAIN_PRIORITY["research"]
        assert DOMAIN_PRIORITY["research"] < DOMAIN_PRIORITY["governance"]

    def test_all_five_domains_present(self):
        from src.router.semantic import DOMAIN_PRIORITY

        expected = {"engineering", "operations", "business", "research", "governance"}
        assert set(DOMAIN_PRIORITY.keys()) == expected
