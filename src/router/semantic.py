from __future__ import annotations

import math
import re
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, selectinload, sessionmaker

from src.catalog.embeddings import check_embedding_compatibility, get_embedding_function
from src.catalog.skills_engine import DirectorSkillsEngine, SkillMatchResult
from src.database.models import Agent, AgentTool

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB_URL = os.getenv("DATABASE_URL", f"sqlite:///{_REPO_ROOT / 'agency_agents.db'}")
DEFAULT_CHROMA_PATH = Path(os.getenv("CHROMA_PERSIST_DIR", str(_REPO_ROOT / "chroma")))
DEFAULT_CATALOG_PATH = Path(os.getenv("CATALOG_PATH", str(_REPO_ROOT / "src" / "bots_config")))


# Fixed domain priority for deterministic tie-breaking (lower = higher priority)
DOMAIN_PRIORITY: dict[str, int] = {
    'engineering': 0,
    'operations': 1,
    'business': 2,
    'research': 3,
    'governance': 4,
}
_DEFAULT_DOMAIN_PRIORITY = 99


class CatalogEmptyError(RuntimeError):
    pass


class RouterUnavailableError(RuntimeError):
    pass


class RouterDataError(RuntimeError):
    pass


class AuthorizedTool(BaseModel):
    model_config = ConfigDict(extra='ignore')

    tool_id: str
    name: str
    description: str
    tool_type: str
    read_only: bool
    input_schema: dict = Field(default_factory=dict)
    output_schema: dict = Field(default_factory=dict)
    permissions_override: dict | None = None


class RouteAgentResult(BaseModel):
    model_config = ConfigDict(extra='ignore')

    matched: bool
    score: float | None = None
    agent_id: str | None = None
    name: str | None = None
    macro_domain: str | None = None
    squad: str | None = None
    system_prompt: str | None = None
    tools: list[AuthorizedTool] = Field(default_factory=list)
    skill_match: dict[str, Any] | None = None
    reason: str


def _read_prompt(source_root: Path, relative_path: str) -> str:
    root = source_root.resolve()
    prompt_path = (root / relative_path).resolve()
    try:
        prompt_path.relative_to(root)
    except ValueError as exc:
        raise RouterDataError('agent prompt path escapes catalog root') from exc
    try:
        return prompt_path.read_text(encoding='utf-8')
    except OSError as exc:
        raise RouterDataError(f'unable to read agent prompt: {relative_path}') from exc


def _authorized_tools(agent: Agent) -> list[AuthorizedTool]:
    tools: list[AuthorizedTool] = []
    for link in sorted(agent.tool_links, key=lambda item: item.tool_id):
        tool = link.tool
        tools.append(
            AuthorizedTool(
                tool_id=tool.tool_id,
                name=tool.name,
                description=tool.description,
                tool_type=tool.tool_type,
                read_only=tool.read_only,
                input_schema=tool.input_schema,
                output_schema=tool.output_schema,
                permissions_override=link.permissions_override,
            )
        )
    return tools


def route_agent(
    query: str,
    threshold: float = 0.30,
    *,
    database_url: str | None = None,
    chroma_path: Path | str | None = None,
    source_root: Path | str | None = None,
    embedding_function: Any | None = None,
    **kwargs
) -> RouteAgentResult:
    import logging
    _logger = logging.getLogger(__name__)

    query = query.strip()
    if not query:
        raise ValueError('query must not be empty')
    if not 0.0 <= threshold <= 1.0:
        raise ValueError('threshold must be between 0 and 1')

    db_url = database_url or DEFAULT_DB_URL
    c_path = Path(chroma_path) if chroma_path else DEFAULT_CHROMA_PATH
    s_root = Path(source_root) if source_root else DEFAULT_CATALOG_PATH

    embedder = embedding_function or get_embedding_function()

    # Check compatibility with index
    is_compat, warn_msg = check_embedding_compatibility(c_path, embedder)
    if not is_compat and warn_msg:
        _logger.warning(warn_msg)

    # 1. ChromaDB Vector Search
    vector_scores: dict[str, float] = {}
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(c_path))
        try:
            collection = client.get_collection('agency_agents')
            count = collection.count()
            if count > 0:
                v_res = collection.query(
                    query_embeddings=embedder([query]),
                    n_results=min(15, count),
                    include=['distances'],
                )
                ids = v_res.get('ids') or [[]]
                dists = v_res.get('distances') or [[]]
                for aid, dist in zip(ids[0], dists[0], strict=False):
                    sim = max(0.0, min(1.0, 1.0 - float(dist)))
                    vector_scores[aid] = sim
        except Exception:
            pass
    except Exception:
        pass

    # 2. SQLite FTS5 Lexical Search
    engine = create_engine(db_url)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    fts_scores: dict[str, float] = {}
    try:
        raw_tokens = re.findall(r'\b[a-zA-Z0-9_]{2,}\b', query)
        stopwords = {'preciso', 'criar', 'uma', 'para', 'otimizar', 'busca', 'desenhar', 'fluxo', 'aplicativo', 'montar', 'proximo', 'dados', 'segundo', 'normas', 'analisar'}
        clean_terms = [t for t in raw_tokens if t.lower() not in stopwords]
        if clean_terms:
            fts_q = ' OR '.join(f'"{t}"' for t in clean_terms)
            sql = "SELECT agent_id, rank FROM agents_fts WHERE agents_fts MATCH :q ORDER BY rank LIMIT 15"
            rows = session.execute(text(sql), {"q": fts_q}).fetchall()
            for r in rows:
                abs_rank = abs(float(r[1]))
                fts_scores[r[0]] = 1.0 - math.exp(-abs_rank / 5.0)
    except Exception:
        pass

    # 3. Hybrid Scoring Fusion
    candidates = set(vector_scores.keys()) | set(fts_scores.keys())
    if not candidates:
        session.close()
        raise CatalogEmptyError('agency_agents collection or database is empty')

    # Resolve macro_domain for each candidate (for tie-breaking)
    domain_map: dict[str, str] = {}
    if candidates:
        rows = session.execute(
            select(Agent.agent_id, Agent.macro_domain).where(
                Agent.agent_id.in_(candidates)
            )
        ).fetchall()
        domain_map = {r[0]: r[1] for r in rows}

    ranked: list[tuple[str, float, int]] = []
    for aid in candidates:
        v_score = vector_scores.get(aid, 0.0)
        f_score = fts_scores.get(aid, 0.0)

        if aid in vector_scores and aid in fts_scores:
            final_score = max(v_score, f_score, (0.40 * v_score) + (0.60 * f_score))
        elif aid in vector_scores:
            final_score = v_score * 0.90
        else:
            final_score = f_score

        domain = domain_map.get(aid, '').lower()
        priority = DOMAIN_PRIORITY.get(domain, _DEFAULT_DOMAIN_PRIORITY)
        ranked.append((aid, final_score, priority))

    # Sort by score descending, then domain priority ascending (deterministic tie-break)
    ranked.sort(key=lambda x: (-x[1], x[2]))
    best_agent_id, best_score, _ = ranked[0]

    if best_score < threshold:
        session.close()
        return RouteAgentResult(
            matched=False,
            score=round(best_score, 4),
            tools=[],
            reason=f'best candidate score ({best_score:.4f}) below threshold ({threshold:.2f})',
        )

    # 4. Resolve Agent details from SQLite
    try:
        statement = (
            select(Agent)
            .where(Agent.agent_id == best_agent_id, Agent.is_active.is_(True))
            .options(selectinload(Agent.tool_links).selectinload(AgentTool.tool))
        )
        agent = session.scalar(statement)
        if agent is None:
            return RouteAgentResult(
                matched=False,
                score=round(best_score, 4),
                tools=[],
                reason='agent found in index but inactive or missing in database',
            )

        # Resolve director skill if applicable
        skill_match_dict: dict[str, Any] | None = None
        try:
            skills_engine = DirectorSkillsEngine(
                db_session=session,
                embedder=embedding_function,
                threshold=0.72,
            )
            match_res = skills_engine.resolve_skill(agent.agent_id, query)
            skill_match_dict = {
                "matched": match_res.matched,
                "score": match_res.score,
                "candidate_id": match_res.candidate_id,
                "skill_name": match_res.skill.name if match_res.skill else None,
                "reason": match_res.reason,
            }
        except (AttributeError, RuntimeError, ValueError, KeyError, TypeError):
            skill_match_dict = None

        return RouteAgentResult(
            matched=True,
            score=round(best_score, 4),
            agent_id=agent.agent_id,
            name=agent.name,
            macro_domain=agent.macro_domain,
            squad=agent.squad,
            system_prompt=_read_prompt(s_root, agent.system_prompt_path),
            tools=_authorized_tools(agent),
            skill_match=skill_match_dict,
            reason='matched candidate above threshold',
        )
    finally:
        session.close()


def resolve_director_skill(
    director_id: str,
    task_prompt: str,
    threshold: float = 0.72,
    *,
    database_url: str | None = None,
    chroma_path: Path | str | None = None,
    embedding_function: Any = None,
) -> SkillMatchResult:
    """Resolve authorized skill for a director with department isolation and thresholding."""
    db_url = database_url or DEFAULT_DB_URL
    engine = create_engine(db_url)
    c_path = Path(chroma_path) if chroma_path else DEFAULT_CHROMA_PATH

    chroma_client = None
    try:
        import chromadb
        chroma_client = chromadb.PersistentClient(path=str(c_path))
    except (ImportError, OSError, RuntimeError, ValueError):
        chroma_client = None

    with Session(engine) as session:
        engine_instance = DirectorSkillsEngine(
            db_session=session,
            chroma_client=chroma_client,
            embedder=embedding_function,
            threshold=threshold,
        )
        return engine_instance.resolve_skill(director_id, task_prompt)

