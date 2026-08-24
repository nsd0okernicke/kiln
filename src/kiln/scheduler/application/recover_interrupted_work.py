"""Recover queue work abandoned by an interrupted scheduler process."""

import logging

from .ports import QueueAccessError
from .process_next_message import SchedulerContext

log = logging.getLogger(__name__)

ICON_RETRY = "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}"
ICON_BLOCKED = "\N{NO ENTRY}"


def recover_interrupted_work(ctx: SchedulerContext) -> int:
    """Re-serve messages this role left processing when it was last interrupted."""
    try:
        recovered = ctx.queue.recover_processing(ctx.role, ctx.branch)
    except QueueAccessError as exc:
        log.warning("could not check for messages left mid-cycle: %s", exc)
        return 0

    for row in recovered:
        log.warning(
            f"{ICON_RETRY} recovered message %s from %s (work item %s), left mid-cycle by a "
            "killed scheduler; re-serving it",
            str(row.get("id", ""))[:8],
            row.get("sender") or "?",
            row.get("work_item") or "-",
        )
    if recovered:
        log.warning(
            f"{ICON_BLOCKED} %d recovered message(s) will be replayed against the existing "
            "worktree: partial work from the killed cycle is still there, so this role may "
            "redo work it already did",
            len(recovered),
        )
    return len(recovered)
