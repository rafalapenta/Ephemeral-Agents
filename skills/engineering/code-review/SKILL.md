---
name: code-review
description: "Two-axis code review comparing Git changes against repository coding standards and spec requirements. Automated PR inspection, smells detection, and standards compliance. Revisão de código, análise de diff, boas práticas e conformidade com especificações."
director: vulcan
source: "https://raw.githubusercontent.com/mattpocock/skills/main/skills/engineering/code-review/SKILL.md"
license: MIT
status: active
version: "1.0.0"
token_budget: 850
notes: "modified from mattpocock/skills"
---

# Code Review

Two-axis review of the diff between `HEAD` and a fixed point (commit, branch, tag, or merge-base):
- **Standards**: does the code conform to this repo's documented coding standards and baseline clean code principles?
- **Spec**: does the code faithfully implement the originating specification or task requirements?

## Quando Usar (When to Use)
- When reviewing a pull request, branch diff, or uncommitted work-in-progress changes.
- When evaluating adherence to repository standards (`CODING_STANDARDS.md`, `CONTRIBUTING.md`).
- When detecting code smells (duplication, feature envy, primitive obsession, shotgun surgery).

## Passos (Steps)
1. **Pin the fixed point**: Determine the target reference (e.g., `main`, commit SHA) and capture the diff (`git diff <fixed-point>...HEAD`).
2. **Review Standards**: Evaluate changes against repository standards and Fowler code smells (Mysterious Name, Duplicated Code, Primitive Obsession, Speculative Generality).
3. **Review Spec**: Evaluate implementation against local spec files in `docs/`, `specs/`, or task descriptions to detect missing requirements or scope creep.
4. **Synthesize Findings**: Present findings separately under Standards and Spec axes without merging or masking issues.

## Critérios de Verificação (Verification Criteria)
- Every standard violation cites the file, line, and documented rule or identified code smell.
- Spec findings distinguish between missing requirements, incorrect behavior, and out-of-scope additions.
- Review avoids network calls or external tracker dependencies; only local repository artifacts are inspected.
- Clear separation between hard violations and subjective design suggestions.

## Formato de Saída (Output Format)
```markdown
## Standards
- [File/Line]: <violation or smell description and recommended fix>

## Spec
- [Requirement]: <compliance status, missing elements, or scope creep>

## Summary
- Total findings: Standards: X, Spec: Y
- Key risk / recommendation: <summary>
```
