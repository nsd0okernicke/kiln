"""
Guards against documentation describing things that do not ship.

Every case here is one that actually drifted: a role file naming a profile that never existed,
a constitution document no agent was told to read, and a config format Kiln has never had.
Prose cannot be type-checked, but these three claims are mechanical, so they get a test instead
of a promise to be careful.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import ClassVar

import pytest

REPO = Path(__file__).resolve().parents[3]
FRAMEWORK_PROFILES = REPO / "src" / "kiln" / "resources" / "profiles.json"
SCAFFOLD = REPO / "src" / "kiln" / "resources" / "project"
CONSTITUTION = SCAFFOLD / "constitution.md"


def _shipped_docs() -> list[Path]:
    """Markdown that ships to users: the framework's own docs plus every project template."""
    roots = [SCAFFOLD, REPO / "docs", REPO / "examples"]
    files = [path for root in roots for path in root.rglob("*.md")]
    return [*files, REPO / "README.md"]


class TestConstitutionLoadOrder:
    """
    `skill-orchestration.md` is the authoritative statement of who owns which quality gate --
    and for a long time nothing told an agent to read it, because constitution.md's load order
    listed only three of the four files beside it. A document that defines ownership and is
    never loaded is worse than no document: roles disagree and nothing arbitrates.
    """

    def test_every_constitution_file_is_scaffolded(self):
        # Worse than unreachable: skill-orchestration.md was absent from CONSTITUTION_FILES,
        # so no scaffolded project ever received it. Listing it in the load order without
        # copying it would have pointed every project at a file that is not there.
        from kiln.launcher.infrastructure.scaffold import CONSTITUTION_FILES

        present = {p.name for p in (SCAFFOLD / "constitution").glob("*.md")}

        assert present - set(CONSTITUTION_FILES) == set(), (
            "bundled constitution files that scaffolding never copies into a project"
        )

    def test_every_constitution_file_is_in_the_load_order(self):
        listed = CONSTITUTION.read_text(encoding="utf-8")
        present = sorted(p.name for p in (SCAFFOLD / "constitution").glob("*.md"))

        missing = [name for name in present if name not in listed]

        assert not missing, (
            f"{missing} ship in constitution/ but constitution.md never tells an agent to "
            "read them, so their rules reach no one"
        )


class TestProfileReferences:
    def test_no_document_names_a_profile_that_does_not_ship(self):
        # `scheduler-all` was documented in human-in-the-loop.md and existed nowhere else;
        # `default` was renamed to `full` and the role files kept the old name.
        shipped = set(json.loads(FRAMEWORK_PROFILES.read_text(encoding="utf-8"))["profiles"])
        retired = {"scheduler-all", "default"}

        offenders = [
            f"{path.relative_to(REPO)}: {name}"
            for path in _shipped_docs()
            for name in retired - shipped
            if f"`{name}` profile" in path.read_text(encoding="utf-8")
        ]

        assert not offenders, f"documented profiles that do not ship: {offenders}"

    def test_the_default_key_names_a_real_profile(self):
        config = json.loads(FRAMEWORK_PROFILES.read_text(encoding="utf-8"))
        assert config["default"] in config["profiles"]


class TestConfigFormat:
    @pytest.mark.parametrize("path", _shipped_docs(), ids=lambda p: str(p.name))
    def test_no_document_points_at_a_yaml_profile_file(self, path):
        # Kiln has never had a YAML profile format. Four documents told users to edit one.
        assert "profiles.yaml" not in path.read_text(encoding="utf-8")


class TestShippedWorkerTimeouts:
    """
    The 900s module default is sized for an LLM session, not for what these roles are asked to
    do. Measured live: cycles that finished spent 60-74% of their wall time waiting on the
    model; cycles that hit the cap spent 14-31% and were cut off mid-progress, running the
    project's own toolchain -- containers, mutation runs, dependency resolution.
    """

    #: Floors, not exact values -- tuning up is fine, dropping back to the default is the
    #: regression. The architect runs a full mutation pass and needs the most.
    MINIMUMS: ClassVar[dict[str, int]] = {
        "specifier": 1800,
        "coder": 1800,
        "refactorer": 1800,
        "architect": 2400,
    }

    def test_every_scheduled_heavy_role_raises_it(self):
        profiles = json.loads(FRAMEWORK_PROFILES.read_text(encoding="utf-8"))["profiles"]
        thin = []
        for name, profile in profiles.items():
            for terminal in profile["terminals"]:
                role = terminal.get("role")
                if role not in self.MINIMUMS or terminal.get("scheduler") != "python":
                    continue
                if terminal.get("workerTimeout", 0) < self.MINIMUMS[role]:
                    thin.append(f"{name}/{role}={terminal.get('workerTimeout')}")
        assert not thin, f"worker timeout too low for the role's own gates: {thin}"

    def test_the_architect_gets_the_longest(self):
        # It owns the full mutation run; every other role does a scan at most.
        profiles = json.loads(FRAMEWORK_PROFILES.read_text(encoding="utf-8"))["profiles"]
        full = {t["role"]: t for t in profiles["full"]["terminals"]}
        assert full["architect"]["workerTimeout"] > full["coder"]["workerTimeout"]


class TestHandoffSkillVerification:
    """
    The skill is a *second implementation* of the insert/verify pair, written in prose and
    executed by wrapper-mode agents. It is the copy that failed live: its Step 5 asked "is
    there a `queued` message from me?", the receiving scheduler had already taken the message,
    and the agent duly sent the whole handoff a second time.

    Python and prose cannot be kept in sync by a type checker, so this pins the one property
    that matters -- verify by id, never by status.
    """

    def _skill(self) -> str:
        return (SCAFFOLD / "skills" / "kiln-handoff" / "SKILL.md").read_text(encoding="utf-8")

    def test_it_verifies_by_id(self):
        assert "WHERE id=" in self._skill()

    def test_it_does_not_verify_by_status(self):
        # A status is exactly what a fast consumer changes out from under the sender.
        assert "status='queued'" not in self._skill()

    def test_it_asks_the_insert_to_return_the_id(self):
        # Verifying by id is only possible if Step 4 hands one back.
        assert "RETURNING id" in self._skill()

    def test_it_stamps_the_insert_time_rather_than_a_chosen_one(self):
        # Queue order is created_at ASC, so reusing the timestamp from the composed message
        # puts the handoff in the wrong place in the queue -- observed live, ~43s stale.
        assert "datetime('now', 'localtime')" in self._skill()


class TestExternalRuntimePrerequisites:
    """
    A dependency that needs a daemon must say so where a human looks before starting.

    `library-hub`'s README mandated Testcontainers in three places and never mentioned
    Docker anywhere -- its Prerequisites listed PowerShell, Git and the Claude CLI. With no
    daemon running, `PostgresContainer(...)` does not fail, it waits, so a coder ran the
    acceptance suite and hung until its worker timeout, twice, then escalated 75 minutes in.
    Neither the brief nor the constitution gave it any reason to check first.
    """

    #: Any engine satisfies this -- the requirement is a reachable daemon, not Docker itself.
    ENGINES: ClassVar[tuple[str, ...]] = ("docker", "podman", "colima", "container engine")

    def _prerequisites(self, text: str) -> str:
        """The Prerequisites section: from its heading to the next one of any level."""
        start = re.search(r"^#+\s*Prerequisites\s*$", text, re.M)
        if start is None:
            return ""
        rest = text[start.end() :]
        end = re.search(r"^#+\s", rest, re.M)
        return (rest if end is None else rest[: end.start()]).lower()

    @pytest.mark.parametrize(
        "readme", sorted((REPO / "examples").glob("*/README.md")), ids=lambda p: p.parent.name
    )
    def test_an_example_needing_containers_names_one_in_its_prerequisites(self, readme):
        text = readme.read_text(encoding="utf-8")
        if "testcontainers" not in text.lower():
            pytest.skip("no container-backed fixtures")

        prerequisites = self._prerequisites(text)

        assert any(engine in prerequisites for engine in self.ENGINES), (
            f"{readme.parent.name} mandates Testcontainers but its Prerequisites never name a "
            "container engine, so nothing tells a reader — or an agent — to start one"
        )


class TestVirtualenvInvocation:
    """
    `Scripts\\activate` hangs in a non-interactive shell, and a hung activation is invisible.

    Live: a codex coder followed "always activate before any Python command", the activation
    never returned, and it polled the background cell 63 times over 20 minutes. It diagnosed
    itself correctly -- "a shell activation issue, not a test failure" -- and still produced
    nothing: no `pyproject.toml`, no package, one wedged handoff. Naming the interpreter needs
    no shell state and cannot hang, so the instruction must not come back.
    """

    @pytest.mark.parametrize(
        "doc",
        sorted((REPO / "examples").glob("*/kiln/project/constitution/project.md")),
        ids=lambda p: p.parents[3].name,
    )
    def test_no_example_tells_an_agent_to_activate_a_virtualenv(self, doc):
        text = doc.read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in text.splitlines()
            # The prose explaining *why not* to activate legitimately names the script.
            if ("Scripts\\activate" in line or "bin/activate" in line)
            and "not activate" not in text[: text.index(line)][-400:]
        ]
        assert not offenders, f"{doc.parents[3].name} still instructs activation: {offenders}"


class TestUnsupportedRoles:
    """
    `reviewer` cannot run: no shipped profile routes it, so the scheduler escalates NO_ROUTE on
    its first handoff. Either it gets a route or it says so at the top of its own file. This
    pins whichever is true, so the two cannot silently disagree again.
    """

    def test_reviewer_is_either_routed_or_labelled_unsupported(self):
        profiles = json.loads(FRAMEWORK_PROFILES.read_text(encoding="utf-8"))["profiles"]
        routed = any("reviewer" in profile.get("routing", {}) for profile in profiles.values())

        role_file = (SCAFFOLD / "roles" / "reviewer.md").read_text("utf-8")

        assert routed or "Unsupported" in role_file, (
            "reviewer has no routing row in any profile, so it stalls on its first handoff -- "
            "its role file must say so"
        )


class TestReadmeProfileDocumentation:
    """Keep profile guidance tied to the authoritative bundled configuration."""

    def test_readme_points_to_the_shipped_profiles(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")

        assert "`src/kiln/resources/profiles.json`" in readme

    def test_readme_names_the_real_default(self):
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        shipped = json.loads(FRAMEWORK_PROFILES.read_text(encoding="utf-8"))

        assert f"`{shipped['default']}`" in readme


class TestEveryShippedProfileDeclaresRouting:
    """
    Routing moved into the profile and workflow.md is rendered from it, so a profile without a
    `routing` block has no table to render and `check_launchable` rejects it. A shipped profile
    in that state is a launch error we ship, not a runtime surprise.
    """

    def test_no_shipped_profile_would_be_refused_at_launch(self):
        profiles = json.loads(FRAMEWORK_PROFILES.read_text(encoding="utf-8"))["profiles"]

        missing = [name for name, profile in profiles.items() if not profile.get("routing")]

        assert not missing, f"shipped profiles that declare no routing: {missing}"


class TestWrapperTemplateSets:
    """
    A backend accepted by the loader but missing a template does not degrade -- it takes the
    launch down. `read_template` raises `TemplateError` for a missing file, and
    `render_instructions` reaches for all of these by name derived from `role.agent`/
    `role.mode`, so the first user to configure the gap gets a crash rather than a fallback.

    This is the shape `grok` was in for its whole scheduler-only life: a legal `agent` value
    with no `loop-auto-grok.md` behind it.
    """

    def _expected(self, agent: str) -> set[str]:
        """Every template `generate.render_instructions` can ask for, for one agent."""
        from kiln.launcher.application.generate import DELEGATING_AGENTS

        names = {
            f"loop-auto-{agent}.md",
            f"loop-manual-{agent}.md",
            f"loop-manual-{agent}-with-inbox.md",
            f"runtime-{agent}.md",
        }
        if agent in DELEGATING_AGENTS:
            names.add(f"wrapper-prompt-auto-{agent}.md")
        return names

    def test_every_accepted_agent_has_a_complete_template_set(self):
        from kiln.launcher.domain.profile import VALID_AGENTS

        present = {p.name for p in (REPO / "src" / "kiln" / "resources" / "templates").glob("*.md")}

        missing = {agent: sorted(self._expected(agent) - present) for agent in VALID_AGENTS}
        incomplete = {agent: names for agent, names in missing.items() if names}

        assert not incomplete, (
            f"agents the loader accepts whose wrapper templates do not ship: {incomplete} -- "
            "configuring one fails the launch with TemplateError"
        )


class TestDocumentedTerminalKeys:
    """Every accepted profile field belongs in the user-facing configuration reference."""

    def test_every_accepted_terminal_key_is_documented(self):
        from kiln.launcher.domain.profile import TERMINAL_KEYS

        readme = (REPO / "README.md").read_text(encoding="utf-8")

        undocumented = sorted(key for key in TERMINAL_KEYS if f"`{key}`" not in readme)

        assert not undocumented, (
            f"terminal keys the loader accepts but README.md never documents: {undocumented}"
        )
