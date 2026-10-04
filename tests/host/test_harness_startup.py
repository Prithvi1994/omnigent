"""A harness's launch command and args, as the Settings page shows them."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.host import harness_startup
from omnigent.host.harness_startup import describe_harness_startup


@pytest.fixture
def config(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    cfg: dict[str, object] = {}
    monkeypatch.setattr(harness_startup, "load_global_config", lambda: cfg)
    monkeypatch.delenv("OMNIGENT_CLAUDE_PATH", raising=False)
    monkeypatch.setattr(harness_startup, "resolve_cli_binary", lambda name: f"/found/{name}")
    return cfg


def test_defaults_to_the_harness_binary(config: dict[str, object]) -> None:
    assert describe_harness_startup("claude-native") == {
        "harness": "claude-native",
        "command": "claude",
        "command_source": "default",
        "env_var": "OMNIGENT_CLAUDE_PATH",
        "resolved_path": "/found/claude",
        "args": None,
        "env_vars": None,
        "reads_config": True,
    }


def test_config_command_and_args_with_secrets_masked(config: dict[str, object]) -> None:
    config["harness"] = {
        "claude-native": {
            "command": "/opt/claude",
            "args": [
                "--model",
                "opus",
                "--api-key",
                "sk-live",
                "--append-system-prompt",
                "be nice",
            ],
        }
    }
    startup = describe_harness_startup("claude-native")
    assert (startup["command"], startup["command_source"]) == ("/opt/claude", "config")
    assert startup["args"] == "--model opus --api-key *** --append-system-prompt 'be nice'"


def test_env_var_wins_over_config(
    config: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    config["harness"] = {"claude-native": {"command": "/opt/claude"}}
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", "/env/claude")
    startup = describe_harness_startup("claude-native")
    assert (startup["command"], startup["command_source"]) == ("/env/claude", "env")


def test_resolves_a_real_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(harness_startup, "load_global_config", dict)
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", str(binary))
    assert describe_harness_startup("claude-native")["resolved_path"] == str(binary)


def test_unwraps_an_env_wrapper_naming_but_not_showing_its_variables(
    config: dict[str, object],
) -> None:
    config["harness"] = {
        "claude-native": {
            "command": "/usr/bin/env",
            "args": ["-i", "-u", "HOME", "TOKEN=s3cret", "MODE=1", "isaac", "--"],
        }
    }
    startup = describe_harness_startup("claude-native")
    assert (startup["command"], startup["resolved_path"]) == ("isaac", "/found/isaac")
    assert (startup["args"], startup["env_vars"]) == ("--", ["TOKEN", "MODE"])
    assert "s3cret" not in str(startup)


def test_shows_an_env_wrapper_it_cannot_parse_as_is_with_secrets_masked(
    config: dict[str, object],
) -> None:
    config["harness"] = {
        "claude-native": {"command": "env", "args": ["-S", "claude --api-key synthetic-value --"]}
    }
    startup = describe_harness_startup("claude-native")
    assert (startup["command"], startup["args"], startup["env_vars"]) == (
        "env",
        "-S 'claude --api-key *** --'",
        None,
    )
    assert "synthetic-value" not in str(startup)


def test_env_wrapper_path_controls_resolution(tmp_path: Path, config: dict[str, object]) -> None:
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    config["harness"] = {
        "claude-native": {"command": "env", "args": [f"PATH={tmp_path}", "claude"]}
    }
    startup = describe_harness_startup("claude-native")
    assert (startup["resolved_path"], startup["env_vars"]) == (str(binary), ["PATH"])
    assert str(tmp_path) not in str({**startup, "resolved_path": None})


def test_codex_prefers_config_then_a_resolvable_env_var(
    tmp_path: Path, config: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
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
    assert (startup["command"], startup["command_source"], startup["args"]) == (
        "/cfg/codex",
        "config",
        "--profile x",
    )


def test_pi_ignores_global_config_its_web_launch_does_not_read(
    config: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_PI_PATH", raising=False)
    monkeypatch.delenv("HARNESS_PI_PATH", raising=False)
    config["harness"] = {"pi-native": {"command": "/cfg/pi", "args": ["--model", "x"]}}
    startup = describe_harness_startup("pi-native")
    assert (startup["command"], startup["command_source"]) == ("pi", "default")
    assert (startup["args"], startup["reads_config"]) == (None, False)


@pytest.mark.parametrize(
    "arg,secret",
    [
        ('{"apiKey": "abc"}', "abc"),
        ('{"env": {"ANTHROPIC_AUTH_TOKEN": "abc123"}}', "abc123"),
    ],
)
def test_masks_secrets_inside_structured_args(
    config: dict[str, object], arg: str, secret: str
) -> None:
    config["harness"] = {"claude-native": {"args": ["--settings", arg]}}
    startup = describe_harness_startup("claude-native")
    assert startup["args"] is not None and "--settings '{" in startup["args"]
    assert secret not in str(startup)


def test_reports_the_legacy_env_var_that_set_the_command(
    config: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OMNIGENT_PI_PATH", raising=False)
    monkeypatch.setenv("HARNESS_PI_PATH", "/legacy/pi")
    startup = describe_harness_startup("pi-native")
    assert (startup["command"], startup["command_source"], startup["env_var"]) == (
        "/legacy/pi",
        "env",
        "HARNESS_PI_PATH",
    )


def test_leaves_claude_continue_flag_alone(config: dict[str, object]) -> None:
    config["harness"] = {"claude-native": {"args": ["-c", "--model=opus"]}}
    assert describe_harness_startup("claude-native")["args"] == "-c --model=opus"


def test_harness_without_a_cli(config: dict[str, object]) -> None:
    startup = describe_harness_startup("not-a-harness")
    assert (startup["command"], startup["command_source"], startup["resolved_path"]) == (
        None,
        None,
        None,
    )
