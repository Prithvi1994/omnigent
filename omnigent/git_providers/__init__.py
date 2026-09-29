"""Git provider descriptors: which forge owns a remote or pull request URL.

A descriptor recognizes its forge's hosts and URLs and names the modules that
implement the rest of the provider (connection, credential, pull request, and
policy facets). Descriptors import only the standard library, so any process
can resolve a URL cheaply and import a facet module only when it needs it.

Usage::

    from omnigent.git_providers import load_facet, resolve_pr_url

    parsed = resolve_pr_url("https://github.com/owner/repo/pull/7")
    facet = load_facet(parsed.provider, "pull_requests") if parsed else None
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit

_logger = logging.getLogger(__name__)

# Built-in provider modules, in registration order. Each exposes ``PROVIDER``.
PROVIDER_MODULES: tuple[str, ...] = (
    "omnigent.git_providers.github",
    "omnigent.git_providers.azure_devops",
)
# Comma-separated extra provider modules, registered after the built-ins.
PROVIDER_MODULES_ENV = "OMNIGENT_GIT_PROVIDER_MODULES"

_FACET_KINDS = ("connection", "credential", "pull_requests", "policy")
_SCP_REMOTE_HOST = re.compile(r"^[\w.\-]+@(?P<host>[\w.\-]+):")
_URL_SCHEMES = frozenset({"http", "https", "ssh"})


@dataclass(frozen=True)
class ParsedRemote:
    """A git remote URL resolved to its provider, host, and repository path."""

    provider: str
    host: str
    repository: str


@dataclass(frozen=True)
class ParsedPullRequest:
    """A pull request URL resolved to its provider identity and canonical URL."""

    provider: str
    host: str
    repository: str
    number: int
    url: str


class Instances(Protocol):
    """Hosts configured for each provider beyond the provider's own defaults."""

    def hosts_for(self, provider_id: str) -> frozenset[str]:
        """Return the lower-cased hosts configured for *provider_id*."""
        ...


class EnvInstances:
    """Provider hosts read from ``OMNIGENT_GIT_PROVIDER_<ID>_HOSTS``.

    The value is a comma-separated host list. One configured host per provider
    is the supported case today; the list leaves room for more instances.
    """

    def hosts_for(self, provider_id: str) -> frozenset[str]:
        """Return the configured hosts for *provider_id*, stripped and lower-cased."""
        raw = os.environ.get(f"OMNIGENT_GIT_PROVIDER_{provider_id.upper()}_HOSTS", "")
        return frozenset(host for entry in raw.split(",") if (host := entry.strip().lower()))


@dataclass(frozen=True)
class FacetModules:
    """Module paths of a provider's facets, imported only by the process that uses one."""

    connection: str | None = None
    credential: str | None = None
    pull_requests: str | None = None
    policy: str | None = None


class GitProvider(Protocol):
    """A standard-library-only description of one git forge.

    The data members are read-only so a provider can declare them as plain
    class attributes or frozen dataclass fields.
    """

    @property
    def id(self) -> str:
        """Stable provider id stored with each reference, e.g. ``"github"``."""
        ...

    @property
    def display_name(self) -> str:
        """Human-readable name, e.g. ``"GitHub"``."""
        ...

    @property
    def default_hosts(self) -> tuple[str, ...]:
        """Lower-cased hosts the provider owns without configuration."""
        ...

    @property
    def facets(self) -> FacetModules:
        """Module paths of the provider's facets."""
        ...

    def matches_host(self, host: str, instances: Instances) -> bool:
        """Return whether *host* (lower-cased) belongs to this provider."""
        ...

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        """Parse a git remote URL on this provider, or return ``None``."""
        ...

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        """Parse a pull request URL on this provider, or return ``None``."""
        ...


def host_of(url: str) -> str | None:
    """Return the lower-cased host of an http(s), ssh, or scp-style remote URL.

    :param url: e.g. ``"https://github.com/o/r"``, ``"ssh://git@host/o/r"``, or
        ``"git@host:o/r.git"``.
    :returns: The host, or ``None`` when *url* has no recognizable host.
    """
    candidate = url.strip()
    scp = _SCP_REMOTE_HOST.match(candidate)
    if scp is not None:
        return scp["host"].lower()
    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme not in _URL_SCHEMES:
        return None
    return host or None


# Registry state. ``_providers`` is computed on first use; the lock is
# re-entrant because a provider module may register more providers on import.
_lock = threading.RLock()
_providers: tuple[GitProvider, ...] | None = None
_registered: dict[str, GitProvider] = {}


def _module_paths() -> list[str]:
    """Return the built-in provider modules followed by the env-configured ones."""
    extra = os.environ.get(PROVIDER_MODULES_ENV, "")
    return [*PROVIDER_MODULES, *(path for entry in extra.split(",") if (path := entry.strip()))]


def _load_module_providers() -> list[GitProvider]:
    """Import each provider module and collect its ``PROVIDER``, skipping broken modules."""
    loaded: list[GitProvider] = []
    for module_path in _module_paths():
        try:
            module = importlib.import_module(module_path)
        except ImportError:
            _logger.warning(
                "Failed to import git provider module %s; skipping",
                module_path,
                exc_info=True,
            )
            continue
        descriptor = getattr(module, "PROVIDER", None)
        if descriptor is None or not isinstance(getattr(descriptor, "id", None), str):
            _logger.warning("Git provider module %s has no usable PROVIDER; skipping", module_path)
            continue
        loaded.append(descriptor)
    return loaded


def _by_id(*descriptors: GitProvider) -> tuple[GitProvider, ...]:
    """Keep first-seen order; a later provider with the same id replaces it in place."""
    merged: dict[str, GitProvider] = {}
    for descriptor in descriptors:
        merged[descriptor.id] = descriptor
    return tuple(merged.values())


def providers() -> tuple[GitProvider, ...]:
    """Return every provider: built-ins, env modules, then registered ones.

    Provider modules are imported on the first call and the result is cached
    until :func:`reset_for_tests`.
    """
    global _providers
    with _lock:
        if _providers is None:
            _providers = _by_id(*_load_module_providers(), *_registered.values())
        return _providers


def provider(provider_id: str) -> GitProvider | None:
    """Return the provider registered under *provider_id*, or ``None``."""
    return next((p for p in providers() if p.id == provider_id), None)


def register_provider(descriptor: GitProvider) -> None:
    """Add a provider; one with an existing id replaces that provider in place."""
    global _providers
    with _lock:
        _registered[descriptor.id] = descriptor
        if _providers is not None:
            _providers = _by_id(*_providers, descriptor)


def reset_for_tests() -> None:
    """Forget loaded and registered providers so the next use reloads them."""
    global _providers
    with _lock:
        _providers = None
        _registered.clear()


def _is_module_or_parent(name: str | None, module_path: str) -> bool:
    """Return whether *name* is *module_path* or one of its parent packages."""
    return name is not None and (name == module_path or module_path.startswith(f"{name}."))


def load_facet(provider_id: str, kind: str) -> Any | None:
    """Import a provider facet and return its ``kind.upper()`` attribute.

    :param provider_id: e.g. ``"github"``.
    :param kind: One of ``connection``, ``credential``, ``pull_requests``, or
        ``policy``.
    :returns: The facet object, or ``None`` when the provider or facet is
        unset, the facet module does not exist, or it lacks the attribute.
    :raises ValueError: If *kind* is not a facet kind.
    """
    if kind not in _FACET_KINDS:
        raise ValueError(f"Unknown git provider facet kind: {kind!r}")
    descriptor = provider(provider_id)
    module_path = getattr(descriptor.facets, kind) if descriptor is not None else None
    if not isinstance(module_path, str) or not module_path:
        return None
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        # A missing dependency inside an existing facet module is a real error.
        if not _is_module_or_parent(exc.name, module_path):
            raise
        return None
    return getattr(module, kind.upper(), None)


def _candidates(url: str, instances: Instances) -> Iterator[GitProvider]:
    """Yield providers that claim the URL's host, then the rest, in registration order."""
    host = host_of(url)
    others: list[GitProvider] = []
    for descriptor in providers():
        if host is not None and descriptor.matches_host(host, instances):
            yield descriptor
        else:
            others.append(descriptor)
    yield from others


def resolve_remote(url: str, instances: Instances | None = None) -> ParsedRemote | None:
    """Resolve a git remote URL with the first provider that parses it.

    :param url: A git remote URL, e.g. ``"git@github.com:o/r.git"``.
    :param instances: Configured provider hosts; defaults to :class:`EnvInstances`.
    :returns: The parsed remote, or ``None`` when no provider recognizes it.
    """
    instances = EnvInstances() if instances is None else instances
    for descriptor in _candidates(url, instances):
        parsed = descriptor.parse_remote_url(url, instances)
        if parsed is not None:
            return parsed
    return None


def resolve_pr_url(url: str, instances: Instances | None = None) -> ParsedPullRequest | None:
    """Resolve a pull request URL with the first provider that parses it.

    :param url: A pull request URL, e.g. ``"https://github.com/o/r/pull/7"``.
    :param instances: Configured provider hosts; defaults to :class:`EnvInstances`.
    :returns: The parsed pull request, or ``None`` when no provider recognizes it.
    """
    instances = EnvInstances() if instances is None else instances
    for descriptor in _candidates(url, instances):
        parsed = descriptor.parse_pr_url(url, instances)
        if parsed is not None:
            return parsed
    return None
