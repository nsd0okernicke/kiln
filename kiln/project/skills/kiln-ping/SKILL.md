---
name: kiln-ping
description: Health-check the swarm — send a ping through the full handoff chain and show the per-role status trail. Human-in-the-loop only, run on request.
---

# Kiln Ping

Sends a lightweight status-check message through the same handoff chain a real request takes.
Every role along the way appends a one-line status instead of doing real work; the trail comes
back to you via the normal completion-forwarding path — no separate profile or role needed.

## When to run this

Only when the user explicitly asks for a health check, connectivity check, or "is everyone
alive" — this is not part of the normal receive/handoff loop.

## Steps

### Step 1 — Log sent

Append to `logbook.md`:

```
[SENT] YYYY-MM-DD HH:MM:SS
To: <handoff target role>
Branch: <your branch>
Summary: kiln-ping health check initiated
```

Do not commit yet — this gets folded into the squash.

### Step 2 — Squash

Same mechanics as `/kiln-handoff` Step 2:

```sh
LAST_MERGE=$(git log --merges -1 --format="%H")
git reset --soft "${LAST_MERGE:-$(git rev-list --max-parents=0 HEAD)}"
git commit -m "[<your role>] kiln-ping health check"
```

### Step 3 — Format the ping message

```text
Sender: <your role>
Handoff: kiln-ping-<YYYYMMDDHHMMSS>
Branch: <your branch>
Commit: <squash commit hash from Step 2>

Kiln-Ping: true
Trail:
- <your role> (<your branch>)
```

### Step 4 — INSERT

Call `kiln-db` MCP `query`, targeting your normal handoff target (Handoff Routing table in
Workflow Rules) — same INSERT shape as `/kiln-handoff` Step 4.

### Step 5 — Verify (and retry if needed)

Same as `/kiln-handoff` Step 5 — confirm a queued row exists for this handoff name before
continuing; repeat Step 4 and re-check if not.

### Step 6 — Wait for the trail to come back

Return to your normal loop (`/kiln-receive`). The trail arrives like any other inbound message
once every role has appended its line — `/kiln-receive` recognizes the `Kiln-Ping` marker and,
since you're in `manual` mode, presents the completed trail directly instead of forwarding it
further. Tell the user you're waiting; do not go silent.

---

**Skill complete.** Proceed to `/kiln-receive` as usual.
