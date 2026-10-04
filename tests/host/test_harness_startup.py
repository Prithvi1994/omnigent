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


def test_harness_without_a_cli(config: dict[str, object]) -> None:
    startup = describe_harness_startup("not-a-harness")
    assert (startup["command"], startup["command_source"], startup["resolved_path"]) == (
        None,
        None,
        None,
    )
