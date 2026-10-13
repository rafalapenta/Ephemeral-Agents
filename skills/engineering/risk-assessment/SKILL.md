---
name: risk-assessment
description: "Listar e avaliar riscos técnicos de arquitetura, infraestrutura, segurança, dados, disponibilidade e custos para bot de WhatsApp de imobiliária e serviços web. Análise de vulnerabilidades, bloqueio de contas e mitigação. Technical risk assessment for WhatsApp bots, real estate automation and failure mitigation."
director: vulcan
source: "internal"
license: MIT
status: active
version: "1.0.0"
token_budget: 750
---

# Avaliação de Riscos Técnicos (Risk Assessment)

Identificação sistemática, quantificação e plano de contingência para riscos técnicos em projetos de software, automações, bots de atendimento e integrações críticas.

## Quando Usar
- Ao planejar ou revisar arquiteturas de bots de mensageria (ex: WhatsApp, Telegram), microsserviços ou sistemas transacionais.
- Antes de deploy em produção para avaliar segurança, proteção de dados (LGPD), disponibilidade e infraestrutura.
- Quando a tarefa solicitar levantamento de riscos técnicos, vulnerabilidades operacionais ou planos de mitigação.

## Passos
1. **Identificar os Eixos de Risco**:
   - **Segurança**: Vetores de ataque, injeção de prompt, vazamento de credenciais, abuso de APIs.
   - **Dados e Privacidade**: Exposição de dados de clientes, conformidade LGPD, persistência insegura.
   - **Disponibilidade**: Bloqueio ou banimento de números/contas, concorrência simultânea, timeout em CRMs parceiros.
   - **Custo e Recursos**: Explosão de consumo de tokens, loops infinitos, estouro de cotas de gateways.
2. **Avaliar Probabilidade e Impacto**: Classificar em Baixa, Média ou Alta.
3. **Formular Plano de Mitigação**: Ações preventivas diretas e procedimentos de contingência caso o incidente ocorra.

## Critérios de Verificação
- Cobre os quatro eixos obrigatórios (segurança, dados, disponibilidade, custo) ou os riscos específicos solicitados no escopo.
- Cada risco possui probabilidade, impacto e mitigação acionável e viável.
- Soluções de mitigação evitam dependências mágicas e respeitam a arquitetura local do projeto.

## Formato de Saída
```markdown
## Matriz de Riscos Técnicos

| # | Categoria | Risco Identificado | Probabilidade | Impacto | Mitigação Recomendada |
|---|---|---|---|---|---|
| 1 | Disponibilidade | Bloqueio/banimento de número em automação de WhatsApp | Alta | Alta | Utilizar API oficial (Cloud API), implementar rate limiting e human-in-the-loop |
| 2 | Dados | Exposição de leads e histórico de conversas | Média | Alta | Criptografia em repouso, anonimização de dados pessoais e compliance LGPD |
| 3 | Concorrência | Inconsistência de estado em conversas simultâneas | Média | Média | Gerenciamento de sessão com lock distribuído/Redis e timeouts estritos |

## Recomendações Prioritárias
1. <Ação imediata mais crítica>
2. <Ação preventiva de médio prazo>
```
