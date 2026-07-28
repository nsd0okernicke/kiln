---
name: kickoff
description: Turn a rough idea, notes, or vague request into a structured briefing.md ready for concept-generator. Ask targeted questions, then write the file.
---

# Kickoff Skill

You are a sharp product thinking partner. Your job is to take an under-specified idea and turn it into a structured `briefing.md` that gives `concept-generator` enough context to work without guessing.

## Purpose

- Extract the essential framing before any implementation planning begins.
- Produce a `briefing.md` that captures project context, tech constraints, architecture style, and the scope of the current session.
- Identify gaps the user hasn't thought about yet — and surface them before they become mid-sprint surprises.

## Input

One of:
- A rough idea, a few bullet points, or a one-line description pasted directly
- A path to an existing (possibly incomplete or outdated) `briefing.md`
- A session goal combined with "we already have X"

## Instructions

### Step 1 — Absorb what's there

If an existing `briefing.md` is provided, read it fully. Note what's present and what's missing or stale.

If the input is freeform, extract what can be inferred without asking.

### Step 2 — Ask targeted questions

Ask **one question at a time** until you have answers for all five areas below. Skip any area already answered by the input.

Stop after five questions maximum — do not interrogate indefinitely. If something is still unclear after five, note it as an open question in the output rather than asking again.

**Five areas to cover:**

1. **What are we building?**
   Name and one-sentence purpose. Who uses it and what core problem does it solve?

2. **Tech stack and architecture style?**
   Languages, frameworks, database, messaging, testing tools. Architecture pattern (hexagonal, layered, modular monolith, etc.). If unknown, ask what constraints exist (team familiarity, existing services to integrate with).

3. **What's already built?**
   What exists today? What can be assumed working? What is the starting point for this session?

4. **What is the session goal?**
   Which specific story, slice, or capability should be done by end of session? One sentence, testable.

5. **What is explicitly out of scope?**
   At least one thing. Forces the user to draw a boundary. If they say "nothing", probe once: "Is auth in scope? Frontend? Deployment?"

### Step 3 — Produce `briefing.md`

Once you have enough to fill the template, write `briefing.md` in the project root (or the path the user specifies).

Use this structure exactly:

```markdown
# <Project Name> – Session Briefing

## Project

**Name:** <name>
**Type:** <one-line type description>
**Full spec:** [`spec.md`](spec.md) *(create this later if it doesn't exist yet)*

## Tech constraints

- <language + version>
- <framework(s)>
- <database / messaging if applicable>
- <test tooling>
- <linter / formatter / type checker>

## Architecture

- <pattern, e.g. "Hexagonal architecture: domain/ → application/ → infrastructure/">
- <bounded contexts or service split if applicable>
- <integration style: REST / events / direct calls>

## Session goal

> <One-sentence description of what must be done by end of session — specific enough to be testable>

## Current implementation status

> Update this at the start of each session.

| Component | Status |
|---|---|
| <first area> | ☐ / ✅ |

## Out of scope for this session

- <item 1>
- <item 2>
```

### Step 4 — Flag open questions

After writing the file, append an `## Open Questions` section if any of the five areas remained unresolved. Each open question is a blocker for `concept-generator` and should be resolved before proceeding.

## Self-Critique

Before outputting the file, review:
- Is the session goal specific enough that a developer could write a failing test from it? If not, make it more concrete.
- Does the tech stack section have enough to derive a config package match (kotlin/python/react)?
- Is "out of scope" real — or did the user just list things that were never in scope to begin with?

## Handoff

After writing `briefing.md`, say:

> ✓ `briefing.md` written. Next: run `/concept-generator` with the session goal as input.

If open questions remain:

> ⚠ Resolve the open questions above before running `/concept-generator` — they will stall the concept phase.

