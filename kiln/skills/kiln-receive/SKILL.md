---
name: kiln-receive
description: Full receive sequence — wait_for_message → persist → merge → log received. Run at the start of every loop cycle.
---

# Kiln Receive

**This skill is not complete until every step below has executed successfully.
Do not begin your work until all steps are done.**

## Steps

### Step 1 — Wait for message

Call `wait_for_message()` from the `kiln-channel` MCP server.

- If the result is `{"received": false}`, call it again immediately. Keep calling until `received` is `true`.
- Once a message arrives: **immediately write the full message content verbatim to `tmp/handoff-in.md`** before doing anything else.
- **Extract and save the `id` field** from the result — you will need it to call `mark_processing()` and `mark_processed()` in the loop.

### Step 2 — Auto-compact recovery (if needed)

If auto-compact fires after `wait_for_message()` and the tool result is gone from context:
- Re-read `tmp/handoff-in.md` to restore the message.
- Continue from Step 3 using that content.

### Step 3 — Detect system-communication-test

If the message contains `system-communication-test`:
- This is a diagnostic forwarding message — do not run your normal work.
- After logging (Step 5), forward the message as-is using `/kiln-handoff` and return to Step 1.

### Step 4 — Merge the sender's commit

Extract from the message:
- `Branch:` — sender's branch name
- `Commit:` — sender's commit hash

Run:
```sh
git merge <commit-hash>
```

This merge commit is the squash anchor for `/kiln-handoff`. If the merge fails, stop and report the error before proceeding.

### Step 5 — Log received

Append to `logbook.md`:

```
[RECEIVED] YYYY-MM-DD HH:MM:SS
From: <sender role>
Handoff: <handoff name>
Branch: <branch from message>
Commit: <commit hash>
Plan: <one sentence — what you will do this cycle>
```

Commit the logbook entry:
```sh
git add logbook.md
git commit -m "log: received handoff from <sender>"
```

---

**Skill complete.** Proceed to your role's work rules.
