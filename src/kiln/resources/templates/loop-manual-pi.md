# Interaction Loop

On startup, run `python .kiln/tools/set-status.py {{ROLE}} waiting --mode={{MODE}}`, greet the
user, and ask what to work on. This profile has no companion inbox; use the Cockpit to inspect
incoming work rather than starting a second blocking command inside this Pi session.

For each request, set status to `working`, apply the role rules, obtain explicit approval, and
use Kiln's public `kiln task` or `kiln send` commands for queue actions. Set status back to
`waiting` before ending the turn. Never write provider credentials into the project.
