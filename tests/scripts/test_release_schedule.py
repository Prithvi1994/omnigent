"""Exercise scheduled release planning and reruns without calling GitHub."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/release.yml"
_SOURCE_SHA = "a" * 40
_RELEASE_SHA = "b" * 40
_PICKED_SHA = "c" * 40


def _run_step(
    tmp_path: Path, step_id: str, responses: dict[str, str | None], **inputs: str
) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    script = next(
        step["run"] for step in workflow["jobs"]["plan"]["steps"] if step.get("id") == step_id
    )
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        """
import json, os, sys
from pathlib import Path

responses = json.loads(os.environ["API_RESPONSES"])
endpoint = next(arg for arg in sys.argv if arg.startswith("repos/")).split("/", 3)[3]
query = sys.argv[sys.argv.index("--jq") + 1] if "--jq" in sys.argv else "raw"
key = endpoint + " " + query
if key not in responses:
    Path(os.environ["UNEXPECTED_API"]).write_text(key)
    sys.exit(99)
body = responses[key]
if body is None:
    print('{"message":"Not Found"}')
    sys.exit(1)
print(body)
"""
    )
    gh.chmod(0o755)
    output = tmp_path / f"{step_id}.output"
    unexpected = tmp_path / "unexpected-api"
    result = subprocess.run(
        ["bash", "-c", script],
        env=os.environ
        | {
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "API_RESPONSES": json.dumps(responses),
            "UNEXPECTED_API": str(unexpected),
            "GITHUB_REPOSITORY": "example/project",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        }
        | inputs,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert not unexpected.exists(), unexpected.read_text()
    outputs = (
        dict(line.split("=", 1) for line in output.read_text().splitlines())
        if output.exists()
        else {}
    )
    return result, outputs


@pytest.mark.parametrize(
    ("event", "requested", "marker", "expected"),
    [
        ("schedule", "", "0.6.0.dev0", "0.6.0"),
        ("schedule", "", "0.6.0", None),
        ("schedule", "", "0.6.0rc1.dev0", None),
        ("schedule", "", "0.6.0.dev1", None),
        ("schedule", "", None, None),
        ("workflow_dispatch", "0.6.0rc1", None, "0.6.0rc1"),
    ],
    ids=["pinned-sha", "stable-marker", "rc-marker", "dev1-marker", "api-error", "manual-rc"],
)
def test_release_version(
    tmp_path: Path, event: str, requested: str, marker: str | None, expected: str | None
) -> None:
    result, outputs = _run_step(
        tmp_path,
        "derive",
        {
            f"contents/pyproject.toml?ref={_SOURCE_SHA} raw": (
                f'[project]\nversion = "{marker}"' if marker else None
            ),
            "contents/pyproject.toml?ref=main raw": '[project]\nversion = "0.7.0.dev0"',
        }
        if event == "schedule"
        else {},
        EVENT_NAME=event,
        SOURCE_SHA=_SOURCE_SHA,
        VERSION=requested,
    )
    if expected is None:
        assert result.returncode != 0
        assert outputs == {}
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert outputs == {
            "version": expected,
            "tag": f"v{expected}",
            "branch": "release/v0.6.0",
            "prerelease": str("rc" in expected).lower(),
        }


@pytest.mark.parametrize(
    ("head", "tag_head", "version", "done"),
    [
        (None, None, "0.6.0", "false"),
        (_RELEASE_SHA, _RELEASE_SHA, "0.6.0", "true"),
        (_PICKED_SHA, _RELEASE_SHA, "0.6.0", None),
        (_PICKED_SHA, None, "0.6.1", "false"),
        (_PICKED_SHA, None, "0.6.0rc2", "false"),
    ],
    ids=["initial-cut", "rerun-after-main-bump", "cherry-pick-same-tag", "patch", "next-rc"],
)
def test_release_branch_and_tag_state(
    tmp_path: Path, head: str | None, tag_head: str | None, version: str, done: str | None
) -> None:
    tag = f"v{version}"
    responses = {
        "git/ref/heads/release/v0.6.0 .object.sha": head,
        f"git/ref/tags/{tag} .object.sha": tag_head,
    }
    if head is None:
        responses[f"commits/{_SOURCE_SHA} .sha"] = _SOURCE_SHA
    if tag_head:
        responses[f"git/ref/tags/{tag} .object.type"] = "commit"
        responses[f"contents/pyproject.toml?ref={tag} raw"] = f'version = "{version}"'
    result, outputs = _run_step(
        tmp_path,
        "state",
        responses,
        VERSION=version,
        TAG=tag,
        BRANCH="release/v0.6.0",
        REF="main",
        SOURCE_REF=_SOURCE_SHA,
    )
    if done is None:
        assert result.returncode != 0
        assert "which is not the converged branch head" in result.stdout
        assert outputs == {}
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert outputs == {
            "branch_exists": str(head is not None).lower(),
            "base_sha": head or _SOURCE_SHA,
            "already_done": done,
        }
