"""Unit tests for Deployment → secret ref extraction (preflight, no cluster)."""

from __future__ import annotations

from unittest.mock import MagicMock

from pkg.reasoning.preflight_deployment_secret_refs import secret_refs_from_deployment


def _dep_with_env_secret(name: str, key: str, env_name: str) -> MagicMock:
    sk = MagicMock()
    sk.name = name
    sk.key = key
    vf = MagicMock()
    vf.secret_key_ref = sk
    ev = MagicMock()
    ev.name = env_name
    ev.value_from = vf
    c = MagicMock()
    c.env = [ev]
    c.env_from = []
    ps = MagicMock()
    ps.containers = [c]
    tpl = MagicMock()
    tpl.spec = ps
    spec = MagicMock()
    spec.template = tpl
    dep = MagicMock()
    dep.spec = spec
    return dep


def test_secret_refs_from_deployment_env_valuefrom() -> None:
    dep = _dep_with_env_secret("db-secret", "PASSWORD", "PGPASSWORD")
    refs = secret_refs_from_deployment(dep)
    assert len(refs) == 1
    assert refs[0]["secret_name"] == "db-secret"
    assert refs[0]["secret_key"] == "PASSWORD"
    assert refs[0]["env_var"] == "PGPASSWORD"


def test_secret_refs_dedupes_same_name_key() -> None:
    dep = _dep_with_env_secret("s", "k", "A")
    refs = secret_refs_from_deployment(dep)
    assert len(refs) == 1
