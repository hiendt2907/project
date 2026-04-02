"""deep_scout_autonomous — redaction-free unit checks."""

from __future__ import annotations

import json

from init.deep_scout_autonomous import SYNTH_SYSTEM_VI, _point_id_autonomous


def test_stable_point_id() -> None:
    a = _point_id_autonomous("pod", "ns", "x")
    b = _point_id_autonomous("pod", "ns", "x")
    assert a == b
    assert a != _point_id_autonomous("pod", "ns", "y")


def test_synth_prompt_has_three_sentence_ask() -> None:
    assert "3 câu" in SYNTH_SYSTEM_VI or "ba câu" in SYNTH_SYSTEM_VI.lower()
