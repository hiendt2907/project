"""F-C2: OMNI_ENV_MODE=lab (CLAUDE.md / INVARIANTS) phải map sang dev, không crash.

Trước đây env_mode là Literal["prod","dev"] → set OMNI_ENV_MODE=lab làm pydantic
ValidationError. Lab posture = dev (non-prod, high-action by role).
"""
import pytest

from workers.settings import WorkerSettings


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("lab", "dev"),
        ("LAB", "dev"),
        (" lab ", "dev"),
        ("dev", "dev"),
        ("prod", "prod"),
    ],
)
def test_env_mode_lab_alias_maps_to_dev(monkeypatch, raw, expected):
    monkeypatch.setenv("OMNI_ENV_MODE", raw)
    assert WorkerSettings().env_mode == expected


def test_env_mode_lab_does_not_unlock_prod_failclosed(monkeypatch):
    # lab→dev: KHÔNG bị ép fail-closed như prod (god/lab bypass vẫn theo flag).
    monkeypatch.setenv("OMNI_ENV_MODE", "lab")
    assert WorkerSettings().env_mode != "prod"
