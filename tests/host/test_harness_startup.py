"""A harness's launch command and arg count, as the Settings page shows them."""

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
        "arg_count": 0,
        "env_vars": None,
    }


@pytest.mark.parametrize(
    "args",
    [
        ["--api-key=SYNTHETIC_SECRET"],
        ["--settings", '{"apiKey": "SYNTHETIC_SECRET"}'],
        ["--settings", '{"proxy":"https://u:SYNTHETIC_SECRET@example.invalid/?sig=x"}'],
        # An option-shaped value of a value-taking option, and one after ``--``.
        ["--system-prompt", "--SYNTHETIC_SECRET"],
        ["-p", "--", "--SYNTHETIC_SECRET"],
        ["-pSYNTHETIC_SECRET"],
    ],
)
def test_never_reports_args_only_their_count(config: dict[str, object], args: list[str]) -> None:
    config["harness"] = {
        "claude-native": {"args": args},
        "codex-native": {"args": ["-c", *args]},
    }
    claude = describe_harness_startup("claude-native")
    codex = describe_harness_startup("codex-native")
    assert (claude["arg_count"], codex["arg_count"]) == (len(args), len(args) + 1)
    assert "SYNTHETIC_SECRET" not in json.dumps([claude, codex])


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
    assert (startup["command"], startup["command_source"], startup["arg_count"]) == (
        "/cfg/codex",
        "config",
        2,
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
    assert startup["arg_count"] == 2
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
    assert (startup["command"], startup["arg_count"]) == ("env", 2)
    assert "synthetic-key" not in str(startup)


@pytest.mark.parametrize(
    "harness", ["pi-native", "antigravity-native", "opencode-native", "not-a-harness"]
)
def test_rejects_unsupported_harnesses(config: dict[str, object], harness: str) -> None:
    with pytest.raises(ValueError, match="launch settings aren't reported"):
        describe_harness_startup(harness)
