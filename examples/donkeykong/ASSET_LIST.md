# Donkey Kong — External Asset Reference

Drop any of these PNG files into `donkeykong-sample/` and the game will load
them automatically. When a file is missing the procedural fallback is used
instead, so the game always works.

## Board Backgrounds

Each must be exactly **224×256 pixels** (24-bit or 32-bit PNG).

| File         | Description                                     |
|--------------|-------------------------------------------------|
| `25m.png`    | 25m — 6 girder tiers, oil drum, Kong, Pauline   |
| `50m.png`    | 50m — 3 conveyor belts, moving ladders, Pauline |
| `75m.png`    | 75m — elevator shafts, springs, Kong, Pauline   |
| `100m.png`   | 100m — blue girders, flat rivet structure       |

## Sprites

All sprites should have a **transparent background** (alpha channel).
The natural pixel sizes below are the expected dimensions for the
procedural sprites — your PNGs can be any size; the game textures
them as-is.

### Mario (6 frames, ~13×16 px)

| File                    | Description                     |
|-------------------------|---------------------------------|
| `spr_mario_idle.png`    | Standing still, facing right    |
| `spr_mario_walk_a.png`  | Walk frame A (legs apart)       |
| `spr_mario_walk_b.png`  | Walk frame B (legs together)    |
| `spr_mario_jump.png`    | Jumping, arms spread            |
| `spr_mario_climb_a.png` | Climb frame A (left arm up)     |
| `spr_mario_climb_b.png` | Climb frame B (right arm up)    |

### Kong (2 frames, ~16×16 px)

| File                  | Description                    |
|-----------------------|--------------------------------|
| `spr_kong_idle_a.png` | Idle A (head position 1)       |
| `spr_kong_idle_b.png` | Idle B (head shifted 1 pixel)  |

### Characters

| File               | Description                    |
|--------------------|--------------------------------|
| `spr_pauline.png`  | Pauline on balcony (~13×16 px) |

### Enemies & Hazards

| File                      | Description                               |
|---------------------------|-------------------------------------------|
| `spr_barrel.png`          | Barrel static (~12×12 px)                 |
| `spr_barrel_rolling.png`  | Barrel rolling, band offset for rotation  |
| `spr_fire_walking.png`    | Fire enemy walking upright (~16×16 px)    |
| `spr_fire_rolling.png`    | Fire enemy rolling/tumbling               |
| `spr_spring_up.png`       | Spring extended (~14×8 px)                |
| `spr_spring_down.png`     | Spring compressed (~14×6 px)              |
| `spr_pie.png`             | Cement pie (~14×12 px)                    |

### Items

| File               | Description                    |
|--------------------|--------------------------------|
| `spr_hammer.png`   | Hammer pickup (~16×16 px)      |
| `spr_oil_drum.png` | Oil drum (~12×11 px)           |

### Moving Elements

| File                | Description                          |
|---------------------|--------------------------------------|
| `spr_elevator.png`  | 75m elevator platform (~16×16 px)    |

## Board Tiles (repeating)

These tiles are drawn repeatedly to form the static board layout.
Each must be pixel-perfect for seamless tiling.

| File                | Description                              |
|---------------------|------------------------------------------|
| `tile_girder.png`   | 32×16 pink-red girder with dark edge     |
| `tile_ladder.png`   | 14×8 cyan ladder rail with rungs         |
| `tile_rivet.png`    | 8×8 white rivet stud (100m board)        |
| `tile_pipe.png`     | 20×16 gray pipe (25m board only)         |

## HUD Text

The HUD (score, lives, labels) always uses the procedural 5×7 font.
It cannot be replaced with PNGs.
