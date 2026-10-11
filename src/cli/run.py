"""CLI run command for AGency (`aggency run`).

Executes end-to-end task orchestration using free-tier models:
1. Validates prerequisites (gateway health & free-tier compliance).
2. Creates Kanban card.
3. Routes task (semantic router + skills engine).
4. Compresses context and executes director handoff with ephemeral subagents.
5. Evaluates quality gates and records SkillUsage.
6. Saves run output to state_data/runs/<card_id>.md.
7. Prints structured summary or JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.gateway.models import (
    _gateway_base,
    _gateway_key,
    check_free_tier_compliance,
    check_gateway_health,
    resolve_model,
)
from src.macro_agents.orchestrator import MacroOrchestrator

logger = logging.getLogger(__name__)

DIRECTOR_ALIASES: dict[str, str] = {
    "cto": "vulcan",
    "cmo": "vesper",
    "cfo": "sterling",
    "cpo": "aura",
    "cos": "atlas",
    "ceo": "atlas",
    "cro": "lyra",
    "research": "lyra",
}


def normalize_director(director: str | None) -> str:
    """Normalize user-provided director string (supports aliases like 'cto')."""
    if not director:
        return "auto"
    raw = director.strip().lower()
    return DIRECTOR_ALIASES.get(raw, raw)


def save_run_output(
    card_id: str,
    task_prompt: str,
    director: str,
    skill: str | None,
    outcome: str,
    cost_usd: float,
    cost_source: str,
    reply: str,
    runs_dir: Path | str = "state_data/runs",
) -> Path:
    """Persist run results with metadata frontmatter to state_data/runs/<card_id>.md."""
    target_dir = Path(runs_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = target_dir / f"{card_id}.md"

    # Ensure 1-line task summary (strip internal line breaks and quotes)
    clean_task = " ".join(task_prompt.split()).replace('"', "'").strip()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    content = f"""---
task: "{clean_task}"
director: "{director}"
skill: "{skill or "none"}"
outcome: "{outcome}"
cost_usd: {cost_usd:.6f}
cost_source: "{cost_source}"
date: "{now_iso}"
card_id: "{card_id}"
---

{reply.strip()}
"""
    file_path.write_text(content, encoding="utf-8")
    return file_path


def run_cli(args: list[str], runs_dir: Path | str = "state_data/runs") -> int:
    """Entrypoint for `aggency run` CLI."""
    parser = argparse.ArgumentParser(
        prog="aggency run",
        description="Run a real task end-to-end with free-tier models.",
    )
    parser.add_argument("task", type=str, help="The task prompt to execute")
    parser.add_argument(
        "--director",
        type=str,
        default="auto",
        help="Target director (auto | cto | cmo | cfo | vulcan | vesper | sterling | lyra | aura | atlas)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run deterministic offline execution without making real LLM calls (ideal for CI).",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=0.0,
        help="Maximum allowed cost in USD (default: 0.00 = free-tier only).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output the final result in JSON format.",
    )

    try:
        parsed = parser.parse_args(args)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1

    task_prompt = parsed.task.strip()
    if not task_prompt:
        print("Erro: A tarefa não pode ser vazia.", file=sys.stderr)
        return 1

    director_req = normalize_director(parsed.director)
    dry_run = parsed.dry_run
    max_cost = parsed.max_cost
    json_output = parsed.json_output
    allow_paid = os.getenv("AGENCY_ALLOW_PAID", "0").strip() == "1"

    # ── 1. Pre-check: LLM Gateway connectivity & API Key ───────
    if not dry_run:
        base_url = _gateway_base()
        raw_key = _gateway_key()

        is_healthy, health_msg, _ = check_gateway_health(timeout=3.0)
        if not is_healthy:
            print("=" * 60, file=sys.stderr)
            print("  [ERRO] LLM Gateway indisponível", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            print(f"  URL configurada: {base_url}", file=sys.stderr)
            print(f"  Diagnóstico:     {health_msg}", file=sys.stderr)
            print(file=sys.stderr)
            print("Como resolver:", file=sys.stderr)
            print(
                "  1. Inicie o gateway (ex: OmniRoute em http://localhost:20128/v1).",
                file=sys.stderr,
            )
            print(
                "  2. Ou aponte AGENCY_LLM_BASE_URL para outro gateway OpenAI-compatible no .env.",
                file=sys.stderr,
            )
            print(
                "  3. Para rodar em modo determinístico sem LLM (modo CI), adicione a flag --dry-run:",
                file=sys.stderr,
            )
            print(f'     aggency run "{task_prompt}" --dry-run', file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            return 1

        # Check API key if connecting to public endpoints requiring authorization
        if (
            "openrouter.ai" in base_url or "api.openai.com" in base_url
        ) and not raw_key:
            print("=" * 60, file=sys.stderr)
            print("  [ERRO] Chave de API ausente", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            print(f"  Endpoint: {base_url}", file=sys.stderr)
            print(
                "  Configure a variável AGENCY_LLM_API_KEY no seu arquivo .env.",
                file=sys.stderr,
            )
            print("=" * 60, file=sys.stderr)
            return 1

    # ── 2. Free-tier compliance verification ─────────────────────
    target_check_director = director_req if director_req != "auto" else "vulcan"
    resolved_model = resolve_model(target_check_director)
    if not dry_run:
        compliant, comp_msg = check_free_tier_compliance(
            resolved_model.model, max_cost=max_cost, allow_paid=allow_paid
        )
        if not compliant:
            print("=" * 60, file=sys.stderr)
            print("  [ERRO] Modelo não-gratuito recusado", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            print(f"  {comp_msg}", file=sys.stderr)
            print(file=sys.stderr)
            print("Como resolver:", file=sys.stderr)
            print(
                "  1. Configure um modelo grátis em AGENCY_DEFAULT_MODEL no .env:",
                file=sys.stderr,
            )
            print(
                "     AGENCY_DEFAULT_MODEL=mistralai/mistral-7b-instruct:free",
                file=sys.stderr,
            )
            print(
                "  2. Ou permita modelos pagos definindo AGENCY_ALLOW_PAID=1 no .env ou --max-cost > 0.",
                file=sys.stderr,
            )
            print("=" * 60, file=sys.stderr)
            return 1

    # ── 3. Task Orchestration ────────────────────────────────────
    start_time = time.time()
    orchestrator = MacroOrchestrator(
        dry_run=dry_run,
        fallback_to_candidate=True,
    )

    assignee_arg = director_req if director_req != "auto" else ""
    res = orchestrator.orchestrate(
        title=task_prompt,
        body=task_prompt,
        assignee=assignee_arg,
    )

    duration = time.time() - start_time
    card_id = res.task_id
    director_id = res.director_id or (
        director_req if director_req != "auto" else "unknown"
    )
    director_score = res.route.score if res.route else 1.0
    skill_id = res.resolved_skill_id

    # Extract handoff details
    handoff_res = getattr(res, "handoff_result", {}) or {}
    cost_report = handoff_res.get("cost_report", {}) or orchestrator.get_cost_report(
        card_id
    )
    total_tokens = cost_report.get("total_tokens", 0)
    total_cost_usd = cost_report.get("total_cost_usd", 0.0)
    cost_source = cost_report.get("cost_source", "free_tier")
    ephemerals_created = cost_report.get("ephemeral_count", 0) or handoff_res.get(
        "tool_calls_executed", 0
    )

    # Determine final outcome and reply
    if res.status == "completed" and handoff_res.get("status") == "success":
        outcome = "SUCCESS"
        reply_text = handoff_res.get(
            "reply", f"Tarefa concluída com sucesso pelo diretor {director_id}."
        )
        exit_code = 0
    elif res.status == "escalated" or handoff_res.get("status") == "escalated":
        outcome = "ESCALATED"
        reply_text = (
            handoff_res.get("error")
            or res.error
            or "Tarefa escalada por indisponibilidade de modelo / rate limit."
        )
        exit_code = 1
    elif res.status == "blocked":
        outcome = "BLOCKED"
        reply_text = res.blocked_reason or res.error or "Tarefa bloqueada no Kanban."
        exit_code = 1
    else:
        outcome = "FAILURE"
        reply_text = (
            handoff_res.get("error") or res.error or "Falha na execução da tarefa."
        )
        exit_code = 1

    # ── 4. Persist run output ────────────────────────────────────
    run_file = save_run_output(
        card_id=card_id,
        task_prompt=task_prompt,
        director=director_id,
        skill=skill_id,
        outcome=outcome,
        cost_usd=total_cost_usd,
        cost_source=cost_source,
        reply=reply_text,
        runs_dir=runs_dir,
    )

    # ── 5. Format Output ─────────────────────────────────────────
    if json_output:
        result_payload = {
            "card_id": card_id,
            "director": director_id,
            "director_score": round(director_score, 4),
            "skill": skill_id,
            "ephemerals_created": ephemerals_created,
            "outcome": outcome,
            "result": reply_text,
            "tokens": total_tokens,
            "cost_usd": round(total_cost_usd, 6),
            "cost_source": cost_source,
            "duration_seconds": round(duration, 2),
            "run_file": str(run_file),
        }
        print(json.dumps(result_payload, indent=2, ensure_ascii=False))
    else:
        print()
        print("=" * 60)
        print("  AGency Run — Resumo da Execução")
        print("=" * 60)
        print(f"  Card ID:           {card_id}")
        print(f"  Diretor:           {director_id} (score: {director_score:.4f})")
        print(f"  Skill usada:       {skill_id or 'none'}")
        print(f"  Efêmeros criados:  {ephemerals_created}")
        print(f"  Resultado:         {outcome}")
        print(f"  Tokens:            {total_tokens}")
        print(f"  Custo:             ${total_cost_usd:.4f} ({cost_source})")
        print(f"  Tempo:             {duration:.2f}s")
        print(f"  Arquivo de run:    {run_file}")
        print("-" * 60)
        print("Resposta:")
        print(reply_text.strip())
        print("=" * 60)
        print()

    return exit_code
