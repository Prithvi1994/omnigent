"""E2E regression test: the claude-family web skill menu must match the
skills the host-spawned Claude terminal can load.

For a claude-family session the web composer's slash-command menu is fed by
``GET /v1/skills?session_id={id}`` (``resolve_session_skills`` →
``resolve_harness_skills``), while the embedded terminal's menu is whatever
the real Claude Code CLI discovers itself. The managed native launch runs
``--setting-sources user``, so the terminal loads only its user tier
(``$CLAUDE_CONFIG_DIR/skills``, defaulting to ``~/.claude/skills``):

* project ``<workspace>/.claude/skills`` is gated behind the disabled
  ``projectSettings`` source, and ``<workspace>/.agents/skills`` was never a
  Claude tier — so the web menu must list neither, or it surfaces commands
  the terminal cannot invoke (a native session sends ``/name`` to the CLI as
  plaintext; there is no server-side resolve+inject on that path), and
* the web resolution must honor ``$CLAUDE_CONFIG_DIR`` for the user tier
  rather than only ``Path.home()/.claude/skills`` — otherwise with a
  non-default config dir the terminal shows user skills the web menu omits.

These tests assert that parity contract.

Usage::

    pytest tests/e2e/test_claude_terminal_web_skills_parity_e2e.py -v
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.host.frames import HostSkillsFrame
from omnigent.host.skills import HostSkillDiscovery
from omnigent.runner import create_runner_app
from omnigent.runner.app import ResolvedSpec
from omnigent.spec.types import SkillSpec

_CLAUDE_DIR_SKILL = "claude-dir-skill"
_AGENTS_ONLY_SKILL = "agents-only-skill"
_USER_CFG_SKILL = "user-cfg-skill"


def _skill_md(name: str, description: str) -> str:
    """Minimal SKILL.md with valid frontmatter.

    :param name: Frontmatter skill name (matches its directory name).
    :param description: One-line human description.
    :returns: The SKILL.md contents.
    """
    return f"---\nname: {name}\ndescription: {description}\n---\n\nBody of {name}.\n"


def _seed_workspace(workspace: Path) -> None:
    """
    Seed the two project skill tiers the restricted launch excludes.

    Under ``--setting-sources user`` the host-spawned Claude terminal reads
    neither tier: project ``.claude/skills`` needs the disabled
    ``projectSettings`` source, and ``.agents/skills`` was never a Claude
    tier. The web menu must exclude both to stay in parity.

    :param workspace: The session workspace directory to populate.
    """
    claude_skill = workspace / ".claude" / "skills" / _CLAUDE_DIR_SKILL
    claude_skill.mkdir(parents=True)
    (claude_skill / "SKILL.md").write_text(
        _skill_md(_CLAUDE_DIR_SKILL, "workspace .claude skill (both surfaces)")
    )
    agents_skill = workspace / ".agents" / "skills" / _AGENTS_ONLY_SKILL
    agents_skill.mkdir(parents=True)
    (agents_skill / "SKILL.md").write_text(
        _skill_md(_AGENTS_ONLY_SKILL, "workspace .agents skill (web-only today)")
    )


class _ExecutorStub:
    """Minimal ``ExecutorSpec`` stand-in exposing ``harness_kind``."""

    def __init__(self, harness: str) -> None:
        """:param harness: The session's harness, e.g. ``"claude-native"``."""
        self.harness_kind = harness


class _SpecStub:
    """Minimal ``AgentSpec`` stand-in for runner skill discovery."""

    def __init__(self, harness: str) -> None:
        """:param harness: Harness id driving per-harness skill discovery."""
        self.skills: list[SkillSpec] = []
        self.skills_filter: str = "all"
        self.executor = _ExecutorStub(harness)


class _ServerClient:
    """Fake Omnigent server client returning a fixed session snapshot."""

    def __init__(self, workspace: str) -> None:
        """:param workspace: Session workspace path to report."""
        self._workspace = workspace

    class _Response:
        """Stub 200 snapshot response with an agent_id + workspace."""

        def __init__(self, workspace: str) -> None:
            """:param workspace: Workspace path to include in the body."""
            self.status_code = 200
            self._workspace = workspace

        def json(self) -> dict[str, Any]:
            """:returns: A minimal session snapshot."""
            return {"agent_id": "ag_skillparity", "workspace": self._workspace}

    async def get(self, url: str, **kwargs: Any) -> _Response:
        """:returns: The stub snapshot response (url/kwargs ignored)."""
        del url, kwargs
        return self._Response(self._workspace)


def _make_app(harness: str, workspace: Path) -> Any:
    """
    Build a runner app whose spec resolver returns a stub spec.

    :param harness: The session's harness id, e.g. ``"claude-native"``.
    :param workspace: Session workspace (host-skill discovery root).
    :returns: The configured runner FastAPI app.
    """
    entry = ResolvedSpec(spec=_SpecStub(harness), workdir=workspace)

    async def _spec_resolver(agent_id: str, session_id: str | None) -> Any:
        """Return the stub resolved spec."""
        del agent_id, session_id
        return entry

    return create_runner_app(
        spec_resolver=_spec_resolver,
        server_client=_ServerClient(str(workspace)),  # type: ignore[arg-type]
    )


def _menu_names(harness: str, workspace: Path) -> list[str]:
    """Read the menu catalog on the host, independently of invocation."""

    def unexpected_bundle(_: HostSkillsFrame) -> httpx.Response:
        raise AssertionError("Directory discovery must not fetch a session bundle")

    discovery = HostSkillDiscovery(unexpected_bundle)
    return [
        s["name"]
        for s in discovery.discover(HostSkillsFrame("menu", harness, str(workspace)), workspace)
    ]


async def _client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """
    Yield an httpx client bound to the runner app over ASGI.

    :param app: The runner FastAPI app.
    :returns: Async iterator yielding the client.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        yield c


@pytest.mark.asyncio
async def test_claude_web_menu_lists_only_terminal_loadable_workspace_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The claude-family web menu must list only user-tier skills.

    The user journey: a workspace carries skills under both
    ``.claude/skills/`` and ``.agents/skills/``; the user opens the web
    composer's slash menu and the embedded Claude terminal's slash menu for
    the same claude-native session and compares them. The managed launch runs
    ``--setting-sources user``, so the terminal loads neither workspace tier
    (project ``.claude/skills`` needs the disabled ``projectSettings`` source;
    ``.agents/skills`` is never a Claude tier) — it lists only its user tier.
    Surfacing either workspace entry in the web menu would list a command the
    terminal cannot invoke (a native session sends ``/name`` as plaintext).
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    user_skill = home / ".claude" / "skills" / _USER_CFG_SKILL
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text(
        _skill_md(_USER_CFG_SKILL, "user-tier skill (both surfaces)")
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_workspace(workspace)

    names = _menu_names("claude-native", workspace)

    # Precondition: the user tier both surfaces agree on is listed.
    assert _USER_CFG_SKILL in names, (
        f"precondition: user-tier skill missing from menu; got {names}"
    )

    # Project .claude/skills is gated behind the disabled projectSettings
    # source, so the web menu must not surface it either.
    assert _CLAUDE_DIR_SKILL not in names, (
        f"web menu for a restricted claude session lists {_CLAUDE_DIR_SKILL!r} "
        f"from project .claude/skills, which the host-spawned terminal does not "
        f"load — the two surfaces show different skills. Menu: {names}"
    )

    # ``.agents/skills`` is never a Claude tier, so the terminal never loads it.
    assert _AGENTS_ONLY_SKILL not in names, (
        f"web menu for a claude session lists {_AGENTS_ONLY_SKILL!r} from "
        f".agents/skills, which the Claude Code terminal does not load — the "
        f"two surfaces show different skills. Menu: {names}"
    )


@pytest.mark.asyncio
async def test_claude_web_menu_sources_user_skills_from_claude_config_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The claude-family web menu must honor ``CLAUDE_CONFIG_DIR`` user skills.

    The Claude Code terminal loads user-tier skills from
    ``$CLAUDE_CONFIG_DIR/skills`` (live-verified: its slash menu labels them
    "(user)"), while the web resolution reads only
    ``Path.home()/.claude/skills``. With a non-default config dir the
    terminal therefore lists a skill the web menu omits — the other
    direction of the reported divergence.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    cfg = tmp_path / "claude-config"
    user_skill = cfg / "skills" / _USER_CFG_SKILL
    user_skill.mkdir(parents=True)
    (user_skill / "SKILL.md").write_text(
        _skill_md(_USER_CFG_SKILL, "user config-dir skill (terminal-only today)")
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _seed_workspace(workspace)

    names = _menu_names("claude-native", workspace)

    # The terminal's slash menu lists this skill as "(user)"; the web menu
    # must list it too or the surfaces diverge.
    assert _USER_CFG_SKILL in names, (
        f"Claude terminal loads user skills from $CLAUDE_CONFIG_DIR/skills "
        f"({cfg / 'skills'}), but the web menu omits {_USER_CFG_SKILL!r} — "
        f"the two surfaces show different skills. Menu: {names}"
    )

    # The project .claude/skills entry the restricted launch skips must not
    # appear alongside the user tier.
    assert _CLAUDE_DIR_SKILL not in names, (
        f"web menu lists project skill {_CLAUDE_DIR_SKILL!r} the restricted "
        f"terminal does not load. Menu: {names}"
    )
