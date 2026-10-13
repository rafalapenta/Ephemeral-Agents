---
name: to-spec
description: "Turn current context into formal technical specifications and user stories. Automatic spec generation, requirements formalization, and issue triage. Formalização de requisitos técnicos, user stories e especificações de produto."
director: aura
source: "https://raw.githubusercontent.com/mattpocock/skills/main/skills/engineering/to-spec/SKILL.md"
license: MIT
status: quarantined
version: "1.0.0"
token_budget: 750
---

# To Spec (Quarantined)

> **QUARANTINE NOTICE**: This skill is quarantined pending review. The original source contains instructions requiring remote network issue tracker publishing and executable environment setup commands (`/setup-matt-pocock-skills`). It is indexed in the catalog for governance tracking but strictly blocked from automatic router resolution.

Turn the current conversation context into a comprehensive technical specification with numbered user stories and implementation decisions.

## Quando Usar (When to Use)
- When converting high-level product discussions into structured technical specifications.
- When drafting exhaustive user stories with actors, actions, and business benefits.
- When documenting implementation and testing decisions before development starts.

## Passos (Steps)
1. **Analyze Context**: Synthesize requirements discussed without re-interviewing the user.
2. **Draft Problem & Solution**: Define the problem and proposed solution from the user's perspective.
3. **Detail User Stories**: List exhaustive numbered user stories (`As an <actor>, I want <feature>, so that <benefit>`).
4. **Define Decisions**: Specify module architectural decisions, interface changes, and testing strategy.

## Critérios de Verificação (Verification Criteria)
- Explicit problem and solution statements.
- Exhaustive numbered user stories covering primary workflows and edge cases.
- Out-of-scope boundaries clearly delineated.
- Quarantined: Automated router matching is prevented until security review removes network publishing directives.

## Formato de Saída (Output Format)
```markdown
## Problem Statement
<User-facing problem>

## Solution
<User-facing solution>

## User Stories
1. As an <actor>, I want <feature>, so that <benefit>

## Implementation Decisions
- Architectural and interface decisions (omitting volatile line numbers).

## Testing Decisions
- Seams under test and verification strategy.

## Out of Scope
- Excluded items.
```
