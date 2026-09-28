"""Clips captioned as a hang or a missing prompt must outlast a healthy response."""

import re
from pathlib import Path

from omnigent.spec import load

_DEV = Path(__file__).resolve().parents[2] / "dev"
_LANES = _DEV / "recording-lanes.md"
_REPRO_AGENT = _DEV / "repro-agent"


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _section(path: Path, heading: str) -> str:
    """Return the normalized body of one ``## <heading>`` section."""
    text = path.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    assert match, f"missing section {heading!r} in {path}"
    return _normalized(match.group(1))


def test_finishing_a_clip_requires_hang_claims_to_outlast_healthy_latency() -> None:
    finishing = _section(_LANES, "Finishing a clip")

    assert "says something never appears or the turn hangs" in finishing
    assert "keep recording past the longest healthy latency you observed" in finishing
    assert "the caption must state how long the clip waited" in finishing
    assert "shows an ordinary wait, not the bug" in finishing
    assert (
        "Never shorten a wait anchor while working around an unrelated recorder problem"
        in finishing
    )


def test_terminal_lane_treats_a_hang_claim_as_a_claim_about_time() -> None:
    terminal = _section(_LANES, "`terminal` facets")

    assert 'A pane that "hangs" or a prompt that "never appears" is a claim about time' in terminal
    assert "past the longest healthy latency you observed for that step" in terminal
    assert "say in the caption how long the clip waited" in terminal


def test_cli_lane_anchors_a_hang_tape_past_healthy_latency_and_keeps_the_anchor() -> None:
    cli = _section(_LANES, "`cli` facets")

    assert "When the claim is that nothing arrives, the wait is the output" in cli
    assert "keep a `Wait+Screen@60s /streaming… 2[0-9]s/` anchor or add `Sleep 25s`" in cli
    assert "never on a pattern the first in-progress frame already matches" in cli
    assert "Do not weaken a wait anchor while fixing an unrelated recorder problem" in cli
    assert "re-check that the stop condition still outlasts the healthy latency" in cli


def test_repro_clip_rules_mirror_the_hang_duration_rule() -> None:
    step_four = _section(_REPRO_AGENT / "AGENTS.md", "Step 4 — Record the reproduction")

    assert "caption says a prompt never appears or the turn hangs" in step_four
    assert (
        "keep recording past the longest healthy latency you observed for that step" in step_four
    )
    assert "its caption must state how long the clip waited" in step_four
    assert "Never shorten a wait anchor while fixing an unrelated recorder problem" in step_four


def test_repro_caption_field_requires_the_wait_duration_for_hang_claims() -> None:
    output = _section(_REPRO_AGENT / "AGENTS.md", "Output — the reproduction artifacts")

    assert (
        "A caption that claims something never appears or hangs also states how long the "
        "clip waited" in output
    )


def test_loaded_repro_bundle_instructions_carry_the_hang_duration_rule() -> None:
    spec = load(_REPRO_AGENT)

    assert spec.instructions is not None
    instructions = _normalized(spec.instructions)
    assert "caption says a prompt never appears or the turn hangs" in instructions
    assert "its caption must state how long the clip waited" in instructions
