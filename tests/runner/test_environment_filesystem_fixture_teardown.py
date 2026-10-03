"""The runner filesystem test fixtures stop their os_env helper subprocesses at teardown."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

# Reuses the real fixtures so the test exercises the fixtures under test,
# not copies of them.
_TOUCH_FILESYSTEM = """
from omnigent.entities import DEFAULT_ENVIRONMENT_ID
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from tests.runner.test_environment_filesystem import (  # noqa: F401
    app,
    client,
    glob_client,
    glob_workspace,
    make_os_env,
    registry,
    workspace,
)

_FILESYSTEM = f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/filesystem"


async def test_touch_registry_filesystem(client):
    assert (await client.get(_FILESYSTEM)).status_code == 200


async def test_touch_glob_filesystem(glob_client):
    assert (await glob_client.get(_FILESYSTEM)).status_code == 200


async def test_touch_factory_env(make_os_env, tmp_path):
    os_env = make_os_env(
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert (await os_env.shell("true"))["exit_code"] == 0
"""


def test_fixture_teardown_stops_the_helper(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytester.makepyfile(test_touch_filesystem=_TOUCH_FILESYSTEM)

    probe_log = tmp_path / "probe.jsonl"
    repo_root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([repo_root, os.environ.get("PYTHONPATH", "")])
    )
    # Autoload would pull in every installed plugin (pytest-playwright,
    # structlog); the inner run needs only asyncio and the probe.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("OMNIGENT_FIXTURE_PROBE_LOG", str(probe_log))

    result = pytester.runpytest_subprocess(
        "-p",
        "asyncio",
        "-p",
        "tests.runner._os_env_fixture_teardown_probe",
        "-o",
        "asyncio_mode=auto",
    )
    result.assert_outcomes(passed=3)

    records = [
        json.loads(line)
        for line in probe_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {r["fixture"] for r in records} == {"registry", "glob_client", "make_os_env"}, records
    leaked = sorted(
        {(r["fixture"], pid) for r in records for pid in r["helpers_alive_after_teardown"]}
    )
    assert not leaked, f"os_env helper processes outlived their fixture: {leaked}"
