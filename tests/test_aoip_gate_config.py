"""Canonical AOIP gate config (config/aoip_agent_gate.env) sanity checks.

Two invariants:
  (a) the canonical file is well-formed KEY=VALUE .env — same format
      run.env already uses (scripts/lib/remote_agent_provisioning.py) and
      the format scripts/deploy_aoip_gate_config.sh merges into run.env on
      each VM.
  (b) capability catalog consistency: every failure_mode listed in the
      canonical AOIP_GATE_ALLOWED_FAILURE_MODES has a registered operator in
      src/aoip/recovery.py::OPERATORS for substrate "systemd". This is the
      guard against config claiming a capability the code does not (yet)
      implement — see task context 2026-07-21 and the note in
      config/aoip_agent_gate.env about disk_pressure_journal being added in
      parallel by another agent's capability-journal-vacuum work.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from aoip.recovery import OPERATORS, SUBSTRATE_SYSTEMD

CONFIG_PATH = (
    pathlib.Path(__file__).parent.parent / "config" / "aoip_agent_gate.env"
)

_KEY_VALUE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")

# Keys the deploy script manages (must match
# scripts/deploy_aoip_gate_config.sh MANAGED_KEY_REGEX conceptually).
_REQUIRED_KEYS = {
    "AOIP_GATE_ALLOWED_FAILURE_MODES",
    "AOIP_GATE_ALLOWED_SUBSTRATES",
    "AOIP_GATE_SCOPE_PREFIX",
    "AOIP_GATE_MAX_RISK",
    "AOIP_GATE_MIN_DIAGNOSIS_CONFIDENCE",
    "AOIP_GATE_MAX_DIAGNOSIS_AGE_S",
    "AOIP_ALLOWED_SYSTEMD_UNITS",
}


def parse_env_file(path: pathlib.Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Blank lines and lines starting
    with '#' are comments and are skipped; every other non-blank line MUST
    match KEY=VALUE with no quoting — matches the format run.env uses."""
    result: dict[str, str] = {}
    for lineno, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if not _KEY_VALUE_RE.match(line):
            raise ValueError(f"{path}:{lineno}: not a valid KEY=VALUE line: {raw_line!r}")
        key, _, value = line.partition("=")
        result[key] = value
    return result


def test_canonical_config_file_exists():
    assert CONFIG_PATH.is_file(), f"missing canonical config: {CONFIG_PATH}"


def test_canonical_config_parses_as_clean_env_format():
    parsed = parse_env_file(CONFIG_PATH)
    assert parsed, "canonical config parsed to zero key/value pairs"


def test_canonical_config_has_no_junk_lines():
    """Every non-blank, non-comment line must be a bare KEY=VALUE pair —
    no stray shell syntax, exports, or quoting that run.env doesn't use."""
    for lineno, raw_line in enumerate(CONFIG_PATH.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        assert _KEY_VALUE_RE.match(line), (
            f"{CONFIG_PATH}:{lineno}: junk / non-KEY=VALUE line: {raw_line!r}"
        )
        assert not line.startswith("export "), (
            f"{CONFIG_PATH}:{lineno}: 'export' prefix not used by run.env format: {raw_line!r}"
        )


def test_canonical_config_declares_all_required_gate_keys():
    parsed = parse_env_file(CONFIG_PATH)
    missing = _REQUIRED_KEYS - parsed.keys()
    assert not missing, f"canonical config missing required keys: {sorted(missing)}"


def test_canonical_config_matches_known_lab_values_for_untouched_keys():
    """Everything except AOIP_GATE_ALLOWED_FAILURE_MODES/AOIP_ALLOWED_SYSTEMD_UNITS
    must match the values actually running on the 3 lab VMs today (task
    context 2026-07-21) — this file is meant to capture reality, not invent
    new policy as a side effect of adding disk_pressure_journal.
    AOIP_ALLOWED_SYSTEMD_UNITS gained systemd-journald.service once the
    journal_vacuum operator landed in recovery.py (fixed, hardcoded target
    for capability #3, not tenant-supplied — see
    src/aoip/capabilities/systemd_journal_vacuum.py)."""
    parsed = parse_env_file(CONFIG_PATH)
    assert parsed["AOIP_GATE_ALLOWED_SUBSTRATES"] == "systemd"
    assert parsed["AOIP_GATE_SCOPE_PREFIX"] == "svc:"
    assert parsed["AOIP_GATE_MAX_RISK"] == "0.5"
    assert parsed["AOIP_GATE_MIN_DIAGNOSIS_CONFIDENCE"] == "0.5"
    assert parsed["AOIP_GATE_MAX_DIAGNOSIS_AGE_S"] == "300"
    assert parsed["AOIP_ALLOWED_SYSTEMD_UNITS"] == "payment-api.service,systemd-journald.service"


def test_canonical_config_numeric_gate_values_are_valid_floats():
    parsed = parse_env_file(CONFIG_PATH)
    float(parsed["AOIP_GATE_MAX_RISK"])
    float(parsed["AOIP_GATE_MIN_DIAGNOSIS_CONFIDENCE"])
    float(parsed["AOIP_GATE_MAX_DIAGNOSIS_AGE_S"])


def test_catalog_consistency_every_allowed_failure_mode_has_an_operator():
    """The consistency guard: config MUST NOT allowlist a failure_mode that
    src/aoip/recovery.py::OPERATORS has no (failure_mode, systemd) entry
    for — that would let the gate pass capability_authorized for a mode the
    executor cannot actually recover (operator_for() returns None →
    execute_recovery aborts on 'operator_exists', but the point of this
    catalog test is to catch the mismatch at config-review time, not at
    incident time).

    EXPECTED STATE AT TIME OF WRITING (2026-07-21): this config was updated
    to include "disk_pressure_journal" for capability #3
    (journal-vacuum), which is being built in PARALLEL by another agent.
    If OPERATORS does not yet have a ("disk_pressure_journal", "systemd")
    entry when this test runs, this test is SUPPOSED to fail — that is the
    signal the two pieces of work (config vs. operator registration)
    haven't converged yet. Do not "fix" this by editing recovery.py from
    here; the operator registration belongs to that other slice of work.
    """
    parsed = parse_env_file(CONFIG_PATH)
    allowed_modes = [
        m.strip() for m in parsed["AOIP_GATE_ALLOWED_FAILURE_MODES"].split(",") if m.strip()
    ]
    assert allowed_modes, "AOIP_GATE_ALLOWED_FAILURE_MODES parsed to empty list"

    missing_operators = [
        mode for mode in allowed_modes
        if (mode, SUBSTRATE_SYSTEMD) not in OPERATORS
    ]
    assert not missing_operators, (
        "config allowlists failure_mode(s) with no registered recovery "
        f"operator for substrate={SUBSTRATE_SYSTEMD!r}: {missing_operators} — "
        f"registered catalog: {sorted(k for k, s in OPERATORS if s == SUBSTRATE_SYSTEMD)}"
    )


@pytest.mark.parametrize("mode", ["process_down", "failed_state_stale"])
def test_pre_existing_capabilities_stay_registered(mode: str):
    """Guard the other direction for the two capabilities that predate this
    task: they must stay registered regardless of what happens to
    disk_pressure_journal's parallel rollout."""
    assert (mode, SUBSTRATE_SYSTEMD) in OPERATORS
