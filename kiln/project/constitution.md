<!-- Copied into <project>/kiln/project/constitution.md during project init (kiln.ps1 -Init / kiln.sh init). States load order only — rarely needs editing. -->

# Kiln Constitution

This file takes precedence over subordinate files.
Read and obey the following subordinate documents in order.

1. `kiln/project/constitution/project.md`
2. `kiln/project/constitution/engineering.md`
3. `kiln/project/constitution/workflow.md`

If two subordinate files conflict, the earlier file wins.

Only these three are constitution, and only these three reach a running agent: a one-shot worker
is given its role file plus `project.md` and `engineering.md`; `workflow.md` is added for wrapper
roles, since handoff and messaging protocol belong to whoever dispatches a worker rather than to
the worker itself.

`kiln/project/skill-orchestration.md` sits beside this file and is deliberately *not* in the list.
It is a human-facing reference — the end-to-end gate chain and why the gates run in the order they
do. The binding statements of gate ownership live in `roles/*.md`, which is what workers actually
load.

