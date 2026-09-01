# Donkey Kong — Educational Arcade Platformer Reimplementation

## 1. Purpose and Fidelity Policy

This project is a from-scratch, single-player reimplementation of the 1981 Donkey Kong arcade
game, written in Rust with macroquad. Its purpose is education: practising deterministic game
simulation, fixed-point physics, state machines, testing, and procedural audiovisual work.

The target is a **faithful-but-practical US TKG-04-style reimplementation**, not a ROM emulator:

- Reproduce documented, player-visible behaviour when practical and testable.
- Prefer this specification when historical sources disagree or hardware behaviour would make the
  project disproportionately complex.
- Do not emulate undefined-memory bugs, overflow bugs, CPU timing, or other accidents unless this
  specification explicitly retains them.
- Use data-driven layouts reconstructed from technical references and screenshots. Only values
  explicitly stated here are normative; the project does not claim byte-perfect ROM geometry.
- Use original procedural artwork and original chiptune compositions. Do not include ROM data,
  extracted sprites, sampled audio, or note-for-note copies of copyrighted music.

The README and `kiln/project/constitution/` form one specification. If they conflict, this README
controls game behaviour and `engineering.md` controls implementation rules.

## 2. Product Scope

**Supported platforms:** Windows, Linux, and macOS. Web/WASM is a stretch goal.

**MVP:** keyboard-controlled one-player play through four board types, level progression,
scoring, lives, sound, an in-memory high score, game over, and a safe victory after level 21.

**Display:**

- Simulation canvas: **224 × 256 pixels**, portrait orientation.
- Render output: **672 × 768 pixels** (3× the simulation canvas).
- The board background is rendered procedurally at 224×256 and upscaled to 672×768 with
  **linear filtering** for a smooth appearance.
- Sprites are loaded from high-resolution PNG files (`spr_*.png`, `tile_*.png`, see
  `ASSET_LIST.md`) and drawn at their native pixel size, mapped to 3× simulation coordinates.
- Default window: **672 × 768 pixels** (1:1 with render output).
- Support integer scaling with letterboxing. A window too small for 672×768 may use a
  fractional scale as a usability fallback.
- Simulation runs at exactly **60 ticks per second**.

No ECS is required. Ordinary structs and explicit state machines are clearer at this scale.

## 3. Controls and Top-Level States

| Context | Input | Behaviour |
|---|---|---|
| Title | Enter, Space, or Z | Start a new game |
| Title | Escape | Exit the application |
| Playing | Arrow keys or WASD | Move or climb |
| Playing | Z or Space | Jump |
| Playing | Escape | Pause |
| Paused | Escape | Resume |
| Paused | Q | Return to title |
| Game over or victory | Enter, Space, or Z | Return to title |

Directions are held inputs. Jump, start, pause, and return-to-title are edge-triggered. Consume an
edge at most once even if a rendered frame executes several simulation ticks, and clear
incompatible edges whenever the top-level state changes.

```text
Title -> Playing <-> Paused
Playing -> DeathAnimation -> Playing | GameOver
Playing -> BoardClear -> Playing | Victory
GameOver -> Title
Victory -> Title
Paused -> Title
```

Transitions are explicit values returned by a simulation tick. Rendering and audio react to
events; they do not decide simulation outcomes. Gamepad input and two-player play are out of scope.

## 4. Deterministic Simulation

- The simulation owns an integer tick counter and never reads wall-clock time.
- Positions and velocities use signed 8.8 fixed-point integers unless a board table is pixel-based.
- All random decisions use one PRNG owned by the simulation. `new_game(seed: u32)` accepts an
  explicit seed. The runtime supplies a non-deterministic seed; tests and replays use fixed seeds.
- A small documented generator such as xorshift32 is sufficient. Consume randomness only during
  simulation ticks, in a stable and tested order.
- The same initial state, seed, and input sequence must produce the same state on all platforms.

## 5. Mario Movement

### Walking

- Mario follows the supporting girder slope; horizontal movement updates his support-derived Y.
- Walking uses an integer sub-step cycle: two ticks move one logical pixel each, followed by one
  animation-only tick. Average horizontal speed is about 2/3 pixel per tick.
- Walking off a supported span starts a natural fall with zero initial vertical impulse.
- Mario respects board-specific horizontal limits. There is no global screen wrapping.
- Mario stores a held horizontal direction (`-1` left, `0` neutral, `+1` right) that is updated
  each tick from the input and exposed to the renderer via `Mario::direction()`. The initial
  direction at spawn is `+1` (right).

### Climbing

- Pressing up/down in a ladder capture region snaps Mario to the ladder centre X.
- Climbing down moves 2 pixels every 4 ticks. Climbing up moves 2 pixels every 5 ticks, except for
  board-specific top-of-ladder gating.
- Ladder top/bottom limits determine when Mario returns to grounded movement.
- Mario cannot jump or use a hammer while climbing.
- Broken ladders are climbable only along solid segments and cannot be crossed through a gap.

### Jumping and falling

Screen Y increases downward. A jump uses integer 8.8 fixed-point arithmetic:

```text
initial horizontal velocity = -0x0080, 0, or +0x0080  // -0.5, 0, +0.5 px/tick
initial upward impulse       =  0x0148                  // 1.28125 px/tick
vertical delta at tick t     = (16*t + 8) - 0x0148     // fixed-point units
```

At airborne tick `t`, starting at zero, add the vertical delta to fixed-point Y and horizontal
velocity to X. This produces an apex near ticks 20–21 and a flat-ground air time around 41 ticks,
subject to fixed-point truncation and the support surface.

- Direction is sampled airborne. Left/right immediately selects that horizontal sign; neutral
  preserves the current velocity. There is no air acceleration.
- Keep previous/current foot positions and probe the swept vertical interval for crossed support
  every airborne tick so Mario cannot tunnel through a girder.
- Resolve a valid landing before evaluating a fatal fall on the same tick.
- Airborne tick 20 arms jump-over scoring and fatal-fall evaluation; it does not end the jump.
- A natural fall starts with zero impulse and landing/fatal evaluation armed immediately.
- Falling **15 pixels or more** below take-off Y latches fatal fall. Mario dies when he next lands
  or leaves the board's valid play region.
- **Exception — 100m rivet drops:** When Mario is standing on a rivet support span at the
  moment it is erased (100m board), he drops to the nearest girder surface below immediately on
  the same tick. This drop is not a natural fall and does not latch fatal fall, even when the
  distance between flat-structure tiers (36 px) exceeds the 15 px threshold. If no surface exists
  below (off the board), a natural fall with standard fatal-fall evaluation starts.
- A successful landing begins a four-tick input freeze. Ignore input edges during the freeze.

### Hammer

- Touching an available hammer picks it up. If airborne, queue activation until the four-tick
  post-landing freeze ends.
- Hammer duration is 512 simulation ticks.
- While active, Mario may walk but cannot jump, climb, or voluntarily drop the hammer.
- A strike destroys eligible barrels/fire enemies overlapping its active strike region.
- The held hammer sprite is rendered above Mario's head, swinging side-to-side. It mirrors
  horizontally when Mario faces left, following `Mario::direction()`.

## 6. Board Progression

The observable order is normative; its byte encoding need not match the ROM.

| Level | Board sequence |
|---|---|
| 1 | 25m -> 100m |
| 2 | 25m -> 75m -> 100m |
| 3 | 25m -> 50m -> 75m -> 100m |
| 4 | 25m -> 50m -> 25m -> 75m -> 100m |
| 5–21 | 25m -> 50m -> 25m -> 75m -> 25m -> 100m |

Completing level 21's final 100m board transitions directly to `Victory`. Level 22 is never
constructed. This intentionally replaces the original overflow kill screen.

### Shared layout policy

- Store girders, ladders, conveyors, platforms, hazards, goals, and prizes in board data.
- Reconstruct geometry from the cited technical sources and lawful visual references.
- Maintain one 224×256 golden screenshot per board for static-layout regression.
- Explicit coordinates below are acceptance values. Other coordinates may be tuned without
  changing simulation rules.

### 25m — Girders and barrels

- Six sloping girder tiers connected by complete and broken ladders.
- Kong releases barrels from the upper-left area; Pauline is above the top girder.
- Oil drum logical position: `(39, 224)`. Two hammers are available on lower tiers.
- Reaching Mario Y `< 0x31` in the valid goal span completes the board.
- Barrels follow slopes, may descend complete ladders, fall at ends, bounce with one of 3–4
  fall-distance tiers, and retire at their path endpoint. They do not globally wrap.

### 50m — Conveyor belts

- Three conveyor regions carry Mario and three cement pies.
- Kong moves horizontally here. Conveyor reversal and Kong reaching the endpoint are one board
  state transition, not unrelated timers.
- Two moving ladder assemblies independently cycle through: parked at top -> extend -> bottom
  dwell -> retract -> parked. Top dwell is about 256 ticks; bottom dwell ends through a seeded
  PRNG gate (`rng.next_u8() & 0x0f == 0` on each bottom-dwell service). Player use does not
  permanently remove a ladder.
- Two hammers are available. Reaching Y `< 0x51` in the goal span completes the board.
- Geometry may be tuned by playtest; moving-system state and collision remain deterministic.

### 75m — Elevators and springs

- Two **visible** moving elevator groups carry Mario. The left group rises and the right descends
  before recycling through their board-data paths.
- A supported Mario inherits platform movement until he walks/jumps off or carry ends.
- Springs use 16 types with initial X in `0x28..=0x37` and a table-driven 25-value bounce path.
  A slot follows about 25 ticks entering, 75 bouncing, 56 falling, and 24 cooldown.
- Generation uses a board-data interval table keyed by effective difficulty: 120, 100, 80, 60,
  and 60 ticks for difficulties 1 through 5.
- Safe top-platform spans are `0xA7..=0xB4` and `0x75..=0x82`.
- No hammer is present. Prizes are umbrella, hat, and purse.
- Reaching Y `< 0x31` through the top-ladder goal span completes the board.

### 100m — Rivets

- Eight rivets support sections of the flat structure.
- Crossing edge column X `0x4b` or `0xb3` arms the relevant rivet. Moving away resolves the slot
  from Mario's Y band and X side, clears it, erases its three-tile support span, awards 100, and
  decrements the remaining count.
- A removed span is non-solid. If Mario stands on the erased span when it is removed, he drops
  to the nearest girder surface below immediately (see section 5, "Jumping and falling" — 100m
  rivet-drop exception). The drop is not a fatal fall.
- Firefoxes spawn opposite Mario; exact centre counts as the left side. Two hammers are available.
- Clearing rivet eight triggers Kong's fall animation and board completion.

## 7. Difficulty and Random Behaviour

```text
difficulty = min(level + floor(board_ticks / 2048), 5)
```

Recompute at board start and each exact 2048-tick boundary. `board_ticks` resets whenever the
board is rebuilt, including death. At 60 Hz one step is about 34.13 seconds.

| Difficulty | Barrel control gate | Fire-enemy service ticks per 8 |
|---:|---:|---:|
| 1 | 25% | 4 |
| 2 | 50% | 5 |
| 3 | 50% | 6 |
| 4 | 75% | 6 |
| 5 | 75% | 7 |

The barrel percentage is not total difficulty. Higher values also raise fire-enemy duty cycles
and spring rates.

### Barrel ladder-decision precedence

1. The first barrel on a rebuilt 25m board never takes a ladder.
2. Before the oil drum is lit, all later eligible barrels take each complete ladder.
3. Once lit, a barrel at or below Mario's vertical position never takes a ladder.
4. Above Mario, draw `r = rng.next_u8() & 0x03`; set `gate = floor(difficulty / 2) + 1`. If
   `r >= gate`, skip the ladder.
5. Otherwise take it when approximately aligned over Mario, or when barrel side matches Mario's
   held horizontal direction. If neither applies, a fresh low-two-bit zero gives a 25% fallback.

Fireball and Firefox motion is seeded pseudo-random. A fire enemy may descend a ladder only when
Mario is below. Tests assert invariants and seeded traces, not “truly random” behaviour.

## 8. Collision Rules

Hazard collisions use centred AABBs with at least one pixel overlap, once per simulation tick
after movement. Terrain support uses the swept-foot probe and is the exception to no sub-stepping.

| Entity | Hitbox | Notes |
|---|---:|---|
| Mario | 9 × 13 | Centred on logical body centre |
| Barrel | 5 × 5 | One-pixel bottom-left sprite offset |
| Fireball | 7 × 5 | |
| Firefox | 9 × 3 | |
| Spring | 5 × 5 | All compression frames |
| Cement pie | 17 × 7 | |
| Hammer pickup | Board-defined | Pickup sprite bounds |

For jump scoring, map hazards cleared by that one jump to severity: zero -> 0, one -> 1,
two -> 3, three or more -> 7.

## 9. Scoring, Lives, and Reset

| Action | Score |
|---|---:|
| Clear one hazard with a jump | 100 |
| Clear two hazards with one jump | 300 |
| Clear three or more with one jump | 500 |
| Hammer destruction | 300 (25%), 500 (50%), or 800 (25%) |
| Prize on level 1 | 300 |
| Prize on level 2 | 500 |
| Prize on level 3+ | 800 |
| Remove a rivet | 100 |
| Complete a board | Remaining bonus × 100 |

Jump scoring is evaluated once per jump and is not a persistent combo. Hammer score uses a fresh
low-two-bit draw: `00 -> 300`, odd -> 500, `10 -> 800`.

- Initial lives: 3. Award one extra life when score first reaches/passes 7,000; latch it once.
- Initial bonus units: `min(10 * level + 40, 80)`.
- On 25m decrement one unit for each barrel-release event. On other boards decrement one unit
  every 120 ticks. Zero starts death.
- On completion, add remaining bonus units × 100 before changing boards.

After death, decrement lives. If any remain, rebuild the current board and reset Mario, hazards,
pickups, rivets, board tick, difficulty timing, and board-local schedules. Preserve the single
PRNG state, score, high score, lives, level, board-sequence position, and the extra-life latch. Do
not replay the opening cutscene on same-board respawn.

Death lasts exactly 296 ticks: an initial 64-tick phase; a 104-tick spin phase containing thirteen
eight-tick pose holds (12 changes through four orientation pairs repeated three times); and a
128-tick settle phase. Test duration and phase boundaries, not “four orientations 13 times.”

## 10. Rendering and Procedural Assets

Generate visual assets from colour-index arrays or drawing commands in Rust (`src/asset.rs`).
Include Mario, barrels, fire enemies, springs, pies, visible elevators, hammers, rivets,
Pauline, Kong, board tiles, and a HUD font.

- Simulation coordinates never depend on render scale.
- Draw to a logical render target with nearest filtering, then scale and letterbox.
- Render interpolation is allowed but must not feed back into gameplay.
- Sprites must be flipped horizontally according to the entity's facing direction. Mario's
  sprites (idle, walk, jump, climb frames) and the held hammer sprite mirror when
  `Mario::direction()` returns a negative value. All other entities (barrels, fire enemies,
  Kong, Pauline) always face their movement direction.
- Golden images verify layout/palette; state tests and focused playtests verify animation.

### Visual Quality Standards

Sprites are defined as character-grid pixel art resolved through a shared 19-colour palette.
The palette mimics the arcade's limited colour set and keeps every sprite visually consistent.

**Palette:** See `src/asset.rs` — mapping from characters to RGBA values, arcade-measured
colours (girder pink-red `#ff2155`, ladder cyan `#00ffff`, Mario's red hat `#ff0000`,
overalls blue `#0000ff`, skin `#ffb855`, brown shoes `#b80000`, etc.).

**Reference board layouts** (PPM golden images, 224×256):

| Board | File | Content |
|---|---|---|
| 25m | `tests/golden/board_25m.golden` | Six pink-red girder tiers, cyan ladders, oil drum at (39,224), Kong at (16,72), Pauline at (120,36), two hammers |
| 50m | `tests/golden/board_50m.golden` | Three conveyor belt regions with yellow/orange chevrons, moving ladder assemblies, Pauline, two hammers |
| 75m | `tests/golden/board_75m.golden` | Six girders with open elevator shafts, two elevator groups, springs on bottom tier, Kong, Pauline |
| 100m | `tests/golden/board_100m.golden` | Blue girders (recoloured from red for this board), flat rivet structure with eight white rivet tiles, Kong, Pauline |

**Sprite design notes (arcade reference-driven):**

| Character | Frames | Details |
|---|---|---|
| Mario | 6 (idle, walk×2, jump, climb×2) | Red cap with brim, skin face, red shirt, blue overalls with suspenders, brown shoes. Walk cycle: two movement ticks + one animation-only tick. Climb: left/right arm alternating. Jump: arms spread. Mirror horizontally when `Mario::facing() < 0` |
| Kong | 2 (idle_a/b) | Large brown head with tan muzzle, white eyes with round pupils, thick brown arms. One-pixel head shift between frames for subtle idle animation |
| Pauline | 1 | Black hair, skin face, pink dress with magenta accent, brown shoes. Static on top-girder balcony |
| Barrel | 2 (static, rolling) | Yellow-brown crate with dark outlines. Static has horizontal band pattern; rolling has offset bands for rotation illusion |
| Fire enemies | 2 (walking, rolling) | Orange flame body with bright yellow core and dark centre. Walking is upright with flame tips; rolling compresses as it tumbles |
| Spring | 2 (extended, compressed) | Light-gray coil with darker wire pattern. Extended: taller with visible coils; compressed: shorter/squashed |
| Cement pie | 1 | Circular gray disc with lighter rim and darker centre pattern |
| Elevator | 1 | Rectangular platform with gray body and yellow accent stripes at each rung |
| Hammer | 1 | Long gray handle with brown mallet head ending in a white-and-black striking face. Swings above Mario's head |
| Oil drum | 1 | Red barrel with dark-red bands and alternating red/dark stripe pattern |
| Tile: girder | 16×16 tile | Pink-red body (`r`, #ff2155) with darker red edge (`D`, #970000); subtle brighter-red highlight accents every few columns. 100m remaps to blue (`L`, #0000ff / `l`, #0000aa) |
| Tile: ladder | 16×8 tile | Cyan rails with full rung rows at fixed spacing; centre rungs appear every 4 px |
| Tile: rivet | 8×8 tile | Small white stud/circle on transparent background; seated on girder surface |
| Tile: pipe | 20×16 tile | Gray cylindrical pipe with darker rim at top and bottom; appears only on 25m board |

## 11. Audio

Generate runtime audio with square waves, noise, and envelopes. Music must be original and may
evoke the era without reproducing Nintendo melodies.

Minimum events: footsteps, jump, hammer swing/impact, barrel bounce, item pickup, board clear,
death, one theme per board, and a title theme. Simulation emits semantic events; audio may suppress
duplicates but cannot alter gameplay. On death, stop board music, play death audio, and restart the
board theme after respawn. Pausing suspends or mutes active music consistently.

## 12. Architecture

```text
src/
  lib.rs                 public headless simulation API
  sim/
    mod.rs               Simulation and transition enum
    entities/            Mario, barrels, fire enemies, springs, pies, elevators
    board/               layout data, local state, progression
    physics.rs           fixed-point movement and support probing
    collision.rs         hitboxes and overlap queries
    scoring.rs           score, lives, bonus, extra-life latch
    difficulty.rs        board clock and effective difficulty
    rng.rs               deterministic PRNG
  main.rs                macroquad startup
  runtime.rs             accumulator and event routing
  input.rs               keyboard state -> InputFrame
  renderer.rs            render target, sprites, HUD, letterboxing
  audio.rs               synthesis and event playback
  assets.rs              procedural sprite/font definitions
  screens/               platform-facing views
tests/
  acceptance.rs          cucumber runner
  acceptance/features/   Gherkin files
```

Everything under `src/sim/` is headless: no macroquad, I/O, wall time, or independent random
source. Runtime passes `InputFrame` values into `Simulation::tick`.

Use a 60 Hz accumulator. Poll keyboard once per rendered frame, preserve held inputs across
catch-up ticks, and consume edges only on the first tick. Cap catch-up at five ticks per render;
report and discard excess time. Rendering may be vsync-locked or uncapped.

## 13. Verification

- Unit tests: physics, transitions, collision boundaries, scoring, board order, reset semantics,
  and seeded enemy traces.
- Property tests: bounded difficulty, score monotonicity, collision symmetry, deterministic replay,
  and valid ranges. Do not require the truncated jump arc to be perfectly symmetric.
- Native Rust Cucumber features in `tests/acceptance/features/`, run by
  `cargo test --test acceptance`.
- Golden screenshots use deterministic 224×256 states.
- Manual playtest covers scaling, controls, sound, and each moving board.

## 14. Out of Scope

- Two-player mode, gamepad, persistent scores, networking, leaderboards, editor, and mods
- TATE display and cycle-accurate Z80/sound/video emulation
- Original ROMs, ripped assets, samples, and copied melodies
- Bomb-barrel/uninitialised-memory bug, level-22 kill screen, jump-through-floor, ladder clipping,
  and universal sprite wrapping

## 15. References

- Annotated disassembly: https://www.computerarcheology.com/Arcade/DonkeyKong/Code.html
- Hardware notes: https://computerarcheology.com/Arcade/DonkeyKong/Hardware.html
- MAME driver: https://github.com/mamedev/mame/blob/master/src/mame/nintendo/dkong.cpp
- Rust Cucumber: https://docs.rs/cucumber/
- Macroquad render targets: https://docs.rs/macroquad/latest/macroquad/texture/fn.render_target.html

This is an unofficial educational project, not affiliated with or endorsed by Nintendo.

## 16. Implementation Stories

### Phase 1 — Foundation

| ID | Outcome | Acceptance criteria |
|---|---|---|
| PROJ-1 | Cargo and canvas | `cargo run` opens 672×768 and shows a nearest-scaled 224×256 canvas; Escape exits title. |
| PROJ-2 | Pure simulation | `src/lib.rs` exposes a seeded simulation tickable without macroquad/window. |
| PROJ-3 | States and controls | Documented transitions consume edges exactly once. |
| PROJ-4 | Fixed timestep | Simulation runs at 60 Hz with at most five catch-up ticks per render. |
| PROJ-5 | Procedural shell | Procedural font/sprites and semantic audio routing need no external assets. |

### Phase 2 — 25m Core

| ID | Outcome | Acceptance criteria |
|---|---|---|
| 25M-1 | Board data | Six tiers, ladders, oil `(39,224)`, Kong, Pauline, pickups, goal, and golden image exist. |
| 25M-2 | Walk/climb | Walking and climbing use the exact documented cadences and support slopes. |
| 25M-3 | Jump/fall | Trace matches formula; apex near 20–21; swept landing works; 15-pixel fall latches fatal. |
| 25M-4 | Barrels | Seeded tests cover ladder-decision precedence, difficulty, input, fallback, bounce, retirement. |
| 25M-5 | Fire enemies | Oil activation and seeded movement work; descent requires Mario below. |
| 25M-6 | Collision/death | AABBs hit, terrain cannot tunnel, death lasts 296 ticks. |
| 25M-7 | Hammer | Queued pickup, 512-tick life, strikes, and movement restrictions pass. |
| 25M-8 | Score/lives | Scoring, bonus, timeout, 3 lives, one extra life, and reset preservation pass. |
| 25M-9 | Completion | Entering the goal pays bonus and follows board order. |

### Phase 3 — Remaining Boards

| ID | Outcome | Acceptance criteria |
|---|---|---|
| 50M-1 | Conveyors/pies | Three regions carry Mario/pies; reversal matches Kong endpoint. |
| 50M-2 | Moving ladders | Both complete seeded park/extend/dwell/retract cycles and never vanish after use. |
| 50M-3 | Completion | Y `< 0x51` goal advances correctly. |
| 75M-1 | Elevators | Visible groups move, recycle, collide, and carry Mario. |
| 75M-2 | Springs | Sixteen types follow lifecycle; seeded generation and safe spans pass. |
| 75M-3 | Prizes/goal | Items score by level; Y `< 0x31` top-ladder goal completes. |
| 100M-1 | Rivets/holes | Edge arming removes correct rivet/support and unsupported Mario falls. |
| 100M-2 | Firefoxes | Seeded movement and opposite-side spawn, including centre-as-left, pass. |
| 100M-3 | Final rivet | Eighth rivet triggers Kong fall, bonus, and progression. |

### Phase 4 — Progression and Polish

| ID | Outcome | Acceptance criteria |
|---|---|---|
| DIFF-1 | Difficulty | 2048-tick boundaries, cap 5, barrel gates, and hazard-rate effects pass. |
| DIFF-2 | Board order | Levels 1–4 and level-5–21 group match the table. |
| DIFF-3 | Safe ending | Completing level 21 enters Victory; normal play cannot construct level 22. |
| HUD-1 | HUD/high score | Score, high score, lives, and bonus work; high score lasts for the session. |
| AUDIO-1 | Sound/music | Minimum effects and five original themes play with no external audio. |
| PRESENT-1 | Visual output | Scaling/letterboxing works; deterministic golden images exist for all boards. |
| QA-1 | Full verification | Tests, format, lint, release build, audit, mutation, and playtest satisfy constitutions. |
