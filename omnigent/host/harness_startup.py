"""What a harness launch on this host runs: its binary and how many args it passes.

Read-only and display-oriented. Mirrors Claude and Codex's native web launchers,
against the environment a runner actually gets (``_build_runner_env``) and the
user-level ``~/.omnigent/config.yaml``; a workspace's ``.omnigent/config.yaml``
can still override these. No arg leaves the host, only their count: any arg
may hold a credential or a private prompt, and no rule tells an option name
from a value for every CLI. Likewise only the names an ``env`` wrapper sets
leave, never their values.
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
    _harness_path_env_var,
    resolve_harness_config,
)
from omnigent.onboarding.harness_install import required_cli_for_harness

HarnessStartup = dict[str, str | list[str] | int | None]

_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_CODEX_NATIVE = "codex-native"
# Native harnesses whose web launch reads ``harness.<name>.command`` / ``args``
# (the runner's ``_auto_create_claude_terminal`` / ``_launch_codex_native_tui``).
_CONFIG_LAUNCHED = frozenset({"claude-native", _CODEX_NATIVE})


def describe_harness_startup(harness: str) -> HarnessStartup:
    """Describe the command a web launch of *harness* uses here, and its arg count.

    Mirrors each launcher. ``claude-native``: the ``OMNIGENT_<NAME>_PATH`` env
    var, then config ``harness.<name>.command``, then the built-in binary.
    ``codex-native``: config, then the env var when it resolves, then the
    built-in binary. Only these two harnesses are supported. Env vars count only
    when a runner receives them. A launch wrapped in ``env NAME=value … cmd``
    reports ``cmd`` and the args after it, plus the names ``env`` sets.

    :param harness: A harness id, e.g. ``"claude-native"``.
    :returns: ``harness`` (canonical id); ``command`` and ``command_source``
        (``"env"`` / ``"config"`` / ``"default"``); ``env_var``, the env var
        that overrides the command;
        ``resolved_path``, the executable the command resolves to, or ``None``
        when not found; ``arg_count``, how many config args the launch passes
        (after any ``env`` wrapper's own); ``env_vars``, the names an ``env`` wrapper
        sets, or ``None`` without one.
    :raises ValueError: If the harness's launch settings aren't supported.
    """
    canonical = canonicalize_harness(harness) or harness
    if canonical not in _CONFIG_LAUNCHED:
        raise ValueError("launch settings aren't reported for this harness")
    override = resolve_harness_config(load_global_config())[1].get(canonical, {})
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
        "arg_count": len(args),
        "env_vars": env_vars,
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
        if env_command := env.get(env_var, "").strip():
            return env_command, "env", env_var
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
