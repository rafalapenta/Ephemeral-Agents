from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import yaml
from pydantic import ValidationError
from sqlalchemy import create_engine, delete, text
from sqlalchemy.orm import Session

from src.database.models import Agent, AgentTool, Base
from src.database.schemas import AgentCreate

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_URL = f"sqlite:///{PROJECT_ROOT / 'agency_agents.db'}"
DEFAULT_CHROMA_PATH = PROJECT_ROOT / "chroma"
_COLLECTION_NAME = "agency_agents"

_EXCLUDED_DIRS = {
    "examples", "strategy", "templates", "tests", ".git", "archive",
    "docs", "scripts", "playbooks", "runbooks", "coordination"
}
_IGNORED_FILENAMES = {
    "readme.md", "summary.md", "contributing.md", "license.md",
    "changelog.md", "claude.md", "agents.md", "executive-brief.md", "quickstart.md", "nexus-strategy.md"
}
_ROLE_PATTERN = re.compile(r"(?mi)^\s*-\s*\*\*Role\*\*:\s*(.+?)\s*$")
_TOKEN_PATTERN = re.compile(r"(?u)\b[\w-]+\b")

_SYNONYMS = {
    "privacy": ["lgpd", "gdpr", "privacidade", "conformidade", "clinicos"],
    "dpo": ["lgpd", "privacidade", "lgpd compliance"],
    "sql": ["postgresql", "migration", "postgres", "indices"],
    "database": ["postgresql", "sql", "migration", "indices", "postgres"],
    "fpa": ["dre", "financeiro", "trimestre", "fp&a"],
    "outbound": ["b2b", "precificacao", "contratos", "corporativos"],
    "ux": ["onboarding", "realidade", "espacial", "visionos"],
}


@dataclass(frozen=True, slots=True)
class IndexingReport:
    discovered: int
    valid: int
    invalid: int
    written: int
    errors: list[str] = field(default_factory=list)


class HashEmbeddingFunction:
    def __init__(self, dimensions: int = 256) -> None:
        if dimensions < 16:
            raise ValueError("embedding dimensions must be at least 16")
        self.dimensions = dimensions

    def __call__(self, input: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for document in input:
            vector = [0.0] * self.dimensions
            for token in _TOKEN_PATTERN.findall(document.casefold()):
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dimensions
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise ValueError("missing YAML frontmatter")
    lines = text.splitlines()
    try:
        closing = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("unterminated YAML frontmatter") from exc
    raw = "\n".join(lines[1:closing])
    metadata = yaml.safe_load(raw) or {}
    if not isinstance(metadata, dict):
        raise ValueError("YAML frontmatter must be a mapping")
    return metadata, "\n".join(lines[closing + 1 :])


def _non_empty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _trigger_hooks(metadata: dict[str, Any], body: str) -> list[str]:
    candidates: list[str | None] = [
        _non_empty_string(metadata.get("description")),
        _non_empty_string(metadata.get("vibe")),
    ]
    raw_hooks = metadata.get("trigger_hooks")
    if isinstance(raw_hooks, list):
        for h in raw_hooks:
            candidates.append(_non_empty_string(h))
    elif isinstance(raw_hooks, str):
        candidates.append(_non_empty_string(raw_hooks))

    role_match = _ROLE_PATTERN.search(body)
    candidates.append(role_match.group(1).strip() if role_match else None)

    res = list(dict.fromkeys(value for value in candidates if value))
    return res


def parse_agent_markdown(path: Path, source_root: Path) -> AgentCreate:
    source_root = source_root.resolve()
    path = path.resolve()
    try:
        relative = path.relative_to(source_root)
    except ValueError as exc:
        raise ValueError(f"agent path is outside source root: {path}") from exc
    if len(relative.parts) < 2:
        raise ValueError("agent Markdown must live inside a macro-domain directory")

    metadata, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    macro_domain = relative.parts[0]
    squad = relative.parts[-2] if len(relative.parts) > 2 else None

    agent_id = path.parent.name if path.stem.lower() == "soul" else path.stem
    name = _non_empty_string(metadata.get("name")) or agent_id.replace("-", " ").title()
    hooks = _trigger_hooks(metadata, body)
    if not hooks:
        hooks = [agent_id.replace("-", " "), name]

    return AgentCreate(
        agent_id=agent_id,
        macro_domain=macro_domain,
        squad=squad,
        name=name,
        system_prompt_path=relative.as_posix(),
        trigger_hooks=hooks,
        is_active=True,
    )


parse_markdown_agent = parse_agent_markdown


def discover_agent_files(source_root: Path) -> Iterable[Path]:
    source_root = source_root.resolve()
    for path in sorted(source_root.rglob("*.md")):
        if path.name.lower() in _IGNORED_FILENAMES or any(part.startswith(".") for part in path.parts):
            continue
        if any(part.lower() in _EXCLUDED_DIRS for part in path.parts):
            continue
        if path.parent == source_root:
            continue
        yield path


def init_fts5_db(engine, session):
    Base.metadata.create_all(bind=engine)
    try:
        session.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS agents_fts USING fts5(
                agent_id UNINDEXED,
                name,
                macro_domain,
                squad,
                trigger_hooks,
                search_text
            );
        """))
        session.commit()
    except Exception:
        session.rollback()


def _persist_relational(agents: list[AgentCreate], database_url: str, reindex: bool) -> None:
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        init_fts5_db(engine, session)
        if reindex:
            session.execute(delete(AgentTool))
            session.execute(delete(Agent))
            try:
                session.execute(text("DELETE FROM agents_fts;"))
            except Exception:
                pass
            session.commit()

        for item in agents:
            session.merge(
                Agent(
                    agent_id=item.agent_id,
                    macro_domain=item.macro_domain,
                    squad=item.squad,
                    name=item.name,
                    system_prompt_path=item.system_prompt_path,
                    trigger_hooks=item.trigger_hooks,
                    is_active=item.is_active,
                )
            )

            full_base = f"{item.agent_id} {item.name} {item.macro_domain} {item.squad or ''} {' '.join(item.trigger_hooks)}".casefold()
            extra_terms = []
            for key_term, syn_list in _SYNONYMS.items():
                if key_term in full_base:
                    extra_terms.extend(syn_list)

            search_text = f"{full_base} {' '.join(extra_terms)}"

            try:
                session.execute(
                    text("""
                        INSERT INTO agents_fts (agent_id, name, macro_domain, squad, trigger_hooks, search_text)
                        VALUES (:agent_id, :name, :macro_domain, :squad, :trigger_hooks, :search_text)
                    """),
                    {
                        "agent_id": item.agent_id,
                        "name": item.name,
                        "macro_domain": item.macro_domain,
                        "squad": item.squad or "",
                        "trigger_hooks": " ".join(item.trigger_hooks),
                        "search_text": search_text
                    }
                )
            except Exception:
                pass
        session.commit()


def _persist_vectors(
    agents: list[AgentCreate],
    chroma_path: Path,
    reindex: bool,
    embedding_function: HashEmbeddingFunction,
) -> None:
    import chromadb

    client = chromadb.PersistentClient(path=str(chroma_path))
    names = {
        item.name if hasattr(item, "name") else str(item)
        for item in client.list_collections()
    }
    if reindex and _COLLECTION_NAME in names:
        client.delete_collection(_COLLECTION_NAME)
    collection = client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    if not agents:
        return
    documents = ["\n".join(item.trigger_hooks) for item in agents]
    collection.upsert(
        ids=[item.agent_id for item in agents],
        documents=documents,
        metadatas=[
            {
                "agent_id": item.agent_id,
                "macro_domain": item.macro_domain,
                "squad": item.squad or "",
            }
            for item in agents
        ],
        embeddings=embedding_function(documents),
    )


def run_indexing(
    *,
    source_root: Path | str,
    database_url: str = DEFAULT_DB_URL,
    chroma_path: Path | str = DEFAULT_CHROMA_PATH,
    dry_run: bool = False,
    reindex: bool = False,
    allow_partial: bool = False,
    embedding_function: HashEmbeddingFunction | None = None,
) -> IndexingReport:
    if not dry_run and not reindex:
        raise ValueError("persistent catalog changes require reindex=True")
    source_root_path = Path(source_root)
    chroma_path_obj = Path(chroma_path)

    files = list(discover_agent_files(source_root_path))
    agents: list[AgentCreate] = []
    errors: list[str] = []
    for path in files:
        try:
            agents.append(parse_agent_markdown(path, source_root_path))
        except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
            errors.append(f"{path}: {exc}")

    if (dry_run or errors) and not allow_partial:
        if dry_run:
            return IndexingReport(
                discovered=len(files),
                valid=len(agents),
                invalid=len(errors),
                written=0,
                errors=errors,
            )
        if errors and not allow_partial:
            return IndexingReport(
                discovered=len(files),
                valid=len(agents),
                invalid=len(errors),
                written=0,
                errors=errors,
            )

    embedder = embedding_function or HashEmbeddingFunction()
    if not dry_run:
        _persist_relational(agents, database_url, reindex)
        _persist_vectors(agents, chroma_path_obj, reindex, embedder)

    return IndexingReport(
        discovered=len(files),
        valid=len(agents),
        invalid=len(errors),
        written=0 if dry_run else len(agents),
        errors=errors,
    )


run_indexer = run_indexing


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and index the agency-agents catalog")
    parser.add_argument("source_root", nargs="?", type=Path, default=None, help="Path to the read-only agency-agents checkout")
    parser.add_argument("--path", dest="path_flag", type=Path, default=None, help="Flag opcional para caminho do catálogo")
    parser.add_argument("--database-url", default=DEFAULT_DB_URL)
    parser.add_argument("--chroma-path", type=Path, default=DEFAULT_CHROMA_PATH)
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing SQLite or ChromaDB")
    parser.add_argument("--reindex", action="store_true", help="Explicitly rebuild the persisted catalog")
    parser.add_argument("--allow-partial", action="store_true", help="Ignora erros individuais e prossegue")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    target_path = args.path_flag or args.source_root
    if not target_path:
        print("❌ Erro: informe o caminho do catálogo (posicional ou via --path).")
        return 1

    report = run_indexing(
        source_root=target_path,
        database_url=args.database_url,
        chroma_path=args.chroma_path,
        dry_run=args.dry_run,
        reindex=args.reindex,
        allow_partial=args.allow_partial,
    )
    print(f"📦 Descobertos: {report.discovered} | Válidos: {report.valid} | Ignorados: {report.invalid}")
    if report.errors:
        for err in report.errors:
            print(f"  ⚠️ {err}")
    if report.written > 0:
        print(f"🚀 {report.written} agentes persistidos com sucesso.")
    return 0 if (report.invalid == 0 or args.allow_partial) else 2


if __name__ == "__main__":
    raise SystemExit(main())
