---
name: pr
description: "Pull Request description authoring with structured summaries, architectural diff sketches, evidence, and merge risk assessment. Criação e documentação de Pull Requests, changelog estruturado, raio de impacto e evidências de testes."
director: vulcan
source: "https://raw.githubusercontent.com/mattpocock/skills/main/skills/engineering/pr/SKILL.md"
license: MIT
status: active
version: "1.0.0"
token_budget: 650
---

# Pull Request (PR) Body Authoring

Structured template and guidelines for generating concise, informative Pull Request descriptions.

## Quando Usar (When to Use)
- When preparing a pull request body for code review and merge.
- When summarizing architectural changes, refactors, or bug fixes with visual aids.
- When documenting verification evidence and rollback risk for maintainers.

## Passos (Steps)
1. **Build the Summary**: Choose the most concise representation (call tree, file tree, Mermaid diagram, or pseudo-diff) that captures the core change.
2. **Compile Evidence**: Document before-and-after verification evidence (test run outputs, terminal logs, or component behavior).
3. **Assess Merge Danger**: Classify decision reversibility (one-way door vs. two-way door) and specify the blast radius.
4. **Format Description**: Assemble the standard markdown PR body without conversational preamble.

## Critérios de Verificação (Verification Criteria)
- Includes concise visual aid or structural diff rather than generic prose.
- Features concrete verification evidence (passing tests, console output).
- Explicitly states merge reversibility (one-way / two-way) and blast radius.
- Uses project domain language accurately.

## Formato de Saída (Output Format)
```markdown
## Summary
<diagram, call tree, or structural diff sketch>

## Evidence
- **Before:** <failing test run or baseline behavior>
- **After:** <passing test run or updated output>

## Merge Danger
- **Door:** <one-way or two-way> (<reversibility explanation>)
- **Blast Radius:** <scope of impact across modules/users>
```
