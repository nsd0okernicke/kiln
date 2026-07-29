<!-- Copied into <project>/kiln/project/constitution/skill-orchestration.md by kiln-init. Framework default skill dependency chain — customize only if your project adds/removes quality-gate skills. -->

# Skill Orchestration

This document is the single reference for which skill runs when, in what order, and who owns each
quality gate. Individual role files (`roles/*.md`) state ownership; this document exists so the
end-to-end chain and the reasoning behind the order is visible in one place instead of scattered
across four role files and a dozen `SKILL.md`s.

## Pipeline Order

```
specifier                    coder                       refactorer                          architect
──────────                    ─────                       ──────────                          ─────────
gherkin-spec-workflow    →    tdd-red                 →    coverage-check                 →    final-verification
 (4 phases, user           ↺  tdd-green                    crap-analyzer                        ├─ run-mutation (full)
  approval gate)             tdd-refactor                  mutation-testing (DRY + scan only)    ├─ mutation-testing (DRY)
                              (loop per behavior slice)     property-test-generator               └─ soft Gherkin mutation
```

Each arrow is a handoff (`/kiln-handoff` → `/kiln-receive`); each role runs its own skills to
completion before handing off. `tdd-coordinator` scaffolds the outer-loop step definitions from the
specifier's `.feature` files at the start of the coder's phase; `tdd-red`/`tdd-green`/`tdd-refactor`
then loop per behavior slice.

## Refactorer Quality Gates (in order)

The refactorer's four gates run in this order because each depends on the previous step's output:

1. **`/coverage-check`** — establishes baseline coverage; later steps (CRAP, mutation scan) need
   accurate coverage numbers to be meaningful.
2. **`/crap-analyzer`** — CRAP = complexity² × (1 - coverage) + complexity, so it needs step 1's
   coverage numbers. Target: CRAP ≤ 6 per function.
3. **`/mutation-testing`** (DRY guidance only, not a full mutation run) — duplication is easiest to
   spot and fix once high-CRAP functions are already addressed, since extraction often changes
   which code is duplicated.
4. **`/mutation-testing`** (scan/count mode) — run last, on the now-refactored files, to check
   whether any file exceeds 100 mutation sites and needs a behavior-preserving split before
   handoff. Running this first would count sites in code that's about to change shape.

**`/property-test-generator`** runs alongside step 4, before handoff — the refactorer uses it to
assess property-test coverage on the same finalized module boundaries (`refactorer.md` → "Property
Testing"). It is not a numbered gate because it doesn't block on the gates above; it's an
add-tests step, not a pass/fail check.

## Ownership Matrix

Mutation testing and Gherkin acceptance mutation each have exactly one owner. This was a source of
ambiguity in earlier drafts of the role files; it is now explicit in both the role files and here:

| Gate | Owner | Skill | Explicitly forbidden to |
|---|---|---|---|
| Coverage | refactorer | `/coverage-check` | — |
| CRAP | refactorer | `/crap-analyzer` | — |
| DRY | refactorer (advisory) → architect (verification) | `/mutation-testing` (DRY mode) | — |
| Mutation site count (scan only) | refactorer | `/mutation-testing` (scan mode) | — |
| Property tests | refactorer | `/property-test-generator` | coder (runs only when explicitly requested — `coder.md` → "Properties and Handoff") |
| **Full mutation run** | **architect** | `/run-mutation`, `/final-verification` step 1 | coder, refactorer (`coder.md` / `refactorer.md` → "Non-Ownership": "Do not run mutation tests") |
| **Gherkin acceptance mutation** | **architect** | `/final-verification` step 3 (soft Gherkin mutation) | specifier, coder, refactorer (all three role files explicitly say "Do not run Gherkin acceptance mutation") |
| Mutation manifests (`mutations.xml`, `gherkin-mutations.xml`) | architect owns the run; refactorer preserves manifest state across code splits (`refactorer.md` → "Manifest Protection") — do not hand-edit | `/run-mutation`, `/final-verification` | — |

The rule of thumb: the refactorer's mutation-testing usage is always *diagnostic* (DRY guidance,
site counting) — never a pass/fail mutation run. The architect is the only role that runs mutation
tests to completion and treats survivors as a handoff blocker.

## Tool Mapping (by language)

Individual skills (`coverage-check`, `crap-analyzer`, `mutation-testing`, `run-mutation`) each
document per-language commands inline. This table consolidates them; `constitution/engineering.md`
should carry the authoritative version-pinned copy for your project's actual language(s) — most
projects only need one row.

| Language | Coverage | Complexity/CRAP | DRY | Mutation | Gherkin mutation |
|---|---|---|---|---|---|
| Python | `coverage.py` | `radon` | `radon mi` | `mutmut` | `gherkin-mutator` |
| Java/Kotlin | JaCoCo (`./gradlew jacocoTestReport`) | JaCoCo + PIT CRAP (threshold 30, differs from Python's ≤6 — see `crap-analyzer` "Threshold Note") | detekt / SonarQube | PIT (`./gradlew pitest`) | `gherkin-mutator` |
| Go | `go test -cover` | custom/`go build` complexity tooling | custom or grep-based | `stryker` (or equivalent) | `gherkin-mutator` |

## Skills Outside the Pipeline

These skills are **not** part of the specifier → coder → refactorer → architect chain above. They
are user-invoked utilities, called directly by a human via their slash command, not by role
workflow rules:

- `zoom-out` — `disable-model-invocation: true` by design: its body is a canned user-prompt
  template ("I don't know this area of code well...") meant to be typed by a human via
  `/zoom-out`, not invoked by an agent mid-task. This is intentional, not a bug.
- `caveman` — communication-style toggle, user-invoked.
- `grill-me`, `grill-with-docs`, `kickoff` — pre-specification ideation/discovery, invoked before
  or alongside the specifier's phase, not part of its required workflow.
- `documentation-updater`, `architectural-reviewer`, `code-review-tdd`, `review` — manual review
  aids, invoked ad hoc by a human, not by role loop rules.
- `aps-setup`, `crap-run` — one-off toolchain setup/language-specific variants, invoked as needed.

## Removed Skills

- **`acceptance-test-writer`** — removed (2026-07-29). Superseded by `gherkin-spec-workflow` for
  Kiln's specifier role (mutation-aware parameter pruning, four-phase approval gate); nothing in
  `roles/`, `templates/`, or `profiles.json` referenced it. `gherkin-spec-workflow` is the only
  acceptance-test skill the specifier role invokes.
