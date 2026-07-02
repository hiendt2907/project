"""Coverage for scripts/provision_fresh_tenant.py's provision_api_key() call path
(iteration 11 leftover) — subprocess wiring only; the real Postgres/gateway
mutation path is verified live on the cluster, not here."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from provision_fresh_tenant import ADD_API_KEY_SCRIPT, provision_api_key


def test_provision_api_key_invokes_add_tenant_api_key_script() -> None:
    with patch("provision_fresh_tenant.subprocess.run") as mock_run:
        provision_api_key("tenant-unittest-01")

    mock_run.assert_called_once_with(
        ["bash", str(ADD_API_KEY_SCRIPT), "tenant-unittest-01"],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )


def test_provision_api_key_propagates_script_failure() -> None:
    with patch(
        "provision_fresh_tenant.subprocess.run",
        side_effect=subprocess.CalledProcessError(1, "add_tenant_api_key.sh"),
    ):
        with pytest.raises(subprocess.CalledProcessError):
            provision_api_key("tenant-unittest-01")


def test_add_api_key_script_path_points_at_real_script() -> None:
    assert ADD_API_KEY_SCRIPT.name == "add_tenant_api_key.sh"
    assert ADD_API_KEY_SCRIPT.is_file()
