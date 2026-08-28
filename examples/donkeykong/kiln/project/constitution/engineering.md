# Engineering Rules — Donkey Kong (Rust)

## General Rules

- Work in small, reviewable increments that leave the game runnable.
- Read the README section and acceptance scenarios for a story before implementing it.
- Prefer explicit structs, enums, tables, and state transitions over an ECS or general framework.
- Keep comments, identifiers, diagnostics, and documentation in English.
- Do not change normative gameplay constants merely to make a failing test pass. Correct the code,
  or update the specification with a documented rationale first.
- Avoid unrelated dependency, formatting, or architecture changes.

## Toolchain and Dependencies

- Use the latest stable Rust toolchain compatible with the committed lockfile. Do not run a global
  toolchain upgrade unconditionally on every task.
- Install missing components with `rustup component add rustfmt clippy`.
- Install missing quality tools with `cargo install cargo-mutants cargo-audit`.
- Add dependencies only for a clear requirement. `macroquad` is a runtime dependency; `cucumber`,
  `proptest`, and its async runner are dev-dependencies; Cargo subcommands are not dependencies.
- Commit `Cargo.lock` because this project builds an application.

## Rust Standards

- Run `cargo fmt`; `cargo clippy --all-targets -- -D warnings` is authoritative.
- Document public API exported outside the crate. Internal helpers need comments only when intent or
  an invariant is not clear from code.
- Prefer enums to boolean mode flags and newtypes for units easily confused, such as pixels,
  fixed-point units, ticks, score, level, and board index.
- Use checked or saturating arithmetic at score/timer boundaries. Narrow conversions are explicit.
- No `unwrap()` or `expect()` in production code. Tests may use them when failure is the assertion.
- Do not suppress a lint without a nearby explanation.
- Prefer manual error types for small local errors; add an error crate only when it materially
  improves several call sites.

## Pure Simulation Boundary

`src/sim/` and the public API in `src/lib.rs` must be deterministic and headless:

- no macroquad imports;
- no file, network, environment, terminal, or audio/video I/O;
- no wall-clock reads or sleeps;
- no global mutable state or interior-mutability workaround;
- no thread-local or independently seeded randomness;
- no floating-point values used to decide gameplay outcomes.

The runtime supplies `InputFrame` and an initial seed. A tick returns state changes and semantic
events. Renderer/audio code consumes snapshots and events without mutating simulation decisions.

Before architecture handoff, search the entire simulation tree, not a hard-coded file list:

```text
rg -n "macroquad|std::fs|std::net|std::env|Instant|SystemTime|thread_rng" src/sim src/lib.rs
```

Review every match. The expected result is no prohibited import or call; comments explaining the
rule may be excluded from the search or reviewed manually.

## Fixed-Timestep Runtime

- Simulation frequency is 60 Hz and uses an accumulator independent of rendering.
- Poll input once per rendered frame. Held directions apply to all catch-up ticks; edge actions
  apply only to the first.
- Execute at most five simulation ticks per render. Log and discard excess accumulated time so an
  overloaded process recovers instead of spiralling.
- Do not pass a floating `dt` into simulation physics. One call to `tick` always means one tick.
- Render interpolation is presentation-only and never changes simulation state or hitboxes.

## Movement and Physics Contract

These rules replace the contradictory “one pixel per frame” wording from the earlier draft:

- Logical positions/velocities use signed 8.8 fixed-point values.
- Walking repeats two one-pixel movement ticks and one animation-only tick.
- Climbing down moves 2 pixels every 4 ticks; climbing up moves 2 pixels every 5 ticks, subject to
  an explicit board-specific ladder-top gate.
- Jump horizontal velocity is `-0x0080`, `0`, or `+0x0080`.
- Jump upward impulse is `0x0148`, equal to 1.28125 pixels/tick in 8.8 notation.
- At airborne tick `t`, screen-Y delta is `(16*t + 8) - 0x0148` fixed-point units.
- Landing/support uses previous-to-current swept-foot probing. Hazard AABBs are checked once after
  movement with a minimum one-pixel overlap.
- Airborne tick 20 arms jump scoring/fatal-fall evaluation. It does not terminate integration.
- Natural falls start with zero impulse and are immediately armed.
- A drop of at least 15 pixels latches fatal fall; resolve a valid landing first on the same tick.
- Landing freezes input for exactly four ticks. Hammer lifetime is exactly 512 ticks.

Represent timer phases and comparison order explicitly. Off-by-one behaviour must be proven with
trace tests around ticks 0, 19, 20, 21, landing, freeze ticks 1–4, and expiry boundaries.

## Board Data and Fidelity

- Keep static geometry and board-specific movement tables outside renderer code.
- The 224×256 logical coordinate system is authoritative.
- Only README-listed coordinates are exact acceptance values. Reconstruct other coordinates from
  cited references, then freeze accepted layouts as board data plus golden screenshots.
- Do not claim exact original geometry for values chosen by playtest.
- Removed rivet support is collision data, not a visual-only tile change.
- Elevator sprites are visible and their collision path is the same data used for rendering.
- Moving 50m ladders are cyclic state machines; player use does not delete them.
- Never implement universal sprite wrapping. Each entity path specifies constrain, recycle,
  retire, or board-local wrap behaviour.

## Randomness and Replay

- The simulation owns exactly one documented PRNG state.
- Production may derive the initial seed outside the simulation. Record the seed in debug output.
- Tests use named constant seeds and, where useful, stored input traces.
- Consume a new random value only at a specified decision point. Refactors must not silently alter
  seeded outcomes without updating and explaining trace fixtures.
- For probability code, test direct mapping of all low-bit values as well as longer seeded traces.

## Coverage Measurement

- Use `cargo tarpaulin` or `cargo llvm-cov` for line and branch coverage.
- Game logic modules (under `src/sim/` and `src/lib.rs`) should aim for high coverage.
- Rendering, platform, and asset-loading code is excluded from coverage targets.

## Testing

### Unit tests

Place focused tests near code. Cover phase boundaries, integer truncation, all comparison edges,
state-entry resets, board transitions, and collision just-touching versus one-pixel overlap.

### Acceptance tests

Use the native Rust `cucumber` crate—not pytest terminology. Feature files live under
`tests/acceptance/features/`; `tests/acceptance.rs` is the async runner and is configured with
`harness = false`.

Acceptance steps drive the public headless simulation. They may inspect documented test snapshots
or query helpers, but may not create a window or couple to private struct layout.

```gherkin
Feature: Mario jump
  Scenario: The landing phase is armed without ending the jump
    Given Mario is grounded on a flat girder
    When Mario starts a vertical jump
    Then airborne tick 20 arms jump scoring
    And Mario remains airborne until his swept feet cross the girder
```

### Property tests

Use `proptest` for true invariants such as:

- effective difficulty is in `1..=5` for valid play;
- the same seed and inputs produce identical snapshots/events;
- AABB overlap is symmetric;
- score never decreases and the extra-life latch awards at most once;
- board-order indices remain valid;
- active-entity counts stay within configured capacities.

Do not assert false mathematical properties such as perfect symmetry of a truncated fixed-point
jump.

### Mutation, audit, and presentation

- `cargo mutants` targets the pure simulation; at least 80% of non-unviable mutants must be caught.
- Review surviving mutants instead of adding assertions with no behavioural value.
- `cargo audit` findings must be fixed, upgraded, or explicitly documented and accepted before the
  full-quality handoff.
- Deterministic golden images test all four layouts. Manual playtest checks scaling, keyboard feel,
  moving platforms, sound output, pause/resume, death/respawn, game over, and victory.

## Role Gates

| Role | Required gate before handoff |
|---|---|
| game-coder | `cargo build`, `cargo test --all-targets`, and `cargo clippy --all-targets -- -D warnings` |
| game-refactorer | Full tests, property tests, `cargo clippy -- -D warnings`, coverage check |
| game-reviewer | Release build, all tests, lint, format, `cargo audit`, code quality scan |
| game-architect | `cargo check --all-targets`, lint, boundary search, dependency-direction review |

Mutation testing (`cargo mutants`) is on-demand, not a per-cycle gate.
A failing mandatory gate blocks handoff. Include the relevant failure output in the handoff rather
than paraphrasing it.

## Non-Functional Requirements

- Native Windows, Linux, and macOS builds; Web/WASM is optional.
- Session-only high score; no persistence or networking.
- Invalid input must not panic. Impossible internal states should be prevented by types and state
  transitions, with recoverable errors at platform boundaries.
- The project must build without external art/audio files or original arcade data.
