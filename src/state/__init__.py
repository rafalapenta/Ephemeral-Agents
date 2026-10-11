"""Project state adapters.

Public re-exports:
  - ``merge_patch``  — RFC 7396 JSON Merge Patch function.
  - ``StateManager`` — Persistent state with journal and locking.
"""

from .manager import JournalEntry, StateManager
from .merge_patch import merge_patch

__all__ = ["JournalEntry", "StateManager", "merge_patch"]
