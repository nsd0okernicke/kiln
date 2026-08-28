"""
A single, authoritative version resolver for the Kiln framework.

Releases are identified by annotated ``vMAJOR.MINOR.PATCH`` Git tags.  This module
reads the tag from the framework checkout and exposes it through one public function so
every presentation surface — CLI, Cockpit, WezTerm — shows exactly the same value.

Resolution strategy (first match wins):

1. ``git describe --tags --dirty`` from the framework checkout root.
2. ``PKG-INFO`` metadata embedded at install time (fallback when ``.git`` is absent).
3. The literal string ``"unknown"`` (last resort).
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

#: Cache the resolved version so callers (including the WezTerm backend) never
#: re-invoke ``git describe`` on every call.  Cleared only by process exit.
_VERSION_CACHE: str | None = None

#: Pattern that ``git describe`` output must match to be accepted.
#: Groups: (tag, commits_after, commit_hash, dirty_suffix).
#: Accepts vMAJOR.MINOR (v0.1), vMAJOR.MINOR.PATCH (v0.4.0), and anything after
#: PATCH such as pre-release suffixes (v0.4.0-rc1).
_DESCRIBE_RE = re.compile(
    r"^v(?P<tag>\d+\.\d+(?:\.\d+)?(?:-[a-zA-Z0-9.]+)?)"  # annotated tag
    r"(?:-(?P<commits>\d+)-g(?P<hash>[0-9a-f]+))?"  # dev: -12-gabc1234
    r"(?P<dirty>-dirty)?$"  # dirty marker
)

#: Fallback version when no Git metadata is available at all.
FALLBACK_VERSION = "unknown"


def resolve_version(framework_root: Path | None = None) -> str:
    """
    Resolve the framework version from the checkout.

    Parameters
    ----------
    framework_root:
        Path to the Kiln framework checkout (the directory containing ``.git``).
        When *None*, the root is resolved from this module's location
        (five parent directories up, matching ``resolve_framework_root`` in ``templates.py``).

    Returns
    -------
    str
        A human-readable version string such as ``"v0.4.0"``, ``"v0.4.0-12-gabc1234"``,
        ``"v0.4.0-12-gabc1234-dirty"``, or ``"unknown"``.
    """
    global _VERSION_CACHE
    root = framework_root or _default_framework_root()

    # Cache only the default-root resolution (no explicit framework_root).
    # Tests pass explicit roots and must not receive stale cached values.
    if framework_root is None and _VERSION_CACHE is not None:
        return _VERSION_CACHE

    cache = framework_root is None
    version = _resolve_unchecked(root)
    if cache:
        _VERSION_CACHE = version
    return version


def clear_cache() -> None:
    """Clear the cached version.  Used by tests to avoid cross-test pollution."""
    global _VERSION_CACHE
    _VERSION_CACHE = None


def version_tuple(version: str) -> tuple[int, ...]:
    """
    Parse a ``vMAJOR.MINOR`` or ``vMAJOR.MINOR.PATCH`` version into a sortable tuple.

    Returns ``(0,)`` for unrecognised strings such as ``"unknown"`` so callers can
    compare without guarding against the fallback.
    """
    match = re.match(r"^v?(\d+)\.(\d+)(?:\.(\d+))?", version)
    if match:
        parts = [int(g) for g in match.groups() if g is not None]
        return tuple(parts)
    return (0,)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _default_framework_root() -> Path:
    """Resolve the framework root from this module's location."""
    return Path(__file__).resolve().parent.parent.parent.parent.parent


def _resolve_unchecked(root: Path) -> str:
    """
    Try each resolution strategy in order, returning the first success.
    Always returns a string (FALLBACK_VERSION when all strategies fail).
    """
    version = _from_git_describe(root)
    if version is not None:
        return version
    version = _from_pkg_info(root)
    if version is not None:
        return version
    return FALLBACK_VERSION


def _git_dir(root: Path) -> Path | None:
    """The ``.git`` path for *root*, or *None* when absent (or not a directory)."""
    candidate = root / ".git"
    return candidate if candidate.exists() else None


def _run_git_describe(root: Path) -> str | None:
    """
    Run ``git describe --tags --dirty --match 'v*'`` and return raw output.
    Returns *None* on any failure (missing git, timeout, non-zero exit).
    """
    try:
        result = subprocess.run(
            ["git", "describe", "--tags", "--dirty", "--match", "v*"],
            capture_output=True,
            text=True,
            cwd=str(root),
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        log.debug("git describe failed: %s", exc)
        return None

    if result.returncode != 0:
        log.debug("git describe exited with code %d: %s", result.returncode, result.stderr.strip())
        return None

    output = result.stdout.strip()
    if not output:
        return None
    return output


def _from_git_describe(root: Path) -> str | None:
    """
    Resolve version via ``git describe`` when a ``.git`` directory exists.

    Returns *None* when Git is not available, no matching tag exists, or
    ``.git`` is absent.
    """
    if _git_dir(root) is None:
        return None
    output = _run_git_describe(root)
    if output is None:
        return None
    return _validate_describe(output)


def _validate_describe(output: str) -> str | None:
    """Accept *output* only if it matches the expected tag format."""
    return output if _DESCRIBE_RE.match(output) else None


def _from_pkg_info(framework_root: Path) -> str | None:
    """
    Read the version from ``PKG-INFO``, which setuptools writes during install.

    Two places to look:
    * ``<root>/src/kiln_swarm.egg-info/PKG-INFO`` — editable install.
    * ``<root>/kiln_swarm.egg-info/PKG-INFO`` — possible alternative layout.

    Returns *None* when neither file exists or the version cannot be parsed.
    """
    candidates = [
        framework_root / "src" / "kiln_swarm.egg-info" / "PKG-INFO",
        framework_root / "kiln_swarm.egg-info" / "PKG-INFO",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            version = _parse_pkg_info_version(path.read_text(encoding="utf-8"))
            if version is not None:
                return version
        except OSError as exc:
            log.debug("could not read %s: %s", path, exc)

    return None


def _parse_pkg_info_version(text: str) -> str | None:
    """Extract the Version: field from PKG-INFO content."""
    for line in text.splitlines():
        if line.startswith("Version:"):
            raw = line[len("Version:") :].strip()
            if raw:
                return f"v{raw}" if not raw.startswith("v") else raw
    return None
