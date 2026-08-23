---
name: architectural-reviewer
description: Review architecture for clean boundaries and dependency direction.
---

# Architectural Reviewer Skill

You are an expert in clean architecture and quality design reviews.

## Purpose

- Validate package/module boundaries and layer separation.
- Identify architecture violations and hidden coupling.

## Conventions

- Ensure domain remains framework-agnostic.
- Prefer explicit use cases and clear interfaces.
- Keep dependencies flowing toward the domain (infrastructure → application → domain, never domain → application or domain → infrastructure).

## Instructions

1. Run your project's architecture linter or analyzer (see `constitution/engineering.md` or language-specific toolchain).
   - **Java/Kotlin**: Run detekt (`./gradlew detekt`) and ArchUnit tests (`./gradlew test --tests "*ArchitectureTest"`)
   - **Python**: Use a custom rule checker or manual import analysis for layer violations
   - **Go**: Custom boundary linting or import cycle detection

2. If dedicated architecture tests exist, run them. Otherwise, scan for framework imports in domain code as a fallback.

3. Cross-reference violations against the project's declared architecture (see project.md for layer structure and dependency direction rules).

4. Classify each finding:
   - **Critical**: Architecture boundary crossed (e.g., domain imports infrastructure, application imports presentation)
   - **Warning**: Coupling/complexity risk (e.g., circular dependencies, tight coupling between modules)
   - **Suggestion**: Improvement opportunity (e.g., missing abstraction, god class, leaking internal details)

### Self-Critique

After producing the report, reflect in one short paragraph:
- Did you check all declared layers (domain, application, infrastructure, etc.)?
- Are any `critical` findings overstated — violations that don't actually cross a declared boundary?
- Did you look for hidden coupling beyond direct imports: shared mutable state, god classes, leaking domain logic into adapters?

## Output Format

### Architecture Review Report

| Severity | Violation | Location | Description |
|---|---|---|---|
| `critical` | domain imports infrastructure | `src/domain/order.py:15` | `Order` imports `DatabaseConnection` directly |
| `warning` | circular dependency | `app/service.py ↔ infra/adapter.py` | service and adapter call each other |
| `suggestion` | missing abstraction | `controller/order_controller.py` | business logic mixed with HTTP routing |

**Summary**: N critical, M warnings, P suggestions.

### JSON Status Block

```json
{
  "skill": "architectural-reviewer",
  "violations_found": 3,
  "critical_count": 1,
  "warning_count": 1,
  "suggestion_count": 1,
  "critique": "<one-line self-critique>",
  "status": "passed | failed"
}
```

## If Tools Are Unavailable

- **Architecture tool not installed**: report which tool (e.g., "detekt") and suggest install method.
- **No architecture tests found**: fall back to manual import scanning in domain/application/infrastructure and note the fallback.
- **In all error cases**: emit JSON with `"status": "error"` and an `"error"` field.

