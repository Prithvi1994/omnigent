"""Git provider registry: loading order, registration, facets, and URL resolution."""

from __future__ import annotations

import logging
import re
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from omnigent.git_providers import (
    EnvInstances,
    FacetModules,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
    host_of,
    load_facet,
    provider,
    providers,
    register_provider,
    reset_for_tests,
    resolve_pr_url,
    resolve_remote,
)
from omnigent.git_providers.github import PROVIDER as GITHUB
from omnigent.runner.session_prs import PullRequestRef

GITHUB_SHAPED_PR = "https://git.example.test/owner/repo/pull/7"
GITLAB_MR = "https://git.example.test/g/s/p/-/merge_requests/7"


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Start from the built-in providers, with no ambient GitHub host configuration."""
    for name in ("OMNIGENT_GIT_PROVIDER_MODULES", "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "GH_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    reset_for_tests()
    yield
    reset_for_tests()


@dataclass(frozen=True)
class FakeProvider:
    """Claims its default and configured hosts; ``any_host`` also parses unclaimed ones."""

    id: str
    display_name: str = "Fake"
    default_hosts: tuple[str, ...] = ()
    facets: FacetModules = field(default_factory=FacetModules)
    any_host: bool = False

    def matches_host(self, host: str, instances: Instances) -> bool:
        return host in self.default_hosts or host in instances.hosts_for(self.id)

    def _parsed_host(self, url: str, instances: Instances) -> str | None:
        host = host_of(url)
        if host is None or not (self.any_host or self.matches_host(host, instances)):
            return None
        return host

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        host = self._parsed_host(url, instances)
        if host is None:
            return None
        return ParsedRemote(provider=self.id, host=host, repository="fake/repo")

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        host = self._parsed_host(url, instances)
        match = re.search(r"/([1-9][0-9]*)/?$", url)
        if host is None or match is None:
            return None
        return ParsedPullRequest(
            provider=self.id, host=host, repository="fake/repo", number=int(match[1]), url=url
        )


class FakeGitLab:
    """A GitLab-shaped descriptor: merge requests of projects in nested groups."""

    id = "gitlab"
    display_name = "GitLab"
    default_hosts = ("git.example.test",)
    facets = FacetModules()

    def matches_host(self, host: str, instances: Instances) -> bool:
        return host in self.default_hosts

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        return None

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        parts = urlsplit(url)
        host = parts.hostname or ""
        match = re.fullmatch(r"/([\w.-]+(?:/[\w.-]+)+)/-/merge_requests/([1-9][0-9]*)", parts.path)
        if parts.scheme != "https" or match is None or not self.matches_host(host, instances):
            return None
        repository, number = match[1].lower(), int(match[2])
        return ParsedPullRequest(
            provider=self.id,
            host=host,
            repository=repository,
            number=number,
            url=f"https://{host}/{repository}/-/merge_requests/{number}",
        )


def _provider_module(monkeypatch: pytest.MonkeyPatch, name: str, descriptor: object) -> None:
    module = types.ModuleType(name)
    module.PROVIDER = descriptor
    monkeypatch.setitem(sys.modules, name, module)


def _ids() -> list[str]:
    return [descriptor.id for descriptor in providers()]


# ── Loading and registration ────────────────────────────────────────────────


def test_providers_load_on_first_use_and_stay_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups: list[str] = []
    module = types.ModuleType("gp_test_lazy_forge")

    def module_getattr(name: str) -> object:
        if name != "PROVIDER":
            raise AttributeError(name)
        lookups.append(name)
        return FakeProvider("lazy")

    module.__getattr__ = module_getattr
    monkeypatch.setitem(sys.modules, "gp_test_lazy_forge", module)
    # Set after the registry reset: modules are read on first use, not before.
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_MODULES", "gp_test_lazy_forge")
    assert lookups == []

    loaded = providers()

    assert [descriptor.id for descriptor in loaded] == ["github", "lazy"]
    assert loaded[0] is GITHUB
    assert providers() is loaded
    assert lookups == ["PROVIDER"]


def test_env_modules_follow_builtins_and_broken_modules_are_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _provider_module(monkeypatch, "gp_test_forge_a", FakeProvider("forge_a"))
    _provider_module(monkeypatch, "gp_test_forge_b", FakeProvider("forge_b"))
    monkeypatch.setitem(
        sys.modules, "gp_test_no_provider", types.ModuleType("gp_test_no_provider")
    )
    monkeypatch.setenv(
        "OMNIGENT_GIT_PROVIDER_MODULES",
        " gp_test_forge_a, gp_test_missing_module ,, gp_test_no_provider,gp_test_forge_b ",
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.git_providers"):
        assert _ids() == ["github", "forge_a", "forge_b"]

    warnings = [record.getMessage() for record in caplog.records]
    assert any("gp_test_missing_module" in message for message in warnings)
    assert any("gp_test_no_provider" in message for message in warnings)


def test_registered_providers_follow_env_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    _provider_module(monkeypatch, "gp_test_forge_env", FakeProvider("forge_env"))
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_MODULES", "gp_test_forge_env")
    register_provider(FakeProvider("registered"))

    assert _ids() == ["github", "forge_env", "registered"]


@pytest.mark.parametrize("loaded_first", [False, True])
def test_register_provider_replaces_an_existing_id_in_place(loaded_first: bool) -> None:
    if loaded_first:
        providers()
    register_provider(FakeProvider("gitlab"))
    replacement = FakeProvider("github", display_name="Replacement")
    register_provider(replacement)
    newer_gitlab = FakeProvider("gitlab", display_name="Newer")
    register_provider(newer_gitlab)

    assert _ids() == ["github", "gitlab"]
    assert provider("github") is replacement
    assert provider("gitlab") is newer_gitlab


def test_reset_for_tests_forgets_registered_providers() -> None:
    register_provider(FakeProvider("gitlab"))
    register_provider(FakeProvider("github", display_name="Replacement"))

    reset_for_tests()

    assert _ids() == ["github"]
    assert provider("github") is GITHUB
    assert provider("gitlab") is None


# ── Facets ──────────────────────────────────────────────────────────────────


def _register_forge_facets(**facets: str) -> None:
    register_provider(FakeProvider("forge", facets=FacetModules(**facets)))


def test_load_facet_returns_none_when_the_provider_or_facet_is_unset() -> None:
    _register_forge_facets()

    assert load_facet("no-such-provider", "pull_requests") is None
    assert load_facet("forge", "pull_requests") is None


@pytest.mark.parametrize(
    "module_path",
    ["tests.git_providers.gp_no_such_facet", "gp_no_such_package.facets.pull_requests"],
)
def test_load_facet_returns_none_when_the_facet_module_does_not_exist(module_path: str) -> None:
    _register_forge_facets(pull_requests=module_path)

    assert load_facet("forge", "pull_requests") is None


def test_load_facet_returns_the_kind_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    facet = object()
    module = types.ModuleType("gp_test_facets")
    module.PULL_REQUESTS = facet
    monkeypatch.setitem(sys.modules, "gp_test_facets", module)
    _register_forge_facets(pull_requests="gp_test_facets", credential="gp_test_facets")

    assert load_facet("forge", "pull_requests") is facet
    # The module exists but defines no CREDENTIAL attribute.
    assert load_facet("forge", "credential") is None


def test_load_facet_propagates_other_import_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "gp_test_facet_missing_dependency.py").write_text(
        "import gp_test_absent_dependency\n"
    )
    (tmp_path / "gp_test_facet_raises.py").write_text(
        "raise RuntimeError('facet import failed')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    _register_forge_facets(
        pull_requests="gp_test_facet_missing_dependency", policy="gp_test_facet_raises"
    )

    with pytest.raises(ModuleNotFoundError) as missing:
        load_facet("forge", "pull_requests")
    assert missing.value.name == "gp_test_absent_dependency"
    with pytest.raises(RuntimeError, match="facet import failed"):
        load_facet("forge", "policy")


def test_load_facet_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="webhooks"):
        load_facet("github", "webhooks")


# ── Resolution ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://GitHub.com/o/r", "github.com"),
        ("http://git.example.test:8080/o/r.git", "git.example.test"),
        ("https://user:secret@git.example.test/o/r", "git.example.test"),
        ("ssh://git@Git.Example.Test:22/o/r.git", "git.example.test"),
        ("git@Git.Example.Test:o/r.git", "git.example.test"),
        ("  https://github.com/o/r\n", "github.com"),
        ("git://github.com/o/r", None),
        ("file:///srv/git/r.git", None),
        ("/srv/git/r.git", None),
        ("github.com/o/r", None),
        ("https:///o/r", None),
        ("https://[github.com/o/r", None),
        ("not a url", None),
        ("", None),
    ],
)
def test_host_of(url: str, host: str | None) -> None:
    assert host_of(url) == host


def test_env_instances_read_the_provider_host_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OMNIGENT_GIT_PROVIDER_FORGE_HOSTS", " Git.Example.Test , ,other.example.test"
    )
    monkeypatch.delenv("OMNIGENT_GIT_PROVIDER_UNSET_HOSTS", raising=False)

    assert EnvInstances().hosts_for("forge") == {"git.example.test", "other.example.test"}
    assert EnvInstances().hosts_for("unset") == frozenset()


def test_a_provider_that_claims_the_host_wins_over_earlier_host_agnostic_ones() -> None:
    # GitHub (registered first) parses PR URLs on any host, as does "agnostic".
    register_provider(FakeProvider("agnostic", any_host=True))
    register_provider(FakeProvider("claimer", default_hosts=("git.example.test",)))

    pull_request = resolve_pr_url(GITHUB_SHAPED_PR)
    remote = resolve_remote("https://git.example.test/owner/repo.git")

    assert pull_request is not None and pull_request.provider == "claimer"
    assert remote is not None and remote.provider == "claimer"


def test_unclaimed_hosts_fall_back_to_registration_order() -> None:
    register_provider(FakeProvider("agnostic", any_host=True))
    register_provider(FakeProvider("claimer", default_hosts=("git.example.test",)))

    pull_request = resolve_pr_url("https://other.example.test/owner/repo/pull/7")
    remote = resolve_remote("https://other.example.test/owner/repo.git")

    assert pull_request is not None and pull_request.provider == "github"
    assert remote is not None and remote.provider == "agnostic"
    assert resolve_remote("not a remote") is None


def test_resolution_reads_configured_instances_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_provider(FakeProvider("claimer"))
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_CLAIMER_HOSTS", "git.example.test")

    parsed = resolve_pr_url(GITHUB_SHAPED_PR)

    assert parsed is not None and parsed.provider == "claimer"


def test_explicit_instances_replace_the_environment() -> None:
    class Configured:
        def hosts_for(self, provider_id: str) -> frozenset[str]:
            return frozenset({"git.example.test"} if provider_id == "claimer" else ())

    register_provider(FakeProvider("claimer"))

    from_env = resolve_pr_url(GITHUB_SHAPED_PR)
    configured = resolve_pr_url(GITHUB_SHAPED_PR, Configured())

    assert from_env is not None and from_env.provider == "github"
    assert configured is not None and configured.provider == "claimer"


def test_pull_request_ref_uses_a_registered_gitlab_descriptor() -> None:
    with pytest.raises(ValueError):
        PullRequestRef.from_url(GITLAB_MR)
    register_provider(FakeGitLab())

    reference = PullRequestRef.from_url(GITLAB_MR)

    assert reference.provider == "gitlab"
    assert reference.host == "git.example.test"
    assert reference.repository == "g/s/p"
    assert reference.number == 7
    assert reference.url == GITLAB_MR
