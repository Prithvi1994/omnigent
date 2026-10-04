"""A harness's launch command and arg names, as the Settings page shows them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.host import harness_startup
from omnigent.host.harness_startup import describe_harness_startup


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    cfg: dict[str, object] = {}
    monkeypatch.setattr(harness_startup, "load_global_config", lambda: cfg)
    for var in (
        "OMNIGENT_CLAUDE_PATH",
        "OMNIGENT_CODEX_PATH",
        "OMNIGENT_PI_PATH",
        "HARNESS_PI_PATH",
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(harness_startup, "resolve_cli_binary", lambda name, **_: f"/found/{name}")
    return cfg


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_defaults_to_the_harness_binary(config: dict[str, object]) -> None:
    assert describe_harness_startup("claude-native") == {
        "harness": "claude-native",
        "command": "claude",
        "command_source": "default",
        "env_var": "OMNIGENT_CLAUDE_PATH",
        "resolved_path": "/found/claude",
        "arg_names": None,
        "arg_count": 0,
        "env_vars": None,
        "reads_config": True,
    }


@pytest.mark.parametrize(
    "secret_arg",
    [
        "--api-key=synthetic-key",
        '{"apiKey": "synthetic-key"}',
        '{"proxy":"https://fake-user:fake-pass@example.invalid/?sig=fake-signature"}',
        "claude --api-key synthetic-key --",
        "-psynthetic-key",
    ],
)
def test_reports_option_names_never_arg_values(config: dict[str, object], secret_arg: str) -> None:
    config["harness"] = {
        "claude-native": {"args": ["--model", "opus", "--settings", secret_arg]},
        "codex-native": {"args": ["-c", secret_arg]},
    }
    claude = describe_harness_startup("claude-native")
    codex = describe_harness_startup("codex-native")
    assert claude["arg_count"] == 4 and codex["arg_count"] == 2
    payload = json.dumps([claude, codex])
    for fragment in ("synthetic-key", "fake-pass", "fake-signature", "opus"):
        assert fragment not in payload
    assert claude["arg_names"] is not None and claude["arg_names"][:2] == ["--model", "--settings"]


def test_option_names_drop_attached_values() -> None:
    names = harness_startup._option_names(
        ["--model=opus", "--api-key", "k", "-pSECRET", "-c", "--", "codex", "-"]
    )
    assert names == ["--model", "--api-key", "-p", "-c"]


def test_config_command_for_claude(config: dict[str, object]) -> None:
    config["harness"] = {"claude-native": {"command": "/opt/claude"}}
    startup = describe_harness_startup("claude-native")
    assert (startup["command"], startup["command_source"]) == ("/opt/claude", "config")


def test_env_var_counts_only_when_runners_receive_it(
    config: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    config["harness"] = {"claude-native": {"command": "/opt/claude"}}
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", "/env/claude")
    # The host doesn't forward OMNIGENT_CLAUDE_PATH to runners by default.
    startup = describe_harness_startup("claude-native")
    assert (startup["command"], startup["command_source"]) == ("/opt/claude", "config")
    monkeypatch.setenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "OMNIGENT_CLAUDE_PATH")
    startup = describe_harness_startup("claude-native")
    assert (startup["command"], startup["command_source"]) == ("/env/claude", "env")


def test_reports_the_legacy_env_var_that_set_the_command(
    config: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_PI_PATH", "/legacy/pi")
    monkeypatch.setenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "HARNESS_PI_PATH")
    startup = describe_harness_startup("pi-native")
    assert (startup["command"], startup["command_source"], startup["env_var"]) == (
        "/legacy/pi",
        "env",
        "HARNESS_PI_PATH",
    )


def test_resolves_a_real_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = _executable(tmp_path / "claude")
    monkeypatch.setattr(harness_startup, "load_global_config", dict)
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", str(binary))
    monkeypatch.setenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "OMNIGENT_CLAUDE_PATH")
    assert describe_harness_startup("claude-native")["resolved_path"] == str(binary)


def test_codex_prefers_config_then_a_resolvable_env_var(
    tmp_path: Path, config: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _executable(tmp_path / "codex")
    # OMNIGENT_CODEX_PATH is on the host's runner allowlist.
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", str(binary))
    startup = describe_harness_startup("codex-native")
    assert (startup["command"], startup["command_source"]) == (str(binary), "env")
    # The app server falls back to `codex` when the override doesn't resolve.
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", str(tmp_path / "missing"))
    startup = describe_harness_startup("codex-native")
    assert (startup["command"], startup["command_source"]) == ("codex", "default")
    # A config command wins over the env var for the web-launched terminal.
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", str(binary))
    config["harness"] = {"codex-native": {"command": "/cfg/codex", "args": ["--profile", "x"]}}
    startup = describe_harness_startup("codex-native")
    assert (startup["command"], startup["command_source"], startup["arg_names"]) == (
        "/cfg/codex",
        "config",
        ["--profile"],
    )


def test_pi_ignores_global_config_its_web_launch_does_not_read(
    config: dict[str, object],
) -> None:
    config["harness"] = {"pi-native": {"command": "/cfg/pi", "args": ["--model", "x"]}}
    startup = describe_harness_startup("pi-native")
    assert (startup["command"], startup["command_source"]) == ("pi", "default")
    assert (startup["arg_names"], startup["arg_count"], startup["reads_config"]) == (
        None,
        0,
        False,
    )


def test_unwraps_an_env_wrapper_naming_but_not_showing_its_variables(
    config: dict[str, object],
) -> None:
    config["harness"] = {
        "claude-native": {
            "command": "/usr/bin/env",
            "args": ["-i", "-u", "HOME", "TOKEN=s3cret", "MODE=1", "isaac", "--model", "x"],
        }
    }
    startup = describe_harness_startup("claude-native")
    assert (startup["command"], startup["resolved_path"]) == ("isaac", "/found/isaac")
    assert (startup["arg_names"], startup["arg_count"]) == (["--model"], 2)
    assert startup["env_vars"] == ["TOKEN", "MODE"]
    assert "s3cret" not in str(startup)


def test_env_wrapper_path_controls_resolution(
    tmp_path: Path, config: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent._platform import resolve_cli_binary

    monkeypatch.setattr(harness_startup, "resolve_cli_binary", resolve_cli_binary)
    binary = _executable(tmp_path / "claude")
    config["harness"] = {
        "claude-native": {"command": "env", "args": [f"PATH={tmp_path}", "claude"]}
    }
    startup = describe_harness_startup("claude-native")
    assert (startup["resolved_path"], startup["env_vars"]) == (str(binary), ["PATH"])
    assert str(tmp_path) not in str({**startup, "resolved_path": None})


def test_shows_an_env_wrapper_it_cannot_parse_as_is(config: dict[str, object]) -> None:
    config["harness"] = {
        "claude-native": {"command": "env", "args": ["-S", "claude --api-key synthetic-key --"]}
    }
    startup = describe_harness_startup("claude-native")
    assert (startup["command"], startup["arg_names"], startup["arg_count"]) == ("env", ["-S"], 2)
    assert "synthetic-key" not in str(startup)


def test_harness_without_a_cli(config: dict[str, object]) -> None:
    startup = describe_harness_startup("not-a-harness")
    assert (startup["command"], startup["command_source"], startup["resolved_path"]) == (
        None,
        None,
        None,
    )
