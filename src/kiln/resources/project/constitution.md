<!-- Copied into <project>/kiln/project/constitution.md during project init (kiln.ps1 -Init / kiln.sh init). States load order only — rarely needs editing. -->

# Kiln Constitution

This file takes precedence over subordinate files.
Read and obey the following subordinate documents in order.

1. `kiln/project/constitution/project.md`
2. `kiln/project/constitution/engineering.md`
3. `kiln/project/constitution/workflow.md`
4. `kiln/project/constitution/skill-orchestration.md`

If two subordinate files conflict, the earlier file wins.

`skill-orchestration.md` is the authoritative statement of which quality gate belongs to which
role and in what order the gates run. It is listed last because it elaborates the cycle
`workflow.md` defines rather than competing with it — but it must be listed, or the document
defining gate ownership never reaches the agents expected to honour it.

