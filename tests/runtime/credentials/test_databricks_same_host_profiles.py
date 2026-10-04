"""A named Databricks profile must resolve when another section shares its host.

The SDK's ``databricks-cli`` strategy mints through ``databricks auth token``.
databricks-sdk releases before 0.94.0 selected that token by ``--host``, which
the CLI refuses as ambiguous when two profiles point at the same workspace,
even though the profile was named explicitly and ``--profile`` would have
minted fine. Every omnigent path that resolves a named profile builds its SDK
``Config`` through ``sdk_config`` so that layout works on any supported SDK.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from omnigent.runtime.credentials import databricks_sdk

_WORKSPACE = "https://acme.cloud.databricks.com"
_PROFILE = "acme"

_CFG = f"""\
[DEFAULT]
host = {_WORKSPACE}
auth_type = databricks-cli

[{_PROFILE}]
host = {_WORKSPACE}
auth_type = databricks-cli
"""

# Mirrors the real CLI (v1.14.1): a host lookup matching two sections is rejected
# before any token cache is consulted; a named profile answers from the cache.
# Each invocation's arguments are appended to $FAKE_DATABRICKS_CLI_LOG as JSON.
_FAKE_CLI = f"""\
#!{sys.executable}
import configparser, json, os, sys
from datetime import datetime, timedelta, timezone


def norm(host):
    return host.strip().rstrip("/").split("://", 1)[-1].lower()


argv = sys.argv[1:]
with open(os.environ["FAKE_DATABRICKS_CLI_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
if argv[:1] == ["version"]:
    print(json.dumps({{"Version": "1.14.1", "Major": 1, "Minor": 14, "Patch": 1}}))
    sys.exit(0)
if argv[:2] != ["auth", "token"]:
    sys.exit(f"unsupported invocation: {{argv}}")
opts = {{}}
index = 2
while index < len(argv):
    arg = argv[index]
    if arg in ("--host", "--profile", "-p", "--output", "-o", "--timeout", "--account-id"):
        opts[arg.lstrip("-")] = argv[index + 1]
        index += 2
    else:
        index += 1
cfg = configparser.ConfigParser(default_section="@none@")
cfg.read(os.environ["DATABRICKS_CONFIG_FILE"])
sections = {{name: dict(cfg[name]) for name in cfg.sections()}}
profile = opts.get("profile") or opts.get("p")
host = opts.get("host")
if profile is None and host is not None:
    matches = [name for name, body in sections.items() if norm(body.get("host", "")) == norm(host)]
    if len(matches) > 1:
        sys.stderr.write(
            f"Error: {{' and '.join(matches)}} match {{host}} in ~/.databrickscfg. "
            "Use --profile to specify which profile to use\\n"
        )
        sys.exit(1)
    if not matches:
        sys.stderr.write(
            "Error: cache: databricks OAuth is not configured for this host. no cached "
            "credentials; run `databricks auth login` to sign in\\n"
        )
        sys.exit(1)
    profile = matches[0]
if profile not in sections:
    sys.stderr.write(f"Error: profile {{profile!r}} not found\\n")
    sys.exit(1)
expiry = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
print(json.dumps({{"access_token": f"tok-{{profile}}", "token_type": "Bearer", "expiry": expiry}}))
"""


@dataclass(frozen=True)
class _SignedInCli:
    path: Path
    log_path: Path

    def mint_invocations(self) -> list[list[str]]:
        """``auth token`` argument lists the CLI ran with, in order."""
        if not self.log_path.exists():
            return []
        calls = [json.loads(line) for line in self.log_path.read_text().splitlines()]
        return [argv for argv in calls if argv[:2] == ["auth", "token"]]


def _raise_offline_host_metadata(host: str) -> None:
    """Stand in for ``databricks.sdk.config.get_host_metadata`` without connecting.

    SDK releases from 0.94 onward probe the host over HTTPS while building a
    ``Config`` and retry connection failures for a long time; raising keeps
    these credential tests offline. Older releases have no such probe, so this
    is only ever installed, never called, there.
    """
    raise ConnectionError(f"offline test stub: refusing to probe {host}")


@pytest.fixture
def signed_in_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _SignedInCli:
    """Two same-host profiles plus a stand-in for a signed-in ``databricks`` CLI on PATH."""
    cfg_path = tmp_path / "databrickscfg"
    cfg_path.write_text(_CFG)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "databricks"
    # The SDK only trusts a ``databricks`` binary larger than 1 MiB.
    cli.write_text(_FAKE_CLI + "#" + "x" * (1024 * 1024) + "\n")
    cli.chmod(0o755)
    log_path = tmp_path / "databricks-cli.log"
    for name in list(os.environ):
        if name.startswith("DATABRICKS_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("FAKE_DATABRICKS_CLI_LOG", str(log_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(
        "databricks.sdk.config.get_host_metadata",
        _raise_offline_host_metadata,
        raising=False,
    )
    return _SignedInCli(path=cli, log_path=log_path)


def test_stand_in_cli_matches_real_host_lookup(signed_in_cli: _SignedInCli) -> None:
    by_host = subprocess.run(
        [str(signed_in_cli.path), "auth", "token", "--host", _WORKSPACE, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert by_host.returncode == 1
    assert f"DEFAULT and {_PROFILE} match {_WORKSPACE}" in by_host.stderr
    by_profile = subprocess.run(
        [str(signed_in_cli.path), "auth", "token", "--profile", _PROFILE, "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(by_profile.stdout)["access_token"] == f"tok-{_PROFILE}"
    version = subprocess.run(
        [str(signed_in_cli.path), "version", "--output", "json"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(version.stdout)["Major"] == 1


def test_executor_auth_uses_the_named_profile(host_minting_sdk: _SignedInCli) -> None:
    from omnigent.inner.databricks_executor import _resolve_databricks_auth

    auth, host = _resolve_databricks_auth(_PROFILE)

    assert host.rstrip("/") == _WORKSPACE
    assert auth.current_token() == f"tok-{_PROFILE}"


def test_workspace_credentials_use_the_named_profile(host_minting_sdk: _SignedInCli) -> None:
    from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

    creds = resolve_databricks_workspace(_PROFILE)

    assert (creds.host, creds.token) == (_WORKSPACE, f"tok-{_PROFILE}")


def test_credential_proxy_uses_the_named_profile(host_minting_sdk: _SignedInCli) -> None:
    from omnigent.inner.credential_proxy import DatabricksProfileTokenProvider

    provider = DatabricksProfileTokenProvider(_PROFILE)

    assert provider.workspace_url == _WORKSPACE
    assert provider.resolve() == f"tok-{_PROFILE}"


def test_token_entrypoint_uses_the_named_profile(host_minting_sdk: _SignedInCli) -> None:
    from omnigent.inner.databricks_token import _sdk_bearer

    assert _sdk_bearer(_PROFILE, _WORKSPACE) == (_WORKSPACE, f"tok-{_PROFILE}")


def test_smart_routing_profile_auth_uses_the_named_profile(host_minting_sdk: _SignedInCli) -> None:
    from omnigent.server.smart_routing import ExternalRoutingClient

    client = ExternalRoutingClient(
        base_url="https://router.example.invalid/v1",
        router_name="task_v1",
        databricks_profile=_PROFILE,
    )

    auth = client._profile_auth()

    assert auth is not None
    request = httpx.Request("POST", "https://router.example.invalid/v1/routes:select")
    assert next(auth.auth_flow(request)).headers["Authorization"] == f"Bearer tok-{_PROFILE}"


def test_llm_adapter_uses_the_named_profile(host_minting_sdk: _SignedInCli) -> None:
    from omnigent.llms.adapters.databricks import DatabricksAdapter

    assert DatabricksAdapter()._resolve_via_sdk(_PROFILE) == {
        "base_url": f"{_WORKSPACE}/serving-endpoints",
        "api_key": f"tok-{_PROFILE}",
    }


def test_opencode_gateway_uses_the_named_profile(
    host_minting_sdk: _SignedInCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.opencode_native import provider

    # Listing serving endpoints would call the workspace; the gateway's own
    # host and bearer are what this layout breaks.
    monkeypatch.setattr(provider, "_list_gateway_models", lambda _config: ())

    assert provider._databricks_bearer_token(_PROFILE) == f"tok-{_PROFILE}"
    resolution = provider.resolve_databricks_gateway(_PROFILE, model_id="databricks-acme-chat")
    assert resolution is not None
    assert (resolution.base_url, resolution.api_key) == (
        f"{_WORKSPACE}/serving-endpoints",
        f"tok-{_PROFILE}",
    )


@pytest.mark.parametrize(
    ("cmd", "profile", "expected"),
    [
        (
            ["databricks", "auth", "token", "--host", _WORKSPACE],
            _PROFILE,
            ["databricks", "auth", "token", "--profile", _PROFILE],
        ),
        (
            ["databricks", "auth", "token", "--host", _WORKSPACE, "--account-id", "acct"],
            _PROFILE,
            ["databricks", "auth", "token", "--profile", _PROFILE, "--account-id", "acct"],
        ),
        (
            ["databricks", "auth", "token", "--profile", _PROFILE],
            _PROFILE,
            ["databricks", "auth", "token", "--profile", _PROFILE],
        ),
        (
            ["databricks", "auth", "token", "-p", _PROFILE],
            _PROFILE,
            ["databricks", "auth", "token", "-p", _PROFILE],
        ),
        (
            ["databricks", "auth", "token", "--host", _WORKSPACE],
            None,
            ["databricks", "auth", "token", "--host", _WORKSPACE],
        ),
    ],
    ids=["host-to-profile", "keeps-account-id", "already-profile", "short-profile", "no-profile"],
)
def test_pin_cli_command_to_profile(
    cmd: list[str], profile: str | None, expected: list[str]
) -> None:
    assert databricks_sdk.pin_cli_command_to_profile(cmd, profile) == expected


def test_sdk_config_pins_only_sdks_that_mint_by_host(monkeypatch: pytest.MonkeyPatch) -> None:
    import databricks.sdk.config as sdk_config_mod

    constructed: list[dict[str, object]] = []

    class _RecordingConfig:
        def __init__(self, **kwargs: object) -> None:
            constructed.append(kwargs)

    monkeypatch.setattr(sdk_config_mod, "Config", _RecordingConfig)

    monkeypatch.setattr(databricks_sdk, "_sdk_version", lambda: (0, 93, 0))
    databricks_sdk.sdk_config(profile=_PROFILE)
    monkeypatch.setattr(databricks_sdk, "_sdk_version", lambda: (0, 94, 0))
    databricks_sdk.sdk_config(profile=_PROFILE)
    monkeypatch.setattr(databricks_sdk, "_sdk_version", lambda: ())
    databricks_sdk.sdk_config(profile=_PROFILE)

    pinned, stock, unknown = constructed
    assert pinned["profile"] == _PROFILE
    assert "credentials_strategy" in pinned, "pin was silently skipped: SDK chain not customizable"
    assert pinned["credentials_strategy"].auth_type() == "default"
    # A newer release and an unknown release both keep the SDK's own chain.
    assert stock == {"profile": _PROFILE}
    assert unknown == {"profile": _PROFILE}


def _force_host_minting(monkeypatch: pytest.MonkeyPatch, cli_path: Path) -> None:
    """Make the SDK's CLI token source build a pre-0.94 host-keyed mint command.

    Releases from 0.94 onward already mint by ``--profile``; forcing the host
    shape keeps the pin under test exercised whatever SDK is installed.
    """
    from databricks.sdk import credentials_provider as sdk_credentials

    original = sdk_credentials.DatabricksCliTokenSource.__init__

    def forced_init(self, cfg):
        original(self, cfg)
        self._cmd = [str(cli_path), "auth", "token", "--host", cfg.host]

    monkeypatch.setattr(sdk_credentials.DatabricksCliTokenSource, "__init__", forced_init)


@pytest.fixture
def host_minting_sdk(signed_in_cli: _SignedInCli, monkeypatch: pytest.MonkeyPatch) -> _SignedInCli:
    """A signed-in CLI whose SDK is pinned to the pre-0.94 host-keyed mint.

    A modern installed SDK already mints by ``--profile``, so a boundary test
    would pass even if its caller stopped routing through ``sdk_config`` and
    built the ``Config`` directly. Forcing the host-keyed command and an old
    reported version makes the same-host ambiguity reappear unless the pin runs,
    so these tests fail if a consumer bypasses the shared helper.
    """
    _force_host_minting(monkeypatch, signed_in_cli.path)
    monkeypatch.setattr(databricks_sdk, "_sdk_version", lambda: (0, 67, 0))
    return signed_in_cli


def test_profile_pinned_chain_mints_by_profile(
    signed_in_cli: _SignedInCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin selects the profile where the SDK's own mint would key by host."""
    from databricks.sdk import credentials_provider as sdk_credentials
    from databricks.sdk.config import Config

    _force_host_minting(monkeypatch, signed_in_cli.path)
    monkeypatch.setattr(databricks_sdk, "_sdk_version", lambda: (0, 67, 0))

    # Without the pin, the host-keyed mint hits the same-host ambiguity. The SDK
    # surfaces the CLI's rejection as a ``ValueError`` while building the config.
    with pytest.raises((ValueError, OSError), match="Use --profile"):
        Config(
            profile=_PROFILE, credentials_strategy=sdk_credentials.DefaultCredentials()
        ).authenticate()
    assert signed_in_cli.mint_invocations() == [["auth", "token", "--host", _WORKSPACE]]

    # The pinned chain mints the same profile by name instead.
    cfg = databricks_sdk.sdk_config(profile=_PROFILE)
    assert cfg.authenticate() == {"Authorization": f"Bearer tok-{_PROFILE}"}
    assert cfg.auth_type == "databricks-cli"
    assert ["auth", "token", "--profile", _PROFILE] in signed_in_cli.mint_invocations()
