# Quality metrics

Kiln keeps a reviewed quality baseline in `quality-baseline.json`. Generated artifacts live under
the ignored `reports/` directory and contain the Git commit, Python and OS details, tool versions,
commands, and exit statuses in `reports/metadata.json`.

Install the development environment:

```text
python -m pip install -r requirements-dev.txt
```

The fast local signal is:

```text
python -m tools.quality_metrics --tier fast
```

The full deterministic report suite is:

```text
python -m tools.quality_metrics --tier deterministic
```

Add `--observe` only when collecting all reports despite a known failing gate. It does not hide
failures: every command's exit status and output remain in the reports.

The deterministic run creates:

- pytest JUnit, slowest-test output, statement and branch coverage XML/JSON/HTML;
- Radon complexity, maintainability, and raw-size reports;
- per-function CRAP JSON and Markdown derived from coverage and Radon reports;
- Ruff human-readable and JSON output plus a formatting check;
- gradual Pyright diagnostics for the enrolled pure/state modules;
- a lightweight identical-function duplication signal;
- reproducibility metadata.

Pytest discovers three complementary suites by default:

- `tests/unit/` contains fixed examples and regression cases;
- `tests/property/` contains Hypothesis-generated invariants and mirrors `src/kiln/`;
- `tests/integration/` exercises deterministic local boundaries.

Property modules use the `test_<subject>_properties.py` suffix so they can be collected in the
same run as same-subject unit and integration modules. Property testing is a generation method,
not a coverage target: use it for algebra, round-trips, normalization, bounds, monotonicity and
other stable invariants rather than wrapping every orchestration path in arbitrary data.

The cockpit, live agent CLIs, authenticated backends, and terminal emulators are not prerequisites.
Tests marked `integration` still use only deterministic local SQLite, Git, filesystem, and HTTP.

Selective acceptance scenarios live under `tests/acceptance/` and run separately:

```text
python -m pytest tests/acceptance
```

They invoke installed Kiln entry points with deterministic fake workers and local Git, SQLite,
filesystem, and loopback HTTP resources. Add one when a public workflow or cross-process
regression could remain broken while its individual adapters pass; keep adapter edge cases in
the faster unit and integration suites.

## Mutation tiers

Cosmic Ray remains split into the existing Windows-compatible pure-policy and SQLite tiers under
`tests/mutation/`. The project runner creates every output directory, finds the platform's scripts,
and produces the readable and JSON summaries:

```text
python -m tools.run_mutation pure
python -m tools.run_mutation db
```

Each command starts with a fresh enumeration so changed source or configuration cannot silently
reuse stale mutants. Pass `--reuse` only to resume an interrupted session. Existing reports remain
under `reports/mutation/` until a completed run replaces them.

Mutation is slower and stays on-demand or nightly. Use `--init-only` to enumerate a tier without
executing it. Mutant counts and accepted behavioral scores are recorded in
`quality-baseline.json`; they change whenever the domain or Cosmic Ray configuration changes.
Mutation remains intentionally outside the ordinary deterministic command.

## Policy

Pytest and Ruff are hard gates. CRAP must remain at or below 6. Coverage and behavioral mutation
scores should ratchet from the reviewed baseline rather than regress without an explicit review.
Annotation-only mutation survivors are reported separately because changing a runtime-erased type
annotation does not exercise application behavior.
