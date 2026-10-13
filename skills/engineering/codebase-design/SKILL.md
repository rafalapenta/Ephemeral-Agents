---
name: codebase-design
description: "Design deep modules with minimal interfaces and rich implementations. Architectural seams, adapter separation, testability, and module leverage. Arquitetura de software, design de módulos profundos, interfaces coesas e desacoplamento de dependências."
director: vulcan
source: "https://raw.githubusercontent.com/mattpocock/skills/main/skills/engineering/codebase-design/SKILL.md"
license: MIT
status: active
version: "1.0.0"
token_budget: 800
---

# Codebase Design

Shared vocabulary and principles for designing deep modules: substantial behavior behind compact, coherent interfaces placed at clean seams.

## Quando Usar (When to Use)
- When architecting new modules, packages, or services.
- When refactoring shallow pass-through classes or bloated interfaces.
- When deciding where architectural seams and adapters belong for testability.

## Passos (Steps)
1. **Analyze Interface Depth**: Contrast the surface area of the interface against the internal complexity hidden behind it.
2. **Apply Deletion Test**: Verify if removing the module removes real complexity or merely pass-through boilerplate.
3. **Establish Architectural Seams**: Place public boundaries where callers observe behavior without coupling to concrete infrastructure.
4. **Design for Testability**: Ensure dependencies are passed in, methods produce return values over side effects, and surface areas remain minimal.

## Critérios de Verificação (Verification Criteria)
- Interface minimizes exposed methods and complex parameter structures.
- Seam exists only where concrete implementation truly varies or requires isolation.
- Internal seams remain private to the module implementation.
- Callers and automated tests cross the same public interface boundary.

## Formato de Saída (Output Format)
- Interface definition (types, function signatures, invariants).
- Diagram or description of the deep module vs. shallow alternatives.
- Placement of seams and adapter implementations.
- Evaluation of caller leverage and maintainer locality.
