"""GitHub provider descriptor, covering github.com and GitHub Enterprise Server."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from omnigent.git_providers import (
    FacetModules,
    GitProvider,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
)

# An unindented key in the gh CLI's hosts.yml names a host gh is signed in to.
_HOSTS_YML_KEY = re.compile(r"^([A-Za-z0-9.-]+):\s*$")
_SCP_REMOTE = re.compile(r"^[\w.\-]+@(?P<host>[\w.\-]+):(?P<path>.+)$")
_URL_REMOTE = re.compile(r"^\w+://(?:[^@/]+@)?(?P<host>[\w.\-]+)/(?P<path>.+)$")


def _valid_hostname(host: str) -> bool:
    """Validate bounded ASCII DNS labels without hostname regex backtracking."""
    if len(host) > 253 or not host.isascii():
        return False
    labels = host.split(".")
    return (
        len(labels) > 1
        and all(
            1 <= len(label) <= 63
            and label[0].isalnum()
            and label[-1].isalnum()
            and label.replace("-", "").isalnum()
            for label in labels
        )
        and labels[-1][0].isalpha()
    )


def _gh_config_dir() -> Path:
    """The gh CLI config dir (``GH_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/gh``)."""
    override = (os.environ.get("GH_CONFIG_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "gh"


def _gh_signed_in_hosts() -> frozenset[str]:
    """Return the lower-cased top-level host keys of gh's ``hosts.yml``, if readable."""
    try:
        text = (_gh_config_dir() / "hosts.yml").read_text(encoding="utf-8", errors="replace")
    except (OSError, RuntimeError):
        # RuntimeError: Path.home() found no home directory.
        return frozenset()
    return frozenset(
        match[1].lower() for line in text.splitlines() if (match := _HOSTS_YML_KEY.match(line))
    )


class GitHubProvider:
    """github.com, plus GitHub Enterprise Server hosts that gh or Omnigent knows."""

    id = "github"
    display_name = "GitHub"
    default_hosts = ("github.com",)
    facets = FacetModules(
        pull_requests="omnigent.runner.git_providers.github",
    )

    def matches_host(self, host: str, instances: Instances) -> bool:
        """Claim github.com, ``GH_HOST``, configured instances, and gh's signed-in hosts.

        GitHub Enterprise Server users have their host in ``GH_HOST`` or in gh's
        ``hosts.yml``, so their remotes keep resolving to GitHub.
        """
        host = host.lower()
        if not host:
            return False
        if host in self.default_hosts:
            return True
        if host == (os.environ.get("GH_HOST") or "").strip().lower():
            return True
        if host in {configured.lower() for configured in instances.hosts_for(self.id)}:
            return True
        return host in _gh_signed_in_hosts()

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        """Parse an HTTPS, SSH, or scp-style remote on a GitHub host to ``owner/repo``."""
        candidate = url.strip()
        match = _SCP_REMOTE.match(candidate) or _URL_REMOTE.match(candidate)
        if match is None:
            return None
        parts = match["path"].removesuffix(".git").strip("/").split("/")
        if len(parts) < 2 or not parts[-1] or not parts[-2]:
            return None
        host = match["host"].lower()
        if not self.matches_host(host, instances):
            return None
        return ParsedRemote(provider=self.id, host=host, repository=f"{parts[-2]}/{parts[-1]}")

    def parse_pr_url(
        self,
        url: str,
        instances: Instances,  # noqa: ARG002 - GitHub PR URLs parse on any host
    ) -> ParsedPullRequest | None:
        """Normalize an HTTPS GitHub PR URL, rejecting non-PR and credential-bearing URLs."""
        try:
            parsed = urlsplit(url.strip())
            host = (parsed.hostname or "").lower()
        except ValueError:
            return None
        match = re.fullmatch(
            r"/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)(?:/(?:files|commits|checks))?/?",
            parsed.path,
            flags=re.ASCII,
        )
        if (
            parsed.scheme != "https"
            or not _valid_hostname(host)
            or parsed.netloc.lower() != host
            or match is None
        ):
            return None
        repository = match[1].lower()
        if any(part in {".", ".."} for part in repository.split("/")):
            return None
        try:
            number = int(match[2])
        except ValueError:
            # The number exceeds Python's integer string conversion limit.
            return None
        return ParsedPullRequest(
            provider=self.id,
            host=host,
            repository=repository,
            number=number,
            url=f"https://{host}/{repository}/pull/{number}",
        )


PROVIDER: GitProvider = GitHubProvider()
