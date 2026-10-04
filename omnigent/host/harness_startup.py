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

HarnessStartup = dict[str, str | list[str] | None]

_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_CODEX_NATIVE = "codex-native"


def describe_harness_startup(harness: str) -> HarnessStartup:
    """Describe the command and base args a launch of *harness* uses here.

    The command follows the launch precedence: the ``OMNIGENT_<NAME>_PATH`` env
    var, then config ``harness.<name>.command``, then the harness's built-in
    binary. The web-launched Codex terminal skips the env var (see the runner's
    ``_launch_codex_native_tui``), so ``codex-native`` does too. A launch wrapped in
    ``env NAME=value … cmd`` reports ``cmd`` and the args after it, plus the names
    (never the values) ``env`` sets.

    :param harness: A harness id, e.g. ``"claude-native"``.
    :returns: ``harness`` (canonical id); ``command`` and ``command_source``
        (``"env"`` / ``"config"`` / ``"default"``), both ``None`` for a harness
        without a CLI; ``env_var``, the env var that overrides the command (the
        deprecated ``HARNESS_*`` name when that one supplied it);
        ``resolved_path``, the executable the command resolves to, or ``None``
        when not found; ``args``, the masked config args, or ``None`` when none
        are set; and ``env_vars``, the names an ``env`` wrapper sets, or ``None``
        without one.
    """
    canonical = canonicalize_harness(harness) or harness
    _, overrides = resolve_harness_config(load_global_config())
    override = overrides.get(canonical, {})
    spec = required_cli_for_harness(canonical)
    env_var = _harness_path_env_var(canonical)
    command: str | None
    source: str | None
    env_command = None if canonical == _CODEX_NATIVE else resolve_harness_path(canonical)
    if env_command:
        command, source = env_command, "env"
        if not os.environ.get(env_var, "").strip():
            env_var = "HARNESS_" + env_var.removeprefix("OMNIGENT_")
    elif config_command := override.get("command"):
        command, source = config_command, "config"
    elif spec is not None:
        command, source = spec.binary, "default"
    else:
        command, source = None, None
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
    }


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
        if any(c.isspace() for c in arg):
            try:
                words = " ".join(_masked(shlex.split(arg), codex=codex))
            except ValueError:  # unbalanced quotes: nothing safe to show
                words = "***"
            masked.append(shlex.quote(words))
        else:
            masked.append(redact_log_text(arg))
    for index in range(1, len(masked)):
        if _names_secret(masked[index - 1]):
            masked[index] = "***"
    return masked


def _names_secret(arg: str) -> bool:
    """Whether *arg* is a flag like ``--api-key`` or ``--token`` whose next arg is a secret."""
    if not arg.startswith("-"):
        return False
    probe = f"{arg} value"
    return redact_log_text(probe, include_whitespace_credentials=True) != probe
