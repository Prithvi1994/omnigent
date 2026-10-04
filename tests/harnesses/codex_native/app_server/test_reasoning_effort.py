"""Model capability checks at Codex's native settings boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import pytest
from cachetools import TTLCache

from omnigent.harnesses.codex_native import app_server
from omnigent.harnesses.codex_native.bridge import read_codex_config_effort
from omnigent.server.smart_routing import RoutingSettings, parse_routing_tables


def test_gateway_restrictions_still_apply_to_catalog_supported_efforts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI's built-in catalog does not override restrictions of the serving gateway."""
    settings = RoutingSettings(
        **parse_routing_tables(
            {"effort_caps": {"gpt-5.6-sol": {"fallback": "medium", "unsupported": ["ultra"]}}}
        )
    )
    monkeypatch.setattr(
        "omnigent.runtime._globals._caps", SimpleNamespace(routing_settings=settings)
    )
    catalog = [
        {
            "id": "gpt-5.6-sol",
            "supportedReasoningEfforts": [
                {"reasoningEffort": value} for value in ("low", "medium", "high", "max", "ultra")
            ],
        }
    ]
    assert app_server.clamp_codex_effort_for_model("ultra", "gpt-5.6-sol", catalog) == "medium"
    assert app_server.clamp_codex_effort_for_model("max", "gpt-5.6-sol", catalog) == "max"


@pytest.mark.parametrize(
    ("effort", "supported", "expected"),
    [
        ("minimal", ["low", "medium", "high", "xhigh"], "low"),
        ("max", ["low", "medium", "high", "xhigh"], "xhigh"),
        ("ultra", ["low", "high"], "high"),
        ("medium", ["high", "low"], "low"),
        ("max", ["high", "max", "ultra"], "max"),
        ("ultra", ["high", "max", "ultra"], "ultra"),
        ("none", ["none", "low"], "none"),
        (None, ["low", "medium", "high"], None),
    ],
)
@pytest.mark.parametrize("schema", ["id", "model", "debug"])
def test_effort_uses_the_matching_models_advertised_levels(
    effort: str | None, supported: list[str], expected: str | None, schema: str
) -> None:
    """Both catalog shapes and model aliases use the model's own ordered ladder."""
    catalog: object
    if schema == "debug":
        catalog = {
            "models": [
                {
                    "slug": "gpt-5.6-sol",
                    "supported_reasoning_levels": [{"effort": value} for value in supported],
                }
            ]
        }
    else:
        catalog = [
            {
                schema: "gpt-5.6-sol",
                "supportedReasoningEfforts": [{"reasoningEffort": value} for value in supported],
            }
        ]
    assert (
        app_server.clamp_codex_effort_for_model(effort, "system.ai.gpt-5-6-sol", catalog)
        == expected
    )


@pytest.mark.parametrize(
    "catalog",
    [
        None,
        [],
        {"models": "unavailable"},
        [{"id": "gpt-5.6-sol"}],
        [{"id": "gpt-5.6-sol", "supportedReasoningEfforts": []}],
        [{"id": "gpt-5.6-sol", "supportedReasoningEfforts": "high"}],
        [{"id": "gpt-5.6-sol", "supportedReasoningEfforts": [None, {}, {"reasoningEffort": 1}]}],
        [
            {
                "id": "another-model",
                "isDefault": True,
                "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
            }
        ],
    ],
)
def test_missing_capabilities_do_not_guess_another_models_effort(catalog: object) -> None:
    """Missing or malformed capabilities preserve the existing fallback rules."""
    assert app_server.clamp_codex_effort_for_model("ultra", "gpt-5.6-sol", catalog) == "ultra"
    assert app_server.clamp_codex_effort_for_model("ultra", None, catalog) == "ultra"
    assert app_server.clamp_codex_effort_for_model("ultra", "glm-5-2", catalog) == "medium"


async def test_live_effort_validation_reads_hidden_models_and_later_pages() -> None:
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.side_effect = [
        {"result": {"data": [{"id": "unrelated-model"}], "nextCursor": "page2"}},
        {
            "result": {
                "data": [
                    {
                        "id": "gpt-5.4",
                        "hidden": True,
                        "supportedReasoningEfforts": [{"reasoningEffort": "xhigh"}],
                    }
                ],
                "nextCursor": None,
            }
        },
    ]

    assert await app_server.resolve_codex_effort_for_model(client, "max", "gpt-5.4") == "xhigh"
    assert client.request.await_args_list == [
        call("model/list", {"includeHidden": True}),
        call("model/list", {"includeHidden": True, "cursor": "page2"}),
    ]


@pytest.mark.parametrize("boundary", ["same-server", "new-server", "expired"])
async def test_successful_catalog_discovery_is_shared_between_turn_clients(
    monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    now = 0.0
    monkeypatch.setattr(
        app_server, "_effort_catalog_cache", TTLCache(maxsize=2, ttl=60, timer=lambda: now)
    )
    first = AsyncMock(spec=app_server.CodexAppServerClient)
    second = AsyncMock(spec=app_server.CodexAppServerClient)
    for client, supported in ((first, ["low", "xhigh"]), (second, ["medium"])):
        client.request.return_value = {
            "result": {
                "data": [
                    {
                        "id": "gpt-5.4",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": value} for value in supported
                        ],
                    }
                ]
            }
        }
    transport = "ws://127.0.0.1:12345"
    assert (
        await app_server.resolve_codex_effort_for_model(
            first, "max", "gpt-5.4", transport=transport
        )
        == "xhigh"
    )
    if boundary == "new-server":
        transport = "ws://127.0.0.1:54321"
    elif boundary == "expired":
        now = 61.0
    expected = "low" if boundary == "same-server" else "medium"
    assert (
        await app_server.resolve_codex_effort_for_model(
            second, "minimal", "gpt-5.4", transport=transport
        )
        == expected
    )
    assert first.request.await_count == 1
    assert second.request.await_count == (0 if boundary == "same-server" else 1)


@pytest.mark.parametrize("failure", ["unavailable", "malformed", "timeout"])
async def test_live_catalog_failures_do_not_block_effort_updates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str
) -> None:
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    if failure == "unavailable":
        client.request.side_effect = app_server.CodexAppServerResponseError(
            {"code": -32601, "message": "method unavailable"}
        )
    elif failure == "malformed":
        client.request.return_value = {"result": {"data": None}}
    else:

        async def stalled(*_args: object) -> None:
            await asyncio.Event().wait()

        client.request.side_effect = stalled
        monkeypatch.setattr(app_server, "_EFFORT_CATALOG_TIMEOUT_SECONDS", 0.01)

    transport = str(tmp_path / "app-server.sock")
    assert (
        await app_server.resolve_codex_effort_for_model(
            client, "ultra", "gpt-5.6-sol", transport=transport
        )
        == "ultra"
    )
    assert (
        await app_server.resolve_codex_effort_for_model(
            client, "ultra", "glm-5-2", transport=transport
        )
        == "medium"
    )
    client.request.side_effect = None
    client.request.return_value = {
        "result": {
            "data": [
                {"id": "gpt-5.6-sol", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}
            ]
        }
    }
    assert (
        await app_server.resolve_codex_effort_for_model(
            client, "ultra", "gpt-5.6-sol", transport=transport
        )
        == "high"
    )


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("gpt-5.4", "minimal", "low"),
        ("gpt-5.4", "max", "xhigh"),
        ("gpt-5.6-sol", "ultra", "ultra"),
        (None, "ultra", "ultra"),
        ("glm-5-2", "ultra", "medium"),
    ],
)
async def test_resume_applies_and_mirrors_supported_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    model: str | None,
    effort: str,
    expected: str,
) -> None:
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        (f'model = "{model}"\n' if model else "") + 'model_reasoning_effort = "medium"\n'
    )
    client = AsyncMock(spec=app_server.CodexAppServerClient)
    client.request.side_effect = lambda method, params: (
        {
            "result": {
                "data": [
                    {
                        "id": "gpt-5.4",
                        "supportedReasoningEfforts": [
                            {"reasoningEffort": value}
                            for value in ("low", "medium", "high", "xhigh")
                        ],
                    }
                ]
            }
        }
        if method == "model/list"
        else {"result": {}}
    )
    monkeypatch.setattr(app_server, "client_for_transport", lambda *args, **kwargs: client)

    await app_server.apply_codex_thread_effort(
        "ws://127.0.0.1:9876",
        "thread_resumed",
        effort,
        model=None if model == "gpt-5.4" else model,
        bridge_dir=tmp_path,
    )

    client.request.assert_awaited_with(
        "thread/settings/update", {"threadId": "thread_resumed", "effort": expected}
    )
    assert read_codex_config_effort(tmp_path) == expected
    assert client.request.await_count == (2 if model else 1)
    client.connect.assert_awaited_once()
    client.close.assert_awaited_once()
