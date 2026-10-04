"""What a harness launch on this host runs: its binary and base args.

Read-only and display-oriented. Reads the host's environment and the user-level
``~/.omnigent/config.yaml``; a project's ``.omnigent/config.yaml`` can still
override these per workspace. Args are shell-joined with secret-looking values
masked, since users put tokens and credentialed URLs in them.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil

from omnigent._platform import resolve_cli_binary
from omnigent.config import load_global_config
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_startup_config import (
    _harness_path_env_var,
    resolve_harness_config,
    resolve_harness_path,
)
from omnigent.onboarding.harness_install import required_cli_for_harness
from omnigent.process_logging import redact_log_text

HarnessStartup = dict[str, str | list[str] | bool | None]

_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_CODEX_NATIVE = "codex-native"
# Native harnesses whose web launch reads ``harness.<name>.command`` / ``args``
# (the runner's ``_auto_create_claude_terminal`` / ``_launch_codex_native_tui``).
_CONFIG_LAUNCHED = frozenset({"claude-native", _CODEX_NATIVE})


def describe_harness_startup(harness: str) -> HarnessStartup:
    """Describe the command and base args a web launch of *harness* uses here.

    Mirrors each launcher. ``claude-native``: the ``OMNIGENT_<NAME>_PATH`` env
    var, then config ``harness.<name>.command``, then the built-in binary.
    ``codex-native``: config, then the env var when it resolves, then the
    built-in binary. Other native harnesses: the env var, then the built-in
    binary; they read neither command nor args from config. A launch wrapped in
    ``env NAME=value … cmd`` reports ``cmd`` and the args after it, plus the names
    (never the values) ``env`` sets.

    :param harness: A harness id, e.g. ``"claude-native"``.
    :returns: ``harness`` (canonical id); ``command`` and ``command_source``
        (``"env"`` / ``"config"`` / ``"default"``), both ``None`` for a harness
        without a CLI; ``env_var``, the env var that overrides the command (the
        deprecated ``HARNESS_*`` name when that one supplied it);
        ``resolved_path``, the executable the command resolves to, or ``None``
        when not found; ``args``, the masked config args, or ``None`` when none
        are set; ``env_vars``, the names an ``env`` wrapper sets, or ``None``
        without one; and ``reads_config``, whether the launch reads
        ``harness.<name>.command`` / ``args`` at all.
    """
    canonical = canonicalize_harness(harness) or harness
    reads_config = canonical in _CONFIG_LAUNCHED
    override = (
        resolve_harness_config(load_global_config())[1].get(canonical, {}) if reads_config else {}
    )
    spec = required_cli_for_harness(canonical)
    command, source, env_var = _launch_command(
        canonical, override.get("command"), spec.binary if spec else None
    )
    args = override.get("args") or []
    env_vars: list[str] | None = None
    search_path: str | None = None
    if command and (unwrapped := _unwrap_env(command, args)):
        command, args, env_vars, search_path = unwrapped
    resolved = None
    if command:
        resolved = (
            shutil.which(command, path=search_path)
            if search_path is not None
            else resolve_cli_binary(command)
        )
    return {
        "harness": canonical,
        "command": command,
        "command_source": source,
        "env_var": env_var,
        "resolved_path": resolved,
        "args": " ".join(_masked(args, codex=canonical == _CODEX_NATIVE)) if args else None,
        "env_vars": env_vars,
        "reads_config": reads_config,
    }


def _launch_command(
    canonical: str, config_command: str | None, default: str | None
) -> tuple[str | None, str | None, str]:
    """Return ``(command, source, env_var)`` for the web launch of *canonical*."""
    env_var = _harness_path_env_var(canonical)
    if canonical == _CODEX_NATIVE:
        if config_command:
            return config_command, "config", env_var
        # Like the app server's ``_find_codex_cli``: an unresolvable override falls back.
        env_command = os.environ.get(env_var, "").strip()
        if env_command and _is_executable(env_command):
            return env_command, "env", env_var
    else:
        if env_command := resolve_harness_path(canonical):
            if not os.environ.get(env_var, "").strip():
                env_var = "HARNESS_" + env_var.removeprefix("OMNIGENT_")
            return env_command, "env", env_var
        if config_command:
            return config_command, "config", env_var
    if default:
        return default, "default", env_var
    return None, None, env_var


def _is_executable(command: str) -> bool:
    """Whether *command* names an executable on ``PATH`` or an executable file."""
    return bool(shutil.which(command)) or (os.path.isfile(command) and os.access(command, os.X_OK))


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


def _masked(args: list[str], *, codex: bool) -> list[str]:
    """Mask credentials in URLs, ``NAME=value`` pairs, and keys in *args*, for display.

    E.g. ``["--api-key", "k", "--model", "opus"]`` → ``["--api-key", "***", "--model",
    "opus"]``. An arg holding a whole command line (``env -S "cmd --api-key k"``)
    has its words masked too.

    :param codex: Whether these are Codex args, whose ``-c`` takes a config
        override; other CLIs' ``-c`` (Claude's ``--continue``) is left alone.
    """
    # Lazy: keeps the Codex harness package off the host's import path until asked.
    from omnigent.harnesses.codex_native.launch_args import redact_codex_launch_args

    tokens = (
        redact_codex_launch_args(args)
        if codex
        else [redact_codex_launch_args([arg])[0] for arg in args]
    )
    masked: list[str] = []
    for arg in tokens:
        # Mask the arg whole first: splitting it would strip the quotes the
        # masker keys on (``{"apiKey": "k"}``).
        whole = redact_log_text(arg)
        if any(c.isspace() for c in whole):
            masked.append(shlex.quote(_masked_command_line(whole, codex=codex)))
        else:
            masked.append(whole)
    for index in range(1, len(masked)):
        if _names_secret(masked[index - 1]):
            masked[index] = "***"
    return masked


def _masked_command_line(arg: str, *, codex: bool) -> str:
    """Mask an arg holding a whole command line (``env -S "cmd --api-key k"``) word by word.

    Kept as-is (e.g. a JSON blob) when its words need no further masking.
    """
    try:
        words = shlex.split(arg)
    except ValueError:  # unbalanced quotes: nothing safe to show
        return "***"
    masked = _masked(words, codex=codex)
    return " ".join(masked) if masked != words else arg


def _names_secret(arg: str) -> bool:
    """Whether *arg* is a flag like ``--api-key`` or ``--token`` whose next arg is a secret."""
    if not arg.startswith("-"):
        return False
    probe = f"{arg} value"
    return redact_log_text(probe, include_whitespace_credentials=True) != probe
