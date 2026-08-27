"""Initialize, execute, and report one configured Cosmic Ray tier cross-platform."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sysconfig
from pathlib import Path

from .mutation_summary import main as summarize

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
        Path.home() / "AppData" / "Roaming" / "Python" / "Python314" / "Scripts" / f"{name}{suffix}",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tier", choices=TIERS)
    parser.add_argument("--init-only", action="store_true")
    parser.add_argument(
        "--reuse",
        action="store_true",
        help="reuse an existing session to resume it; fresh enumeration is the default",
    )
    args = parser.parse_args(argv)
    sessions = ROOT / ".kiln-mutation"
    reports = ROOT / "reports" / "mutation"
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
            cwd=ROOT,
            check=False,
        ).returncode
        if code:
            return code
    if args.init_only:
        return 0
    code = subprocess.run(
        [executable("cosmic-ray"), "exec", str(config), str(session)],
        cwd=ROOT,
        env=env,
        check=False,
    ).returncode
    with (reports / f"{args.tier}.txt").open("w", encoding="utf-8") as output:
        subprocess.run(
            [executable("cr-report"), str(session)],
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    summarize([str(session), "--output", str(reports / f"{args.tier}-summary.json")])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
