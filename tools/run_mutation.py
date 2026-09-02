"""Initialize, execute, and report one configured Cosmic Ray tier cross-platform.

**Scratch-clone mode (--scratch)**: Clone the repo into a temporary directory and run
mutation there instead of in the working tree. This prevents killed or timed-out
mutation runs from leaving the source silently mutated (issue #47, finding 4).

**Clean-tree assertion**: Before any mutation run, assert that `git status --porcelain`
is empty in the working copy. A dirty tree means the results cannot be reproduced.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sysconfig
import tempfile
from pathlib import Path

from .mutation_summary import main as summarize

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
TIERS = {
    "pure": ROOT / "tests" / "mutation" / "pure-modules.toml",
    "db": ROOT / "tests" / "mutation" / "db-module.toml",
}


def executable(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    suffix = ".exe" if sysconfig.get_platform().startswith("win") else ""
    # Pip on Windows may install to the user scripts directory rather than
    # sysconfig's scripts path.  Check both.
    candidates = [
        Path(sysconfig.get_path("scripts")) / f"{name}{suffix}",
        Path.home()
        / "AppData"
        / "Roaming"
        / "Python"
        / "Python314"
        / "Scripts"
        / f"{name}{suffix}",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise FileNotFoundError(f"{name} is not installed; install requirements-dev.txt")


def _user_scripts() -> str | None:
    """
    The pip user-site scripts directory on Windows, or None.

    pip may install scripts to the user-site Scripts directory rather than
    sysconfig's scripts path, leaving subprocess.run unable to find them.
    """
    if sysconfig.get_platform().startswith("win"):
        scripts = Path.home() / "AppData" / "Roaming" / "Python" / "Python314" / "Scripts"
        if scripts.is_dir():
            return str(scripts)
    return None


def _env_with_path() -> dict[str, str]:
    """Environment with the pip user-scripts directory on PATH (Windows)."""
    import os as os_mod
    env = dict(os_mod.environ)
    scripts = _user_scripts()
    if scripts:
        env["PATH"] = scripts + os_mod.pathsep + env.get("PATH", "")
    return env


def _assert_tree_clean(cwd: Path) -> None:
    """Raise SystemExit if the working tree is dirty."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=15,
    )
    if result.stdout.strip():
        log.error(
            "Mutation refused: working tree is dirty. Stash or commit before running mutation.\n%s",
            result.stdout.strip()[:2000],
        )
        raise SystemExit(1)


def _prepare_scratch_clone(args) -> tuple[Path, Path, Path]:
    """
    Clone ROOT into a temp directory for the mutation run.

    Returns (scratch_root, sessions_dir, reports_dir).
    """
    scratch = Path(tempfile.mkdtemp(prefix="kiln-mutation-"))
    log.info("cloning into scratch clone: %s", scratch)
    subprocess.run(
        ["git", "clone", str(ROOT), str(scratch)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    scratch_root = scratch / ROOT.name if (scratch / ROOT.name).exists() else scratch
    scratch_sessions = scratch_root / ".kiln-mutation"
    scratch_reports = scratch_root / "reports" / "mutation"
    scratch_sessions.mkdir(parents=True, exist_ok=True)
    scratch_reports.mkdir(parents=True, exist_ok=True)
    return scratch_root, scratch_sessions, scratch_reports


def _install_deps_in_scratch(scratch_root: Path) -> None:
    """Install cosmic-ray and dev dependencies in the scratch clone."""
    log.info("installing dependencies in scratch clone")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".", "cosmic-ray", "cr-report"],
        cwd=str(scratch_root),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tier", choices=TIERS)
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="reuse an existing session to resume it; fresh enumeration is the default",
    )
    parser.add_argument(
        "--scratch",
        action="store_true",
        help="run mutation in a scratch clone instead of the working tree",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="skip the clean-tree assertion (not recommended)",
    )
    args = parser.parse_args(argv)

    # Determine working directory
    if args.scratch:
        # Clean-tree assertion on the original repo before cloning
        if not args.allow_dirty:
            _assert_tree_clean(ROOT)
        cwd, sessions, reports = _prepare_scratch_clone(args)
        _install_deps_in_scratch(cwd)
    else:
        if not args.allow_dirty:
            _assert_tree_clean(ROOT)
        sessions = ROOT / ".kiln-mutation"
        reports = ROOT / "reports" / "mutation"
        cwd = ROOT

    sessions.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    session = sessions / f"{args.tier}.sqlite"
    config = TIERS[args.tier]
    env = _env_with_path()

    if session.is_file() and not args.reuse:
        session.unlink()
    if not session.is_file():
        code = subprocess.run(
            [executable("cosmic-ray"), "init", str(config), str(session)],
            cwd=cwd,
            check=False,
        ).returncode
        if code:
            return code
    if args.init_only:
        # Clean-tree assertion after mutation (even on init-only)
        if not args.allow_dirty and not args.scratch:
            _assert_tree_clean(ROOT)
        return 0

    code = subprocess.run(
        [executable("cosmic-ray"), "exec", str(config), str(session)],
        cwd=cwd,
        env=env,
        check=False,
    ).returncode

    with (reports / f"{args.tier}.txt").open("w", encoding="utf-8") as output:
        subprocess.run(
            [executable("cr-report"), str(session)],
            cwd=cwd,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    summarize([str(session), "--output", str(reports / f"{args.tier}-summary.json")])

    # Clean-tree assertion after mutation run
    if not args.allow_dirty and not args.scratch:
        _assert_tree_clean(ROOT)

    if args.scratch:
        log.info("cleaning up scratch clone: %s", cwd)
        shutil.rmtree(cwd, ignore_errors=True)

    return code


if __name__ == "__main__":
    raise SystemExit(main())
