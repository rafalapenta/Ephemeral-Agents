# AGency

Semantic multi-agent orchestration pipeline.

---

## What it does

AGency routes natural-language task descriptions to the most relevant domain agent using a hybrid search strategy, then runs the full task lifecycle through a structured orchestration pipeline. A task arrives as a query string; the `SemanticRouter` scores every registered agent using both ChromaDB vector similarity and SQLite FTS5 lexical matching, then selects the best match above a configurable threshold. The `MacroOrchestrator` takes that routing result and drives the task through a validated Kanban state machine, compresses the handoff context to keep payloads lean, dispatches to the target agent, and evaluates four quality gates before marking the task complete. All state mutations are persisted atomically to disk via `StateManager`, which uses RFC 7396 merge-patch, an append-only journal, and cross-platform file locking to guarantee consistency across restarts.

---

## Architecture

```
query
  |
  v
SemanticRouter  ---- ChromaDB (vector similarity)
  |              ---- SQLite FTS5 (lexical search)
  |
  v
MacroOrchestrator
  |-- KanbanBoard  (todo -> ready -> in_progress -> review -> done)
  |-- ContextCompressor
  |-- Handoff (domain agent dispatch)
  +-- QualityGates (GATE1-GATE4)
  |
  v
StateManager  (state.json + journal.jsonl)
```

The router runs both searches in parallel and fuses scores with a weighted hybrid formula. If no agent clears the threshold the task is moved to `blocked` and the orchestration result records the reason. When a match is found, the context is stripped of verbose keys and string values are truncated before the handoff payload is assembled, keeping downstream token usage predictable.

---

## Project structure

```
src/
|-- catalog/          # Agent indexer -- parses SOUL.md, populates SQLite + ChromaDB
|-- database/         # SQLAlchemy models and Pydantic schemas
|-- macro_agents/     # MacroOrchestrator -- full task lifecycle
|-- mcp_servers/      # FastMCP tool definitions (route_agent, list_agents)
|-- orchestration/    # KanbanBoard state machine + ContextCompressor
|-- router/           # SemanticRouter -- hybrid vector + FTS5 routing
|-- state/            # StateManager -- atomic persistence + journal
+-- bots_config/      # Agent SOUL.md personas (atlas, aura, lyra, sterling, vesper, vulcan)
tests/                # 14 test files
data/                 # Runtime state (goals.json)
docker-compose.yml
pyproject.toml
```

---

## Quickstart

**1. Clone and install**

```bash
git clone <repo>
cd <repo>
pip install -e ".[dev]"
```

**2. Copy the env file**

```bash
cp .env.example .env
# Edit DATABASE_URL, CHROMA_PERSIST_DIR, CATALOG_PATH if needed
```

**3. Index the agent catalog**

First run detects a missing database and initialises automatically. No flags required.

```bash
aggency-index src/bots_config
```

**4. Run the test suite**

```bash
pytest tests/ -q
```

**5. Use the orchestrator directly in Python**

```python
from src.macro_agents.orchestrator import MacroOrchestrator

orch = MacroOrchestrator(
    state_dir="state_data",
    database_url="sqlite:///./data/agency_agents.db",
    chroma_path="./chroma_data",
    source_root="./src/bots_config",
)

result = orch.orchestrate(
    title="Build payment API",
    query="design and implement a REST payment API with Stripe",
    priority="high",
)

print(result.to_dict())
```

The returned `OrchestrationResult` contains the matched agent, all gate verdicts, the final Kanban status, and the current state version.

---

## Key modules

### `src/catalog/indexer.py` and `src/catalog/embeddings.py`

Discovers agent definition files (SOUL.md) under a source root, parses their YAML frontmatter and body to extract `agent_id`, `macro_domain`, `squad`, `name`, `trigger_hooks`, and `system_prompt_path`, then writes the results to both a SQLite relational store (via SQLAlchemy) and a ChromaDB vector collection.

Embeddings are configured via `AGENCY_EMBEDDINGS` (`local` | `gateway` | `hash`):
- **`local` (default)**: Uses `sentence-transformers` with `paraphrase-multilingual-MiniLM-L12-v2` for high-quality local multilingual semantic search without external API costs (install via `pip install "aggency[local-embeddings]"`).
- **`gateway`**: Uses OmniRoute or OpenAI-compatible embedding API (`openai/mistral-embed`).
- **`hash`**: Deterministic Blake2b hashing with zero dependencies, serving as offline fallback and for unit tests.

The indexer saves embedding backend metadata alongside Chroma vectors. On first run it detects a missing database and initialises automatically; subsequent runs require the explicit `--reindex` flag.

### `src/router/semantic.py`

Implements `route_agent()`, the core routing function. It runs two searches concurrently: a ChromaDB approximate nearest-neighbour query using the configured embedding function (`AGENCY_EMBEDDINGS`), and a SQLite FTS5 match against agent names, domains, squads, and trigger hooks. If the current embedding backend or model differs from the metadata used to index the catalog, the router logs a compatibility warning requesting an `aggency-index --reindex`. Scores from both sources are fused with a hybrid formula that favours candidates appearing in both result sets. Ties are broken deterministically by a fixed domain priority order (`engineering` > `operations` > `business` > `research` > `governance`). If the best candidate score falls below the configured threshold the function returns a `RouteAgentResult` with `matched=False`. On a successful match it loads the full agent record from SQLite, reads the system prompt from disk, and assembles the authorised tool list from the `agent_tools` join table.

### `src/macro_agents/orchestrator.py`

Contains `MacroOrchestrator`, which coordinates the seven-step task lifecycle: create a `KanbanTask`, persist initial state, call `route_agent`, compress the handoff context, invoke the handoff function (or simulate it in dry-run mode), update state to `completed`, and evaluate quality gates. Each step is wrapped in structured error handling; a `TransitionError` from the Kanban state machine or any unexpected exception sets the result status to `error` and records the message. The orchestrator is synchronous by design; callers in async contexts should wrap `orchestrate()` with `asyncio.to_thread()`.

### `src/orchestration/kanban.py`

Defines `KanbanBoard` and `KanbanTask` with a strict state machine enforced by a transition table. Valid moves are `todo -> ready`, `ready -> in_progress`, `in_progress -> review`, `review -> done`, with `blocked` reachable from most states and recoverable back to `ready` or `todo`. Any attempt to make an unlisted transition raises `TransitionError`. Every transition appends an `AuditEntry` to the task's `audit_trail` with a timestamp, actor, and optional reason. The board is in-memory; callers are responsible for persisting snapshots via `StateManager` if restart survival is required.

### `src/orchestration/context.py`

Provides `compress_context()`, a pure function that reduces a handoff payload before it is passed to the target agent. It removes a configurable set of verbose keys (`full_log`, `raw_response`, `debug_trace`, `embedding_vector`, and others), truncates any string value longer than 2000 characters with a `[truncated]` suffix, and recurses into nested dicts and lists. A separate set of essential keys (`task_id`, `title`, `priority`, `target_agent`, `state_version`, and others) are always preserved regardless of the drop list. The function never mutates the input dict.

### `src/state/manager.py`

Implements `StateManager`, which persists a JSON state document with monotonically increasing version numbers. Every call to `apply()` accepts an RFC 7396 merge-patch dict, bumps the version, appends a `JournalEntry` to `journal.jsonl`, and atomically replaces `state.json` using `os.replace()` after an `fsync`. Concurrent access is safe via a two-layer lock: a `threading.Lock` for in-process concurrency and an advisory file lock (`fcntl` on POSIX, `msvcrt` on Windows) for multi-process scenarios. The `journal()` method returns all entries since a given version, enabling incremental replay and audit.

---

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and edit as needed.

| Variable | Default | Description |
|---|---|---|
| `AGENCY_EMBEDDINGS` | `local` | Embedding backend: `local` (sentence-transformers), `gateway` (OmniRoute/OpenAI), `hash` (blake2b) |
| `AGENCY_LLM_BASE_URL` | `http://localhost:20128/v1` | OpenAI-compatible gateway base URL (alias: `OMNIROUTE_BASE_URL`) |
| `AGENCY_LLM_API_KEY` | `""` | Gateway bearer token / API key (alias: `OMNIROUTE_API_KEY`) |
| `AGENCY_EPHEMERAL_MODEL`| `mistral/ministral-8b-latest` | Default model for ephemeral subagents |
| `DATABASE_URL` | `sqlite:///./data/agency_agents.db` | SQLAlchemy DB URL for the agent catalog |
| `CHROMA_PERSIST_DIR` | `./chroma_data` | ChromaDB persistence directory |
| `CATALOG_PATH` | `./src/bots_config` | Root path scanned for agent SOUL.md files |
| `ROUTER_THRESHOLD` | `0.25` | Minimum hybrid score to consider a routing match |
| `CORS_ORIGINS` | `http://localhost:8080,http://127.0.0.1:8080` | Allowed origins for any HTTP interface |
| `LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `MODEL_PROVIDER` | `openai` | LLM provider used for agent dispatch |
| `MODEL_NAME` | `gpt-4o-mini` | Model name passed to the LLM provider |
| `MCP_SERVER_URL` | `http://localhost:8765` | FastMCP server endpoint |
| `MCP_TIMEOUT_SECONDS` | `30` | Timeout for MCP tool calls |

---

## Running with Docker

```bash
docker-compose up --build
```

The compose file defines two named volumes:

| Volume | Container path | Purpose |
|---|---|---|
| `agency_data` | `/app/data` | Persistent state, goals, and the SQLite catalog |
| `agency_chroma` | `/app/chroma_data` | ChromaDB vector store |

The service exposes port `8080` by default. Override with `DASHBOARD_PORT` in your environment if needed.

Health check: `GET http://localhost:8080/health` must return `200` within 10 seconds, retried up to 3 times at 30-second intervals.

---

## Adding agents

Create a Markdown file inside `src/bots_config/<macro_domain>/` with YAML frontmatter:

```markdown
---
name: My Agent
description: One-line description used as a trigger hook.
vibe: Short personality note.
trigger_hooks:
  - keyword one
  - keyword two
---

# My Agent

- **Role**: What this agent does
```

Then reindex:

```bash
aggency-index src/bots_config --reindex
```

The agent is immediately available to the router on the next `orchestrate()` call.

---

## Testing

The test suite covers the catalog indexer, semantic router, Kanban state machine, state manager, MCP server, and end-to-end orchestration. All tests use `tmp_path` fixtures and in-memory or temporary SQLite/ChromaDB instances; no external services are required.

```bash
# Run all tests
pytest tests/ -q

# Run a specific module
pytest tests/test_router.py -v

# Skip performance tests
pytest tests/ -q -m "not performance"
```

---

## License

MIT. See [LICENSE](LICENSE) for details.
