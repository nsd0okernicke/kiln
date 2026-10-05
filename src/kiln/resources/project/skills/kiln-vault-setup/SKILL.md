---
name: kiln-vault-setup
description: Point a Kiln project's knowledge at an Obsidian vault. Use after project initialization when no vault is configured yet, or when the vault path or excluded folders need a deliberate change.
---

# Kiln Vault Setup

The knowledge the swarm searches comes from the project's Obsidian vault. The pointer is
`kiln/project/knowledge.json` — a committed v2 config, not a catalog of documents:

```json
{
  "version": 2,
  "vault": "C:/path/to/vault",
  "exclude": ["01 Inbox", "05 Daily Notes"]
}
```

1. Ask the user which vault to ground the project on, and which folders hold churn rather
   than knowledge (inbox, daily notes, attachments). Never point a project at a vault the
   user has not named.
2. Write the config with `version: 2`, the vault path, and the `exclude` prefixes. Excludes
   match vault-relative folder prefixes case-insensitively.
3. Run `kiln knowledge sync` and report the updated, skipped, removed and failed counts.
   Every launch re-syncs incrementally, so the index stays fresh without this skill.
4. Run `kiln knowledge status` to confirm the vault and the last sync. Run
   `kiln knowledge search "<query>"` for one spot check that returns cited notes.

The vault stays the user's. Never write into it, never move its notes, never resolve its
gaps by inventing content — knowledge gaps flow back via handoff reports. An unreadable or
v1 catalog file is replaced with the v2 shape only with the user's approval.
