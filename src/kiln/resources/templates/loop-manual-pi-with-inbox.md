# Interaction Loop

The companion inbox pane receives and merges messages addressed to this role. Read its output;
if it reports a merge conflict, resolve that conflict before treating the work as received.

On startup, run `python .kiln/tools/set-status.py {{ROLE}} waiting --mode={{MODE}}`, greet the
user, and ask what to work on. For each request:

1. Set status to `working` and apply the role rules.
2. Present the result and obtain explicit user approval.
3. For human-owned backlog work, use `kiln task create`, `update`, `handoff`, or `archive`.
   For an intentional direct intervention, use `kiln send` with the configured target.
4. Set status back to `waiting` before ending the turn.

Never put provider URLs, API keys, tokens, or certificate material in project files or command
output.
