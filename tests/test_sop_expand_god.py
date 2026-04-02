"""SOP expand: god_mode mở auto_execute cho shell tools."""

from __future__ import annotations

from training.sop_expand import SopSeedFile, SopTemplateModel, expand_entries


def test_expand_god_mode_sets_auto_execute_for_shell_template() -> None:
    seed = SopSeedFile(
        version=1,
        templates=[
            SopTemplateModel(
                template_id="shell_one",
                allow_auto_execute=True,
                tool="execute_shell_command",
                args={"command": "kubectl version --client"},
                match_text_template="cli check {x}",
                slots={"x": ["a"]},
            ),
        ],
    )
    default_entries = expand_entries(seed, max_total=10, god_mode=False)
    god_entries = expand_entries(seed, max_total=10, god_mode=True)
    assert default_entries[0].auto_execute is False
    assert god_entries[0].auto_execute is True
