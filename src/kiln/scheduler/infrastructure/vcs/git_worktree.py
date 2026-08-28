"""Git implementation of the application worktree port."""

import logging
from pathlib import Path

from . import git
from .git import GitResult

log = logging.getLogger(__name__)


class GitWorktree:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def already_contains(self, target: str) -> bool:
        return git.already_contains(target, self.path)

    def merge(self, target: str, message: str):
        return git.merge_commit(target, self.path, message=message)

    def squash_anchor(self) -> str:
        return git.squash_anchor(self.path)

    def has_commits_since(self, anchor: str) -> bool:
        return git.has_commits_since(anchor, self.path)

    def has_pending_changes(self) -> bool:
        return git.has_pending_changes(self.path)

    def squash_since(self, anchor: str, message: str):
        return git.squash_since(anchor, message, self.path)

    def head_commit(self) -> str:
        return git.head_commit(self.path)

    def push_branch(self, branch: str) -> None:
        git.push_branch(branch, self.path)

    def reset_hard(self, target: str) -> GitResult:
        return git.reset_hard(target, self.path)

    def ensure_generated_ignored(self) -> None:
        git.ensure_generated_ignored(self.path)

    def persist_inbound(self, content: str) -> Path | None:
        try:
            self.ensure_generated_ignored()
            target = self.path / "tmp" / "handoff-in.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return target
        except OSError as exc:
            log.warning("could not write tmp/handoff-in.md: %s", exc)
            return None
