"""Unit tests for the container (Docker) runner module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from kiln.launcher.infrastructure import container


class TestBuildImage:
    def test_raises_when_docker_not_found(self, monkeypatch):
        monkeypatch.setattr(container.shutil, "which", lambda name: None)
        try:
            container.build_image(Path("/x"), dry_run=False)
            assert False, "expected ContainerError"
        except container.ContainerError as exc:
            assert "docker not found" in str(exc)

    def test_dry_run_logs_without_building(self, monkeypatch):
        monkeypatch.setattr(container.shutil, "which", lambda name: "docker")
        run_calls = []
        monkeypatch.setattr(container.subprocess, "run", lambda *a, **kw: run_calls.append(a))

        result = container.build_image(Path("/x"), dry_run=True)

        assert result is True
        assert run_calls == []

    def test_build_failure_returns_false(self, monkeypatch):
        monkeypatch.setattr(container.shutil, "which", lambda name: "docker")
        monkeypatch.setattr(
            container.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(returncode=1, stderr="build error"),
        )

        result = container.build_image(Path("/tmp"), dry_run=False)

        assert result is False

    def test_successful_build_returns_true(self, monkeypatch):
        monkeypatch.setattr(container.shutil, "which", lambda name: "docker")
        monkeypatch.setattr(
            container.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(returncode=0, stderr="", stdout=""),
        )

        result = container.build_image(Path("/tmp"), dry_run=False)

        assert result is True


class TestSeedCredentials:
    def test_creates_credentials_dir(self, tmp_path):
        seeded = container.seed_credentials(tmp_path)
        assert (tmp_path / container.CREDENTIALS_DIR).is_dir()

    def test_returns_empty_when_no_creds_on_host(self, tmp_path, monkeypatch):
        # Make home point to a non-existent dir so no creds are found.
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "no-such-home")
        seeded = container.seed_credentials(tmp_path)
        assert seeded == {}

    def test_copies_claude_creds_when_present(self, tmp_path, monkeypatch):
        # Simulate ~/.claude/auth.json on host.
        claude_src = tmp_path / "host-home" / ".claude"
        claude_src.mkdir(parents=True)
        (claude_src / "auth.json").write_text('{"token": "test"}', encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: tmp_path / "host-home")

        run_dir = tmp_path / "run"
        run_dir.mkdir()
        seeded = container.seed_credentials(run_dir)

        assert "claude" in seeded
        assert (run_dir / "credentials" / "claude" / "auth.json").is_file()


class TestStartContainer:
    def test_raises_when_docker_not_found(self, monkeypatch):
        monkeypatch.setattr(container.shutil, "which", lambda name: None)
        try:
            container.start_container("test", Path("/p"), Path("/c"), dry_run=False)
            assert False, "expected ContainerError"
        except container.ContainerError as exc:
            assert "docker not found" in str(exc)

    def test_dry_run_returns_dummy_id(self, monkeypatch):
        monkeypatch.setattr(container.shutil, "which", lambda name: "docker")
        result = container.start_container("test", Path("/p"), Path("/c"), dry_run=True)
        assert result == "dry-run-container-id"

    def test_successful_start_returns_container_id(self, monkeypatch, tmp_path):
        monkeypatch.setattr(container.shutil, "which", lambda name: "docker")
        monkeypatch.setattr(
            container.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(returncode=0, stdout="abc123\n", stderr=""),
        )

        checkout = tmp_path / "checkout"
        checkout.mkdir(parents=True, exist_ok=True)

        cid = container.start_container("test-run", tmp_path, checkout, dry_run=False)
        assert cid == "abc123"

    def test_failed_start_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(container.shutil, "which", lambda name: "docker")
        monkeypatch.setattr(
            container.subprocess,
            "run",
            lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="error"),
        )

        checkout = tmp_path / "checkout"
        checkout.mkdir(parents=True, exist_ok=True)

        cid = container.start_container("test-run", tmp_path, checkout, dry_run=False)
        assert cid is None

    def test_command_includes_name_and_mounts(self, monkeypatch, tmp_path):
        """Verify the docker run command structure."""
        monkeypatch.setattr(container.shutil, "which", lambda name: "docker")
        recorded = []

        def fake_run(*args, **kwargs):
            recorded.append(args[0])
            return SimpleNamespace(returncode=0, stdout="cid\n", stderr="")

        monkeypatch.setattr(container.subprocess, "run", fake_run)

        checkout = tmp_path / "checkout"
        checkout.mkdir(parents=True, exist_ok=True)

        container.start_container("exp-auth", tmp_path, checkout, dry_run=False)

        assert len(recorded) == 1
        cmd = recorded[0]
        assert "--name" in cmd
        flat = " ".join(str(a) for a in cmd)
        assert "kiln-exp-auth" in flat
        assert "/seed" in flat  # read-only mount
        assert "/run" in flat  # read-write mount
        assert "KILN_REF" in flat
        assert "KILN_PROFILE" in flat
        assert "KILN_RUN_ID" in flat
        assert "KILN_STATE_DIR" in flat


class TestStopContainer:
    def test_dry_run_logs_without_stopping(self, monkeypatch):
        run_calls = []
        monkeypatch.setattr(container.subprocess, "run", lambda *a, **kw: run_calls.append(a))

        result = container.stop_container("abc123", dry_run=True)

        assert result is True
        assert run_calls == []

    def test_stop_and_remove_are_called(self, monkeypatch):
        recorded = []

        def fake_run(*args, **kwargs):
            recorded.append(args[0])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(container.subprocess, "run", fake_run)

        result = container.stop_container("abc123", dry_run=False)

        assert result is True
        stop_cmds = [c for c in recorded if "stop" in c]
        rm_cmds = [c for c in recorded if "rm" in c]
        assert len(stop_cmds) == 1
        assert len(rm_cmds) == 1


class TestContainerFromRunJson:
    def test_returns_none_when_no_run_json(self, tmp_path):
        from kiln.launcher.infrastructure.stop import _container_from_run_json

        result = _container_from_run_json(tmp_path / "checkout")
        assert result is None

    def test_returns_none_for_local_executor(self, tmp_path):
        from kiln.launcher.infrastructure.stop import _container_from_run_json

        run_dir = tmp_path / ".kiln" / "runs" / "test-run"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"executor": {"kind": "local"}}), encoding="utf-8"
        )
        checkout = tmp_path / ".worktrees" / "test-run"

        result = _container_from_run_json(checkout)
        assert result is None

    def test_returns_container_id_for_docker_executor(self, tmp_path):
        from kiln.launcher.infrastructure.stop import _container_from_run_json

        run_dir = tmp_path / ".kiln" / "runs" / "test-run"
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(
            json.dumps({"executor": {"kind": "docker", "container_id": "abc123def"}}),
            encoding="utf-8",
        )
        checkout = tmp_path / ".worktrees" / "test-run"

        result = _container_from_run_json(checkout)
        assert result == "abc123def"
