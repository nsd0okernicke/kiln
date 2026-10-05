"""
Container runner — launches a Kiln swarm inside a Docker container.

Phase 3: one container = one headless swarm. The container receives the host
repository read-only at ``/seed`` and the run directory read-write at ``/run``.
It clones, checks out the target ref, installs Kiln, and launches the swarm.

Host-side state is observable through the Phase 2 hub: the container writes
``messages.db``, ``status/``, and ``logs/`` into ``/run``, which is the host's
``.kiln/runs/<run_id>/`` directory.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

#: Default Docker image name.
IMAGE_NAME = "kiln-runner"

#: How long to wait for the container to start before checking its health.
CONTAINER_START_TIMEOUT_SEC = 30

#: Subdirectory inside the run dir where host credentials are staged.
CREDENTIALS_DIR = "credentials"


class ContainerError(Exception):
    """Docker-related launch failure."""


def build_image(dockerfile_dir: Path, tag: str = IMAGE_NAME, dry_run: bool = False) -> bool:
    """
    Build the Kiln runner Docker image.

    Returns True when the build succeeded (or would have, in dry-run mode).
    """
    if not shutil.which("docker"):
        raise ContainerError("docker not found on PATH. Install Docker Desktop or the Docker CLI.")

    cmd = ["docker", "build", "-t", tag, "-f", str(dockerfile_dir / "Dockerfile"), str(dockerfile_dir.parent)]  # noqa: E501
    if dry_run:
        log.info("[dry-run] would build: %s", " ".join(cmd))
        return True

    log.info("building Docker image %s ...", tag)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        log.error("Docker build failed:\n%s", result.stderr.strip())
        return False
    log.info("Docker image %s built", tag)
    return True


def _credential_sources() -> dict[str, Path]:
    """
    Discover credential directories on the host to seed into the container.

    Returns ``{label: path}`` for each supported CLI's credential directory
    that exists on the host. This is mechanism 2 from the issue: copy, don't
    mount (Windows privilege constraints and refresh semantics).
    """
    home = Path.home()
    sources: dict[str, Path] = {}

    # Claude: ~/.claude/ (OAuth tokens).
    claude = home / ".claude"
    if claude.is_dir():
        sources["claude"] = claude

    # Codex: CODEX_HOME or ~/.codex/.
    codex_home = os.environ.get("CODEX_HOME")
    codex = Path(codex_home) if codex_home else home / ".codex"
    if codex.is_dir():
        sources["codex"] = codex

    # Grok: ~/.grok/ or XAI_CONFIG_DIR.
    grok = home / ".grok"
    if grok.is_dir():
        sources["grok"] = grok

    # Pi: ~/.pi/ (config, session, API keys).
    pi = home / ".pi"
    if pi.is_dir():
        sources["pi"] = pi

    return sources


def seed_credentials(run_dir: Path) -> dict[str, Path]:
    """
    Copy the host's credential directories into the run dir for the container.

    Returns ``{label: target_path}`` for each credential set that was copied.
    The container entrypoint picks these up from ``/run/credentials/``.
    """
    creds_dir = run_dir / CREDENTIALS_DIR
    creds_dir.mkdir(parents=True, exist_ok=True)

    seeded: dict[str, Path] = {}
    for label, source in _credential_sources().items():
        target = creds_dir / label
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        try:
            shutil.copytree(source, target, ignore_dangling_symlinks=True)
            seeded[label] = target
            log.info("seeded %s credentials -> %s", label, target)
        except OSError as exc:
            log.warning("could not seed %s credentials: %s", label, exc)

    return seeded


def start_container(
    run_id: str,
    project_root: Path,
    checkout_path: Path,
    framework_root: Path | None = None,
    ref: str = "main",
    profile: str = "full",
    image: str = IMAGE_NAME,
    extra_mounts: list[tuple[str, str]] | None = None,
    extra_add_hosts: list[str] | None = None,
    dry_run: bool = False,
) -> str | None:
    """
    Start a Docker container for one run.

    Returns the container ID, or None on failure.

    Mounts:
      - ``<project_root>`` → ``/seed`` (read-only)
      - ``<checkout_path>`` → ``/run`` (read-write)
      - ``<framework_root>`` → ``/kiln`` (read-only, optional)
      - ``<extra_mounts>`` → additional host-to-container bind mounts

    Extra:
      - ``<extra_add_hosts>`` → ``--add-host`` entries (HOSTNAME:IP)

    Environment variables set in the container:
      - ``KILN_REF`` — the git ref to check out
      - ``KILN_PROFILE`` — the profile to launch
      - ``KILN_RUN_ID`` — the run identifier
      - ``KILN_STATE_DIR`` — /run/workspace/.kiln so set-status.py can find state
      - ``KILN_COCKPIT_BIND`` — set to 0.0.0.0 so Docker port publishing can reach the cockpit
      - ``PYTHONPATH`` — includes /kiln/src (if framework_root provided) and /run/workspace/src
    """
    if not shutil.which("docker"):
        raise ContainerError("docker not found on PATH.")

    checkout_path.mkdir(parents=True, exist_ok=True)

    cmd = [
        "docker", "run", "--detach",
        "--name", f"kiln-{run_id}",
        "-p", "127.0.0.1:8765:8765",  # cockpit UI
        "--mount", f"type=bind,source={project_root.resolve()},target=/seed,readonly",
        "--mount", f"type=bind,source={checkout_path.resolve()},target=/run",
        "-e", f"KILN_REF={ref}",
        "-e", f"KILN_PROFILE={profile}",
        "-e", f"KILN_RUN_ID={run_id}",
        "-e", "KILN_STATE_DIR=/run/workspace/.kiln",
        "-e", "KILN_COCKPIT_BIND=0.0.0.0",
    ]

    if framework_root:
        cmd += [
            "--mount", f"type=bind,source={framework_root.resolve()},target=/kiln,readonly",
            "-e", "PYTHONPATH=/kiln/src:/run/workspace/src",
        ]
    else:
        cmd += ["-e", "PYTHONPATH=/run/workspace/src"]

    # Add extra user-specified mounts (--docker-mount).  Expand ~ to $HOME and resolve.
    for host_path, container_path in (extra_mounts or []):
        expanded = Path(host_path).expanduser().resolve()
        cmd += [
            "--mount",
            f"type=bind,source={expanded},target={container_path}",
        ]

    # Add extra host entries (--docker-add-host).
    for entry in (extra_add_hosts or []):
        cmd += ["--add-host", entry]

    # Mount the Docker socket if available (needed for acceptance gates that
    # use testcontainers inside the swarm container).
    docker_socket = Path("/var/run/docker.sock")
    if docker_socket.exists():
        cmd += ["--mount", "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock"]

    cmd.append(image)

    if dry_run:
        log.info("[dry-run] would run: %s", " ".join(cmd))
        return "dry-run-container-id"

    log.info("starting container for run %s ...", run_id)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )

    if result.returncode != 0:
        log.error("container start failed:\n%s", result.stderr.strip())
        return None

    container_id = result.stdout.strip()
    log.info("container %s started for run %s", container_id[:12], run_id)
    return container_id


def stop_container(container_id: str, dry_run: bool = False) -> bool:
    """Stop and remove a Kiln container by its ID or name."""
    if dry_run:
        log.info("[dry-run] would stop container %s", container_id)
        return True

    for action in ("stop", "rm"):
        result = subprocess.run(
            ["docker", action, "--time", "10", container_id],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode != 0:
            log.warning("docker %s %s: %s", action, container_id[:12], result.stderr.strip())
            return False

    log.info("container %s stopped and removed", container_id[:12])
    return True


def container_logs(container_id: str, tail: int = 50) -> str:
    """Return the last N lines of container logs."""
    result = subprocess.run(
        ["docker", "logs", "--tail", str(tail), container_id],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
    )
    return (result.stdout or "") + (result.stderr or "")
