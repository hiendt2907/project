"""SOP seed expand + round-robin cap."""

from __future__ import annotations

from pathlib import Path

import pytest

from training.sop_expand import READ_ONLY_AUTO_EXECUTE, expand_entries, load_seed_path

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sop_mini.yaml"


def test_load_seed() -> None:
    s = load_seed_path(_FIXTURE)
    assert len(s.templates) == 3


def test_expand_round_robin_cap() -> None:
    seed = load_seed_path(_FIXTURE)
    full_a = 2 * 2
    full_b = 3
    full_c = 2
    full = full_a + full_b + full_c  # 9
    merged = expand_entries(seed, max_total=500)
    assert len(merged) == full

    capped = expand_entries(seed, max_total=5)
    assert len(capped) == 5


def test_point_id_stable() -> None:
    seed = load_seed_path(_FIXTURE)
    a = expand_entries(seed, max_total=500)
    b = expand_entries(seed, max_total=500)
    assert [x.point_id for x in a] == [x.point_id for x in b]


def test_shuffle_changes_order_not_ids() -> None:
    seed = load_seed_path(_FIXTURE)
    a = expand_entries(seed, max_total=50, shuffle_seed=None)
    b = expand_entries(seed, max_total=50, shuffle_seed=42)
    assert sorted(e.point_id for e in a) == sorted(e.point_id for e in b)
    assert [e.point_id for e in a] != [e.point_id for e in b]


def test_mutating_tool_auto_execute_false() -> None:
    seed = load_seed_path(_FIXTURE)
    merged = expand_entries(seed, max_total=500)
    mut = [e for e in merged if e.tool == "k8s_rollout_restart"]
    assert len(mut) == 2  # mini_mutating: 2 slot values
    assert all(not e.auto_execute for e in mut)


def test_read_only_echo_auto_execute() -> None:
    assert "echo" in READ_ONLY_AUTO_EXECUTE
    seed = load_seed_path(_FIXTURE)
    merged = expand_entries(seed, max_total=500)
    echo_e = [e for e in merged if e.tool == "echo"]
    assert echo_e
    assert all(e.auto_execute for e in echo_e)


def test_unknown_tool_raises() -> None:
    from training.sop_expand import SopSeedFile, SopTemplateModel

    bad = SopSeedFile(
        templates=[
            SopTemplateModel(
                template_id="bad",
                allow_auto_execute=False,
                tool="no_such_tool_xyz",
                args={},
                match_text_template="x",
                slots={"a": ["1"]},
            )
        ]
    )
    with pytest.raises(ValueError, match="unknown tool"):
        expand_entries(bad, max_total=10)
