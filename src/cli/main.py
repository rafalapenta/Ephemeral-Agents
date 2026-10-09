"""Main CLI dispatcher for AGency (`aggency`)."""
from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: aggency <command> [options]")
        print()
        print("Commands:")
        print("  doctor      Run system diagnostics (embeddings, index, LLM gateway, models)")
        print("  index       Discover and index agent catalog")
        print("  mcp         Start MCP server")
        print("  scheduler   Run autonomous scheduler")
        sys.exit(0)

    cmd = args[0].lower()
    if cmd == "doctor":
        from src.cli.doctor import run_doctor
        sys.exit(run_doctor())
    elif cmd == "index":
        from src.catalog.indexer import main as indexer_main
        sys.argv = [sys.argv[0]] + args[1:]
        indexer_main()
    elif cmd == "mcp":
        from src.mcp_servers.server import main as mcp_main
        sys.argv = [sys.argv[0]] + args[1:]
        mcp_main()
    elif cmd == "scheduler":
        from src.scheduler.runner import main as scheduler_main
        sys.argv = [sys.argv[0]] + args[1:]
        scheduler_main()
    else:
        print(f"Unknown command: '{cmd}'. Run 'aggency --help' for available commands.")
        sys.exit(1)


if __name__ == "__main__":
    main()
