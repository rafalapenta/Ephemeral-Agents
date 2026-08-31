# SPEC-006: Protocolo A2A de Descoberta Seletiva e Alinhamento de Escopo

> **Status:** Ativo / Implementado
> **Data:** 2026-08-31
> **Autor:** AGgency Architecture Team

---

## 1. Visão Geral

O objetivo do protocolo **A2A (Agent-to-Agent) de Descoberta Seletiva** é garantir que o ecossistema de agentes analise e antecipe pontos cegos técnicos, de produto, de negócio, de growth e regulatórios antes de apresentar um plano de execução ao usuário, mantendo **alta velocidade (baixa latência)** e **baixo consumo de tokens (context window limpo)**.

---

## 2. Princípios de Arquitetura

1. **Triage Inicial pelo Atlas (CEO):** Se a demanda inicial do usuário for vaga ou sem contexto suficiente, o Atlas faz de 1 a 2 perguntas de alinhamento com o usuário antes de envolver diretores.
2. **Consulta Seletiva (Máximo 2 Diretores):** Em demandas de Nível 2 (Projetos/Features), o Atlas nunca consulta todos os 5 diretores simultaneamente. Ele consulta apenas os 1 ou 2 diretores cujo domínio é diretamente afetado.
3. **Respostas Estritas em 2 Bullets:** Os diretores respondem ao ping de diagnóstico exclusivamente com **2 bullets curtos e densos**. É estritamente proibido gerar código, planos longos ou mockups nesta etapa.
4. **Circuit Breaker & Transparência:** Se um diretor demorar mais de 10 segundos para responder, o Atlas tenta 1 retry rápido. Se a falha persistir, o Atlas avisa o usuário com transparência e prossegue com as informações que possui.
5. **Síntese & Descarte de Contexto (Context Hygiene):** Os pings brutos entre agentes são descartados da memória permanente após o Atlas sintetizar a mensagem executiva com até 5 perguntas numeradas para o usuário.
6. **Teto de Turnos:** São permitidas no máximo **2 rodadas de perguntas** com o usuário. Caso ainda restem dúvidas após o segundo turno, o Atlas assume premissas conservadoras documentadas e avança para a proposta do Plano Executivo.

---

## 3. Contrato de Mensagens

### 3.1 Ping de Diagnóstico (Atlas ➔ Diretor)
```text
[A2A-Diagnostic-Ping]:
Demanda: [Breve descrição da demanda do usuário]
Foco: Identifique 2 pontos cegos ou restrições críticas do seu domínio para formulação de perguntas ao usuário.
```

### 3.2 Resposta de Diagnóstico (Diretor ➔ Atlas)
```text
[A2A-Diagnostic-Reply]:
- [Bullet 1: Ponto cego/risco principal]
- [Bullet 2: Restrição técnica ou premissa essencial]
```

### 3.3 Mensagem Consolidada ao Usuário (Atlas ➔ Usuário)
```text
Com base na análise preliminar de diretoria, precisamos definir os seguintes pontos antes do plano de execução:

1. [Pergunta Estratégica 1]
2. [Pergunta Técnica/Produto 2]
3. [Pergunta de Escopo/Prazo 3]
```

---

## 4. Mapeamento de Domínios dos Diretores

| Diretor | Perfil | Domínio de Foco no Diagnóstico |
| :--- | :--- | :--- |
| **@tech_director** (`vulcan`) | Tech & Infra | Viabilidade de stack, APIs, banco de dados, segurança, SRE e performance. |
| **@product_director** (`aura`) | Product & Spatial | Experiência UX/UI, fluxos críticos, casos de borda e design imersivo. |
| **@growth_director** (`vesper`) | Growth & Sales | Definição de ICP, canais de atração, conversão e métricas de sucesso (KPIs). |
| **@business_director** (`sterling`) | Business & Finance | Cronograma/marcos, viabilidade financeira (CAC/Burn) e escopo de MVP. |
| **@research_director** (`lyra`) | Research & Verticals | Conformidade legal/LGPD/saúde, validação de fontes e rigor metodológico. |

---

## 5. Exemplo de Fluxo Completo

1. **Usuário:** *"Atlas, queremos criar um dashboard de cashflow integrado com a Hotmart."*
2. **Atlas:** Identifica demanda Nível 2 (financeiro + backend).
3. **Atlas ➔ @tech_director & @business_director:** `[A2A-Diagnostic-Ping]`
4. **@tech_director:**
   `[A2A-Diagnostic-Reply]:`
   - Integração via Webhooks da Hotmart requer endpoint público seguro e rate limiting.
   - Banco de dados precisa de idempotência para evitar duplicidade de transações.
5. **@business_director:**
   `[A2A-Diagnostic-Reply]:`
   - Definir se o cálculo de churn deve ser em D+30 ou em tempo real de reembolsos.
   - Definir período histórico mínimo de importação (últimos 3 meses ou completo).
6. **Atlas ➔ Usuário:**
   *"Para estruturarmos a arquitetura do Dashboard de Cashflow Hotmart, defina:*
   *1. O painel deve recalcular transações em tempo real via Webhooks ou lote diário?*
   *2. Qual a janela histórica inicial de vendas a importar (ex: últimos 90 dias)?*
   *3. Há exigência de visualização de reembolsos e chargebacks discriminados?"*
