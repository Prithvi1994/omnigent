"""
Build databricks-sdk ``Config`` objects for named ``~/.databrickscfg`` profiles.

The SDK's ``databricks-cli`` strategy mints through ``databricks auth token``.
databricks-sdk releases before 0.94.0 always select that token by ``--host``,
which the CLI refuses as ambiguous once two profiles point at the same
workspace ("DEFAULT and dev match <host>. Use --profile ...") even when the
caller named the profile explicitly. :func:`sdk_config` keeps the mint pinned to
the named profile on those releases; newer releases pass ``--profile`` on their
own, so their credential chain is used unchanged.
"""

from __future__ import annotations

import importlib.metadata
import logging
from typing import Any

_logger = logging.getLogger(__name__)

# databricks-sdk mints a named profile with ``auth token --profile`` from here on.
_PROFILE_AWARE_SDK = (0, 94, 0)


def sdk_config(**kwargs: Any) -> Any:  # type: ignore[explicit-any]  # SDK Config, imported lazily
    """
    Construct a ``databricks.sdk.config.Config``.

    A named ``profile`` resolves exactly that profile's credentials, including
    on SDK releases whose CLI mint would otherwise look the token up by host.

    :param kwargs: ``Config`` keyword arguments, e.g. ``profile="dev"`` or
        ``host="https://example.databricks.com"``.
    :returns: The constructed ``Config``.
    :raises ImportError: When the ``databricks`` extra is not installed.
    :raises ValueError: When the SDK cannot resolve configuration or credentials.
    """
    from databricks.sdk.config import Config

    strategy = profile_pinned_credentials() if _sdk_mints_by_host() else None
    if strategy is None:
        return Config(**kwargs)  # type: ignore[arg-type]
    return Config(credentials_strategy=strategy, **kwargs)  # type: ignore[arg-type]


def _sdk_version() -> tuple[int, ...]:
    """Return the installed databricks-sdk version as integers, or ``()`` when unknown."""
    try:
        raw = importlib.metadata.version("databricks-sdk")
    except importlib.metadata.PackageNotFoundError:
        return ()
    parts: list[int] = []
    for piece in raw.split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    return tuple(parts)


def _sdk_mints_by_host() -> bool:
    """Whether the installed SDK's ``databricks-cli`` mint ignores ``Config.profile``."""
    version = _sdk_version()
    return not version or version < _PROFILE_AWARE_SDK


def profile_pinned_credentials() -> Any | None:  # type: ignore[explicit-any]  # SDK CredentialsStrategy
    """
    Return the SDK's default credential chain with its CLI mint pinned to ``Config.profile``.

    :returns: A fresh ``DefaultCredentials`` whose ``databricks-cli`` step mints
        with ``--profile``, or ``None`` when the installed SDK does not expose
        the chain this expects, in which case its own strategy applies.
    """
    try:
        from databricks.sdk import credentials_provider as sdk_credentials
    except ImportError:
        return None
    try:
        strategy = sdk_credentials.DefaultCredentials()
        providers = strategy._auth_providers
        index = next(
            position
            for position, provider in enumerate(providers)
            if provider.auth_type() == "databricks-cli"
        )
        providers[index] = _profile_pinned_databricks_cli(sdk_credentials)
    except (AttributeError, StopIteration, TypeError):
        _logger.debug("databricks-sdk credential chain is not customizable; using it unchanged")
        return None
    return strategy


def _profile_pinned_databricks_cli(sdk_credentials: Any) -> Any:  # type: ignore[explicit-any]
    """
    Build the ``databricks-cli`` strategy around a profile-pinned token source.

    Mirrors the SDK's own strategy apart from the command its token source runs.

    :param sdk_credentials: The imported ``databricks.sdk.credentials_provider`` module.
    :returns: An ``OauthCredentialsStrategy`` named ``databricks-cli``.
    """

    class _ProfileTokenSource(sdk_credentials.DatabricksCliTokenSource):  # type: ignore[misc]
        def __init__(self, cfg: Any) -> None:  # type: ignore[explicit-any]
            super().__init__(cfg)
            self._cmd = pin_cli_command_to_profile(self._cmd, cfg.profile)

    @sdk_credentials.oauth_credentials_strategy("databricks-cli", ["host"])
    def databricks_cli(cfg: Any) -> Any:  # type: ignore[explicit-any]
        try:
            token_source = _ProfileTokenSource(cfg)
        except FileNotFoundError as exc:
            _logger.debug("%s", exc)
            return None
        try:
            token_source.token()
        except OSError as exc:
            if "databricks OAuth is not" in str(exc):
                _logger.debug("OAuth not configured or not available: %s", exc)
                return None
            raise

        def headers() -> dict[str, str]:
            token = token_source.token()
            return {"Authorization": f"{token.token_type} {token.access_token}"}

        return sdk_credentials.OAuthCredentialsProvider(headers, token_source.token)

    return databricks_cli


def pin_cli_command_to_profile(cmd: list[str], profile: str | None) -> list[str]:
    """
    Select *profile* instead of ``--host`` in a ``databricks auth token`` command.

    :param cmd: The SDK-built command, e.g.
        ``["databricks", "auth", "token", "--host", "https://example.databricks.com"]``.
    :param profile: The ``Config.profile`` to pin, or ``None`` to leave *cmd* alone.
    :returns: *cmd* with ``--host <host>`` replaced by ``--profile <profile>``;
        unchanged when there is no profile or the command already names one.
    """
    if not profile or "--profile" in cmd or "--host" not in cmd:
        return list(cmd)
    index = cmd.index("--host")
    return [*cmd[:index], "--profile", profile, *cmd[index + 2 :]]
