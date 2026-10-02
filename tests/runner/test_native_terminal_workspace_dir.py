"""Workspace resolution for native-terminal launches must not read a dead cwd."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.native.orchestration import (
    _claude_session_workspace,
    _runner_workspace_dir,
)


def test_env_wins_without_touching_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A set workspace must be returned even when the process cwd is unreadable.

    ``os.environ.get(name, str(Path.cwd()))`` evaluated the default eagerly, so a
    runner whose worktree had been removed under it died with ``FileNotFoundError``
    from ``os.getcwd()`` while the answer sat in the environment all along.
    """
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))

    def _dead_cwd() -> Path:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(Path, "cwd", staticmethod(_dead_cwd))

    assert _runner_workspace_dir() == str(tmp_path)


def test_falls_back_to_cwd_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)
    monkeypatch.chdir(tmp_path)

    assert Path(_runner_workspace_dir()).resolve() == tmp_path.resolve()


def test_empty_env_value_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "")
    monkeypatch.chdir(tmp_path)

    assert Path(_runner_workspace_dir()).resolve() == tmp_path.resolve()


def test_dead_cwd_without_env_reports_the_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)

    def _dead_cwd() -> Path:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(Path, "cwd", staticmethod(_dead_cwd))

    with pytest.raises(RuntimeError, match="no longer exists"):
        _runner_workspace_dir()


def test_removed_worktree_is_the_real_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live failure: cwd is a directory that was deleted under the process."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.chdir(worktree)
    worktree.rmdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))

    with pytest.raises(FileNotFoundError):
        os.getcwd()
    assert _runner_workspace_dir() == str(tmp_path)


def test_claude_workspace_prefers_session_without_reading_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The explicit session workspace is used even when the process cwd is gone."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)

    def _dead_cwd() -> Path:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(Path, "cwd", staticmethod(_dead_cwd))

    assert _claude_session_workspace(str(workspace)) == str(workspace)


def test_claude_workspace_falls_back_to_runner_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no session workspace, the runner's configured workspace is used."""
    workspace = tmp_path / "runner-workspace"
    workspace.mkdir()
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(workspace))

    assert _claude_session_workspace(None) == str(workspace)


def test_claude_workspace_rejects_removed_session_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session workspace that no longer exists is a WORKSPACE_MISSING error,
    never a silent fall-through to another directory."""
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))

    with pytest.raises(OmnigentError) as failure:
        _claude_session_workspace(str(tmp_path / "gone"))
    assert failure.value.code == ErrorCode.WORKSPACE_MISSING


def test_claude_workspace_rejects_non_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace path that is a file, not a directory, is rejected before launch."""
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)

    with pytest.raises(OmnigentError) as failure:
        _claude_session_workspace(str(not_a_dir))
    assert failure.value.code == ErrorCode.WORKSPACE_MISSING


def test_claude_workspace_dead_cwd_without_env_is_workspace_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A removed cwd with no configured workspace is still a WORKSPACE_MISSING
    condition, not a bare RuntimeError that would surface as a generic 500."""
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)

    def _dead_cwd() -> Path:
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(Path, "cwd", staticmethod(_dead_cwd))

    with pytest.raises(OmnigentError) as failure:
        _claude_session_workspace(None)
    assert failure.value.code == ErrorCode.WORKSPACE_MISSING


def test_claude_workspace_normalizes_padding_and_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A padded or ``~``-prefixed workspace that exists is launched, not misread
    as missing (matching Codex workspace normalization)."""
    monkeypatch.delenv("OMNIGENT_RUNNER_WORKSPACE", raising=False)
    project = tmp_path / "project"
    project.mkdir()

    assert _claude_session_workspace(f"  {project}  ") == str(project)

    monkeypatch.setenv("HOME", str(tmp_path))
    assert _claude_session_workspace("~/project") == str(project)
