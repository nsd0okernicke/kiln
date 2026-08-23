# Interaction Loop

An `inbox` pane below this one already receives and merges every message addressed to you —
see `roles/human-in-the-loop.md` → "Receiving Messages". You never poll `kiln-db` for
inbound messages or merge anything yourself in this profile. Read what the inbox pane
prints; if it reports `MERGE FAILED`, that work is not in your tree yet — resolve the
conflict here before treating the message as received.

**On first startup**: call `python .kiln/tools/set-status.py {{ROLE}} waiting --mode={{MODE}}` first, then greet the user and ask what to work on.

**CRITICAL: "Work complete" or "approval received" is NOT end-of-turn — and neither is a
verified handoff. The turn is not over until Step 4 has also run, in this same response. Do
not stop, summarize, or wait for the user to say anything between Step 3 and Step 4.**

Repeat this sequence for every request the user brings you:

**Signal state change to terminal:** Before each step, call `python .kiln/tools/set-status.py {{ROLE}} <state> --mode={{MODE}}` so your tab title reflects where you are in the cycle. Emit these status signals at each transition (you may see the command fail silently if the status dir doesn't exist yet — that's harmless).

1. **Work** — call `python .kiln/tools/set-status.py {{ROLE}} working --mode={{MODE}}` first. Then apply your role rules. The Role section above defines your work process.

2. **Get approval** — call `python .kiln/tools/set-status.py {{ROLE}} approval --mode={{MODE}}` first. Then present your result to the user and ask for explicit approval.
   Do not continue to Step 3 without approval.

3. **Send handoff** — call `python .kiln/tools/set-status.py {{ROLE}} handoff --mode={{MODE}}` first. Then run `/kiln-handoff`. Handles: log sent, squash, INSERT into messages,
   verify, and retry (all via `kiln-db` — no `kiln-channel` dependency). Do not consider the
   turn over until a queued row is confirmed.

4. **Return to waiting** — call `python .kiln/tools/set-status.py {{ROLE}} waiting --mode={{MODE}}` immediately after Step 3 confirms, in this same turn. Without this the tab title
   stays stuck on `handoff` indefinitely — nothing else resets it, since there is no
   receive step in this loop to re-emit `waiting` the way there would be without an inbox
   pane. Then end your turn normally: the next thing you see — a completion report or a new
   request — arrives via the inbox pane or the user, not through polling.
