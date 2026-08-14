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

Squash all your commits since the last merge commit into one concise, agent-prefixed commit.
Run each command below **separately, with the literal hash pasted in** — do not combine them
with `$(...)` shell substitution. A substitution makes the whole line unrecognizable to the
permission allowlist and forces a manual approval even though every command here is
individually pre-approved.

1. `git log --merges -1 --format="%H"` — if this prints a hash, that's your `<merge-hash>`.
   If it prints nothing (no merges yet), run `git rev-list --max-parents=0 HEAD` instead and
   use that hash as `<merge-hash>`.
2. `git reset --soft <merge-hash>` — substitute the literal hash from step 1.
3. `git commit -m "[<your role>] <short outcome-focused summary>"`

Note the resulting commit hash — you need it in Step 3.

### Step 3 — Format the handoff message

Use the **Handoff Message Format** template from your Workflow Rules section (already in
context). Fill in: sender role, specifier handoff name from the inbound message, your branch,
and the squash commit hash from Step 2.

### Step 4 — INSERT

Call `kiln-db` MCP `query`:

```sql
INSERT INTO messages (sender, target, priority, status, content, created_at, branch, work_item)
VALUES (
  '<your role>',
  '<handoff target>',
  50,
  'queued',
  '<formatted message from Step 3>',
  datetime('now', 'localtime'),
  '<your branch>',
  '<the Handoff name from Step 3>'
)
```

`work_item` must be **the same handoff name you put in the message**, character for character.
It is what groups every message belonging to one piece of work, so cost, cycle counts and loop
detection can be answered per feature. Copy it from the inbound message; never invent a new one
and never leave it out.

**One exception, and it matters: if the handoff name is `pending`, write SQL `NULL` instead** —
unquoted, not the string `'pending'`:

```sql
  ...
  '<your branch>',
  NULL
)
```

`pending` is the placeholder a human puts in an opening request; the specifier replaces it with
a real name. It is not a work item, it is the *absence* of one, and storing it as a value makes
every unrelated request in the project share a single group called `pending`. That is not
cosmetic — the max-cycles guard and the cost cap both count per work item, so one shared bucket
makes them count across features that have nothing to do with each other.

So the human's opening request is the only message with no `work_item`, because the specifier
is what invents the name and there is nothing to carry yet.

### Step 5 — Verify (and retry if needed)

Call `kiln-db` MCP `query`:

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
