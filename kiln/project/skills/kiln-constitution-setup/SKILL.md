---
name: kiln-constitution-setup
description: Analyze an existing or planned project and create project-specific Kiln engineering and project constitution files. Use after kiln init when the generated constitution is still generic or needs a deliberate refresh.
---

# Kiln Constitution Setup

Turn repository evidence and user decisions into these project-owned files:

- `kiln/project/constitution/engineering.md`
- `kiln/project/constitution/project.md`

Do not change `workflow.md`, roles, profiles, routing, or other skills.

## Choose the input mode

Run the evidence inventory from the project root:

```bash
python kiln/project/skills/kiln-constitution-setup/scripts/project_evidence.py .
```

- If the inventory identifies substantial implementation evidence, read
  [references/repository-mode.md](references/repository-mode.md).
- If it identifies little or none, read
  [references/interview-mode.md](references/interview-mode.md).
- For an incomplete repository, use both: infer what the files prove and interview only for
  missing decisions.

Always read [references/output-contract.md](references/output-contract.md) before drafting.

## Required workflow

1. Inspect the evidence files themselves; the inventory only routes attention.
2. Separate facts supported by repository evidence from decisions supplied by the user.
3. Surface contradictions and material unknowns. Do not silently choose an answer.
4. Draft both complete files with no placeholders or speculative claims presented as facts.
5. Show the proposed content and its evidence/decision basis before replacing either existing
   file. Wait for explicit approval unless the user already explicitly approved the exact
   replacements in this session.
6. Write both files, then re-read them and verify that paths, commands, and named tools agree
   with the evidence and confirmed decisions.

Existing files are user-owned. Never silently overwrite them, even when they still resemble
the scaffold defaults.
