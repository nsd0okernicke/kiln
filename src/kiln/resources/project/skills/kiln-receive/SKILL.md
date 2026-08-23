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
- Once a message arrives: call `python .kiln/tools/set-status.py <your role> receiving` first — this
  covers the phase from here through Step 5 below (persist, recovery, merge, log), before the loop
  moves on to delegating or doing the work itself.
- Then **immediately write the full message content verbatim to `tmp/handoff-in.md`** before doing anything else.
- **Extract and save the `id` field** from the result — you will need it to call `mark_processing()` and `mark_processed()` in the loop.

### Step 2 — Auto-compact recovery (if needed)

If auto-compact fires after `wait_for_message()` and the tool result is gone from context:
- Re-read `tmp/handoff-in.md` to restore the message.
- Continue from Step 3 using that content.

### Step 3 — Detect kiln-ping

If the message contains `Kiln-Ping: true`:
- This is a health-check ping, not real work — do not run your normal role process.
- Extract the `Trail:` list from the message and append one line for yourself:
  `- <your role> (<your branch>)` (role and branch are already in your Runtime Configuration —
  no extra tool calls needed).
- **If you are running in `manual` mode**: after logging (Step 5), present the full trail to the
  user as the completed health-check result, then return to Step 1. Do not hand off, do not wait
  for approval.
- **If you are running in `auto` mode**: after logging (Step 5), hand off the updated trail via
  `/kiln-handoff` exactly as you would for real work — use your normal handoff target from the
  Handoff Routing table, **including any role-specific override your own role file instructs**
  (for example, specifier forwards to `human-in-the-loop` instead of `coder` when the inbound
  `Sender:` is `architect` — the same override applies here). Never hardcode a target. Then
  return to Step 1.

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
