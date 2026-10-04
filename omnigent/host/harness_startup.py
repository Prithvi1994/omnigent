"""What a harness launch on this host runs: its binary and base args.

Read-only and display-oriented. Reads the host's environment and the user-level
``~/.omnigent/config.yaml``; a project's ``.omnigent/config.yaml`` can still
override these per workspace. Args are shell-joined with secret-looking values
masked, since users put tokens and credentialed URLs in them.
"""

from __future__ import annotations

import shlex

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

HarnessStartup = dict[str, str | None]


def describe_harness_startup(harness: str) -> HarnessStartup:
    """Describe the command and base args a launch of *harness* uses here.

    The command follows the launch precedence: the ``OMNIGENT_<NAME>_PATH`` env
    var, then config ``harness.<name>.command``, then the harness's built-in
    binary.

    :param harness: A harness id, e.g. ``"claude-native"``.
    :returns: ``harness`` (canonical id); ``command`` and ``command_source``
        (``"env"`` / ``"config"`` / ``"default"``), both ``None`` for a harness
        without a CLI; ``env_var`` that overrides the command; ``resolved_path``,
        the executable the command resolves to, or ``None`` when not found; and
        ``args``, the masked config args, or ``None`` when none are set.
    """
    canonical = canonicalize_harness(harness) or harness
    _, overrides = resolve_harness_config(load_global_config())
    override = overrides.get(canonical, {})
    spec = required_cli_for_harness(canonical)
    command: str | None
    source: str | None
    if env_command := resolve_harness_path(canonical):
        command, source = env_command, "env"
    elif config_command := override.get("command"):
        command, source = config_command, "config"
    elif spec is not None:
        command, source = spec.binary, "default"
    else:
        command, source = None, None
    args = override.get("args")
    return {
        "harness": canonical,
        "command": command,
        "command_source": source,
        "env_var": _harness_path_env_var(canonical),
        "resolved_path": resolve_cli_binary(command) if command else None,
        "args": _masked_args(args) if args else None,
    }


def _masked_args(args: list[str]) -> str:
    """Join *args* for display, masking credentials in URLs, ``NAME=value`` pairs, and keys.

    E.g. ``["--api-key", "k", "--model", "opus"]`` → ``"--api-key *** --model opus"``.
    """
    # Lazy: keeps the Codex harness package off the host's import path until asked.
    from omnigent.harnesses.codex_native.launch_args import redact_codex_launch_args

    masked = [redact_log_text(arg) for arg in redact_codex_launch_args(args)]
    for index in range(1, len(masked)):
        if _names_secret(masked[index - 1]):
            masked[index] = "***"
    return " ".join(shlex.quote(arg) if any(c.isspace() for c in arg) else arg for arg in masked)


def _names_secret(arg: str) -> bool:
    """Whether *arg* is a flag like ``--api-key`` or ``--token`` whose next arg is a secret."""
    if not arg.startswith("-"):
        return False
    probe = f"{arg} value"
    return redact_log_text(probe, include_whitespace_credentials=True) != probe
