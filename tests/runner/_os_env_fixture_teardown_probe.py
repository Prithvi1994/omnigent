"""Pytest plugin recording os_env helper subprocesses that outlive a watched fixture.

Load with ``-p tests.runner._os_env_fixture_teardown_probe``. After each
:data:`WATCHED_FIXTURES` finalizer runs, it appends the ``omnigent.inner.os_env
helper`` children that were not alive before the test to the JSON-lines file
named by ``OMNIGENT_FIXTURE_PROBE_LOG``.
"""

from __future__ import annotations

import json
import os
from typing import Any

import psutil
import pytest

WATCHED_FIXTURES = frozenset({"registry", "glob_client", "make_os_env"})

_before_by_nodeid: dict[str, set[int]] = {}
_recorded: set[tuple[str, str]] = set()


def live_helper_pids() -> set[int]:
    """Return the pids of this process's live ``os_env`` helper descendants."""
    pids: set[int] = set()
    for child in psutil.Process().children(recursive=True):
        try:
            # A killed but not yet reaped helper is a zombie, not a leak.
            if child.status() == psutil.STATUS_ZOMBIE:
                continue
            cmdline = child.cmdline()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "omnigent.inner.os_env" in cmdline and "helper" in cmdline:
            pids.add(child.pid)
    return pids


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    _before_by_nodeid[item.nodeid] = live_helper_pids()


def pytest_fixture_post_finalizer(fixturedef: Any, request: Any) -> None:
    if fixturedef.argname not in WATCHED_FIXTURES:
        return
    log = os.environ.get("OMNIGENT_FIXTURE_PROBE_LOG")
    if not log:
        return
    nodeid = request.node.nodeid
    # The hook fires once per registered finalizer; record each fixture once.
    if (nodeid, fixturedef.argname) in _recorded:
        return
    _recorded.add((nodeid, fixturedef.argname))
    before = _before_by_nodeid.get(nodeid, set())
    record = {
        "test": nodeid,
        "fixture": fixturedef.argname,
        "helpers_alive_after_teardown": sorted(live_helper_pids() - before),
    }
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    _before_by_nodeid.pop(item.nodeid, None)
    _recorded.difference_update({key for key in _recorded if key[0] == item.nodeid})
