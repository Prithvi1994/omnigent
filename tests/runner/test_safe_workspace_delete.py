"""Workspace delete confinement and confirmed-completion regressions."""

from __future__ import annotations

import io
import json
import os
import shutil
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.inner import os_env as os_env_module
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import CallerProcessOSEnvironment, create_os_environment
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runner.transports.ws_tunnel.serve import _send_hello
from tests.runner.helpers import NullServerClient

CAPABILITY = "workspace_delete_nofollow_v1"
FS_URL = "/v1/sessions/conv_test/resources/environments/default/filesystem"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "victim").write_bytes(b"workspace bytes")
    return root


@pytest.fixture
def environment(workspace: Path) -> Iterator[CallerProcessOSEnvironment]:
    env = create_os_environment(
        OSEnvSpec(cwd=str(workspace), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    yield env
    env.close()


@pytest.fixture
async def client(
    environment: CallerProcessOSEnvironment, workspace: Path
) -> AsyncIterator[httpx.AsyncClient]:
    registry = SessionResourceRegistry()
    registry._primary_envs["conv_test"] = environment
    app = create_runner_app(
        resource_registry=registry,
        runner_workspace=workspace,
        server_client=NullServerClient(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as runner_client:
        yield runner_client


def _helper_delete(workspace: Path, path: str, *, recursive: bool = False) -> dict:
    return os_env_module._handle_helper_request(
        request={"op": "delete", "path": path, "recursive": recursive},
        cwd=workspace,
        shell_path="/bin/sh",
        sandbox=SandboxPolicy(
            backend_type="none",
            active=False,
            read_roots=None,
            write_roots=[],
            write_files=[],
            allow_network=True,
        ),
    )


@pytest.mark.asyncio
async def test_delete_uses_runner_helper_not_workspace_package(
    client: httpx.AsyncClient, workspace: Path
) -> None:
    package = workspace / "omnigent"
    package.mkdir()
    (package / "__init__.py").write_text(
        "raise RuntimeError('workspace checkout must not supply the delete helper')\n"
    )
    response = await client.delete(f"{FS_URL}/victim")
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    assert not (workspace / "victim").exists()


@pytest.mark.asyncio
async def test_symlinked_parent_preserves_outside_bytes(
    client: httpx.AsyncClient, workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "victim"
    victim.write_bytes(b"outside bytes")
    (workspace / "link").symlink_to(outside, target_is_directory=True)
    response = await client.delete(f"{FS_URL}/link/victim")
    assert victim.read_bytes() == b"outside bytes"
    assert (workspace / "victim").read_bytes() == b"workspace bytes"
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_leaf_symlinks_only_unlink_the_link(
    client: httpx.AsyncClient, workspace: Path, tmp_path: Path
) -> None:
    targets = [tmp_path / "file", tmp_path / "broken", tmp_path / "directory"]
    targets[0].write_bytes(b"outside file")
    targets[2].mkdir()
    (targets[2] / "child").write_bytes(b"outside child")
    for index, target in enumerate(targets):
        link = workspace / f"link{index}"
        link.symlink_to(target, target_is_directory=index == 2)
        response = await client.delete(f"{FS_URL}/{link.name}")
        assert response.status_code == 200
        assert not link.is_symlink()
        assert response.json()["type"] == "symlink"
        assert targets[0].read_bytes() == b"outside file"
        assert not targets[1].exists()
        assert (targets[2] / "child").read_bytes() == b"outside child"


@pytest.mark.asyncio
async def test_file_and_directory_delete_never_shells(
    client: httpx.AsyncClient,
    environment: CallerProcessOSEnvironment,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_shell(*args: object, **kwargs: object) -> dict:
        pytest.fail("delete must not shell out")

    def forbidden_local(*args: object, **kwargs: object) -> dict:
        pytest.fail("delete must run in the helper process")

    monkeypatch.setattr(environment, "shell", forbidden_shell)
    monkeypatch.setattr(os_env_module, "_delete_impl", forbidden_local, raising=False)
    directory = workspace / "directory"
    directory.mkdir()
    (directory / "child").write_bytes(b"child")
    response = await client.delete(f"{FS_URL}/directory")
    assert response.status_code == 409
    assert (directory / "child").read_bytes() == b"child"
    (directory / "child").unlink()
    assert (await client.delete(f"{FS_URL}/directory")).status_code == 200
    assert not directory.exists()
    assert (await client.delete(f"{FS_URL}/victim")).status_code == 200
    assert not (workspace / "victim").exists()
    assert (await client.delete(f"{FS_URL}/missing")).status_code == 404


@pytest.mark.asyncio
async def test_unreadable_directory_does_not_fail_open(
    client: httpx.AsyncClient,
    environment: CallerProcessOSEnvironment,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = workspace / "unreadable"
    directory.mkdir()
    child = directory / "child"
    child.write_bytes(b"keep")
    directory.chmod(0)
    original_shell = environment.shell

    async def restore_after_listing(command: str, **kwargs: object) -> dict:
        result = await original_shell(command, **kwargs)
        if "os.listdir" in command:
            assert result.get("exit_code") != 0
            directory.chmod(0o700)
        return result

    monkeypatch.setattr(environment, "shell", restore_after_listing)
    try:
        response = await client.delete(f"{FS_URL}/unreadable")
    finally:
        if directory.exists():
            directory.chmod(0o700)
    assert child.read_bytes() == b"keep"
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_unconfirmed_helper_results_never_succeed(
    client: httpx.AsyncClient,
    environment: CallerProcessOSEnvironment,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "absolute-victim"
    outside.write_bytes(b"outside")
    results = [
        {"error": "helper disconnected"},
        {},
        {"deleted": True},
        {"deleted": True, "exit_code": 1, "type": "file", "bytes_deleted": 1},
        {"deleted": True, "exit_code": 0, "type": "file", "error": "timeout"},
    ]
    for result in results:

        async def unconfirmed(
            *args: object, helper_result: dict = result, **kwargs: object
        ) -> dict:
            return helper_result

        monkeypatch.setattr(environment, "delete", unconfirmed, raising=False)
        response = await client.delete(f"{FS_URL}/victim")
        assert (workspace / "victim").read_bytes() == b"workspace bytes"
        assert response.status_code >= 400
        response = await client.delete(f"{FS_URL}/{outside}")
        assert outside.read_bytes() == b"outside"
        assert response.status_code >= 400
    for exception in (TimeoutError("timeout"), ConnectionError("disconnected")):

        async def interrupted(
            *args: object, failure: Exception = exception, **kwargs: object
        ) -> dict:
            raise failure

        monkeypatch.setattr(environment, "delete", interrupted)
        response = await client.delete(f"{FS_URL}/victim")
        assert (workspace / "victim").read_bytes() == b"workspace bytes"
        assert response.status_code >= 400


def test_helper_refuses_root_aliases(workspace: Path) -> None:
    for path in ("", ".", "./"):
        result = _helper_delete(workspace, path)
        assert (workspace / "victim").read_bytes() == b"workspace bytes"
        assert result.get("code") == "invalid_path"


def test_recursive_helper_never_follows_inner_symlink(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "child").write_bytes(b"outside")
    tree = workspace / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(outside, target_is_directory=True)
    result = _helper_delete(workspace, "tree", recursive=True)
    assert (outside / "child").read_bytes() == b"outside"
    assert result.get("deleted") is True or result.get("code") == "unsupported"
    assert not tree.exists() if result.get("deleted") else tree.is_dir()
    if not tree.exists():
        tree.mkdir()
        (tree / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)
    result = _helper_delete(workspace, "tree", recursive=True)
    assert result.get("code") == "unsupported"
    assert tree.is_dir()
    assert (outside / "child").read_bytes() == b"outside"


@pytest.mark.asyncio
async def test_runner_recursive_delete_is_helper_only(
    client: httpx.AsyncClient,
    environment: CallerProcessOSEnvironment,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "child").write_bytes(b"outside")
    tree = workspace / "tree"
    tree.mkdir()
    (tree / "link").symlink_to(outside, target_is_directory=True)

    async def forbidden_shell(*args: object, **kwargs: object) -> dict:
        pytest.fail("recursive delete must not shell out")

    monkeypatch.setattr(environment, "shell", forbidden_shell)
    response = await client.delete(f"{FS_URL}/tree?recursive=true")
    assert (outside / "child").read_bytes() == b"outside"
    assert response.status_code == 200
    assert not tree.exists()


def test_parent_swap_between_descriptor_steps_is_confined(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = workspace / "parent"
    parent.mkdir()
    (parent / "inner").mkdir()
    (parent / "inner" / "victim").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "inner").mkdir()
    (outside / "inner" / "victim").write_bytes(b"outside")
    original_open = os.open

    def swap_after_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == "parent":
            parent.rename(workspace / "moved")
            parent.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(os_env_module.os, "open", swap_after_open)
    result = _helper_delete(workspace, "parent/inner/victim")
    assert (outside / "inner" / "victim").read_bytes() == b"outside"
    assert result.get("deleted") is True
    assert not (workspace / "moved" / "inner" / "victim").exists()


@pytest.mark.asyncio
async def test_capability_and_metadata_follow_platform_support(
    client: httpx.AsyncClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames: list[str] = []

    async def capture(frame: str) -> None:
        frames.append(frame)

    await _send_hello(capture, "test")
    assert CAPABILITY in json.loads(frames[-1])["capabilities"]
    response = await client.get(FS_URL.removesuffix("/filesystem"))
    assert response.json()["metadata"]["workspace_delete"] == {"available": True}
    monkeypatch.setattr(os_env_module, "SAFE_WORKSPACE_DELETE_SUPPORTED", False, raising=False)
    await _send_hello(capture, "test")
    assert CAPABILITY not in json.loads(frames[-1])["capabilities"]
    response = await client.get(FS_URL.removesuffix("/filesystem"))
    availability = response.json()["metadata"]["workspace_delete"]
    assert availability["available"] is False
    assert availability["reason"]
    result = _helper_delete(workspace, "victim")
    assert result.get("code") == "unsupported"
    assert (workspace / "victim").read_bytes() == b"workspace bytes"


def test_delete_disconnect_is_not_retried(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = create_os_environment(
        OSEnvSpec(cwd=str(workspace), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    helper = env._helper
    starts: list[bool] = []

    class DisconnectedProcess:
        stdin = io.StringIO()
        stdout = io.StringIO()

    def start() -> None:
        starts.append(True)
        helper._proc = DisconnectedProcess()

    monkeypatch.setattr(helper, "_ensure_started_locked", start)
    monkeypatch.setattr(helper, "_stop_locked", lambda: None)
    monkeypatch.setattr(helper, "_helper_exit_detail_locked", lambda: "disconnected")
    try:
        result = helper.request({"op": "delete", "path": "victim"})
        assert result.get("error")
        assert len(starts) == 1
        assert (workspace / "victim").read_bytes() == b"workspace bytes"
    finally:
        helper._proc = None
        env.close()
