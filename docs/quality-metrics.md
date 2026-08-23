# Quality metrics

Kiln records an observe-first quality baseline before architectural refactoring. Generated
artifacts live under the ignored `reports/` directory and contain the Git commit, Python and OS
details, tool versions, commands, and exit statuses in `reports/metadata.json`.

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
python -m tools.quality_metrics --tier deterministic --observe
```

Remove `--observe` when pytest and Ruff should be enforced locally. The observe flag does not
hide failures: every command's exit status and output remain in the reports; it only lets the
driver finish collecting the rest of a previously unknown baseline.

The deterministic run creates:

- pytest JUnit, slowest-test output, statement and branch coverage XML/JSON/HTML;
- Radon complexity, maintainability, and raw-size reports;
- per-function CRAP JSON and Markdown derived from coverage and Radon reports;
- Ruff human-readable and JSON output plus a formatting check;
- gradual Pyright diagnostics for the enrolled pure/state modules;
- a lightweight identical-function duplication signal;
- reproducibility metadata.

The cockpit, live agent CLIs, authenticated backends, and terminal emulators are not prerequisites.
Tests marked `integration` still use only deterministic local SQLite, Git, filesystem, and HTTP.

## Mutation tiers

Cosmic Ray remains split into the existing Windows-compatible pure-policy and SQLite tiers under
`tests/mutation/`. The project runner creates every output directory, finds the platform's scripts,
and produces the readable and JSON summaries:

```text
python -m tools.run_mutation pure
python -m tools.run_mutation db
```

Mutation is slower and stays on-demand/nightly until its accepted baseline is known.
Use `--init-only` to enumerate a tier without executing it; the initial pure tier contains 2,085
mutants and is intentionally not part of the ordinary deterministic command.

## Policy

Pytest and Ruff remain hard gates. All newly introduced metrics begin as reports. Thresholds are
added only after reviewing the baseline, and should ratchet from that accepted state rather than
using arbitrary aspirational numbers. Existing hotspots belong in a reviewed baseline file, not
in broad exclusions.
