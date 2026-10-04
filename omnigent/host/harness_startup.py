"""What a harness launch on this host runs: its binary and the names of its args.

Read-only and display-oriented. Mirrors each native harness's web launcher,
against the environment a runner actually gets (``_build_runner_env``) and the
user-level ``~/.omnigent/config.yaml``; a workspace's ``.omnigent/config.yaml``
can still override these. Only option *names* leave the host: arg values, like
the values an ``env`` wrapper sets, may hold credentials and stay here.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path

from omnigent._platform import resolve_cli_binary
from omnigent.config import load_global_config
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_startup_config import (
    _LEGACY_PATH_VARS,
    _harness_path_env_var,
    resolve_harness_config,
)
from omnigent.onboarding.harness_install import required_cli_for_harness

HarnessStartup = dict[str, str | list[str] | int | bool | None]

_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
# A long option name (``--model``) or a short one (``-c``); anything else is a value.
_LONG_OPTION = re.compile(r"--[A-Za-z0-9][A-Za-z0-9_.-]*")
_SHORT_OPTION = re.compile(r"-[A-Za-z0-9]")
_CODEX_NATIVE = "codex-native"
# Native harnesses whose web launch reads ``harness.<name>.command`` / ``args``
# (the runner's ``_auto_create_claude_terminal`` / ``_launch_codex_native_tui``).
_CONFIG_LAUNCHED = frozenset({"claude-native", _CODEX_NATIVE})


def describe_harness_startup(harness: str) -> HarnessStartup:
    """Describe the command and arg names a web launch of *harness* uses here.

    Mirrors each launcher. ``claude-native``: the ``OMNIGENT_<NAME>_PATH`` env
    var, then config ``harness.<name>.command``, then the built-in binary.
    ``codex-native``: config, then the env var when it resolves, then the
    built-in binary. Other native harnesses: the env var, then the built-in
    binary; they read neither command nor args from config. Env vars count only
    when a runner receives them. A launch wrapped in ``env NAME=value … cmd``
    reports ``cmd`` and the args after it, plus the names ``env`` sets.

    :param harness: A harness id, e.g. ``"claude-native"``.
    :returns: ``harness`` (canonical id); ``command`` and ``command_source``
        (``"env"`` / ``"config"`` / ``"default"``), both ``None`` for a harness
        without a CLI; ``env_var``, the env var that overrides the command (the
        deprecated ``HARNESS_*`` name when that one supplied it);
        ``resolved_path``, the executable the command resolves to, or ``None``
        when not found; ``arg_names``, the option names among the config args,
        e.g. ``["--model"]``, or ``None`` when none are set; ``arg_count``, how
        many config args there are; ``env_vars``, the names an ``env`` wrapper
        sets, or ``None`` without one; and ``reads_config``, whether the launch
        reads ``harness.<name>.command`` / ``args`` at all.
    """
    canonical = canonicalize_harness(harness) or harness
    reads_config = canonical in _CONFIG_LAUNCHED
    override = (
        resolve_harness_config(load_global_config())[1].get(canonical, {}) if reads_config else {}
    )
    spec = required_cli_for_harness(canonical)
    runner_env = _runner_env(canonical)
    command, source, env_var = _launch_command(
        canonical, override.get("command"), spec.binary if spec else None, runner_env
    )
    args = override.get("args") or []
    env_vars: list[str] | None = None
    search_path = runner_env.get("PATH")
    if command and (unwrapped := _unwrap_env(command, args)):
        command, args, env_vars, wrapper_path = unwrapped
        search_path = wrapper_path if wrapper_path is not None else search_path
    resolved = (
        resolve_cli_binary(command, which=lambda name: shutil.which(name, path=search_path))
        if command
        else None
    )
    return {
        "harness": canonical,
        "command": command,
        "command_source": source,
        "env_var": env_var,
        "resolved_path": resolved,
        "arg_names": _option_names(args) if args else None,
        "arg_count": len(args),
        "env_vars": env_vars,
        "reads_config": reads_config,
    }


def _runner_env(canonical: str) -> dict[str, str]:
    """The environment a runner launched from this host would get."""
    from omnigent.host.connect import _build_runner_env

    return _build_runner_env(
        os.environ,
        server_url="",
        runner_id="",
        binding_token="",
        workspace=str(Path.home()),
        parent_pid=os.getpid(),
        harness=canonical,
    )


def _launch_command(
    canonical: str,
    config_command: str | None,
    default: str | None,
    env: Mapping[str, str],
) -> tuple[str | None, str | None, str]:
    """Return ``(command, source, env_var)`` for the web launch of *canonical*."""
    env_var = _harness_path_env_var(canonical)
    if canonical == _CODEX_NATIVE:
        if config_command:
            return config_command, "config", env_var
        # Like the app server's ``_find_codex_cli``: an unresolvable override falls back.
        env_command = env.get(env_var, "").strip()
        if env_command and _is_executable(env_command, env.get("PATH")):
            return env_command, "env", env_var
    else:
        legacy_var = "HARNESS_" + env_var.removeprefix("OMNIGENT_")
        for var in (env_var, legacy_var if legacy_var in _LEGACY_PATH_VARS else None):
            if var and (env_command := env.get(var, "").strip()):
                return env_command, "env", var
        if config_command:
            return config_command, "config", env_var
    if default:
        return default, "default", env_var
    return None, None, env_var


def _is_executable(command: str, path: str | None) -> bool:
    """Whether *command* names an executable on *path* or an executable file."""
    return bool(shutil.which(command, path=path)) or (
        os.path.isfile(command) and os.access(command, os.X_OK)
    )


def _option_names(args: list[str]) -> list[str]:
    """The option names in *args*, never their values.

    E.g. ``["--model=opus", "--api-key", "k", "-c"]`` → ``["--model", "--api-key", "-c"]``.
    """
    names: list[str] = []
    for arg in args:
        if arg.startswith("--"):
            if match := _LONG_OPTION.fullmatch(arg.split("=", 1)[0]):
                names.append(match.group())
        elif match := _SHORT_OPTION.match(arg):
            names.append(match.group())  # ``-pVALUE`` → ``-p``
    return names


def _unwrap_env(
    command: str, args: list[str]
) -> tuple[str, list[str], list[str], str | None] | None:
    """Split an ``env [-i] [-u NAME] NAME=value … cmd args`` launch into its parts.

    E.g. ``("env", ["FOO=1", "isaac", "codex"])`` →
    ``("isaac", ["codex"], ["FOO"], None)``.

    :returns: ``(cmd, cmd_args, env_names, path)``, where *path* is a ``PATH``
        the wrapper sets (it decides which ``cmd`` runs; never sent off the
        host), or ``None`` when *command* isn't ``env`` or uses options this
        doesn't parse (``-S``, ``-C``, …), so the caller shows it as-is.
    """
    if os.path.basename(command) != "env":
        return None
    names: list[str] = []
    path: str | None = None
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-i", "--ignore-environment", "--"):
            index += 1
        elif arg in ("-u", "--unset"):
            index += 2
        elif arg.startswith("--unset="):
            index += 1
        elif _ENV_ASSIGNMENT.match(arg):
            name, _, value = arg.partition("=")
            names.append(name)
            if name == "PATH":
                path = value
            index += 1
        else:
            break
    if index >= len(args) or args[index].startswith("-"):
        return None
    return args[index], args[index + 1 :], names, path
