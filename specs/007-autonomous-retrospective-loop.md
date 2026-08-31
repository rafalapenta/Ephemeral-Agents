# SPEC-007: Arquitetura de Melhoria Autônoma e Loop de Retrospectiva (Roadmap)

> **Status:** Registrado / Planejado para Commit Futuro
> **Data:** 2026-08-31
> **Autor:** AGgency Architecture Team

---

## 1. Visão Geral

Esta especificação define o mecanismo de **Aprendizado Contínuo e Auto-Aprimoramento** do ecossistema AGgency. O objetivo é permitir que os Diretores e o Atlas refinem automaticamente sua interpretação semântica, diretrizes de domínio e precisão técnica a partir do histórico de uso real, feedbacks do usuário e taxas de aprovação de entregáveis.

---

## 2. Pilares do Sistema de Aprendizado

### 2.1 Memória Semântica Dinâmica (Short/Long-term Memory)
- **Log de Interações:** Armazenamento estruturado de tarefas concluídas, correções feitas pelo usuário e padrões aprovados.
- **Camada de Memória:** Atualização contínua do `MEMORY.md` individual de cada agente e do cofre de conhecimento compartilhado (Obsidian/LLMWiki).

### 2.2 Reunião de Retrospectiva Periódica (Weekly Agent Retrospective)
- **Gatilho:** Execução agendada (ex: semanalmente via cron do Hermes ou a cada 50 tarefas concluídas).
- **Processo:**
  1. O Atlas convoca uma sessão assíncrona de retrospectiva.
  2. Cada Diretor analisa suas execuções anteriores, erros corrigidos e feedbacks recebidos.
  3. Cada Diretor emite um relatório padronizado: `RETROSPECTIVE_[DIRETOR].md`.
  4. O Atlas sintetiza os aprendizados em `docs/weekly_learnings.md` e propõe atualizações de premissas.

### 2.3 Curadoria de Exemplos de Ouro (Golden Few-Shots)
- Exemplos de código, PRDs, análises e planos que receberam aprovação imediata do usuário são classificados como **Golden Examples**.
- O roteador semântico passa a injetar esses exemplos nas novas tarefas para calibrar o tom e o nível de profundidade esperado.

### 2.4 Governança e Self-Prompt Optimization
- Quando um padrão de comportamento for identificado de forma recorrente (ex: o usuário sempre pede testes em pytest com fixtures), o sistema formula uma **sugestão de emenda ao SOUL.md**.
- **Regra de Segurança:** Nenhuma alteração no `SOUL.md` entra em vigor sem a aprovação explícita do usuário (Human-in-the-Loop).

---

## 3. Estrutura de Artefatos Proposta

```text
AGgency/
├── src/
│   └── learning/
│       ├── __init__.py
│       ├── retrospective_engine.py   # Motor de análise de logs e feedbacks
│       ├── memory_consolidator.py    # Consolidador de MEMORY.md e Obsidian
│       └── golden_curator.py         # Curador de few-shot examples
├── data/
│   └── golden_examples/             # Exemplos aprovados por domínio
└── docs/
    └── retrospectives/               # Relatórios periódicos de lições aprendidas
```

---

## 4. Próximos Passos de Implementação (Roadmap)

1. [ ] Implementação do coletor de feedback em `src/learning/retrospective_engine.py`.
2. [ ] Configuração do job periódico no scheduler/cron do Hermes.
3. [ ] Integração do `memory_consolidator` com a skill `obsidian-vault-curator`.
4. [ ] Interface de aprovação de emendas de `SOUL.md` no painel do Hermes.
