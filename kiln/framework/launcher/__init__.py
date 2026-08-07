"""
Kiln launcher — the Python core that replaces bin/kiln.ps1 and bin/kiln.sh.

Import with `kiln/framework` on sys.path:

    from launcher import config, paths

Structure mirrors what the shell scripts did, minus the duplication between them:

    paths.py      — every derived path, computed once
    config.py     — profiles.json -> RoleConfig/Profile
    commands.py   — the command string injected into each pane
    terminals/    — one module per backend (WezTerm, Windows Terminal, tmux)

The shells keep only what genuinely needs a shell: nothing. `bin/kiln.ps1` and `bin/kiln.sh`
survive as thin shims so existing docs and muscle memory keep working.
"""
