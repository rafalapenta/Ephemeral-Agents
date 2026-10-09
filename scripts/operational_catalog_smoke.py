from __future__ import annotations

import json
from pathlib import Path

import chromadb
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from src.database.models import Agent, AgentTool, Tool
from src.router.semantic import route_agent

import os
import sys

ROOT = Path(__file__).resolve().parent.parent
source_env = os.environ.get("AGENCY_AGENTS_SOURCE")
if not source_env:
    sys.exit("Error: AGENCY_AGENTS_SOURCE environment variable is not set. Please set it to the source directory of agency-agents.")
SOURCE = Path(source_env)
DB_PATH = ROOT / 'agency_agents.db'
DB_URL = f'sqlite:///{DB_PATH.as_posix()}'
CHROMA = ROOT / 'chroma'
QUERIES = {
 'Tech': 'Preciso criar uma migration SQL para PostgreSQL e otimizar índices de busca.',
 'Product': 'Desenhar o fluxo de onboarding e UX para um aplicativo de realidade espacial no visionOS.',
 'Growth': 'Montar uma estratégia de Outbound B2B e precificação de contratos corporativos.',
 'Business': 'Elaborar o modelo financeiro de FP&A e análise de DRE para o próximo trimestre.',
 'Research': 'Analisar conformidade de dados clínicos de pacientes segundo as normas da LGPD.',
}


def main() -> None:
 engine = create_engine(DB_URL)
 session = Session(engine)
 agents = session.scalar(select(func.count()).select_from(Agent)) or 0
 tools = session.scalar(select(func.count()).select_from(Tool)) or 0
 agent_tools = session.scalar(select(func.count()).select_from(AgentTool)) or 0
 agent_rows = list(session.scalars(select(Agent)))
 hook_total = sum(len(agent.trigger_hooks) for agent in agent_rows)
 agent_by_id = {agent.agent_id: agent for agent in agent_rows}
 session.close()

 client = chromadb.PersistentClient(path=str(CHROMA))
 collections = client.list_collections()
 collection = next(item for item in collections if getattr(item, 'name', str(item)) == 'agency_agents')
 chroma_rows = collection.get(include=['documents', 'metadatas'])
 chroma_hook_total = sum(len((doc or '').splitlines()) for doc in (chroma_rows.get('documents') or []))

 smoke = []
 for domain, query in QUERIES.items():
  result = route_agent(query, 0.30, database_url=DB_URL, chroma_path=CHROMA, source_root=SOURCE)
  payload = result.model_dump(mode='json')
  agent_id = payload.get('agent_id')
  agent = agent_by_id.get(agent_id)
  prompt_file = (SOURCE / agent.system_prompt_path).resolve() if agent else None
  source_root = SOURCE.resolve()
  prompt_safe = bool(prompt_file and prompt_file.is_relative_to(source_root))
  prompt_exists = bool(prompt_file and prompt_file.exists())
  prompt_loaded = bool(payload.get('prompt') or payload.get('system_prompt'))
  score = payload.get('score')
  authorized = payload.get('authorized_tools', payload.get('tools', []))
  smoke.append({
   'macro_domain': domain,
   'query': query,
   'response_keys': sorted(payload.keys()),
   'matched': payload.get('matched'),
   'score': score,
   'score_ge_0_30': bool(score is not None and score >= 0.30),
   'agent_id': agent_id,
   'agent_macro_domain': agent.macro_domain if agent else None,
   'system_prompt_path': agent.system_prompt_path if agent else None,
   'system_prompt_exists': prompt_exists,
   'system_prompt_path_safe': prompt_safe,
   'system_prompt_loaded': prompt_loaded,
   'authorized_tools': authorized,
   'error': None,
   })

 report = {
  'database': str(ROOT / 'agency_agents.db'),
  'chroma_path': str(CHROMA),
  'sqlite_agents': agents,
  'sqlite_tools': tools,
  'sqlite_agent_tools': agent_tools,
  'sqlite_trigger_hooks': hook_total,
  'chroma_collections': [getattr(item, 'name', str(item)) for item in collections],
  'chroma_documents': collection.count(),
  'chroma_trigger_hooks': chroma_hook_total,
  'smoke': smoke,
 }
 print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
 main()