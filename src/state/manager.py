"""Persistent State Manager with journal, versioning, and cross-platform locking.

Provides atomic state mutations via RFC 7396 merge-patch, append-only
journal for audit, and monotonically increasing state versions.

Thread-safety:
  - ``fcntl`` on POSIX, ``msvcrt`` on Windows.
  - ``threading.Lock`` for in-process concurrency.

Persistence:
  - ``os.replace`` for atomic writes (no partial state on crash).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .merge_patch import merge_patch

_DEFAULT_STATE_DIR = Path("state_data")


# ── Cross-platform file locking ────────────────────────────────

class _FileLock:
    """Advisory file lock: fcntl on POSIX, msvcrt on Windows."""

    def __init__(self, lock_path: Path) -> None:
        self._path = lock_path
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._path), os.O_CREAT | os.O_RDWR)
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(self._fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_EX)

    def release(self) -> None:
        if self._fd is None:
            return
        if sys.platform == "win32":
            import msvcrt
            try:
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None


# ── Journal Entry ──────────────────────────────────────────────

class JournalEntry:
    """Single append-only journal record."""

    __slots__ = ("patch", "source", "timestamp", "version")

    def __init__(
        self,
        version: int,
        timestamp: float,
        patch: dict[str, Any],
        source: str = "system",
    ) -> None:
        self.version = version
        self.timestamp = timestamp
        self.patch = patch
        self.source = source

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "timestamp": self.timestamp,
            "patch": self.patch,
            "source": self.source,
        }


# ── State Manager ─────────────────────────────────────────────

class StateManager:
    """Manages persistent JSON state with RFC 7396 merge-patch mutations.

    Parameters
    ----------
    state_dir:
        Directory for ``state.json``, ``journal.jsonl``, and ``.state.lock``.
    initial_state:
        Seed state used when no ``state.json`` exists on disk.
    """

    def __init__(
        self,
        state_dir: Path | str = _DEFAULT_STATE_DIR,
        initial_state: dict[str, Any] | None = None,
    ) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

        self._state_path = self._dir / "state.json"
        self._journal_path = self._dir / "journal.jsonl"
        self._lock_path = self._dir / ".state.lock"

        self._file_lock = _FileLock(self._lock_path)
        self._thread_lock = threading.Lock()

        # Load or initialise
        if self._state_path.exists():
            self._state = self._read_state()
        else:
            self._state = {
                "state_version": 0,
                **(initial_state or {}),
            }
            self._persist()

    # ── Public API ─────────────────────────────────────────────

    @property
    def version(self) -> int:
        return self._state.get("state_version", 0)

    def get(self, key: str | None = None) -> Any:
        """Return the full state dict, or a single key."""
        if key is None:
            return dict(self._state)
        return self._state.get(key)

    def apply(
        self,
        patch: dict[str, Any],
        *,
        source: str = "system",
    ) -> dict[str, Any]:
        """Apply an RFC 7396 merge-patch, persist, and return the new state.

        The ``state_version`` is bumped monotonically.
        A journal entry is appended before the state file is written.
        """
        with self._locked():
            new_version = self.version + 1
            # Protect state_version from external patches
            patch_clean = {k: v for k, v in patch.items() if k != "state_version"}
            versioned_patch = {**patch_clean, "state_version": new_version}

            self._state = merge_patch(self._state, versioned_patch)

            entry = JournalEntry(
                version=new_version,
                timestamp=time.time(),
                patch=patch_clean,
                source=source,
            )
            self._append_journal(entry)
            self._persist()

        return dict(self._state)

    def journal(self, *, since_version: int = 0) -> list[dict[str, Any]]:
        """Return journal entries with ``version > since_version``."""
        entries: list[dict[str, Any]] = []
        if not self._journal_path.exists():
            return entries
        with open(self._journal_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("version", 0) > since_version:
                    entries.append(record)
        return entries

    def record_telemetry(
        self,
        metrics: dict[str, Any],
        journal_path: Path | str | None = None,
    ) -> dict[str, Any]:
        """Record post-task execution telemetry in journal.jsonl."""
        target = Path(journal_path) if journal_path is not None else Path("data/journal.jsonl")
        return record_telemetry(metrics, journal_path=target)

    # ── Internals ──────────────────────────────────────────────

    @contextmanager
    def _locked(self) -> Generator[None, None, None]:
        self._thread_lock.acquire()
        try:
            self._file_lock.acquire()
            try:
                yield
            finally:
                self._file_lock.release()
        finally:
            self._thread_lock.release()

    def _read_state(self) -> dict[str, Any]:
        with open(self._state_path, encoding="utf-8") as fh:
            return json.load(fh)

    def _persist(self) -> None:
        tmp_path = self._state_path.with_suffix(".tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(self._state, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp_path), str(self._state_path))

    def _append_journal(self, entry: JournalEntry) -> None:
        with open(self._journal_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def record_telemetry(
    metrics: dict[str, Any],
    journal_path: Path | str = Path("data/journal.jsonl"),
) -> dict[str, Any]:
    """Append a post-execution telemetry record to data/journal.jsonl.

    Ensures thread-safe and process-safe writing with advisory file locking.
    """
    target = Path(journal_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / f".{target.name}.lock"
    lock = _FileLock(lock_path)
    lock.acquire()
    try:
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(metrics, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        lock.release()
    return metrics

