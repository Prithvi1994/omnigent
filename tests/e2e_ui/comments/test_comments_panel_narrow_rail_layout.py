"""E2E: Comments panel and editor toolbar layout in a narrow Workspace rail.

Opening the Comments panel next to the rich-text editor inside the Workspace
rail squeezes the editor column on a laptop-sized window. The panel's controls
must stay inside the rail and the window without covering the editor's
formatting toolbar, the toolbar's buttons must keep to one row inside the
toolbar, and every Workspace tab must stay reachable.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail

_FILE_PATH = "AGENTS.md"
_SELECTABLE_TEXT = "Guidance for AI agents"
_MARKDOWN_CONTENT = """\
# Agent guidance

Guidance for AI agents (Claude Code, Copilot, Cursor, etc.) working in this \
repository. See CONTRIBUTING.md for the full contributor workflow.

## Committing

Run the pre-commit hook before committing (pre-commit run --all-files, or let \
it run on staged files via git commit). Fix any issues it reports so the \
commit lands clean.
"""

# A 14" MacBook default window, a 13" laptop, and a half-width window.
_VIEWPORTS = [(1512, 982), (1280, 800), (1024, 768)]
_VIEWPORT_IDS = [f"{w}x{h}" for w, h in _VIEWPORTS]
_LAYOUT_SETTLE_MS = 500
_RECORDING_HOLD_MS = 2500
_EDGE_TOLERANCE_PX = 1
_ROW_TOLERANCE_PX = 4

Box = dict[str, float]

# Workspace tabs clipped by an ancestor the user cannot scroll are unreachable.
_TAB_STRIP_CLIP_JS = """
() => {
  const out = [];
  const strip = [...document.querySelectorAll('.workspace-tab-strip')]
    .find((s) => s.getBoundingClientRect().width > 0);
  if (!strip) return out;
  for (const b of strip.querySelectorAll("button, [role='button']")) {
    const r = b.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const cs = getComputedStyle(b);
    if (cs.visibility === 'hidden' || cs.display === 'none') continue;
    let a = b.parentElement, container = null;
    while (a && a !== strip.parentElement) {
      if (getComputedStyle(a).overflowX !== 'visible') { container = a; break; }
      a = a.parentElement;
    }
    if (!container) continue;
    if (['auto', 'scroll'].includes(getComputedStyle(container).overflowX)) continue;
    const cr = container.getBoundingClientRect();
    if (r.right > cr.right + 1 || r.left < cr.left - 1) {
      out.push({
        label: (b.getAttribute('aria-label') || b.title || b.textContent || '')
          .trim().replace(/\\s+/g, ' ').slice(0, 40),
        rect: [r.left, r.right].map(Math.round),
        container: [cr.left, cr.right].map(Math.round),
      });
    }
  }
  return out;
}
"""


@pytest.fixture
def seeded_agents_md_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Write AGENTS.md into the session workspace and yield ``(base_url, session_id)``."""
    base_url, session_id = seeded_session
    resp = httpx.put(
        f"{base_url}/v1/sessions/{session_id}"
        f"/resources/environments/default/filesystem/{_FILE_PATH}",
        json={"content": _MARKDOWN_CONTENT, "encoding": "utf-8"},
        timeout=10.0,
    )
    resp.raise_for_status()
    yield (base_url, session_id)


def _new_page(browser: Browser, width: int, height: int) -> Page:
    kwargs: dict[str, Any] = {"viewport": {"width": width, "height": height}}
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        kwargs["record_video_dir"] = record_dir
        kwargs["record_video_size"] = {"width": width, "height": height}
    page = browser.new_context(**kwargs).new_page()
    page.add_init_script("window.localStorage.setItem('omnigent:default-workspace-panel', 'open')")
    return page


def _open_file_with_pending_comment(
    page: Page, base_url: str, session_id: str
) -> tuple[Locator, Locator]:
    """Open AGENTS.md in the rail editor, select a paragraph and click "Add comment".

    :returns: ``(file_viewer, comments_panel)`` locators.
    """
    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)

    file_button = page.get_by_role("button", name=re.compile(re.escape(_FILE_PATH))).filter(
        has_text=_FILE_PATH
    )
    expect(file_button).to_be_visible(timeout=30_000)
    file_button.click()

    file_viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(file_viewer).to_be_visible()
    editor = file_viewer.locator("[contenteditable='true']")
    expect(editor).to_be_visible(timeout=10_000)
    expect(editor).to_contain_text(_SELECTABLE_TEXT)
    page.wait_for_timeout(_LAYOUT_SETTLE_MS)

    editor.locator("p", has_text=_SELECTABLE_TEXT).first.select_text()
    floating_add_comment = page.get_by_role(
        "button", name=re.compile("Add comment", re.IGNORECASE)
    )
    expect(floating_add_comment).to_be_visible()
    floating_add_comment.click()

    panel = file_viewer.locator('[data-testid="comments-panel"]')
    expect(panel.locator("span.font-semibold", has_text="Comments")).to_be_visible()
    expect(panel.get_by_role("button", name="Add Comment")).to_be_visible()
    page.wait_for_timeout(_LAYOUT_SETTLE_MS)
    return file_viewer, panel


def _box(locator: Locator, label: str) -> Box:
    box = locator.bounding_box()
    assert box is not None, f"{label} has no bounding box"
    return box


def _right(box: Box) -> float:
    return box["x"] + box["width"]


def _intersects(a: Box, b: Box) -> bool:
    return (
        a["x"] < _right(b)
        and b["x"] < _right(a)
        and a["y"] < b["y"] + b["height"]
        and b["y"] < a["y"] + a["height"]
    )


def _panel_controls(panel: Locator) -> dict[str, Locator]:
    return {
        "Comments header": panel.locator("span.font-semibold", has_text="Comments"),
        "Address All button": panel.get_by_role("button", name="Address All"),
        "Open tab": panel.get_by_role("button", name=re.compile(r"^Open\b")),
        "Addressed tab": panel.get_by_role("button", name=re.compile(r"^Addressed\b")),
        "Add Comment button": panel.get_by_role("button", name="Add Comment"),
    }


def _toolbar_buttons(toolbar: Locator) -> list[tuple[str, Box]]:
    buttons: list[tuple[str, Box]] = []
    for button in toolbar.get_by_role("button").all():
        if not button.is_visible():
            continue
        box = button.bounding_box()
        if box is None:
            continue
        label = button.get_attribute("aria-label") or button.get_attribute("title") or "?"
        buttons.append((label, box))
    return buttons


def _measure(page: Page, file_viewer: Locator, panel: Locator, out_dir: Path, tag: str) -> dict:
    """Capture the layout geometry plus a screenshot; returns the geometry report."""
    out_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(out_dir / f"{tag}.png"))
    viewport = page.viewport_size
    assert viewport is not None
    conversations = page.locator('aside[aria-label="Conversations"]')
    editor = file_viewer.locator("[contenteditable='true']")
    toolbar = file_viewer.get_by_role("toolbar", name="Formatting")
    report = {
        "url": page.url,
        "video": page.video.path() if page.video else None,
        "viewport": viewport,
        "sidebar_collapsed": conversations.get_attribute("data-collapsed") == "true",
        "rail": _box(page.get_by_role("complementary", name="Workspace"), "Workspace rail"),
        "panel": _box(panel, "Comments panel"),
        "editor": editor.bounding_box(),
        "toolbar": _box(toolbar, "Formatting toolbar"),
        "controls": {name: _box(loc, name) for name, loc in _panel_controls(panel).items()},
        "toolbar_buttons": _toolbar_buttons(toolbar),
        "clipped_tabs": page.evaluate(_TAB_STRIP_CLIP_JS),
    }
    (out_dir / f"{tag}.json").write_text(json.dumps(report, indent=1))
    print(f"layout {tag}: {json.dumps(report)}")
    if page.video:
        page.wait_for_timeout(_RECORDING_HOLD_MS)
    return report


def _control_violations(report: dict) -> list[str]:
    rail_right = _right(report["rail"])
    window_right = report["viewport"]["width"]
    violations: list[str] = []
    for name, box in report["controls"].items():
        right = _right(box)
        if right > rail_right + _EDGE_TOLERANCE_PX:
            violations.append(
                f"{name} ends at x={right:.0f}, past the rail's right edge ({rail_right:.0f})"
            )
        if right > window_right + _EDGE_TOLERANCE_PX:
            violations.append(
                f"{name} ends at x={right:.0f}, past the window's right edge ({window_right})"
            )
        for label, button in report["toolbar_buttons"]:
            if _intersects(box, button):
                violations.append(f"{name} covers the editor toolbar's {label!r} button")
    editor = report["editor"]
    if editor is None or editor["width"] < 1:
        violations.append("editor column squeezed to 0 px")
    return violations


def _toolbar_rows(buttons: list[tuple[str, Box]]) -> list[list[str]]:
    rows: list[tuple[float, list[str]]] = []
    for label, box in sorted(buttons, key=lambda item: (item[1]["y"], item[1]["x"])):
        if rows and abs(rows[-1][0] - box["y"]) <= _ROW_TOLERANCE_PX:
            rows[-1][1].append(label)
        else:
            rows.append((box["y"], [label]))
    return [labels for _, labels in rows]


def _toolbar_violations(report: dict) -> list[str]:
    violations: list[str] = []
    rows = _toolbar_rows(report["toolbar_buttons"])
    if len(rows) != 1:
        layout = "; ".join(f"row {i + 1}: {', '.join(labels)}" for i, labels in enumerate(rows))
        violations.append(f"the toolbar wrapped onto {len(rows)} rows: {layout}")
    toolbar_right = _right(report["toolbar"])
    for label, box in report["toolbar_buttons"]:
        if _right(box) > toolbar_right + _EDGE_TOLERANCE_PX:
            violations.append(
                f"{label!r} ends at x={_right(box):.0f}, past the toolbar's right edge "
                f"({toolbar_right:.0f})"
            )
    return violations


def _tab_strip_violations(report: dict) -> list[str]:
    return [
        f"workspace tab {c['label']!r} is clipped outside its non-scrollable strip: "
        f"tab x={c['rect'][0]}..{c['rect'][1]} vs strip x={c['container'][0]}..{c['container'][1]}"
        for c in report["clipped_tabs"]
    ]


def _report_for(
    browser: Browser, session: tuple[str, str], output_path: str, tag: str, width: int, height: int
) -> dict:
    base_url, session_id = session
    page = _new_page(browser, width, height)
    try:
        file_viewer, panel = _open_file_with_pending_comment(page, base_url, session_id)
        return _measure(page, file_viewer, panel, Path(output_path), f"{tag}-{width}x{height}")
    finally:
        page.context.close()


@pytest.mark.parametrize(("width", "height"), _VIEWPORTS, ids=_VIEWPORT_IDS)
def test_comments_panel_controls_stay_inside_rail(
    browser: Browser,
    seeded_agents_md_session: tuple[str, str],
    output_path: str,
    width: int,
    height: int,
) -> None:
    """Panel controls stay inside the rail and window and clear of the toolbar."""
    report = _report_for(browser, seeded_agents_md_session, output_path, "controls", width, height)
    violations = _control_violations(report)
    assert not violations, f"at {width}x{height}:\n" + "\n".join(violations)


@pytest.mark.parametrize(("width", "height"), _VIEWPORTS, ids=_VIEWPORT_IDS)
def test_editor_toolbar_keeps_one_row_beside_comments_panel(
    browser: Browser,
    seeded_agents_md_session: tuple[str, str],
    output_path: str,
    width: int,
    height: int,
) -> None:
    """The formatting toolbar keeps one row of buttons inside the toolbar."""
    report = _report_for(browser, seeded_agents_md_session, output_path, "toolbar", width, height)
    violations = _toolbar_violations(report)
    assert not violations, f"at {width}x{height}:\n" + "\n".join(violations)


@pytest.mark.parametrize(("width", "height"), _VIEWPORTS, ids=_VIEWPORT_IDS)
def test_workspace_tabs_stay_reachable_beside_comments_panel(
    browser: Browser,
    seeded_agents_md_session: tuple[str, str],
    output_path: str,
    width: int,
    height: int,
) -> None:
    """No Workspace tab is clipped out of reach when the rail is narrow."""
    report = _report_for(browser, seeded_agents_md_session, output_path, "tabs", width, height)
    violations = _tab_strip_violations(report)
    assert not violations, f"at {width}x{height}:\n" + "\n".join(violations)
