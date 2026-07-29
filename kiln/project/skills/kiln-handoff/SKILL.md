---
name: kiln-handoff
description: Full send sequence — log sent → squash → INSERT handoff message → verify → retry. Run after completing work, before returning to /kiln-receive.
---

# Kiln Handoff

**The loop cycle is NOT complete until this skill finishes successfully.
"Work done" or "tests pass" is not the end. The handoff must be sent and verified.**

## Values to use

Look these up from your Runtime Configuration section (already in context):
- **Your role name** — shown as `Role:`
- **Your branch** — shown as `Branch:` (the root branch, not the worktree sub-branch)
- **Your handoff target** — from the Handoff Routing table in Workflow Rules
- **Your commit prefix** — use your role name in brackets (for example `[Coder]`, `[Specifier]`, `[Architect]`, or `[Human-in-the-loop]`) and add a short outcome-focused summary

## Steps

### Step 1 — Log sent

Append to `logbook.md`:

```
[SENT] YYYY-MM-DD HH:MM:SS
To: <handoff target role>
Branch: <your branch>
Summary: <one sentence — what was accomplished>
```

Do not commit yet — this gets folded into the squash.

### Step 2 — Squash

Squash all your commits since the last merge commit into one concise, agent-prefixed commit:

```sh
LAST_MERGE=$(git log --merges -1 --format="%H")
git reset --soft "${LAST_MERGE:-$(git rev-list --max-parents=0 HEAD)}"
git commit -m "[<your role>] <short outcome-focused summary>"
```

Note the resulting commit hash — you need it in Step 3.

### Step 3 — Format the handoff message

Use the **Handoff Message Format** template from your Workflow Rules section (already in
context). Fill in: sender role, specifier handoff name from the inbound message, your branch,
and the squash commit hash from Step 2.

### Step 4 — INSERT

Call `kiln-db` MCP `write_query`:

```sql
INSERT INTO messages (sender, target, priority, status, content, created_at, branch)
VALUES (
  '<your role>',
  '<handoff target>',
  50,
  'queued',
  '<formatted message from Step 3>',
  datetime('now', 'localtime'),
  '<your branch>'
)
```

### Step 5 — Verify (and retry if needed)

Call `kiln-db` MCP `read_query`:

```sql
SELECT id FROM messages
WHERE sender='<your role>'
  AND branch='<your branch>'
  AND status='queued'
ORDER BY created_at DESC
LIMIT 1
```

- **Row returned** → skill complete. Return to `/kiln-receive`.
- **No row returned** → INSERT failed silently. Repeat Step 4 then re-run Step 5. Do not return to `/kiln-receive` until a row is confirmed.
