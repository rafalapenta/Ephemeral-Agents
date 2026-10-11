#!/usr/bin/env python3
"""
validate_context_budget.py — Context Budget Gate para Roster C-Level

Valida que nenhum diretor excede o limite de tokens base (2.000)
com suas skills ativas registradas no active_skills_manifest.json.

Uso:
    python scripts/validate_context_budget.py
    python scripts/validate_context_budget.py --strict
    python scripts/validate_context_budget.py --json
"""

import json
import sys
from pathlib import Path

MAX_CONTEXT_TOKENS = 2000
ESTIMATED_TOKENS_PER_SKILL = 200  # Estimativa conservadora por skill active


def load_manifest(manifest_path: Path) -> dict:
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_director(director_name: str, director_data: dict) -> dict:
    active_skills = director_data.get("active_skills", [])
    num_skills = len(active_skills)
    estimated_tokens = num_skills * ESTIMATED_TOKENS_PER_SKILL
    budget_max = director_data.get("context_budget_max", MAX_CONTEXT_TOKENS)

    is_valid = estimated_tokens <= budget_max
    headroom = budget_max - estimated_tokens

    return {
        "director": director_name,
        "role": director_data.get("role", "Unknown"),
        "active_skills_count": num_skills,
        "estimated_tokens": estimated_tokens,
        "budget_max": budget_max,
        "headroom": headroom,
        "is_valid": is_valid,
        "skills": [s["name"] for s in active_skills],
    }


def main():
    base = Path(__file__).parent.parent / "src" / "bots_config"
    manifest_path = base / "catalog" / "active_skills_manifest.json"

    if not manifest_path.exists():
        print(f"ERROR: Manifest not found at {manifest_path}")
        sys.exit(1)

    manifest = load_manifest(manifest_path)
    directors = manifest.get("directors", {})

    results = []
    all_valid = True

    print("=" * 60)
    print("CONTEXT BUDGET VALIDATION — Roster C-Level")
    print("=" * 60)
    print()

    for name, data in directors.items():
        result = validate_director(name, data)
        results.append(result)

        status = "✓ PASS" if result["is_valid"] else "✗ FAIL"
        print(f"[{status}] {name.upper()}")
        print(f"       Role: {result['role']}")
        print(f"       Active Skills: {result['active_skills_count']}")
        print(
            f"       Estimated Tokens: {result['estimated_tokens']} / {result['budget_max']}"
        )
        print(f"       Headroom: {result['headroom']} tokens")
        print()

        if not result["is_valid"]:
            all_valid = False

    print("-" * 60)
    total_skills = sum(r["active_skills_count"] for r in results)
    total_estimated = sum(r["estimated_tokens"] for r in results)
    print(f"TOTAL: {total_skills} active skills across {len(results)} directors")
    print(f"TOTAL ESTIMATED TOKENS: {total_estimated}")
    print()

    if all_valid:
        print("✓ ALL DIRECTORS WITHIN CONTEXT BUDGET")
        sys.exit(0)
    else:
        print("✗ SOME DIRECTORS EXCEED CONTEXT BUDGET")
        sys.exit(1)


if __name__ == "__main__":
    main()
