# Project Rules — Donkey Kong (Rust)

## Purpose

This is an unofficial educational reimplementation. It must remain buildable from source without
ROMs, ripped graphics, sampled arcade audio, or other proprietary game data. Procedural assets and
music must be original. The behavioural contract is the repository README.

## Language and Tooling

- Rust latest stable, selected through `rustup` and recorded by `Cargo.lock`.
- macroquad for windowing, rendering, input, and audio output.
- Cargo for dependency management, builds, and tests.
- Built-in Rust tests for unit and integration coverage.
- `cucumber` for native Rust Gherkin acceptance tests.
- `proptest` for simulation invariants.
- `cargo clippy` and `cargo fmt` for linting and formatting.
- `cargo-mutants` and `cargo-audit` are installed Cargo subcommands, not crate dependencies.

Expected manifest shape:

```toml
[dependencies]
macroquad = "..."

[dev-dependencies]
cucumber = "..."
proptest = "..."
tokio = { version = "...", features = ["macros", "rt-multi-thread"] }

[[test]]
name = "acceptance"
harness = false
```

Use currently compatible stable versions when scaffolding, then commit `Cargo.lock`. Do not invent
a dependency named `proptest-dev`, and do not add `cargo-mutants` or `cargo-audit` to `Cargo.toml`.

## Package Structure

```text
donkeykong/
├── Cargo.toml
├── Cargo.lock
├── src/
│   ├── lib.rs
│   ├── sim/
│   │   ├── mod.rs
│   │   ├── entities/
│   │   ├── board/
│   │   ├── physics.rs
│   │   ├── collision.rs
│   │   ├── scoring.rs
│   │   ├── difficulty.rs
│   │   └── rng.rs
│   ├── main.rs
│   ├── runtime.rs
│   ├── input.rs
│   ├── renderer.rs
│   ├── audio.rs
│   ├── assets.rs
│   └── screens/
└── tests/
    ├── acceptance.rs
    └── acceptance/features/
```

`src/lib.rs` exposes the headless simulation required by tests. `src/main.rs` is a thin macroquad
entrypoint. Everything under `src/sim/` is platform-independent and deterministic.

## Required Quality Gates

These gates are mandatory for the full-quality profile:

| Gate | Command or evidence | Requirement |
|---|---|---|
| Build | `cargo build --release` | Successful release build |
| Unit/integration/acceptance | `cargo test --all-targets` | All tests pass headlessly |
| Acceptance alone | `cargo test --test acceptance` | All Gherkin scenarios pass |
| Lint | `cargo clippy --all-targets -- -D warnings` | No warnings |
| Format | `cargo fmt --check` | No differences |
| Dependency audit | `cargo audit` | No unacknowledged vulnerable dependency |
| Mutation | `cargo mutants` | At least 80% of non-unviable mutants caught in `src/sim/` |
| Architecture | boundary inspection described in `engineering.md` | No macroquad/I/O in simulation |
| Presentation | manual playtest and deterministic golden images | All boards, scaling, input, and audio checked |

If an optional local tool is unavailable during early development, that fact may be recorded, but
the full-quality handoff does not pass until the tool is installed and its gate succeeds.

Generated mutation reports and golden-image failure artifacts must go in ignored build/artifact
directories, not source folders.

## Common Commands

```text
cargo run
cargo run --release
cargo test --all-targets
cargo test --test acceptance
cargo clippy --all-targets -- -D warnings
cargo fmt --check
cargo build --release
cargo mutants
cargo audit
```

Manual playtest is limited to platform-facing concerns that headless tests cannot establish:
window creation/scaling, keyboard feel, actual audio output, and visual motion on all four boards.
