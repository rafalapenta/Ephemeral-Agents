"""Seed script to migrate and populate the skills catalog and DirectorSkill associations."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session

from src.database.models import Agent, Base, DirectorSkill, SkillCatalog

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_URL = os.getenv("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'agency_agents.db'}")
DEFAULT_CATALOG_PATH = PROJECT_ROOT / "src" / "bots_config" / "catalog" / "ephemeral_tools.json"

DIRECTOR_METADATA: dict[str, dict[str, Any]] = {
    "atlas": {
        "name": "Atlas",
        "macro_domain": "governance",
        "system_prompt_path": "src/bots_config/atlas/SOUL.md",
        "trigger_hooks": ["governance", "orchestration", "strategy", "ceo"],
    },
    "vulcan": {
        "name": "Vulcan",
        "macro_domain": "engineering",
        "system_prompt_path": "src/bots_config/vulcan/SOUL.md",
        "trigger_hooks": ["software engineering", "devops", "infrastructure", "security"],
    },
    "aura": {
        "name": "Aura",
        "macro_domain": "product",
        "system_prompt_path": "src/bots_config/aura/SOUL.md",
        "trigger_hooks": ["product", "design", "ux", "ui"],
    },
    "vesper": {
        "name": "Vesper",
        "macro_domain": "growth",
        "system_prompt_path": "src/bots_config/vesper/SOUL.md",
        "trigger_hooks": ["growth", "sales", "marketing", "market intelligence"],
    },
    "sterling": {
        "name": "Sterling",
        "macro_domain": "business",
        "system_prompt_path": "src/bots_config/sterling/SOUL.md",
        "trigger_hooks": ["finance", "unit economics", "contracts", "operations"],
    },
    "lyra": {
        "name": "Lyra",
        "macro_domain": "research",
        "system_prompt_path": "src/bots_config/lyra/SOUL.md",
        "trigger_hooks": ["research", "spatial data", "analysis", "experiments"],
    },
}

BASE_DIRECTOR_SKILLS: list[dict[str, Any]] = [
    # Vulcan (CTO / Engineering & Infrastructure)
    {
        "id": "pytest-runner",
        "name": "Pytest Runner",
        "description": "Execução autônoma de suíte de testes unitários e de integração com pytest.",
        "director_id": "vulcan",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 600,
        "source": "local",
    },
    {
        "id": "clean-architecture",
        "name": "Clean Architecture Validator",
        "description": "Estruturação por camadas, dependency rule e desacoplamento de módulos.",
        "director_id": "vulcan",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 600,
        "source": "local",
    },
    {
        "id": "clean-code",
        "name": "Clean Code Refactorer",
        "description": "Padrões de nomes claros, funções puras, tipagem estrita e refatoração preventiva.",
        "director_id": "vulcan",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 500,
        "source": "local",
    },
    {
        "id": "tdd",
        "name": "Test-Driven Development",
        "description": "Ciclo TDD atômico Red-Green-Refactor com verificação contínua de contratos.",
        "director_id": "vulcan",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 700,
        "source": "local",
    },
    {
        "id": "diagnosing-bugs",
        "name": "Systematic Bug Diagnoser",
        "description": "Diagnóstico metódico de causa raiz, análise de stack trace e regressões.",
        "director_id": "vulcan",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 650,
        "source": "local",
    },
    {
        "id": "docker-healthcheck",
        "name": "Docker Healthcheck",
        "description": "Monitoramento, diagnóstico e recuperação de containers e volumes Docker.",
        "director_id": "vulcan",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 400,
        "source": "local",
    },
    {
        "id": "security-trivy-scan",
        "name": "Security Trivy Scan",
        "description": "Varredura de vulnerabilidades CVE e dependências vulneráveis no projeto.",
        "director_id": "vulcan",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 450,
        "source": "local",
    },
    {
        "id": "git-secrets-detector",
        "name": "Git Secrets Detector",
        "description": "Detecção e bloqueio de credenciais, chaves e tokens vazados em repositório.",
        "director_id": "vulcan",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 350,
        "source": "local",
    },
    # Sterling (CFO/COO / Business & Finance)
    {
        "id": "xlsx-modeler",
        "name": "XLSX Modeler",
        "description": "Modelagem contábil, projeção de DRE e relatórios estruturados de FP&A.",
        "director_id": "sterling",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 700,
        "source": "local",
    },
    {
        "id": "token-cost-calculator",
        "name": "Token Cost Calculator",
        "description": "Rastreamento do consumo de tokens e cálculo de custo unitário por invocação.",
        "director_id": "sterling",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 450,
        "source": "local",
    },
    {
        "id": "to-tickets",
        "name": "To Tickets Decomposer",
        "description": "Decomposição de requisitos e planos em tickets executáveis com estimativa.",
        "director_id": "sterling",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 500,
        "source": "local",
    },
    {
        "id": "client-facing-mvp-planning",
        "name": "Client-Facing MVP Planning",
        "description": "Estruturação de pacotes de MVP orientados a valor e validação de hipóteses.",
        "director_id": "sterling",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 600,
        "source": "local",
    },
    {
        "id": "contract-clause-analyzer",
        "name": "Contract Clause Analyzer",
        "description": "Identificação de termos de risco, cláusulas de responsabilidade e rescisão.",
        "director_id": "sterling",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 550,
        "source": "local",
    },
    {
        "id": "runway-forecasting",
        "name": "Runway Forecasting",
        "description": "Projeção de fluxo de caixa, queima de capital e simulações de runway financeiro.",
        "director_id": "sterling",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 500,
        "source": "local",
    },
    # Aura (CPO / Product & Spatial)
    {
        "id": "pm-product-strategy",
        "name": "PM Product Strategy",
        "description": "Elaboração de PRDs formais, user stories, personas e critérios de aceitação.",
        "director_id": "aura",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 700,
        "source": "local",
    },
    {
        "id": "ux-heuristics",
        "name": "UX Heuristics Evaluator",
        "description": "Avaliação de usabilidade baseada nas 10 heurísticas clássicas de Nielsen.",
        "director_id": "aura",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 500,
        "source": "local",
    },
    {
        "id": "shadcn",
        "name": "Shadcn Component System",
        "description": "Composição e padronização de interfaces utilizando o design system Shadcn/UI.",
        "director_id": "aura",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 600,
        "source": "local",
    },
    {
        "id": "design-taste-frontend",
        "name": "Design Taste Frontend",
        "description": "Diretrizes estéticas premium, paleta harmoniosa, tipografia e microinterações.",
        "director_id": "aura",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 650,
        "source": "local",
    },
    {
        "id": "adversarial-ux-test",
        "name": "Adversarial UX Test",
        "description": "Testes adversariais para identificar fricções e inconsistências de usabilidade.",
        "director_id": "aura",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 450,
        "source": "local",
    },
    {
        "id": "high-end-visual-design",
        "name": "High-End Visual Design",
        "description": "Design visual sofisticado com glassmorphism, gradientes e layout responsivo.",
        "director_id": "aura",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 600,
        "source": "local",
    },
    # Vesper (CMO/CRO / Growth & Sales)
    {
        "id": "competitor-news-monitor",
        "name": "Competitor News Monitor",
        "description": "Monitoramento contínuo de concorrentes, lançamentos e movimentações de mercado.",
        "director_id": "vesper",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 550,
        "source": "local",
    },
    {
        "id": "marketplace-official-research",
        "name": "Marketplace Official Research",
        "description": "Pesquisa sistemática em marketplaces para identificação de nichos e oportunidades.",
        "director_id": "vesper",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 500,
        "source": "local",
    },
    {
        "id": "product-price-monitor",
        "name": "Product Price Monitor",
        "description": "Monitoramento de preços concorrentes e benchmarks de posicionamento comercial.",
        "director_id": "vesper",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 450,
        "source": "local",
    },
    {
        "id": "social-commerce-content-strategy",
        "name": "Social Commerce Content Strategy",
        "description": "Estratégia de conteúdo e funis de atração para crescimento em redes e plataformas.",
        "director_id": "vesper",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 600,
        "source": "local",
    },
    {
        "id": "scrapling-official",
        "name": "Scrapling Official",
        "description": "Extração de dados e inteligência competitiva com bypass ético de proteções.",
        "director_id": "vesper",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 650,
        "source": "local",
    },
    {
        "id": "agentmail",
        "name": "AgentMail Dispatcher",
        "description": "Caixa postal e automação de comunicação assíncrona para prospecção B2B.",
        "director_id": "vesper",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 350,
        "source": "local",
    },
    # Lyra (Head of Research & Spatial Data)
    {
        "id": "research",
        "name": "Primary Source Researcher",
        "description": "Investigação rigorosa contra fontes primárias, literatura acadêmica e benchmarks.",
        "director_id": "lyra",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 750,
        "source": "local",
    },
    {
        "id": "schema-bound-corpus-extraction",
        "name": "Schema-Bound Corpus Extraction",
        "description": "Extração estruturada de corpora de documentos em JSON Schema rigorosamente validado.",
        "director_id": "lyra",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 600,
        "source": "local",
    },
    {
        "id": "jupyter-live-kernel",
        "name": "Jupyter Live Kernel",
        "description": "Análise exploratória interativa e computação estatística via kernels Python.",
        "director_id": "lyra",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 700,
        "source": "local",
    },
    {
        "id": "maps",
        "name": "GeoSpatial Mapper",
        "description": "Processamento, geocodificação e enriquecimento de dados geográficos e espaciais.",
        "director_id": "lyra",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 550,
        "source": "local",
    },
    {
        "id": "grounded-citations",
        "name": "Grounded Citations Verifier",
        "description": "Verificação de alegações factuais e amarração de fontes verificáveis em relatórios.",
        "director_id": "lyra",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 400,
        "source": "local",
    },
    {
        "id": "notebooklm-knowledge-pipeline",
        "name": "NotebookLM Knowledge Pipeline",
        "description": "Curadoria de cadernos de pesquisa e síntese de inteligência com LLMs avançados.",
        "director_id": "lyra",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 600,
        "source": "local",
    },
    # Atlas (CEO & General Orchestrator)
    {
        "id": "agent-handoff-protocols",
        "name": "Agent Handoff Protocols",
        "description": "Protocolo de transferência estruturada e orquestração de tarefas inter-diretores.",
        "director_id": "atlas",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 500,
        "source": "local",
    },
    {
        "id": "context-budget-optimizer",
        "name": "Context Budget Optimizer",
        "description": "Monitoramento e otimização do consumo de contexto para blindar contra context bloat.",
        "director_id": "atlas",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 400,
        "source": "local",
    },
    {
        "id": "wayfinder",
        "name": "Wayfinder Trajectory Planner",
        "description": "Planejamento de trajetória em grafos de tarefas e resolução de objetivos complexos.",
        "director_id": "atlas",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 600,
        "source": "local",
    },
    {
        "id": "to-spec",
        "name": "To Spec Formalizer",
        "description": "Formalização de diálogos executivos e demandas operacionais em especificações técnicas.",
        "director_id": "atlas",
        "is_core": True,
        "load_priority": 1,
        "token_budget": 500,
        "source": "local",
    },
    {
        "id": "grill-me",
        "name": "Grill-Me Challenger",
        "description": "Inquirição socrática implacável para validação de hipóteses e mitigação de vieses.",
        "director_id": "atlas",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 450,
        "source": "local",
    },
    {
        "id": "weekly-review-planning",
        "name": "Weekly Review Planning",
        "description": "Revisão periódica de metas, blockers e consolidação do roadmap semanal.",
        "director_id": "atlas",
        "is_core": False,
        "load_priority": 2,
        "token_budget": 350,
        "source": "local",
    },
]


def render_skill_markdown(name: str, slug: str, description: str, keywords: list[str] | None = None) -> str:
    """Generate standard SKILL.md markdown text."""
    kw_line = ", ".join(keywords) if keywords else "n/a"
    return f"""---
name: {slug}
description: {description}
---

# {name}

## Descrição
{description}

## Gatilhos & Domínio
- Palavras-chave: {kw_line}

## Instruções Operacionais
1. Analise o contexto da solicitação do Diretor responsável.
2. Execute a tarefa respeitando os limites orçamentários de tokens definidos.
3. Produza saída determinística, auditável e estruturada.
"""


def ensure_directors_exist(session: Session) -> None:
    """Ensure the 6 C-Level directors exist in the agents table."""
    for agent_id, meta in DIRECTOR_METADATA.items():
        existing = session.scalar(select(Agent).where(Agent.agent_id == agent_id))
        if not existing:
            new_agent = Agent(
                agent_id=agent_id,
                macro_domain=meta["macro_domain"],
                name=meta["name"],
                system_prompt_path=meta["system_prompt_path"],
                trigger_hooks=meta["trigger_hooks"],
                is_active=True,
            )
            session.add(new_agent)
    session.flush()


def load_ephemeral_tools_from_json(catalog_path: Path) -> list[dict[str, Any]]:
    """Load tools from ephemeral_tools.json if present."""
    if not catalog_path.is_file():
        return []
    try:
        content = json.loads(catalog_path.read_text(encoding="utf-8"))
        items = content.get("ephemeral_tools", [])
        converted: list[dict[str, Any]] = []
        for item in items:
            tool_id = item.get("tool_id") or item.get("id")
            if not tool_id:
                continue
            name = item.get("name", tool_id)
            desc = item.get("description", "")
            director_id = item.get("target_director", "atlas")
            keywords = item.get("trigger_keywords", [])
            budget = int(item.get("budget_tokens", item.get("token_budget", 800)))
            converted.append({
                "id": str(tool_id),
                "name": str(name),
                "description": str(desc),
                "director_id": str(director_id),
                "is_core": False,
                "load_priority": 2,
                "token_budget": budget,
                "source": "catalog_json",
                "content_md": render_skill_markdown(str(name), str(tool_id), str(desc), keywords),
            })
        return converted
    except (json.JSONDecodeError, OSError, KeyError, ValueError) as exc:
        print(f"Warning: failed to parse catalog JSON ({exc})", file=sys.stderr)
        return []


def seed_skills_catalog(
    engine: Engine | None = None,
    db_url: str | None = None,
    catalog_path: Path | None = None,
) -> dict[str, int]:
    """Populate skills_catalog and associate to the 6 directors."""
    if engine is None:
        url = db_url or DEFAULT_DB_URL
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    resolved_catalog_path = catalog_path or DEFAULT_CATALOG_PATH

    skills_to_seed: list[dict[str, Any]] = []
    # 1. Base technical domain skills
    for item in BASE_DIRECTOR_SKILLS:
        item_copy = dict(item)
        if "content_md" not in item_copy:
            item_copy["content_md"] = render_skill_markdown(
                name=item_copy["name"],
                slug=item_copy["id"],
                description=item_copy["description"],
            )
        skills_to_seed.append(item_copy)

    # 2. Ephemeral tools from JSON if available
    ephemeral_skills = load_ephemeral_tools_from_json(resolved_catalog_path)
    skills_to_seed.extend(ephemeral_skills)

    seeded_skills_count = 0
    seeded_links_count = 0

    with Session(engine) as session:
        ensure_directors_exist(session)

        for item in skills_to_seed:
            skill_id = item["id"]
            existing_skill = session.scalar(select(SkillCatalog).where(SkillCatalog.id == skill_id))
            if existing_skill:
                existing_skill.name = item["name"]
                existing_skill.description = item["description"]
                existing_skill.content_md = item["content_md"]
                existing_skill.token_budget = item.get("token_budget", 800)
                existing_skill.source = item.get("source", "local")
            else:
                new_skill = SkillCatalog(
                    id=skill_id,
                    name=item["name"],
                    description=item["description"],
                    content_md=item["content_md"],
                    token_budget=item.get("token_budget", 800),
                    source=item.get("source", "local"),
                    created_at=datetime.now(UTC),
                )
                session.add(new_skill)
                seeded_skills_count += 1

            session.flush()

            director_id = item["director_id"]
            existing_link = session.scalar(
                select(DirectorSkill).where(
                    DirectorSkill.director_id == director_id,
                    DirectorSkill.skill_id == skill_id,
                )
            )
            if existing_link:
                existing_link.is_core = item.get("is_core", False)
                existing_link.load_priority = item.get("load_priority", 1)
            else:
                new_link = DirectorSkill(
                    director_id=director_id,
                    skill_id=skill_id,
                    is_core=item.get("is_core", False),
                    load_priority=item.get("load_priority", 1),
                )
                session.add(new_link)
                seeded_links_count += 1

        session.commit()

    return {
        "skills_seeded": seeded_skills_count,
        "links_seeded": seeded_links_count,
        "total_catalog_items": len(skills_to_seed),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed skills catalog and director associations")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL, help="Database connection URL")
    parser.add_argument(
        "--catalog-path",
        type=Path,
        default=DEFAULT_CATALOG_PATH,
        help="Path to ephemeral_tools.json",
    )
    args = parser.parse_args()

    print(f"Connecting to database: {args.db_url}")
    print(f"Checking catalog file at: {args.catalog_path}")
    result = seed_skills_catalog(db_url=args.db_url, catalog_path=args.catalog_path)
    print(
        f"Seeding completed successfully:\n"
        f"  Total items processed: {result['total_catalog_items']}\n"
        f"  New skills created: {result['skills_seeded']}\n"
        f"  New director-skill links created: {result['links_seeded']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
