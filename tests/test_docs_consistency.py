"""
Guards against documentation describing things that do not ship.

Every case here is one that actually drifted: a role file naming a profile that never existed,
a constitution document no agent was told to read, and a config format Kiln has never had.
Prose cannot be type-checked, but these three claims are mechanical, so they get a test instead
of a promise to be careful.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
FRAMEWORK_PROFILES = REPO / "kiln" / "framework" / "profiles.json"
CONSTITUTION = REPO / "kiln" / "project" / "constitution.md"


def _shipped_docs() -> list[Path]:
    """Markdown that ships to users: the framework's own docs plus every project template."""
    roots = [REPO / "kiln" / "project", REPO / "docs", REPO / "examples"]
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
        from launcher.scaffold import CONSTITUTION_FILES

        present = {p.name for p in (REPO / "kiln" / "project" / "constitution").glob("*.md")}

        assert present - set(CONSTITUTION_FILES) == set(), (
            "bundled constitution files that scaffolding never copies into a project"
        )

    def test_every_constitution_file_is_in_the_load_order(self):
        listed = CONSTITUTION.read_text(encoding="utf-8")
        present = sorted(p.name for p in (REPO / "kiln" / "project" / "constitution").glob("*.md"))

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
        return (
            REPO / "kiln" / "project" / "skills" / "kiln-handoff" / "SKILL.md"
        ).read_text(encoding="utf-8")

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


class TestUnsupportedRoles:
    """
    `reviewer` cannot run: no shipped profile routes it, so the scheduler escalates NO_ROUTE on
    its first handoff. Either it gets a route or it says so at the top of its own file. This
    pins whichever is true, so the two cannot silently disagree again.
    """

    def test_reviewer_is_either_routed_or_labelled_unsupported(self):
        profiles = json.loads(FRAMEWORK_PROFILES.read_text(encoding="utf-8"))["profiles"]
        routed = any("reviewer" in profile.get("routing", {}) for profile in profiles.values())

        role_file = (REPO / "kiln" / "project" / "roles" / "reviewer.md").read_text("utf-8")

        assert routed or "Unsupported" in role_file, (
            "reviewer has no routing row in any profile, so it stalls on its first handoff -- "
            "its role file must say so"
        )
