"""Azure DevOps pull request facet for the session PR panel.

PR metadata, checks, comments, and changed files come from the Azure DevOps REST
API through :mod:`omnigent.runner.azure_devops_client`, which contacts only
``dev.azure.com``. The whole-PR diff and file contents come from local git when
the PR's commits are in the workspace. The observer imports this module on every
tool completion, so the REST client and ``httpx`` load inside the functions that
use them.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar
from urllib.parse import quote

from omnigent.git_providers.azure_devops import (
    AzureRepo,
    canonical_pr_url,
    parse_azure_devops_remote,
)
from omnigent.runner.git_providers import (
    CHANGED_FILE_OBJECT,
    FILE_DIFF_OBJECT,
    INFO_OBJECT,
    PR_DIFF_OBJECT,
    PR_OUTSIDE_WORKSPACE,
    ProviderCapabilities,
    PullRequestAuth,
    PullRequestFacet,
    ShellPrOp,
    ShellSegment,
)
from omnigent.runner.session_prs import PullRequestRef

if TYPE_CHECKING:
    import httpx

    from omnigent.runner.azure_devops_client import AzureDevOpsClient, AzureToken

_logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")

_PROVIDER_ID = "azure_devops"
_CAPABILITIES = ProviderCapabilities(
    account_switching=False,
    base_remote_selection=False,
    line_counts=False,
    linked_pr_diff=False,
)
_AUTH_HINT = "Run az login on the host or set AZURE_DEVOPS_EXT_PAT."
_INACCESSIBLE = "Cannot access this pull request with the Azure DevOps credentials on the host"
_PR_LOAD_FAILED = "Azure DevOps could not load the selected PR's file content"
_UNEXPECTED_RESPONSE = "Azure DevOps returned an unexpected file response"
_REVISION_LOAD_FAILED = "Azure DevOps could not load the selected file revision"
_NO_CONTEXT = "Expanded context is unavailable for this file"
_GIT_TIMEOUT_SECONDS = 30.0
# The GitHub panel's caps; the check counts stay exact.
_MAX_CHECK_RUNS = 300
_MAX_COMMENTS = 100
# Commit ids reach git as arguments, so only full SHA-1 ids are used.
_COMMIT_ID = re.compile(r"[0-9a-fA-F]{40}")
_DENIED_STATUSES = frozenset({401, 403, 404})
_PR_STATES = {"active": "OPEN", "completed": "MERGED", "abandoned": "CLOSED"}
_STATUS_BUCKETS = {
    "succeeded": "passing",
    "failed": "failing",
    "error": "failing",
    "pending": "pending",
    "notset": "pending",
}
_EVALUATION_BUCKETS = {
    "approved": "passing",
    "rejected": "failing",
    "broken": "failing",
    "queued": "pending",
    "running": "pending",
}
_NOT_APPLICABLE = "notapplicable"
_BUILD_POLICY_TYPE = "0609b952-1397-4640-95ec-e00a01b2c241"
# The panel runs git unattended, so neither git nor Git Credential Manager may prompt.
_NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}


# ── Local git ─────────────────────────────────────────────────────────────────


def _git(root: str, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    """Run ``git -C root``, or return ``None`` when git cannot start or times out."""
    try:
        return subprocess.run(
            ["git", "-C", root, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={**os.environ, **_NO_PROMPT_ENV},
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _logger.debug("azure_devops: git %s did not finish: %s", args[0], type(exc).__name__)
        return None


def _git_out(root: str, *args: str) -> str | None:
    """Return the output of a git command that succeeded, or ``None``."""
    result = _git(root, *args)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def _branch(root: str) -> str | None:
    """Return the checked-out branch (``HEAD`` when detached), or ``None`` outside a checkout."""
    out = _git_out(root, "rev-parse", "--abbrev-ref", "HEAD")
    return None if out is None else out.strip()


def _remotes(root: str) -> list[tuple[str, AzureRepo]]:
    """Return the workspace's Azure DevOps remotes as ``(name, repo)``, ``origin`` first.

    Reads the configured URLs, before any ``url.<base>.insteadOf`` rewrite.
    """
    out = _git_out(root, "config", "-z", "--get-regexp", r"^remote\..+\.url$") or ""
    urls: dict[str, str] = {}
    for entry in out.split("\0"):
        key, _, url = entry.partition("\n")
        if key.startswith("remote.") and key.endswith(".url"):
            urls.setdefault(key[len("remote.") : -len(".url")], url)
    names = sorted(urls, key=lambda name: name != "origin")
    return [
        (name, repo)
        for name in names
        if (repo := parse_azure_devops_remote(urls[name])) is not None
    ]


def _has_commits(root: str, *commits: str) -> bool:
    """Return whether every commit is in the workspace's object store."""
    return all(
        _git_out(root, "cat-file", "-e", f"{commit}^{{commit}}") is not None for commit in commits
    )


def _merge_base(root: str, base: str, head: str) -> str | None:
    """Return the merge base of two local commits, or ``None``."""
    out = _git_out(root, "merge-base", base, head)
    return (out or "").strip() or None


def _fetch(root: str, remote: str, refs: Sequence[str]) -> None:
    """Fetch branches from ``remote`` so a PR's commits become local; failures are logged."""
    result = _git(root, "fetch", "--no-tags", "--quiet", remote, *refs)
    if result is None or result.returncode != 0:
        _logger.info("azure_devops: could not fetch the pull request branches from %s", remote)


def _read_blob(root: str, ref: str, path: str) -> bytes | None:
    """Read a file at a local revision; only a confirmed absent tree entry means no content."""
    result = _git(root, "show", f"{ref}:{path}")
    if result is not None and result.returncode == 0:
        return result.stdout
    # Tree entries remain available in blobless clones even if a lazy blob fetch fails.
    tree = _git(root, "--literal-pathspecs", "ls-tree", "-z", "--full-tree", ref, "--", path)
    if tree is not None and tree.returncode == 0 and not tree.stdout:
        return None
    from omnigent.errors import ErrorCode, OmnigentError

    raise OmnigentError(
        f"Unable to read file content for {path!r} at {ref!r}. Check repository access "
        "and connectivity, then retry; a partial clone may need to fetch missing objects.",
        code=ErrorCode.INTERNAL_ERROR,
    )


def _resolve_diff_base(root: str, base: str) -> str:
    """Resolve a base branch to the merge base of ``origin/<base>`` (or ``<base>``) and HEAD.

    :raises OmnigentError: If the base is unavailable or shallow ancestry is missing.
    """
    from omnigent.errors import ErrorCode, OmnigentError

    resolved: str | None = None
    for candidate in (f"origin/{base}", base):
        if _git_out(root, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"):
            resolved = candidate
            break
    if resolved is None:
        raise OmnigentError(
            f"Diff base {base!r} is not available locally. Fetch the base branch explicitly "
            "with `git fetch origin <base>:refs/remotes/origin/<base>` (replace <base> "
            "with the branch name), then retry. Single-branch clones do not fetch "
            "other branches automatically.",
            code=ErrorCode.INVALID_INPUT,
        )
    result = _git(root, "merge-base", resolved, "HEAD")
    if result is None:
        return resolved
    merge_base = result.stdout.decode("utf-8", errors="replace").strip()
    if result.returncode == 0 and merge_base:
        return merge_base
    if result.returncode == 1:
        shallow = _git_out(root, "rev-parse", "--is-shallow-repository")
        if shallow is not None and shallow.strip() == "true":
            raise OmnigentError(
                f"No merge base found between HEAD and {resolved!r} in this shallow repository. "
                "Fetch more history with `git fetch --deepen=100 origin` or "
                "`git fetch --unshallow origin`, then retry. "
                "Include both branch refspecs if origin tracks only one branch.",
                code=ErrorCode.INVALID_INPUT,
            )
    return resolved


def _checkout_text(root: str, ref: str, path: str) -> str | None:
    """Read a file of the local checkout diff, decoding invalid UTF-8 leniently."""
    data = _read_blob(root, ref, path)
    return None if data is None else data.decode("utf-8", errors="replace")


def _pr_text(root: str, ref: str, path: str) -> str | None:
    """Read a PR file at a local revision; binary content has no expanded context."""
    data = _read_blob(root, ref, path)
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(_NO_CONTEXT) from exc
    if "\x00" in text:
        raise ValueError(_NO_CONTEXT)
    return text


# ── REST ──────────────────────────────────────────────────────────────────────


class _RestFailure(Exception):
    """A request to Azure DevOps failed.

    :ivar denied: The credentials cannot read the resource.
    :ivar timed_out: The request ran out of time.
    """

    def __init__(self, *, denied: bool = False, timed_out: bool = False) -> None:
        super().__init__("Azure DevOps request failed")
        self.denied = denied
        self.timed_out = timed_out


def _call(read: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Run one client call, raising any client or transport error as :class:`_RestFailure`."""
    import httpx

    from omnigent.runner.azure_devops_client import AzureDevOpsError

    try:
        return read(*args, **kwargs)
    except AzureDevOpsError as exc:
        _logger.debug("azure_devops: %s", exc)
        # A failed 2xx is a non-JSON body, such as the sign-in page a rejected credential gets.
        denied = exc.status in _DENIED_STATUSES or 200 <= exc.status < 300
        raise _RestFailure(denied=denied) from exc
    except httpx.TimeoutException as exc:
        raise _RestFailure(timed_out=True) from exc
    except httpx.HTTPError as exc:
        _logger.debug("azure_devops: request failed: %s", type(exc).__name__)
        raise _RestFailure() from exc


def _optional(
    read: Callable[_P, list[dict[str, Any]]], *args: _P.args, **kwargs: _P.kwargs
) -> list[dict[str, Any]]:
    """Run a list call, returning an empty list when it fails."""
    try:
        return _call(read, *args, **kwargs)
    except _RestFailure:
        return []


def _token() -> AzureToken | None:
    """Return the host's Azure DevOps credential, or ``None``."""
    from omnigent.runner.azure_devops_client import resolve_token

    return resolve_token()


def _auth(authenticated: bool) -> PullRequestAuth:
    """Return the ``auth`` block; ``az`` also counts when found at the Homebrew paths."""
    from omnigent.runner.azure_devops_client import _find_az

    return {
        "authenticated": authenticated,
        "hint": _AUTH_HINT,
        "cli": {"name": "az", "available": _find_az() is not None},
        "accounts": None,
        "selected_account": None,
    }


def _info(*, authenticated: bool, **fields: Any) -> dict[str, Any]:
    """Return an available info payload with the provider fields and ``fields``."""
    return {
        "object": INFO_OBJECT,
        "available": True,
        "provider": _PROVIDER_ID,
        "auth": _auth(authenticated),
        "capabilities": _CAPABILITIES.to_json(),
        **fields,
    }


# ── Payload shaping ───────────────────────────────────────────────────────────


def _nested(value: object, *keys: str) -> Any:
    """Follow ``keys`` through nested JSON objects; ``None`` when a level is missing."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _text(value: object) -> str | None:
    """Return ``value`` when it is a non-empty string."""
    return value if isinstance(value, str) and value else None


def _number(value: object) -> int | None:
    """Return ``value`` when it is an integer and not a bool."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _commit(value: object, key: str) -> str | None:
    """Return ``value[key].commitId`` when it is a full commit id."""
    commit = _nested(value, key, "commitId")
    return commit if isinstance(commit, str) and _COMMIT_ID.fullmatch(commit) else None


def _short_ref(ref: object) -> str | None:
    """Return a branch ref without its ``refs/heads/`` prefix."""
    return ref.removeprefix("refs/heads/") if isinstance(ref, str) and ref else None


def _reference_repo(reference: PullRequestRef) -> AzureRepo | None:
    """Return the repository of a tracked PR, whose ``repository`` is ``org/project/repo``."""
    parts = reference.repository.split("/")
    if len(parts) != 3 or not all(parts):
        return None
    org, project, repo = parts
    return AzureRepo(org, project, repo)


def _target_repo(root: str, reference: PullRequestRef | None) -> AzureRepo | None:
    """Return the reference's repository, or the workspace's for its branch PR."""
    if reference is not None:
        return _reference_repo(reference)
    remotes = _remotes(root)
    return remotes[0][1] if remotes else None


def _name(repo: AzureRepo) -> str:
    """Return ``org/project/repo``, the panel's ``repo.name_with_owner``."""
    return f"{repo.org}/{repo.project}/{repo.repo}"


def _branch_pr(
    client: AzureDevOpsClient, repo: AzureRepo, branch: str | None
) -> dict[str, Any] | None:
    """Return the branch's PR from the PR list: the newest active one, else the newest.

    PR ids grow over time, so the highest id is the newest PR.

    :raises _RestFailure: When the list cannot be read.
    """
    if branch is None or branch == "HEAD":
        return None
    found = _call(client.find_pull_requests, repo.project, repo.repo, branch)
    prs = [
        pr for pr in found if isinstance(pr, dict) and _number(pr.get("pullRequestId")) is not None
    ]
    active = [pr for pr in prs if pr.get("status") == "active"]
    return max(active or prs, key=lambda pr: pr["pullRequestId"], default=None)


def _pr_number(
    client: AzureDevOpsClient, root: str, repo: AzureRepo, reference: PullRequestRef | None
) -> int | None:
    """Return the reference's PR id, or the id of the checked-out branch's PR.

    :raises _RestFailure: When the branch's PRs cannot be listed.
    """
    if reference is not None:
        return reference.number
    listed = _branch_pr(client, repo, _branch(root))
    return None if listed is None else listed["pullRequestId"]


def _latest_iteration(iterations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the iteration with the highest id, the PR's latest push."""
    numbered = [
        it for it in iterations if isinstance(it, dict) and _number(it.get("id")) is not None
    ]
    return max(numbered, key=lambda it: it["id"], default=None)


def _status_runs(statuses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bucket the latest status of each ``genre/name`` context, dropping not-applicable ones."""
    latest: dict[tuple[str, str], tuple[tuple[int, int], dict[str, Any]]] = {}
    for index, status in enumerate(statuses):
        if not isinstance(status, dict):
            continue
        genre = _text(_nested(status, "context", "genre")) or ""
        name = _text(_nested(status, "context", "name")) or ""
        rank = (_number(status.get("id")) or 0, index)
        if (genre, name) not in latest or rank > latest[(genre, name)][0]:
            latest[(genre, name)] = (rank, status)
    runs: list[dict[str, Any]] = []
    for (genre, name), (_, status) in latest.items():
        state = str(status.get("state") or "").lower()
        if state == _NOT_APPLICABLE:
            continue
        runs.append(
            {
                "name": "/".join(part for part in (genre, name) if part) or "check",
                "bucket": _STATUS_BUCKETS.get(state, "pending"),
                "url": _text(status.get("targetUrl")),
            }
        )
    return runs


def _is_build_policy(evaluation: dict[str, Any]) -> bool:
    """Return whether a policy evaluation belongs to a build validation policy."""
    policy_type = _nested(evaluation, "configuration", "type")
    return _nested(policy_type, "id") == _BUILD_POLICY_TYPE or (
        str(_nested(policy_type, "displayName") or "").lower() == "build"
    )


def _evaluation_runs(evaluations: list[dict[str, Any]], repo: AzureRepo) -> list[dict[str, Any]]:
    """Bucket the build policy evaluations, dropping not-applicable ones."""
    runs: list[dict[str, Any]] = []
    for evaluation in evaluations:
        if not isinstance(evaluation, dict) or not _is_build_policy(evaluation):
            continue
        status = str(evaluation.get("status") or "").lower()
        if status == _NOT_APPLICABLE:
            continue
        build_id = _number(_nested(evaluation, "context", "buildId"))
        url = None
        if build_id is not None:
            org, project = (quote(name, safe="") for name in (repo.org, repo.project))
            url = f"https://dev.azure.com/{org}/{project}/_build/results?buildId={build_id}"
        runs.append(
            {
                "name": _text(_nested(evaluation, "configuration", "settings", "displayName"))
                or _text(_nested(evaluation, "context", "buildDefinitionName"))
                or "Build",
                "bucket": _EVALUATION_BUCKETS.get(status, "pending"),
                "url": url,
            }
        )
    return runs


def _summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the ``checks`` block: exact bucket counts and the capped list of runs."""
    counts = {"passing": 0, "failing": 0, "pending": 0}
    for run in runs:
        counts[run["bucket"]] += 1
    return {**counts, "total": sum(counts.values()), "runs": runs[:_MAX_CHECK_RUNS]}


def _inline_location(context: object) -> str | None:
    """Return ``path:line`` for a thread on a file, or ``None`` for a PR-level thread."""
    path = _text(_nested(context, "filePath"))
    if path is None:
        return None
    # A comment on a removed line has only a left-side position.
    line = _number(_nested(context, "rightFileStart", "line")) or _number(
        _nested(context, "leftFileStart", "line")
    )
    location = path.removeprefix("/")
    return f"{location}:{line}" if line else location


def _comments(threads: list[dict[str, Any]], pr_url: str) -> list[dict[str, Any]]:
    """Shape the threads' comments, skipping deleted and system ones, capped at 100."""
    shaped: list[dict[str, Any]] = []
    for thread in threads:
        if not isinstance(thread, dict) or thread.get("isDeleted"):
            continue
        location = _inline_location(thread.get("threadContext"))
        thread_id = _number(thread.get("id"))
        url = None if thread_id is None else f"{pr_url}?discussionId={thread_id}"
        comments = thread.get("comments")
        for comment in comments if isinstance(comments, list) else []:
            if (
                not isinstance(comment, dict)
                or comment.get("isDeleted")
                or comment.get("commentType") == "system"
            ):
                continue
            body = str(comment.get("content") or "")
            shaped.append(
                {
                    "author": _text(_nested(comment, "author", "displayName")),
                    "author_id": _text(_nested(comment, "author", "id")),
                    "body": f"`{location}`\n\n{body}" if location else body,
                    "created_at": _text(comment.get("publishedDate")),
                    "url": url,
                }
            )
            if len(shaped) >= _MAX_COMMENTS:
                return shaped
    return shaped


def _pr_payload(
    client: AzureDevOpsClient, repo: AzureRepo, pr: dict[str, Any], number: int, url: str
) -> dict[str, Any]:
    """Return the info payload's ``pr``; unreadable checks or comments come back empty."""
    runs = _status_runs(_optional(client.statuses, repo.project, repo.repo, number))
    project_id = _text(_nested(pr, "repository", "project", "id"))
    if project_id is not None:
        evaluations = _optional(client.policy_evaluations, repo.project, project_id, number)
        runs.extend(_evaluation_runs(evaluations, repo))
    description = pr.get("description")
    return {
        "number": number,
        "url": url,
        "title": pr.get("title"),
        "state": _PR_STATES.get(str(pr.get("status") or "").lower(), "OPEN"),
        "is_draft": pr.get("isDraft") is True,
        "author": _text(_nested(pr, "createdBy", "displayName")),
        "author_id": _text(_nested(pr, "createdBy", "id")),
        "base_ref": _short_ref(pr.get("targetRefName")),
        "head_ref": _short_ref(pr.get("sourceRefName")),
        "head_sha": _commit(pr, "lastMergeSourceCommit"),
        "base_sha": _commit(pr, "lastMergeTargetCommit"),
        "checks": _summarize(runs),
        "body": description if isinstance(description, str) and description.strip() else None,
        "comments": _comments(_optional(client.threads, repo.project, repo.repo, number), url),
    }


def _changed_file(change: object) -> dict[str, Any] | None:
    """Shape one iteration change entry; ``None`` for a folder or an entry without a path."""
    item = _nested(change, "item")
    path = (_text(_nested(item, "path")) or "").removeprefix("/")
    if not path or _nested(item, "isFolder") is True or _nested(item, "gitObjectType") == "tree":
        return None
    change_type = _nested(change, "changeType")
    flags = (
        {flag.strip().lower() for flag in change_type.split(",")}
        if isinstance(change_type, str)
        else set()
    )
    # A combined change such as ``edit, rename`` is a rename.
    if "rename" in flags:
        status = "renamed"
    elif "add" in flags:
        status = "created"
    elif "delete" in flags:
        status = "deleted"
    else:
        status = "modified"
    return {
        "object": CHANGED_FILE_OBJECT,
        "path": path,
        "name": path.split("/")[-1],
        "status": status,
        "lines_added": None,
        "lines_removed": None,
    }


# ── Facet ─────────────────────────────────────────────────────────────────────


class AzureDevOpsPullRequests:
    """The session PR panel for repositories on Azure DevOps Services.

    :param transport: HTTP transport for the REST client, such as an
        :class:`httpx.MockTransport` in tests. ``None`` uses the network.
    """

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Every optional panel feature is off."""
        return _CAPABILITIES

    def _client(
        self, org: str, token: AzureToken, *, timeout: float | None = None
    ) -> AzureDevOpsClient:
        from omnigent.runner.azure_devops_client import AzureDevOpsClient

        return AzureDevOpsClient(org, token, timeout=timeout, transport=self._transport)

    def workspace_info(self, root: str) -> dict[str, Any]:
        """Return the checked-out branch, the workspace's Azure DevOps repository, and its PR."""
        branch = _branch(root)
        if branch is None:
            return {"object": INFO_OBJECT, "available": False, "reason": "not_a_git_repo"}
        remotes = _remotes(root)
        repo = remotes[0][1] if remotes else None
        token = _token()
        info = _info(
            authenticated=token is not None,
            branch=branch,
            base_ref=None,
            repo=None if repo is None else {"name_with_owner": _name(repo)},
            pr=None,
        )
        if repo is None or token is None:
            return info
        try:
            with self._client(repo.org, token) as client:
                listed = _branch_pr(client, repo, branch)
                if listed is None:
                    return info
                number = listed["pullRequestId"]
                # The PR list truncates descriptions, so read the PR itself.
                pr = _call(client.get_pull_request, repo.project, repo.repo, number)
                url = canonical_pr_url(repo.org, repo.project, repo.repo, number)
                info["pr"] = _pr_payload(client, repo, pr, number, url)
        except _RestFailure as failure:
            info["auth"]["authenticated"] = not failure.denied
            return info
        info["base_ref"] = info["pr"]["base_ref"]
        return info

    def reference_info(
        self,
        root: str,  # noqa: ARG002 - the PR is read from Azure DevOps, not the checkout
        reference: PullRequestRef,
    ) -> dict[str, Any]:
        """Return the payload for one tracked PR, read by id from Azure DevOps."""
        info = _info(
            authenticated=False,
            branch=None,
            base_ref=None,
            repo={"name_with_owner": reference.repository},
            pr=None,
            selected_pr_url=reference.url,
        )
        repo = _reference_repo(reference)
        token = _token() if repo is not None else None
        if repo is None or token is None:
            return info
        try:
            with self._client(repo.org, token) as client:
                pr = _call(client.get_pull_request, repo.project, repo.repo, reference.number)
                payload = _pr_payload(client, repo, pr, reference.number, reference.url)
        except _RestFailure:
            return info
        info["auth"]["authenticated"] = True
        info.update(branch=payload["head_ref"], base_ref=payload["base_ref"], pr=payload)
        return info

    def titles_available(self, root: str) -> bool:  # noqa: ARG002 - credentials are per host
        """Return whether the host has an Azure DevOps credential."""
        return _token() is not None

    def pr_title(
        self,
        root: str,  # noqa: ARG002 - the title is read from Azure DevOps
        reference: PullRequestRef,
        deadline: float,
    ) -> tuple[str | None, bool]:
        """Read one PR's title; the request timeout is the time left before ``deadline``."""
        repo = _reference_repo(reference)
        if repo is None:
            return None, False
        if time.monotonic() >= deadline:
            return None, True
        token = _token()
        if token is None:
            return None, False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, True
        try:
            with self._client(repo.org, token, timeout=remaining) as client:
                pr = _call(client.get_pull_request, repo.project, repo.repo, reference.number)
        except _RestFailure as failure:
            # The request timeout is the time left, so a timeout means the deadline passed.
            return None, failure.timed_out
        title = pr.get("title")
        return (title.strip() or None) if isinstance(title, str) else None, False

    def verify_accessible(
        self,
        root: str,  # noqa: ARG002 - the PR is read from Azure DevOps
        reference: PullRequestRef,
    ) -> None:
        """Read the PR with the host's credential before it is attached.

        :raises ValueError: When there is no credential or the PR cannot be read.
        """
        repo = _reference_repo(reference)
        token = _token() if repo is not None else None
        if repo is None or token is None:
            raise ValueError(_INACCESSIBLE)
        try:
            with self._client(repo.org, token) as client:
                _call(client.get_pull_request, repo.project, repo.repo, reference.number)
        except _RestFailure as exc:
            raise ValueError(_INACCESSIBLE) from exc

    def on_inferred_pr(self, root: str, reference: PullRequestRef) -> None:
        """Do nothing: Azure DevOps has no per-PR preference to copy."""

    def changed_files(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        """List the files of the PR's latest iteration, compared with the merge base."""
        empty: dict[str, Any] = {"object": "list", "data": [], "has_more": False}
        repo = _target_repo(root, reference)
        token = _token() if repo is not None else None
        if repo is None or token is None:
            return empty
        try:
            with self._client(repo.org, token) as client:
                number = _pr_number(client, root, repo, reference)
                if number is None:
                    return empty
                iterations = _call(client.iterations, repo.project, repo.repo, number)
                latest = _latest_iteration(iterations)
                if latest is None:
                    return empty
                changes = _call(
                    client.iteration_changes,
                    repo.project,
                    repo.repo,
                    number,
                    latest["id"],
                    compare_to=0,
                )
        except _RestFailure:
            return empty
        files = [shaped for change in changes if (shaped := _changed_file(change)) is not None]
        return {"object": "list", "data": files, "has_more": False}

    def pr_diff(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        """Diff the PR's head against its merge base in the workspace, fetching its branches."""
        empty: dict[str, Any] = {"object": PR_DIFF_OBJECT, "patch": ""}
        remotes = _remotes(root)
        if reference is None:
            if not remotes:
                return empty
            remote, repo = remotes[0]
        else:
            wanted = reference.repository.lower()
            match = next((pair for pair in remotes if _name(pair[1]).lower() == wanted), None)
            if match is None:
                return {**empty, "unavailable_reason": PR_OUTSIDE_WORKSPACE}
            remote, repo = match
        token = _token()
        if token is None:
            return empty
        try:
            with self._client(repo.org, token) as client:
                number = _pr_number(client, root, repo, reference)
                if number is None:
                    return empty
                pr = _call(client.get_pull_request, repo.project, repo.repo, number)
        except _RestFailure:
            return empty
        head = _commit(pr, "lastMergeSourceCommit")
        base = _commit(pr, "lastMergeTargetCommit")
        if head is None or base is None:
            return empty
        if not _has_commits(root, head, base):
            refs = [
                ref
                for key in ("sourceRefName", "targetRefName")
                if isinstance(ref := pr.get(key), str) and ref.startswith("refs/heads/")
            ]
            _fetch(root, remote, refs)
            if not _has_commits(root, head, base):
                return empty
        merge_base = _merge_base(root, base, head)
        if merge_base is None:
            return empty
        patch = _git_out(
            root,
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            # Fixed prefixes keep the patch parseable whatever diff.noPrefix says.
            "--src-prefix=a/",
            "--dst-prefix=b/",
            merge_base,
            head,
        )
        return {"object": PR_DIFF_OBJECT, "patch": patch or ""}

    def file_diff(
        self,
        root: str,
        reference: PullRequestRef | None,
        path: str,
        *,
        base: str,
        previous_path: str | None,
        head_sha: str | None,
        base_sha: str | None,
    ) -> dict[str, Any]:
        """Return one file's content at the PR's merge base and head.

        Reads local commits with git and falls back to the REST API. Without a
        ``reference``, diffs the local checkout against ``base``.

        :raises ValueError: When the path is invalid, the PR moved past the shown
            revisions, or the content cannot be read.
        """
        if reference is None:
            return self._checkout_file_diff(root, base, path)
        old_path = previous_path or path
        for candidate in (path, old_path):
            if candidate.startswith("/") or any(
                part in {"", ".."} for part in candidate.split("/")
            ):
                raise ValueError("Invalid repository-relative path")
        repo = _reference_repo(reference)
        token = _token() if repo is not None else None
        if repo is None or token is None:
            raise ValueError(_PR_LOAD_FAILED)
        with self._client(repo.org, token) as client:
            try:
                pr = _call(client.get_pull_request, repo.project, repo.repo, reference.number)
            except _RestFailure as exc:
                raise ValueError(_PR_LOAD_FAILED) from exc
            current_head = _commit(pr, "lastMergeSourceCommit")
            current_base = _commit(pr, "lastMergeTargetCommit")
            if current_head is None or current_base is None:
                raise ValueError(_UNEXPECTED_RESPONSE)
            if (head_sha and head_sha != current_head) or (base_sha and base_sha != current_base):
                raise ValueError("The pull request changed; refresh before expanding context")
            if _has_commits(root, current_head, current_base):
                merge_base = _merge_base(root, current_base, current_head)
                if merge_base is not None:
                    return {
                        "object": FILE_DIFF_OBJECT,
                        "path": path,
                        "before": _pr_text(root, merge_base, old_path),
                        "after": _pr_text(root, current_head, path),
                    }
            try:
                iterations = _call(client.iterations, repo.project, repo.repo, reference.number)
            except _RestFailure as exc:
                raise ValueError(_PR_LOAD_FAILED) from exc
            # Each iteration records the merge base it was compared with.
            merge_base = _commit(_latest_iteration(iterations), "commonRefCommit")
            if merge_base is None:
                raise ValueError(_UNEXPECTED_RESPONSE)

            def contents(commit: str, filename: str) -> str | None:
                try:
                    text = _call(
                        client.item_content, repo.project, repo.repo, f"/{filename}", commit
                    )
                except _RestFailure as exc:
                    raise ValueError(_REVISION_LOAD_FAILED) from exc
                if text is not None and "\x00" in text:
                    raise ValueError(_NO_CONTEXT)
                return text

            return {
                "object": FILE_DIFF_OBJECT,
                "path": path,
                "before": contents(merge_base, old_path),
                "after": contents(current_head, path),
            }

    def _checkout_file_diff(self, root: str, base: str, path: str) -> dict[str, Any]:
        """Diff one file of the local checkout: HEAD against its merge base with ``base``."""
        resolved = base or self._branch_base(root)
        diff_base = _resolve_diff_base(root, resolved) if resolved else None
        return {
            "object": FILE_DIFF_OBJECT,
            "path": path,
            "before": _checkout_text(root, diff_base, path) if diff_base is not None else None,
            "after": _checkout_text(root, "HEAD", path),
        }

    def _branch_base(self, root: str) -> str | None:
        """Return the target branch of the checked-out branch's PR, or ``None``."""
        repo = _target_repo(root, None)
        token = _token() if repo is not None else None
        if repo is None or token is None:
            return None
        try:
            with self._client(repo.org, token) as client:
                listed = _branch_pr(client, repo, _branch(root))
        except _RestFailure:
            return None
        return None if listed is None else _short_ref(listed.get("targetRefName"))

    def set_preference(
        self,
        root: str,  # noqa: ARG002 - there is no preference to save
        reference: PullRequestRef | None,  # noqa: ARG002 - there is no preference to save
        *,
        account: str | None,
        remote: str | None,
    ) -> None:
        """Reject every choice: Azure DevOps has no account or base remote selection.

        :raises ValueError: When ``account`` or ``remote`` is given.
        """
        if account is not None or remote is not None:
            raise ValueError("Azure DevOps pull requests have no account or base remote choice")

    def shell_pr_operations(
        self,
        segments: Sequence[ShellSegment],  # noqa: ARG002 - no az repos pr command is tracked
    ) -> list[ShellPrOp]:
        """Return no operations: Azure DevOps PR shell commands are not tracked."""
        return []

    def pr_from_object(
        self,
        obj: Mapping[str, object],  # noqa: ARG002 - no Azure DevOps-specific fields are read
    ) -> PullRequestRef | None:
        """Return ``None``: only the generic ``url`` fields name Azure DevOps PRs."""
        return None

    def mcp_prs(
        self,
        tool_name: str,  # noqa: ARG002 - no Azure DevOps MCP tool is tracked
        arguments: dict[str, object],  # noqa: ARG002 - no Azure DevOps MCP tool is tracked
        result: object,  # noqa: ARG002 - no Azure DevOps MCP tool is tracked
    ) -> tuple[list[PullRequestRef], bool] | None:
        """Return ``None``: no Azure DevOps MCP tool is tracked."""
        return None


PULL_REQUESTS: PullRequestFacet = AzureDevOpsPullRequests()
