---
name: domain-modeling
description: "Domain-Driven Design (DDD) domain modeling, ubiquitous language refinement, GLOSSARY.md curation, and Architecture Decision Records (ADRs). Modelagem de domínio, glossário ubíquo, refinamento de entidades e registro de decisões arquiteturais."
director: lyra
source: "https://raw.githubusercontent.com/mattpocock/skills/main/skills/engineering/domain-modeling/SKILL.md"
license: MIT
status: active
version: "1.0.0"
token_budget: 750
---

# Domain Modeling

Actively build, sharpen, and document the project's domain model, ubiquitous language, and architectural decisions.

## Quando Usar (When to Use)
- When establishing or refining ubiquitous domain terminology in `GLOSSARY.md`.
- When resolving naming conflicts between codebase concepts and business logic.
- When drafting formal Architecture Decision Records (`docs/adr/`) for irreversible tradeoffs.

## Passos (Steps)
1. **Challenge Language**: Detect overloaded or ambiguous terms and cross-reference them against the domain glossary.
2. **Probe Concrete Scenarios**: Stress-test model boundaries with edge cases and contradictory domain conditions.
3. **Verify Implementation Harmony**: Check that code structures align with stated domain invariants.
4. **Record ADRs**: Document significant architectural decisions involving genuine tradeoffs that are hard to reverse.

## Critérios de Verificação (Verification Criteria)
- Domain terms in `GLOSSARY.md` are completely devoid of volatile implementation details.
- ADRs satisfy the three-part rule: hard to reverse, surprising without context, and result of a real trade-off.
- Concrete scenario examples illustrate each domain rule.

## Formato de Saída (Output Format)
- Glossary definition entry (canonical term, meaning, rejected synonyms).
- Domain boundary scenario analysis.
- Structured ADR markdown (`Title`, `Context`, `Decision`, `Consequences`).
