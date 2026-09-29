"""GitHub descriptor: PR URL identity, remote host matching, and registry compatibility."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.git_providers import (
    EnvInstances,
    ParsedPullRequest,
    ParsedRemote,
    provider,
    reset_for_tests,
    resolve_pr_url,
    resolve_remote,
)
from omnigent.git_providers.github import PROVIDER
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry

A = "https://github.com/example/one/pull/42"
ENTERPRISE_REMOTE = "https://ghe.example.test/o/r.git"
GITHUB_REMOTE = ParsedRemote(provider="github", host="github.com", repository="o/r")
ENTERPRISE = ParsedRemote(provider="github", host="ghe.example.test", repository="o/r")
_MAX_DNS_HOST = ".".join(["a" * 63] * 3 + ["a" * 61])


@pytest.fixture(autouse=True)
def gh_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Hide ambient GitHub host configuration; yield an empty gh config dir."""
    for name in (
        "OMNIGENT_GIT_PROVIDER_MODULES",
        "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS",
        "GH_HOST",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    config_dir = tmp_path / "gh"
    config_dir.mkdir()
    monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))
    reset_for_tests()
    yield config_dir
    reset_for_tests()


def test_descriptor_is_the_registered_github_provider() -> None:
    assert provider("github") is PROVIDER
    assert PROVIDER.id == "github"
    assert PROVIDER.display_name == "GitHub"
    assert PROVIDER.default_hosts == ("github.com",)
    assert PROVIDER.facets.pull_requests == "omnigent.runner.git_providers.github"


# ── Pull request URLs ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "host", "repository", "number"),
    [
        (A, "github.com", "example/one", 42),
        ("https://GITHUB.COM/EXAMPLE/ONE/pull/42/files#diff", "github.com", "example/one", 42),
        ("https://github.com/example/one/pull/42/", "github.com", "example/one", 42),
        ("https://github.com/example/one/pull/42/commits", "github.com", "example/one", 42),
        ("https://github.com/example/one/pull/42/checks/", "github.com", "example/one", 42),
        ("https://github.com/example/one/pull/42?notification=1", "github.com", "example/one", 42),
        ("  https://github.com/example/one/pull/42\n", "github.com", "example/one", 42),
        ("https://github.com/Example/My.Repo-1/pull/7", "github.com", "example/my.repo-1", 7),
        (
            "https://git-2.example.internal/example/one/pull/42",
            "git-2.example.internal",
            "example/one",
            42,
        ),
        pytest.param(
            f"https://{_MAX_DNS_HOST}/example/one/pull/42",
            _MAX_DNS_HOST,
            "example/one",
            42,
            id="maximum-dns-length",
        ),
        ("https://ghe.example.test/o/r/pull/3", "ghe.example.test", "o/r", 3),
    ],
)
def test_accepted_pr_urls_keep_their_identity(
    url: str, host: str, repository: str, number: int
) -> None:
    expected = ParsedPullRequest(
        provider="github",
        host=host,
        repository=repository,
        number=number,
        url=f"https://{host}/{repository}/pull/{number}",
    )

    assert PROVIDER.parse_pr_url(url, EnvInstances()) == expected
    assert resolve_pr_url(url) == expected
    reference = PullRequestRef.from_url(url)
    assert (
        reference.provider,
        reference.host,
        reference.repository,
        reference.number,
        reference.url,
    ) == ("github", host, repository, number, expected.url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/one/issues/42",
        "file:///example/one/pull/42",
        "https://token@github.com/example/one/pull/42",
        "https://localhost/example/one/pull/42",
        "https://github.com/../one/pull/42",
        "https://github.com/./one/pull/42",
        "https://github.com/example/one/pull/0",
        "http://github.com/example/one/pull/42",
        "https://github.com:443/example/one/pull/42",
        "https://github.com/example/one/pull/42/extra",
        "https://github.com/example/pull/42",
        "https://[github.com/example/one/pull/42",
        "https://127.0.0.1/example/one/pull/42",
        "https://git..example.com/example/one/pull/42",
        "https://-git.example.com/example/one/pull/42",
        "https://git-.example.com/example/one/pull/42",
        "https://git_host.example.com/example/one/pull/42",
        "https://github.com./example/one/pull/42",
        "https://gíthub.com/example/one/pull/42",
        pytest.param(f"https://{'a' * 64}.example.com/example/one/pull/42", id="overlong-label"),
        pytest.param(
            f"https://{'.'.join(['a' * 63] * 4)}/example/one/pull/42", id="overlong-host"
        ),
        pytest.param(f"https://{'0' * 100_000}/example/one/pull/42", id="large-numeric-host"),
        pytest.param(f"https://github.com/example/one/pull/{'9' * 5000}", id="huge-number"),
        "not a url",
        "",
    ],
)
def test_rejected_pr_urls_stay_rejected(url: str) -> None:
    assert PROVIDER.parse_pr_url(url, EnvInstances()) is None
    assert resolve_pr_url(url) is None
    with pytest.raises(ValueError):
        PullRequestRef.from_url(url)


# ── Remotes ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url", ["https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r"]
)
def test_github_remotes_parse(url: str) -> None:
    assert PROVIDER.parse_remote_url(url, EnvInstances()) == GITHUB_REMOTE
    assert resolve_remote(url) == GITHUB_REMOTE


def test_host_matching_ignores_case() -> None:
    assert PROVIDER.matches_host("GitHub.COM", EnvInstances())
    assert resolve_remote("git@GitHub.com:o/r.git") == GITHUB_REMOTE


@pytest.mark.parametrize("url", [ENTERPRISE_REMOTE, "https://gitlab.com/g/p.git"])
def test_other_hosts_are_not_github_remotes(url: str) -> None:
    assert PROVIDER.parse_remote_url(url, EnvInstances()) is None
    assert resolve_remote(url) is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GH_HOST", "ghe.example.test"),
        ("GH_HOST", " GHE.Example.Test "),
        ("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "ghe.example.test"),
        ("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "other.example.test, GHE.example.test"),
    ],
)
def test_enterprise_remote_parses_with_a_configured_host(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    assert PROVIDER.parse_remote_url(ENTERPRISE_REMOTE, EnvInstances()) == ENTERPRISE
    assert resolve_remote(ENTERPRISE_REMOTE) == ENTERPRISE
    assert resolve_remote("https://gitlab.com/g/p.git") is None


def test_enterprise_remote_parses_with_a_gh_hosts_file(gh_config_dir: Path) -> None:
    (gh_config_dir / "hosts.yml").write_text(
        "github.com:\n"
        "    users:\n"
        "        alice:\n"
        "    user: alice\n"
        "ghe.example.test:\n"
        "    git_protocol: https\n"
        "    nested.example.test:\n"
        "# commented.example.test:\n"
        "inline.example.test: {}\n",
        encoding="utf-8",
    )

    assert PROVIDER.parse_remote_url(ENTERPRISE_REMOTE, EnvInstances()) == ENTERPRISE
    assert resolve_remote(ENTERPRISE_REMOTE) == ENTERPRISE
    # Only unindented keys name hosts.
    for host in ("nested.example.test", "commented.example.test", "inline.example.test", "user"):
        assert not PROVIDER.matches_host(host, EnvInstances())


@pytest.mark.parametrize("location", ["xdg", "home"])
def test_hosts_file_falls_back_to_the_xdg_and_home_config_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, location: str
) -> None:
    monkeypatch.delenv("GH_CONFIG_DIR")
    if location == "xdg":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        config_dir = tmp_path / "xdg" / "gh"
    else:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        config_dir = tmp_path / "home" / ".config" / "gh"
    config_dir.mkdir(parents=True)
    assert not PROVIDER.matches_host("ghe.example.test", EnvInstances())

    (config_dir / "hosts.yml").write_text("ghe.example.test:\n    user: bob\n", encoding="utf-8")

    assert PROVIDER.matches_host("ghe.example.test", EnvInstances())


def test_unreadable_hosts_file_is_ignored(gh_config_dir: Path) -> None:
    (gh_config_dir / "hosts.yml").mkdir()

    assert not PROVIDER.matches_host("ghe.example.test", EnvInstances())
    assert resolve_remote(ENTERPRISE_REMOTE) is None


# ── Session PR registry ─────────────────────────────────────────────────────


def test_registry_entries_without_provider_load_as_github(tmp_path: Path) -> None:
    store = SessionPrRegistry("conv_legacy", root=tmp_path)
    legacy_entry = {
        "host": "github.com",
        "repository": "example/one",
        "number": 42,
        "url": A,
        "relationship": "created",
        "source": "test",
        "first_seen_at": 10,
        "last_seen_at": 10,
    }
    store.path.write_text(json.dumps({"schema_version": 1, "prs": [legacy_entry]}))

    [entry] = store.list()

    assert entry.provider == "github"
    assert entry.url == A


def test_registry_records_the_provider(tmp_path: Path) -> None:
    store = SessionPrRegistry("conv_new", root=tmp_path)

    store.record([PullRequestRef.from_url(A)], relationship="created", source="test")

    assert json.loads(store.path.read_text())["prs"][0]["provider"] == "github"
