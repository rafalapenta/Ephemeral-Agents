from __future__ import annotations

import json
import math
import tempfile
import time
from pathlib import Path

from src.catalog.indexer import run_indexing
from src.router.semantic import route_agent


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def write_agent(path: Path, name: str, description: str, role: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"""---
name: {name}
description: {description}
vibe: Precise specialist.
---
# {name}
- **Role**: {role}
""",
        encoding="utf-8",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(
        prefix="aggency-benchmark-", ignore_cleanup_errors=True
    ) as temporary:
        base = Path(temporary)
        source_root = base / "agency-agents"
        write_agent(
            source_root / "engineering" / "engineering-frontend-developer.md",
            "Frontend Developer",
            "Expert React frontend accessibility and web performance engineer.",
            "Modern UI implementation specialist",
        )
        write_agent(
            source_root / "finance" / "finance-risk-analyst.md",
            "Risk Analyst",
            "Credit risk fixed income and financial covenant analyst.",
            "Financial risk specialist",
        )
        database_path = base / "catalog.db"
        database_url = f"sqlite:///{database_path.as_posix()}"
        chroma_path = base / "chroma"
        report = run_indexing(
            source_root=source_root,
            database_url=database_url,
            chroma_path=chroma_path,
            dry_run=False,
            reindex=True,
        )
        if report.written != 2:
            raise RuntimeError(f"benchmark seed failed: {report}")

        queries = [
            "React frontend accessibility web performance",
            "credit risk fixed income financial covenant",
        ]

        start = time.perf_counter_ns()
        route_agent(
            queries[0],
            0.1,
            database_url=database_url,
            chroma_path=chroma_path,
            source_root=source_root,
        )
        cold_ms = (time.perf_counter_ns() - start) / 1_000_000

        for index in range(20):
            route_agent(
                queries[index % len(queries)],
                0.1,
                database_url=database_url,
                chroma_path=chroma_path,
                source_root=source_root,
            )

        latencies: list[float] = []
        for index in range(200):
            start = time.perf_counter_ns()
            result = route_agent(
                queries[index % len(queries)],
                0.1,
                database_url=database_url,
                chroma_path=chroma_path,
                source_root=source_root,
            )
            elapsed_ms = (time.perf_counter_ns() - start) / 1_000_000
            if not result.matched:
                raise RuntimeError("benchmark query unexpectedly produced no match")
            latencies.append(elapsed_ms)

        metrics = {
            "samples": len(latencies),
            "warmup": 20,
            "cold_ms": round(cold_ms, 3),
            "p50_ms": round(percentile(latencies, 0.50), 3),
            "p95_ms": round(percentile(latencies, 0.95), 3),
            "p99_ms": round(percentile(latencies, 0.99), 3),
            "max_ms": round(max(latencies), 3),
            "target_ms": 500.0,
        }
        metrics["passed"] = metrics["p95_ms"] < metrics["target_ms"]
        print(json.dumps(metrics, indent=2))
        return 0 if metrics["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
