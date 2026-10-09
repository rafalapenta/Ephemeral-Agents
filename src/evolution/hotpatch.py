"""Hot-patch mechanism for Director SOUL.md guardrails.

Applies surgical atomic updates to the 'Limites Invioláveis' section of
a target director's SOUL.md when a human correction represents a rule or
permission restriction, and logs the change to Git/audit history.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

# Keywords that signify a permission constraint, security rule, or guardrail
RESTRICTION_KEYWORDS = (
    "nunca",
    "não",
    "nao",
    "proibido",
    "vetado",
    "limite",
    "apenas",
    "somente",
    "restrição",
    "restricao",
    "permissão",
    "permissao",
    "guardrail",
    "rule",
    "never",
    "must not",
    "do not",
    "cannot",
    "only",
    "deny",
    "disallow",
    "forbidden",
    "restrict",
)


def is_rule_or_permission_restriction(text: str) -> bool:
    """Return True if text denotes a rule, constraint, or permission restriction."""
    cleaned = text.strip().lower()
    return any(kw in cleaned for kw in RESTRICTION_KEYWORDS)


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str, str]:
    """Split and validate YAML frontmatter from markdown body.

    Returns
    -------
    (frontmatter_dict, raw_frontmatter_block, body)
    """
    if not content.startswith("---"):
        return {}, "", content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, "", content

    raw_yaml = parts[1]
    body = parts[2]
    parsed = yaml.safe_load(raw_yaml) or {}
    if not isinstance(parsed, dict):
        parsed = {}
    return parsed, f"---{raw_yaml}---", body


def apply_human_correction(
    correction_text: str,
    target_director: str,
    *,
    config_dir: Path | str | None = None,
    auto_commit: bool = True,
    history_log_path: Path | str = Path("data/hotpatch_history.jsonl"),
) -> dict[str, Any]:
    """Surgically apply a human correction to the director's SOUL.md.

    If the correction represents a rule or permission restriction, it is appended
    to the 'Limites Invioláveis' section. The write is atomic and preserves
    the exact YAML frontmatter schema.

    Parameters
    ----------
    correction_text:
        The human feedback or instruction.
    target_director:
        The identifier of the director (e.g. 'atlas', 'lyra', 'vulcan').
    config_dir:
        Root configuration directory (defaults to 'src/bots_config').
    auto_commit:
        Whether to attempt a git commit for the change.
    history_log_path:
        Audit log path for tracking hot-patches.

    Returns
    -------
    A dict summarizing the outcome of the patch attempt.
    """
    if not is_rule_or_permission_restriction(correction_text):
        logger.info(
            "Correction for %s is not a rule/permission restriction: %s",
            target_director,
            correction_text,
        )
        return {
            "patched": False,
            "director": target_director,
            "reason": "Correction does not represent a rule or permission restriction",
        }

    base_path = Path(config_dir) if config_dir else Path("src/bots_config")
    soul_path = base_path / target_director / "SOUL.md"

    if not soul_path.exists():
        raise FileNotFoundError(
            f"SOUL.md for director '{target_director}' does not exist at {soul_path}"
        )

    with open(soul_path, encoding="utf-8") as fh:
        original_content = fh.read()

    frontmatter_dict, frontmatter_raw, body = _split_frontmatter(original_content)

    # Format the rule cleanly as a markdown list item
    clean_rule = correction_text.strip()
    if clean_rule.startswith("- "):
        clean_rule = clean_rule[2:].strip()
    formatted_rule = f"- {clean_rule}"

    # Search for Limites Invioláveis section
    section_pattern = re.compile(
        r"(#\s+Limites Invioláveis[^\n]*\n)(.*?)(?=\n#\s+|\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    match = section_pattern.search(body)

    if match:
        section_header = match.group(1)
        section_body = match.group(2)

        # Check if already present to ensure idempotency
        existing_items = [
            line.strip().lower() for line in section_body.splitlines() if line.strip()
        ]
        if formatted_rule.strip().lower() in existing_items:
            return {
                "patched": False,
                "director": target_director,
                "reason": "Rule already present in Limites Invioláveis",
                "path": str(soul_path),
            }

        # Append formatted rule to existing section
        stripped_body = section_body.rstrip()
        new_section_body = f"{stripped_body}\n{formatted_rule}\n" if stripped_body else f"{formatted_rule}\n"
        new_body = (
            body[: match.start()]
            + f"{section_header}{new_section_body}"
            + body[match.end() :]
        )
    else:
        # Create Limites Invioláveis section
        new_section = f"\n# Limites Invioláveis\n{formatted_rule}\n\n"
        # Insert before first workflow or at start of body
        wf_match = re.search(r"\n#\s+Workflows", body, re.IGNORECASE)
        if wf_match:
            new_body = body[: wf_match.start()] + new_section + body[wf_match.start() :]
        else:
            new_body = f"{new_section}{body.lstrip()}"

    # Reconstruct document
    new_content = f"{frontmatter_raw}\n{new_body.lstrip()}" if frontmatter_raw else new_body

    # Safety regression check on frontmatter schema
    test_fm, _, _ = _split_frontmatter(new_content)
    if frontmatter_dict:
        for k in ("name", "role"):
            if k in frontmatter_dict and test_fm.get(k) != frontmatter_dict.get(k):
                raise ValueError(
                    f"Frontmatter schema corruption detected: field '{k}' modified"
                )

    # Atomic write with fsync
    tmp_path = soul_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(new_content)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp_path), str(soul_path))

    # Commit and history logging
    git_committed = False
    commit_msg = f"chore(hotpatch): update guardrails for {target_director}"
    if auto_commit:
        try:
            res_add = subprocess.run(
                ["git", "add", str(soul_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res_add.returncode == 0:
                res_commit = subprocess.run(
                    ["git", "commit", "-m", commit_msg],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                git_committed = res_commit.returncode == 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("Git commit for hotpatch failed: %s", exc)

    # Persist in audit log
    history_record = {
        "timestamp": time.time(),
        "director": target_director,
        "rule": clean_rule,
        "path": str(soul_path),
        "git_committed": git_committed,
    }
    try:
        hist_path = Path(history_log_path)
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        with open(hist_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(history_record, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record hotpatch history: %s", exc)

    return {
        "patched": True,
        "director": target_director,
        "rule": clean_rule,
        "path": str(soul_path),
        "git_committed": git_committed,
    }
