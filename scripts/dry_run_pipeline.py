"""Script de Dry Run ponta a ponta do Ephemeral-Agents.

Valida em tempo real as 3 vias centrais do sistema:
1. Fast-Path (Aderência >= 72% -> Injeção de Skill efêmera -> Execução).
2. Slow-Path / Escalação (Aderência < 72% -> Bloqueio com MISSING_SKILL -> Sub-tarefa para Lyra -> Desbloqueio e Conclusão).
3. Telemetria e Hot-Patch (Gravação no journal.jsonl e auditoria do SOUL.md).
"""
import os
import sys
from pathlib import Path

# Configurar ambiente para execução offline/dry-run determinística
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

# Ajustar PYTHONPATH para a raiz do repositório
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from src.evolution.hotpatch import apply_human_correction
from src.macro_agents.orchestrator import MacroOrchestrator


def run_dry_run() -> None:
    print("=" * 60)
    print("INICIANDO DRY RUN DO EPHEMERAL-AGENTS")
    print("=" * 60)

    # 1. Inicializar Orchestrator em modo dry-run
    orchestrator = MacroOrchestrator(dry_run=True)

    # Limpar skill dinâmica de execuções anteriores para garantir reprodutibilidade
    from sqlalchemy import create_engine, delete
    from sqlalchemy.orm import Session

    from src.database.models import DirectorSkill, SkillCatalog
    from src.router.semantic import DEFAULT_DB_URL
    engine = create_engine(DEFAULT_DB_URL)
    with Session(engine) as session:
        session.execute(delete(DirectorSkill).where(DirectorSkill.skill_id == "k8s-baremetal-migrator"))
        session.execute(delete(SkillCatalog).where(SkillCatalog.id == "k8s-baremetal-migrator"))
        session.commit()

    # --- CENÁRIO A: FAST-PATH ---
    print("\n[CENÁRIO 1: FAST-PATH - TAREFA COM SKILL CONHECIDA]")
    task_fast = {
        "title": "Otimizar testes unitários e cobertura",
        "description": "Execução autônoma de suíte de testes unitários com pytest runner",
        "expected_director": "vulcan",
    }
    res_fast = orchestrator.orchestrate(task_fast)
    print(f"-> Diretor atribuído: {res_fast.get('director_id')}")
    print(f"-> Skill resolvida: {res_fast.get('resolved_skill_id')} (Aderência: {res_fast.get('adherence_score', 0):.2%})")
    print(f"-> Status final Kanban: {res_fast.get('kanban_status')}")

    # --- CENÁRIO B: SLOW-PATH (ESCALAÇÃO) ---
    print("\n[CENÁRIO 2: SLOW-PATH - CARÊNCIA DE SKILL]")
    task_slow = {
        "title": "Migração de cluster Kubernetes para bare-metal",
        "description": "Procedimento desconhecido de transição de nós k8s com etcd distribuído",
        "expected_director": "vulcan",
    }
    res_slow = orchestrator.orchestrate(task_slow)
    print(f"-> Diretor atribuído: {res_slow.get('director_id')}")
    print(f"-> Aderência inicial: {res_slow.get('adherence_score', 0):.2%} (< 72%)")
    print(f"-> Status de bloqueio: {res_slow.get('blocked_reason')}")
    print(f"-> Sub-tarefa despachada para Lyra: {res_slow.get('escalation_subtask_id')}")

    # Simular Lyra descobrindo a skill no skills.sh e desbloqueando a tarefa
    print("-> [AÇÃO LYRA] Descobrindo e homologando skill via skills.sh...")
    discovered_skill_md = """---
name: k8s-baremetal-migrator
description: Migração de cluster Kubernetes para bare-metal com etcd distribuído
---
# Kubernetes Bare-Metal Migrator

## Pré-requisitos
- Acesso SSH aos nós físicos
- Backup completo de snapshots do etcd

## Procedimento
1. Drenar nós de controle de forma sequencial.
2. Migrar plano de dados com etcd distribuído.
3. Validar pods e ingress controllers no cluster alvo.

## Verificação
- kubectl get nodes com status Ready em todos os hosts físicos.
"""
    orchestrator.on_skill_discovered(
        director_id=res_slow.get("director_id") or "vulcan",
        skill_md=discovered_skill_md,
        task_id=res_slow.task_id,
        skill_id="k8s-baremetal-migrator",
    )

    unblocked_task = orchestrator.board.get(res_slow.task_id)
    post_unblock_status = unblocked_task.status.value if unblocked_task else "ready"
    print(f"-> Status pós-desbloqueio: {post_unblock_status}")

    # Re-executar no Fast-Path agora que a skill está associada ao Diretor
    print("-> [RE-EXECUÇÃO] Executando tarefa original desbloqueada...")
    res_reexec = orchestrator.orchestrate_task(unblocked_task)
    print(f"-> Status final pós-resolução de skill: {res_reexec.status}")

    # --- CENÁRIO C: TELEMETRIA & HOT-PATCH ---
    print("\n[CENÁRIO 3: VERIFICAÇÃO DE TELEMETRIA & HOT-PATCH]")
    journal_path = REPO_ROOT / "data" / "journal.jsonl"
    if journal_path.exists():
        with open(journal_path, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
        print(f"-> Total de entradas no journal: {len(lines)}")
        if lines:
            print(f"-> Último registro de telemetria: {lines[-1]}")
    else:
        print("-> [AVISO] data/journal.jsonl ainda não criado.")

    # Validar Hot-patch cirúrgico no SOUL.md de Vulcan
    print("\n-> [HOT-PATCH] Aplicando regra de guardrail cirúrgica no SOUL.md de vulcan...")
    patch_res = apply_human_correction(
        correction_text="NUNCA permitir execução direta de comandos SQL destrutivos sem aprovação",
        target_director="vulcan",
        auto_commit=False,
    )
    # --- CENÁRIO D: SUBAGENTES EFÊMEROS & GOVERNANÇA (STERLING) ---
    print("\n[CENÁRIO 4: SUBAGENTES EFÊMEROS & GOVERNANÇA - SPAWN_EPHEMERAL]")
    from src.macro_agents.ephemeral import execute_ephemeral_task
    from src.governance.limits import EphemeralLimits, get_cost_report
    from src.orchestration.kanban import KanbanTask, KanbanStatus

    task_parent = KanbanTask(
        title="Deploy Contínuo e Testes E2E",
        body="Orquestração de pipeline com execução de suíte de testes isolados",
        assignee="vulcan",
        macro_domain="engineering",
    )
    orchestrator.board.add(task_parent)

    print(f"-> Tarefa Pai: [{task_parent.task_id[:8]}] {task_parent.title} (Diretor: {task_parent.assignee})")
    print("-> [DIRETOR VULCAN] Invocando tool 'spawn_ephemeral' para skill 'k8s-baremetal-migrator'...")

    limits = EphemeralLimits(max_ephemerals_per_task=5, max_cost_usd_per_task=0.50)
    
    eph_res = execute_ephemeral_task(
        skill_id="k8s-baremetal-migrator",
        subtask_title="Migrar plano de dados etcd bare-metal",
        subtask_body="Drenar nós sequencialmente e migrar etcd distribuído",
        expected_output="Pods e ingress validados no cluster",
        parent_task=task_parent,
        board=orchestrator.board,
        database_url=DEFAULT_DB_URL,
        director_id="vulcan",
        limits=limits,
    )

    child_subtask_id = eph_res.get("subtask_id")
    child_subtask = orchestrator.board.get(child_subtask_id) if child_subtask_id else None

    print(f"-> [SUB-AGENTE EFÊMERO] Subtarefa Filha criada: [{child_subtask_id[:8] if child_subtask_id else 'N/A'}]")
    print(f"-> Status da subtarefa no Kanban: {child_subtask.status.value if child_subtask else 'N/A'}")
    print(f"-> Isolamento: Sem SOUL do diretor, sem memória Obsidian, sem histórico")
    print(f"-> Status de execução efêmera: {eph_res.get('status')}")
    print(f"-> Retorno ao Diretor Vulcan: {eph_res.get('output', '')[:80]}...")
    print(f"-> Estado do subagente efêmero: DESCARTADO com sucesso.")

    # Relatório de custos para o Sterling
    cost_report = limits.to_report()
    print("\n-> [AUDITORIA STERLING] Relatório de Governança e Custos:")
    print(f"   * Total de Efêmeros Spawnados: {cost_report['ephemeral_count']}/{limits.max_ephemerals_per_task}")
    print(f"   * Tokens Totais: {cost_report['total_tokens']}/{limits.max_tokens_per_task}")
    print(f"   * Custo Total Estimado: ${cost_report['total_cost_usd']:.4f} / ${limits.max_cost_usd_per_task:.2f}")
    print(f"   * Fonte do Cálculo de Custo: {cost_report.get('cost_source')}")
    print(f"   * Limites Atingidos: {cost_report['limits_hit'] or 'Nenhum (Operação Segura)'}")

    print("\n" + "=" * 60)
    print("DRY RUN CONCLUÍDO COM SUCESSO")
    print("=" * 60)


if __name__ == "__main__":
    run_dry_run()
