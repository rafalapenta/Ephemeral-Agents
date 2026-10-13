# AGency — Tarefas de Teste Ponta a Ponta (End-to-End)

Este documento contém 3 tarefas de teste representativas para execução de ponta a ponta com o comando `aggency run`, cobrindo 3 diretores e domínios essenciais: **CTO (Vulcan)**, **CMO (Vesper)** e **CFO (Sterling)**.

Todas as tarefas são projetadas para rodar exclusivamente em modelos gratuitos (free-tier, custo $0.00), utilizando o gateway configurado (OmniRoute, OpenRouter ou compatível).

---

## Tarefa 1 — CTO / Engenharia (Vulcan)

- **Diretor**: Vulcan (`cto`)
- **Macro-Domínio**: `engineering`
- **Skill Associada**: `clean-architecture` / `diagnosing-bugs`
- **Descrição da Tarefa**: Análise de riscos técnicos, resiliência e arquitetura para um bot de atendimento imobiliário via WhatsApp.

### Comando CLI:
```bash
aggency run "liste 3 riscos técnicos de um bot de WhatsApp para imobiliária" --director cto
```

### Em modo Dry-Run (CI / Offline):
```bash
aggency run "liste 3 riscos técnicos de um bot de WhatsApp para imobiliária" --director cto --dry-run
```

### Saída esperada:
- Identificação de riscos como bloqueio/ban de números por automação não oficial, gestão de estado concorrente em conversas simultâneas e latência/fallback em integrações com CRM imobiliário.
- Handoff para o diretor Vulcan com injeção de skill técnica e spawn de subagente efêmero.
- Custo de execução: `$0.0000 (free_tier)`.

---

## Tarefa 2 — CMO / Marketing & Growth (Vesper)

- **Diretor**: Vesper (`cmo`)
- **Macro-Domínio**: `business` / `growth`
- **Skill Associada**: `social-commerce-content-strategy` / `agentmail`
- **Descrição da Tarefa**: Geração de ganchos (hooks) de alta retenção para posts de LinkedIn e redes sociais lançando serviços de agentes de IA para pequenas e médias empresas.

### Comando CLI:
```bash
aggency run "3 ganchos de post para lançar um serviço de agentes para PMEs" --director cmo
```

### Em modo Dry-Run (CI / Offline):
```bash
aggency run "3 ganchos de post para lançar um serviço de agentes para PMEs" --director cmo --dry-run
```

### Saída esperada:
- 3 ganchos focados em dores reais de PMEs (ex: automação de triagem de leads, redução de custo de atendimento pós-horário comercial, agentes que não dormem).
- Handoff para Vesper, estruturação do copy e validação de qualidade.
- Custo de execução: `$0.0000 (free_tier)`.

---

## Tarefa 3 — CFO / Finanças & Operações (Sterling)

- **Diretor**: Sterling (`cfo`)
- **Macro-Domínio**: `operations` / `finance`
- **Skill Associada**: `token-cost-calculator` / `runway-forecasting`
- **Descrição da Tarefa**: Modelagem financeira e estimativa de custo mensal para 1000 tarefas rodando em infraestrutura com modelos gratuitos vs limites de taxa.

### Comando CLI:
```bash
aggency run "estimativa de custo mensal para 1000 tarefas com modelos grátis" --director cfo
```

### Em modo Dry-Run (CI / Offline):
```bash
aggency run "estimativa de custo mensal para 1000 tarefas com modelos grátis" --director cfo --dry-run
```

### Saída esperada:
- Estimativa detalhada de consumo de tokens (prompt + completion), análise de rate limits diários em modelos free-tier e custo total estimado ($0.00 em gateways gratuitos).
- Auditoria de limites de governança do Sterling (verificação de custo e tokens).
- Custo de execução: `$0.0000 (free_tier)`.

---

## Formato JSON para Automações

Qualquer uma das tarefas acima aceita a flag `--json` para integração em pipelines ou scripts:

```bash
aggency run "estimativa de custo mensal para 1000 tarefas com modelos grátis" --director cfo --dry-run --json
```

O resultado é gravado automaticamente em `state_data/runs/<card_id>.md` com frontmatter estruturado contendo data, diretor, skill, tokens e custo.
