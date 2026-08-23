# Mutation testing handoff

State captured on 2026-08-23 after completing both Cosmic Ray tiers. The portable reports are
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
- Killed: 450
- Survived: 239
- Incompetent: 15
- Mutation score: 65.31%
- Intended target: at least 80%
- Evidence: `reports/mutation/pure.txt` and `pure-summary.json`

This is a valid baseline. Its survivors should be classified and addressed.

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
- Killed: 29
- Survived: 413
- Incompetent: 1
- Mutation score: 6.56%
- Intended target: at least 70%
- Evidence: `reports/mutation/db.txt` and `db-summary.json`

The run completed, but the score is suspiciously low. Do not immediately interpret all 413
survivors as missing tests. First verify that mutations in modules imported through the `db.py`
compatibility facade are actually active in the test process. A representative mutation in each
of `queue_commands.py`, `queue_queries.py`, and `sqlite_message_queue.py` should be checked before
using this as the accepted DB baseline.

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

4. Diagnose the DB setup first. Select representative `SURVIVED` entries from `db.txt`, confirm
   that Cosmic Ray applies them to the module imported by the tests, and fix the configuration if
   the compatibility facade bypasses the mutated module.
5. Group meaningful survivors by production module and mutation operator. Prioritize incorrect
   branches, comparisons, status transitions, queue ordering, token arithmetic, and parsing rules.
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
