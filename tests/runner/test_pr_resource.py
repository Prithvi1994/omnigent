"""Provider resolution and the provider fields of the pull request dispatcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from omnigent.git_providers import reset_for_tests
from omnigent.runner import github_resource, pr_resource
from omnigent.runner.pr_resource import ProviderResolution
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.budgets import budget

LEGACY_FIELDS = ("gh_available", "authenticated", "accounts", "selected_account")
GITHUB_CAPABILITIES = {
    "account_switching": True,
    "base_remote_selection": True,
    "line_counts": True,
    "linked_pr_diff": True,
}
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No ambient provider hosts, gh sign-ins, git config, or session registry."""
    for name in ("OMNIGENT_GIT_PROVIDER_MODULES", "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "GH_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(name, value)
    reset_for_tests()
    yield
    reset_for_tests()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git checkout on ``feature`` with one commit and no remotes."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "checkout", "-q", "-b", "feature")
    (path / "a.txt").write_text("a")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _forbid_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("gh ran for a workspace that GitHub does not serve")

    monkeypatch.setattr(github_resource, "_gh", forbidden)


def test_github_info_equals_pr_info_and_keeps_the_legacy_fields(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    hosts = {"hosts": {"github.com": [{"login": "alice", "active": True, "state": "success"}]}}

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if list(argv[:4]) == ["auth", "status", "--json", "hosts"]:
            return 0, json.dumps(hosts), ""
        # No PR and the repo is unreachable, so all four legacy fields are filled.
        return 1, "", "no access"

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(github_resource, "_workspace_key", lambda _root: None)

    info = github_resource.github_info(str(repo))

    assert info == pr_resource.pr_info(str(repo))
    assert info["provider"] == "github"
    assert info["capabilities"] == GITHUB_CAPABILITIES
    assert [field for field in LEGACY_FIELDS if field not in info] == []
    assert info["auth"] == {
        "authenticated": True,
        "hint": None,
        "cli": {"name": "gh", "available": True},
        "accounts": info["accounts"],
        "selected_account": "alice",
    }


def test_tracked_github_pr_info_carries_the_provider_fields(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/acme/repo/pull/7"
    SessionPrRegistry("session").record(
        [PullRequestRef.from_url(url)], relationship="created", source="test"
    )
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: None)

    info = pr_resource.pr_info(str(repo), session_id="session")

    assert info["selected_pr_url"] == url
    assert (info["provider"], info["capabilities"]) == ("github", GITHUB_CAPABILITIES)
    assert info["auth"]["cli"] == {"name": "gh", "available": False}
    assert info["gh_available"] is False
    assert [pr["provider"] for pr in info["prs"]] == ["github"]


def test_a_directory_outside_git_reports_not_a_git_repo_from_github(tmp_path: Path) -> None:
    info = pr_resource.pr_info(str(tmp_path))

    assert (info["available"], info["reason"]) == (False, "not_a_git_repo")
    assert (info["provider"], info["auth"]) == ("github", None)
    assert info["capabilities"] == GITHUB_CAPABILITIES


def test_a_gitlab_origin_is_an_unsupported_remote(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", "https://gitlab.com/g/p.git")
    _forbid_gh(monkeypatch)

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution(None, "gitlab.com")
    info = pr_resource.pr_info(str(repo))
    assert (info["available"], info["reason"]) == (False, "unsupported_remote")
    assert info["remote_host"] == "gitlab.com"
    session = pr_resource.pr_info(str(repo), session_id="session")
    assert session["reason"] == "unsupported_remote"
    assert (session["prs"], session["tracking_available"]) == ([], True)
    assert pr_resource.pr_changed_files(str(repo))["data"] == []
    assert pr_resource.pr_diff(str(repo))["patch"] == ""
    with pytest.raises(ValueError, match="No supported git provider"):
        pr_resource.pr_file_diff(str(repo), "main", "a.txt")


def test_git_config_provider_wins_over_remote_matching(repo: Path) -> None:
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    _git(repo, "config", "omnigent.gitprovider", "Azure_DevOps")

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution(
        "azure_devops", "github.com"
    )


def test_a_configured_provider_without_a_facet_is_unsupported(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    _git(repo, "config", "omnigent.gitprovider", "forgejo")
    _forbid_gh(monkeypatch)

    info = pr_resource.pr_info(str(repo))

    assert (info["reason"], info["remote_host"]) == ("unsupported_remote", "github.com")


def test_a_ghes_origin_resolves_to_github_through_gh_host(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", "git@ghe.example.com:org/repo.git")
    assert pr_resource.resolve_provider(str(repo)).provider is None

    monkeypatch.setenv("GH_HOST", "ghe.example.com")

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution(
        "github", "ghe.example.com"
    )


def test_origin_is_matched_before_the_other_remotes(repo: Path) -> None:
    _git(repo, "remote", "add", "aaa", "https://gitlab.com/g/p.git")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution("github", "github.com")

    # An unclaimed origin still names the remote host; a later remote may be claimed.
    _git(repo, "remote", "set-url", "origin", "https://gitlab.com/g/p.git")
    _git(repo, "remote", "set-url", "aaa", "https://github.com/acme/repo.git")
    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution("github", "gitlab.com")


def test_workspaces_without_a_network_remote_use_the_first_provider(
    repo: Path, tmp_path: Path
) -> None:
    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution("github")

    _git(repo, "remote", "add", "origin", (tmp_path / "upstream").as_uri())
    _git(repo, "remote", "add", "local", str(tmp_path / "other"))

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution("github")
    assert pr_resource.resolve_provider(str(tmp_path / "missing")) == ProviderResolution("github")


def test_a_tracked_pr_decides_the_provider(repo: Path) -> None:
    _git(repo, "remote", "add", "origin", "https://gitlab.com/g/p.git")
    _git(repo, "config", "omnigent.gitprovider", "forgejo")
    url = "https://github.com/acme/repo/pull/7"
    SessionPrRegistry("session").record(
        [PullRequestRef.from_url(url)], relationship="attached", source="test"
    )

    assert pr_resource.resolve_provider(
        str(repo), session_id="session", pr_url=url
    ) == ProviderResolution("github", "github.com")
    assert pr_resource.resolve_provider(str(repo), session_id="session").provider == "github"
    assert pr_resource.resolve_provider(str(repo)).provider == "forgejo"
    with pytest.raises(ValueError, match="not associated"):
        pr_resource.resolve_provider(str(repo), session_id="session", pr_url=url.replace("7", "8"))


def test_the_github_facet_loads_without_the_panel_module() -> None:
    """The observer loads every facet, so GitHub's must not import ``github_resource``.

    Runs in a fresh interpreter so modules other tests imported cannot hide an import.
    """
    probe = (
        "import sys\n"
        "from omnigent.git_providers import load_facet\n"
        "from omnigent.runner.git_providers import PullRequestFacet\n"
        "facet = load_facet('github', 'pull_requests')\n"
        "assert isinstance(facet, PullRequestFacet), facet\n"
        f"assert facet.capabilities.to_json() == {GITHUB_CAPABILITIES!r}\n"
        "assert 'omnigent.runner.github_resource' not in sys.modules\n"
    )
    child_env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}

    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=budget(120),
    )

    assert result.returncode == 0, result.stderr
