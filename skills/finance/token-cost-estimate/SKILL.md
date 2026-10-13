---
name: token-cost-estimate
description: "Estimativa de custo mensal para 1000 tarefas com modelos grátis e modelos pagos. Cálculo e projeção de consumo de tokens (prompt e completion), análise de rate limits e SkillUsage. Monthly token cost estimate for 1000 tasks on free models."
director: sterling
source: "internal"
license: MIT
status: active
version: "1.0.0"
token_budget: 800
---

# Estimativa de Custo Mensal de Tokens (Token Cost Estimate)

Modelagem financeira e planejamento orçamentário para estimar o custo mensal de operação com modelos de IA por volume projetado de tarefas (ex: 100, 1.000 ou 10.000 execuções), distinguindo claramente entre modelos em tier gratuito (free-tier) e modelos pagos.

## Quando Usar
- Ao projetar custos operacionais e unit economics para esteiras de agentes e automações.
- Quando o usuário solicitar estimativa de custo mensal por volume de tarefas, custo de tokens ou simulação orçamentária.
- Para planejar transição de gateways gratuitos para provedores comerciais respeitando limites de taxa (rate limits).

## Passos
1. **Definir Premissas de Volume e Tokens**:
   - Volume mensal de tarefas ($N$).
   - Média de tokens de prompt por tarefa (ex: 800 a 1.500 tokens).
   - Média de tokens de completion por tarefa (ex: 300 a 700 tokens).
2. **Consultar Histórico e Tabela de Preços**:
   - Verificar médias reais observadas na tabela `SkillUsage` ou dados de `cost_by_skill`.
   - Utilizar a tabela de preços fallback para precificação por milhão de tokens (ex: Mistral 7B, Claude 3.5 Sonnet, GPT-4o-mini).
3. **Calcular Cenário Tier Gratuito (Free-Tier)**:
   - Custo direto de inferência: **$0.00**.
   - Analisar restrições de governança: RPM (requisições por minuto), RPD (requisições por dia) e necessidade de filas ou delays para não estourar quotas.
4. **Calcular Cenário Tier Pago (Pay-As-You-Go)**:
   - Multiplicar total de tokens mensais pelo preço tabelado de entrada e saída.
   - Adicionar margem de segurança operacional (15% a 20% para retries e context bloat).
5. **Apresentar Tabela Comparativa e Recomendações**.

## Critérios de Verificação
- Separa explicitamente o cenário gratuito ($0.00 com análise de rate limits) do cenário pago.
- Apresenta consumo total projetado de tokens (prompt, completion e total).
- Utiliza fórmulas matemáticas transparentes e premissas orçamentárias declaradas.

## Formato de Saída
```markdown
## Premissas Operacionais (1.000 tarefas/mês)
- **Média de Prompt**: 1.000 tokens por execução.
- **Média de Completion**: 400 tokens por execução.
- **Volume Total**: 1.000.000 tokens de prompt + 400.000 tokens de completion = 1.4M tokens/mês.

## Comparativo: Tier Gratuito vs. Tier Pago

| Métrica | Cenário Free-Tier (OmniRoute / OpenRouter Free) | Cenário Pago (Modelos Econômicos ex: GPT-4o-mini) |
|---|---|---|
| **Custo de Inferência** | **$0.00** | ~$0.27 / mês |
| **Limite de Taxa (RPM/RPD)** | Sujeito a cotas diárias (~20-50 RPM) | Sem throttling artificial |
| **Latência e Confiabilidade** | Melhor esforço, filas em horários de pico | Alta disponibilidade e SLAs |
| **Risco de Interrupção** | Médio (necessário buffer e retries) | Baixo |

## Recomendação de Governança
1. **Fase Inicial / MVP**: Operar em Free-Tier com fila assíncrona para diluir requisições ao longo das 24h.
2. **Escala**: Ativar modelo comercial econômico com teto de gastos (`max_cost`) configurado no gateway.
```
