"""``omni setup`` must credit credentials the native ``pi`` / ``codex`` CLIs already run on.

Two user journeys, each driven through the real ``omni setup`` TUI under a
pseudo-TTY with the real harness CLI on PATH and a fresh ``$HOME``:

1. Pi is signed in through its own CLI (``~/.pi/agent/auth.json``); ``pi auth
   check`` reports ``ready``. The Pi row must not read ``Not configured``.
2. Codex is configured through its own ``~/.codex/config.toml`` (a custom
   ``model_provider`` authenticating via ``env_key``, variable exported); a
   bare ``codex`` resolves that provider. The Codex row must not read ``Not
   configured``.

Usage::

    python -m pytest tests/e2e/test_setup_native_harness_credentials.py -v --timeout=300
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")
_POINTER = "❯"
_ROW_MARKERS = ("Not configured", "Not installed", "Needs upgrade", "✓")


def _fresh_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "work").mkdir(parents=True)
    (home / ".omnigent").mkdir()
    return home


def _base_env(home: Path) -> dict[str, str]:
    """Subprocess env: isolated ``HOME``/config home, ambient credentials stripped."""
    env = os.environ.copy()
    for var in list(env):
        if var.startswith(("OPENAI_", "ANTHROPIC_", "OMNIGENT_", "CODEX_", "PI_")):
            env.pop(var, None)
    env["HOME"] = str(home)
    env["OMNIGENT_CONFIG_HOME"] = str(home / ".omnigent")
    env["NO_COLOR"] = "1"
    env["TERM"] = "xterm"
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
        ]
    )
    return env


def _setup_overview_row(env: dict[str, str], name: str) -> str:
    """Run ``omni setup`` under a PTY and return the overview row for *name*."""
    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "setup"],
        env=env,
        encoding=None,
        dimensions=(40, 120),
        timeout=120,
        cwd=env["HOME"],
    )
    row_re = re.compile(rf"^\s*{_POINTER}?\s*{re.escape(name)}\s{{2,}}(.*)$")
    rows: list[str] = []
    try:
        child.expect(re.compile(rb"Configure harnesses"), timeout=120)
        collected = bytes(child.buffer or b"")
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                collected += child.read_nonblocking(size=65536, timeout=1)
            except pexpect.TIMEOUT:
                pass
            except pexpect.EOF:
                break
            text = _ANSI_RE.sub(b"", collected).decode("utf-8", "replace")
            rows = [m.group(1).strip() for m in map(row_re.match, text.splitlines()) if m]
            if any(marker in row for row in rows for marker in _ROW_MARKERS):
                break
    finally:
        try:
            child.send(b"\x1b")
            time.sleep(0.5)
            child.sendcontrol("c")
        except Exception:
            pass
        child.close(force=True)
    assert rows, f"omni setup never rendered a {name} row"
    return rows[-1]


@pytest.mark.timeout(300)
def test_pi_native_login_is_not_reported_as_unconfigured(tmp_path: Path) -> None:
    if shutil.which("pi") is None:
        pytest.skip("pi CLI is required")
    home = _fresh_home(tmp_path)
    agent_dir = home / ".pi" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "auth.json").write_text(
        json.dumps(
            {
                "anthropic": {
                    "type": "oauth",
                    "access": "fake-oauth-access-token",
                    "refresh": "fake-oauth-refresh-token",
                    "expires": int(time.time() * 1000) + 30 * 24 * 3600 * 1000,
                }
            }
        )
    )
    env = _base_env(home)

    check = subprocess.run(
        ["pi", "auth", "check", "--provider", "anthropic", "--no-refresh"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert check.returncode == 0 and "ready" in check.stdout, (
        f"bare pi does not accept the seeded login: {check.stdout!r} {check.stderr!r}"
    )

    row = _setup_overview_row(env, "Pi")
    assert "Not installed" not in row and "Needs upgrade" not in row, row
    assert "Not configured" not in row, (
        f"omni setup reports Pi as unconfigured although `pi auth check` is ready: {row!r}"
    )
    assert "✓" in row, row


@pytest.mark.timeout(300)
def test_codex_config_provider_is_not_reported_as_unconfigured(tmp_path: Path) -> None:
    if shutil.which("codex") is None:
        pytest.skip("codex CLI is required")
    home = _fresh_home(tmp_path)
    codex_dir = home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        'model_provider = "myproxy"\n'
        "\n"
        "[model_providers.myproxy]\n"
        'name = "My Proxy"\n'
        'base_url = "https://myproxy.example.com/v1"\n'
        'env_key = "MYPROXY_API_KEY"\n'
        'wire_api = "responses"\n'
    )
    env = _base_env(home)
    env["MYPROXY_API_KEY"] = "populated-proxy-token"

    version = subprocess.run(
        ["codex", "--version"], env=env, capture_output=True, text=True, timeout=60
    )
    assert version.returncode == 0, version.stderr

    row = _setup_overview_row(env, "Codex")
    assert "Not installed" not in row and "Needs upgrade" not in row, row
    assert "Not configured" not in row, (
        "omni setup reports Codex as unconfigured although codex's own config.toml "
        f"provider (env_key populated) is what a bare `codex` runs on: {row!r}"
    )
    assert "✓" in row, row
