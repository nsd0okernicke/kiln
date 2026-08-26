# Message Loop

Pi auto roles are expected to use Kiln's deterministic Python scheduler. If this wrapper-mode
fallback is selected explicitly, use the Cockpit to identify queued work, apply the role rules
in this worktree, and use the public `kiln send` command for the configured handoff target.
Return to waiting after every handoff. Never copy Pi provider credentials into project files.
