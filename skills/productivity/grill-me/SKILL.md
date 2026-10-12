---
name: grill-me
description: "Relentless Socratic interview to sharpen plans, stress-test architectures, and resolve design ambiguity. Design tree frontier exploration, decision validation, and assumption challenge. Entrevista socrática, validação de hipóteses, desafio de premissas e refinamento de decisões de produto."
director: aura
source: "https://raw.githubusercontent.com/mattpocock/skills/main/skills/productivity/grill-me/SKILL.md"
license: MIT
status: active
version: "1.0.0"
token_budget: 700
notes: "modified from mattpocock/skills"
---

# Grill-Me (Socratic Design Challenger)

Relentless interview method to stress-test thinking, sharpen product designs, and uncover unspoken assumptions through an iterative decision tree.

## Quando Usar (When to Use)
- When a user asks to "grill me", "stress-test this idea", or challenge a product architecture.
- When evaluating complex tradeoffs before committing to an irreversible design path.
- When unblocking ambiguous requirements through structured multiple-choice questions.

## Passos (Steps)
1. **Map the Design Tree**: Model every unsettled decision and the downstream choices that branch from it.
2. **Compute the Frontier**: Group questions whose prerequisites are already settled into one round.
3. **Format Question Rounds**: Number each question, present choices, and state the recommended option such that a "yes" confirms the recommendation.
4. **Iterate**: As decisions settle, recompute the frontier with newly unblocked questions until the tree is completely resolved.

## Critérios de Verificação (Verification Criteria)
- Fact-finding uses existing repository context and files without burdening the user with lookups.
- Each question round is concise and includes actionable recommended defaults.
- Only prerequisite-settled questions appear in the current frontier round.
- The session concludes only when all branches of the design tree have explicit confirmation.

## Formato de Saída (Output Format)
```markdown
❓ **Q1** - **<Question Title>**: <Context and options>
➡️ <Recommended Answer>

---

❓ **Q2** - **<Question Title>**: <Context and options>
➡️ <Recommended Answer>
```
