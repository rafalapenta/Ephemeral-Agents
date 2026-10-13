---
name: tdd
description: "Test-Driven Development (TDD) cycle. Red-green-refactor workflow, vertical slices, seam-based testing, and test design verification. Desenvolvimento orientado a testes, testes unitários, testes de integração e ciclo de testes automatizados."
director: vulcan
source: "https://raw.githubusercontent.com/mattpocock/skills/main/skills/engineering/tdd/SKILL.md"
license: MIT
status: active
version: "1.0.0"
token_budget: 700
---

# Test-Driven Development (TDD)

TDD is the red → green loop. This skill guides producing tests worth keeping: what a good test is, where tests go, anti-patterns, and the rules of the loop.

## Quando Usar (When to Use)
- When building features or fixing bugs test-first.
- When the user mentions "red-green-refactor", unit tests, or integration tests.
- When validating architectural seams and contracts before writing implementation logic.

## Passos (Steps)
1. **Identify the seam**: Agree on the public interface boundary where behavior is observed without reaching into internals.
2. **Red**: Write a single failing test for a vertical slice verifying observable behavior.
3. **Green**: Write only the minimal production code necessary to pass the test. Do not anticipate speculative features.
4. **Refactor**: Clean up the design while keeping all tests passing, consulting design principles.

## Critérios de Verificação (Verification Criteria)
- Test verifies behavior through public interfaces, not implementation details or private methods.
- Assertion values come from an independent source of truth (spec, literal, example).
- Test fails before implementation (Red verified) and passes with minimal implementation (Green verified).
- No tautological assertions or implementation coupling.

## Formato de Saída (Output Format)
- Seam definition and rationale.
- Test code snippet with clear descriptive naming (`test_user_can_...`).
- Minimal implementation code passing the test.
- Verification status summary (Red status → Green status).
