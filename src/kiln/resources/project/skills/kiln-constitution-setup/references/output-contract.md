# Constitution output contract

Both files must be concise instructions for every Kiln role, not an analysis report.

## `engineering.md`

Include only applicable, concrete rules covering:

- supported language/runtime versions and dependency/environment management;
- exact build and verification commands;
- test layers and their locations or markers;
- linting, formatting, type checking, coverage, complexity, and mutation tooling;
- contribution and execution practices that are project-specific or important to safe work.

Preserve broadly applicable Kiln safeguards from meaningful existing content when they still
apply, such as worktree-local tooling, one execution environment per worktree, external-runtime
probes, and generated-output cleanup. Remove language-specific defaults that the project does
not use.

## `project.md`

Include only confirmed information covering:

- purpose, users, high-level capabilities, and deliberate exclusions;
- architecture style and dependency direction;
- module or bounded-context responsibilities;
- persistence and external-system boundaries;
- project-specific constraints and quality gates.

## Basis and review

In the proposal, separately list:

- repository facts with the files that support them;
- user decisions made during the interview;
- contradictions resolved by the user;
- omitted areas that are not applicable or intentionally undecided.

Do not turn that audit list into verbose constitution prose. Before writing, show the complete
proposed file content and state whether each existing destination is absent, scaffold-like, or
meaningfully customized. Meaningful customization always requires explicit replacement approval.
