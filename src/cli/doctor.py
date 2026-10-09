"""AGency System Diagnostic Tool (aggency doctor).

Verifies:
1. Embeddings backend configuration and model compatibility.
2. Catalog indexing status (SQLite + ChromaDB vectors).
3. LLM Gateway connectivity and health (/models endpoint, 3s timeout).
4. Director & Ephemeral model tiers and routing configuration.

Security: Never prints secret keys or tokens.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from src.catalog.embeddings import (
    check_embedding_compatibility,
    get_embedding_function,
)
from src.gateway.models import (
    _gateway_base,
    _gateway_key,
    check_gateway_health,
    describe_matrix,
)


def run_doctor() -> int:
    """Run full diagnostic checks and print formatted results to stdout."""
    print("=" * 60)
    print("  AGency System Doctor")
    print("=" * 60)
    print()

    has_warnings = False
    has_errors = False

    # ── 1. Embeddings Configuration ──────────────────────────────
    backend = os.environ.get("AGENCY_EMBEDDINGS", "local").lower().strip()
    print("[1/4] Embeddings Backend")
    print(f"  Configured: AGENCY_EMBEDDINGS={backend}")

    fn = get_embedding_function()
    print(f"  Active Function: {fn.__class__.__name__}")
    print(f"  Active Model:    {getattr(fn, 'model_name', 'deterministic-blake2b')}")

    if backend == "local":
        try:
            import sentence_transformers  # noqa: F401
            print("  Status: [OK] sentence-transformers is installed.")
        except ImportError:
            print("  Status: [WARN] sentence-transformers not installed; falling back to 'hash'.")
            print("          Install with: pip install \"aggency[local-embeddings]\"")
            has_warnings = True
    elif backend == "hash":
        print("  Status: [OK] Hash embeddings (offline / deterministic mode).")
    elif backend == "gateway":
        print("  Status: [OK] Gateway embeddings.")
    else:
        print(f"  Status: [WARN] Unknown backend '{backend}', defaulting to local.")
        has_warnings = True
    print()

    # ── 2. Catalog & Vector Index Status ─────────────────────────
    print("[2/4] Catalog & Vector Index")
    chroma_dir = os.environ.get("CHROMA_PERSIST_DIR", "./chroma_data")
    db_url = os.environ.get("DATABASE_URL", "sqlite:///./data/agency_agents.db")

    print(f"  SQLite DB:   {db_url}")
    print(f"  Chroma Path: {chroma_dir}")

    # Check embedding compatibility
    compatible, compat_msg = check_embedding_compatibility(chroma_dir, fn)
    if compatible:
        print(f"  Index Check: [OK] {compat_msg or 'Catalog embedding configuration is compatible'}")
    else:
        print(f"  Index Check: [WARN] {compat_msg}")
        print("               Please re-index with: aggency-index --reindex")
        has_warnings = True

    # Check vector count
    try:
        import chromadb
        if Path(chroma_dir).exists():
            client = chromadb.PersistentClient(path=str(chroma_dir))
            try:
                coll = client.get_collection("agency_agents")
                count = coll.count()
                print(f"  Vectors:     [OK] {count} agents indexed in Chroma collection 'agency_agents'")
            except Exception:
                print("  Vectors:     [INFO] Collection 'agency_agents' not found yet (run aggency-index)")
        else:
            print("  Vectors:     [INFO] Chroma directory does not exist yet (run aggency-index)")
    except Exception as exc:
        print(f"  Vectors:     [WARN] Could not inspect ChromaDB: {exc}")
        has_warnings = True
    print()

    # ── 3. LLM Gateway & Health Check ────────────────────────────
    print("[3/4] LLM Gateway & API Health Check")
    base_url = _gateway_base()
    raw_key = _gateway_key()
    key_display = "[SET] (masked: " + ("*" * 6) + raw_key[-4:] + ")" if len(raw_key) >= 4 else ("[SET] (masked: ***)" if raw_key else "[NOT SET] (open gateway mode)")

    print(f"  Base URL:  {base_url}")
    print(f"  API Key:   {key_display}")

    is_healthy, health_msg, available_models = check_gateway_health(timeout=3.0)
    if is_healthy:
        print(f"  Status:    [OK] {health_msg}")
        if available_models:
            print(f"  Models:    {len(available_models)} models discovered ({', '.join(available_models[:4])}...)")
    else:
        print(f"  Status:    [WARN] {health_msg}")
        print("             (Diretores e LLM handoff não vão responder enquanto o gateway estiver fora)")
        has_warnings = True
    print()

    # ── 4. Model Matrix & Director Tiers ─────────────────────────
    print("[4/4] Model Matrix & Director Tiers")
    matrix = describe_matrix()
    print(f"  {'Agent':<22} | {'Tier':<22} | {'Primary Model':<30} | {'Fallback':<25}")
    print("  " + "-" * 105)
    for entry in matrix:
        print(f"  {entry['agent']:<22} | {entry['tier']:<22} | {entry['primary']:<30} | {entry['fallback']:<25}")
    print()

    print("=" * 60)
    if has_errors:
        print("  Diagnostic Result: ERRORS DETECTED")
        return 2
    if has_warnings:
        print("  Diagnostic Result: WARNINGS (System partially functional)")
        return 0
    print("  Diagnostic Result: ALL SYSTEMS OPERATIONAL")
    return 0


def main() -> None:
    """CLI entrypoint."""
    sys.exit(run_doctor())


if __name__ == "__main__":
    main()
