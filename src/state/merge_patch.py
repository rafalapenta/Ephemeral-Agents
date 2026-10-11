"""RFC 7396 — JSON Merge Patch.

Pure-Python implementation of RFC 7396, used by the State Manager
to apply partial mutations to task and orchestration state.

Spec: https://datatracker.ietf.org/doc/html/rfc7396
"""

from __future__ import annotations

from typing import Any


def merge_patch(target: Any, patch: Any) -> Any:
    """Apply *patch* to *target* following RFC 7396 semantics.

    Rules:
      - If *patch* is not a dict, the result is *patch* itself
        (scalar/array replacement).
      - If *patch* is a dict, iterate its items:
          • ``null`` values → remove the key from *target*.
          • dict values → recurse.
          • anything else → set the key.

    Both *target* and *patch* are treated as immutable; a new dict
    is returned when mutations are applied.
    """
    if not isinstance(patch, dict):
        return patch

    if not isinstance(target, dict):
        target = {}
    else:
        # shallow copy to avoid mutating the caller's dict
        target = dict(target)

    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        else:
            target[key] = merge_patch(target.get(key), value)

    return target
