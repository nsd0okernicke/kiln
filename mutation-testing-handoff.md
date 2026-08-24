# Mutation testing handoff

State updated on 2026-08-24 after validating both Cosmic Ray tiers. The portable reports are
checked in under `reports/mutation/`. Cosmic Ray's session databases remain ignored under
`.kiln-mutation/`: they contain machine-local paths and can be regenerated from the committed
configuration.

## Completed runs

### Pure scheduler domain

Command used:

```text
python -m tools.run_mutation pure
```

Configuration: `tests/mutation/pure-modules.toml`

- Target: `src/kiln/scheduler/domain`
- Tests: scheduler domain unit tests plus scheduler domain property tests
- Mutants: 704
- Killed: 474
- Survived: 215
- Incompetent: 15
- Raw mutation score: 68.80%
- Annotation-only survivors: 154
- Behavioral survivors: 61
- Effective behavioral mutation score: 88.60%
- Intended target: at least 80%
- Evidence: `reports/mutation/pure.txt` and `pure-summary.json`

The tier was rerun and strengthened on 2026-08-24. Direct field-wise token arithmetic and a full
retry-decision truth-table property killed 24 meaningful survivors. The raw score cannot reach 80%
with this Cosmic Ray operator set: 154 runtime-invisible PEP 604 annotation mutations cap it below
that threshold even if every behavioral mutant is killed. The source-position-aware behavioral
score is the accepted result and clears the intended target.

### SQLite persistence

The first run targeted only `persistence/db.py`. Refactoring had turned that file mostly into a
compatibility facade, so its 13-mutant/15.38% result was invalid as a persistence baseline and was
replaced.

The repaired configuration targets the complete
`src/kiln/scheduler/infrastructure/persistence` package and excludes export-only `db.py` and
`__init__.py`. It covers `queue_commands.py`, `queue_queries.py`, `queue_storage.py`, and
`sqlite_message_queue.py`.

Commands used after repairing the configuration:

```text
python -m tools.run_mutation db --init-only
python -m tools.run_mutation db --reuse
```

- Mutants: 443
- Killed: 43
- Survived: 399
- Incompetent: 1
- Raw mutation score: 9.73%
- Annotation-only survivors: 385
- Behavioral survivors: 14
- Effective behavioral mutation score: 75.44%
- Intended target: at least 70%
- Evidence: `reports/mutation/db.txt` and `db-summary.json`

The repaired package configuration was validated on 2026-08-24. Representative executable
mutations in `queue_commands.py` and `queue_queries.py` were killed by the configured tests. A
representative exception mutation in `sqlite_message_queue.py` initially survived, then was killed
by a focused adapter error-translation assertion. The compatibility facade does not bypass the
mutated modules.

The raw score is not meaningful for this tier: Cosmic Ray generates 385 mutations of PEP 604
unions inside type annotations, and runtime tests cannot observe them. `mutation_summary.py` now
classifies annotation mutations by their recorded source positions and reports the behavioral
score separately. The 14 remaining behavioral survivors are equivalent or observability-only:
tuple index `0` changed to `-1` on one-column rows, comparisons against SQLite's always-positive
`rowcount`, and changes confined to debug/info logging. They are intentionally not covered by
artificial assertions.

## Continue on another computer

1. Check out the commit containing this handoff, the mutation reports, and both mutation TOMLs.
2. Install the development dependencies:

   ```text
   python -m pip install -r requirements-dev.txt
   ```

3. Run the deterministic suite before changing tests:

   ```text
   python -m pytest
   python -m ruff check src tests tools
   python -m pyright
   ```

4. Treat the DB tier as validated at its effective 75.44% behavioral score. Keep annotation,
   equivalent, incompetent, and observability-only mutants separate from genuine survivors.
5. Both tiers now clear their effective behavioral targets. For future changes, group meaningful
   survivors by production module and mutation operator. Prioritize incorrect branches,
   comparisons, status transitions, queue ordering, token arithmetic, and parsing rules.
6. For each group, add the smallest behavioral unit or property assertion that would fail for the
   mutation. Do not add assertions solely to kill equivalent mutations.
7. Record equivalent, incompetent, and framework-noise mutations separately from genuine
   survivors so the effective score remains explainable.
8. Re-enumerate after any source or mutation-config change. The runner starts fresh by default:

   ```text
   python -m tools.run_mutation pure
   python -m tools.run_mutation db
   ```

   Use `--reuse` only to resume an existing `.kiln-mutation/<tier>.sqlite` session on the same
   machine. The session databases are not portable or checked in.
9. Update `quality-baseline.json` only after both configurations are validated and their results
   are accepted.

## Related follow-up

GitHub issue [#28](https://github.com/nsd0okernicke/kiln/issues/28) tracks the application-layer
naming cleanup. Complete mutation analysis and focused test strengthening first; the structural
renames would otherwise invalidate report paths and make survivor comparison unnecessarily noisy.
