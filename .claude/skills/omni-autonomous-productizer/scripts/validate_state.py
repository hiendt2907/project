#!/usr/bin/env python3
"""Validate docs/operations/AUTONOMOUS_LOOP_STATE.json against the schema
used by omni-autonomous-productizer. Exits non-zero on any invalid state so
the skill/supervisor never silently continues on corrupt state.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VALID_STATUSES = {
    "IDLE", "DISCOVERING", "PLANNING", "IMPLEMENTING", "TESTING", "BUILDING",
    "DEPLOYING", "OBSERVING", "DEBUGGING", "VALIDATING", "DOCUMENTING",
    "COMMITTING", "QUOTA_DRAINING", "SLEEPING_UNTIL_QUOTA_RESET", "RESUMING",
    "BLOCKED_FOR_HUMAN", "STOPPED", "COMPLETED",
}

REQUIRED_TOP_LEVEL = (
    "schema_version", "status", "updated_at", "project_root", "branch", "head",
    "iteration", "quota", "runtime", "working_tree", "resume_checks", "blocker",
)

# Reject keys that look like they might hold a secret, wherever they appear.
SECRET_KEY_PATTERN = re.compile(r"(api[_-]?key|secret|password|token|credential)", re.IGNORECASE)


def _walk_keys(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield f"{path}.{k}" if path else k
            yield from _walk_keys(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_keys(v, f"{path}[{i}]")


def validate(state: dict) -> list[str]:
    errors: list[str] = []

    for key in REQUIRED_TOP_LEVEL:
        if key not in state:
            errors.append(f"missing required top-level field: {key}")

    status = state.get("status")
    if status is not None and status not in VALID_STATUSES:
        errors.append(f"invalid status: {status!r} — must be one of {sorted(VALID_STATUSES)}")

    if not isinstance(state.get("schema_version"), int):
        errors.append("schema_version must be an int")

    iteration = state.get("iteration")
    if not isinstance(iteration, dict):
        errors.append("iteration must be an object")
    else:
        for field in ("id", "bottleneck", "phase", "acceptance_passed", "last_successful_step",
                      "last_failed_step", "hypothesis", "next_step"):
            if field not in iteration:
                errors.append(f"iteration missing field: {field}")

    quota = state.get("quota")
    if not isinstance(quota, dict):
        errors.append("quota must be an object")
    else:
        if "buffer_seconds" in quota and not isinstance(quota["buffer_seconds"], (int, float)):
            errors.append("quota.buffer_seconds must be numeric")

    if status == "SLEEPING_UNTIL_QUOTA_RESET" and not (state.get("quota") or {}).get("reset_at"):
        errors.append("status=SLEEPING_UNTIL_QUOTA_RESET but quota.reset_at is not set")

    for key_path in _walk_keys(state):
        leaf_name = key_path.rsplit(".", 1)[-1].split("[")[0]
        if SECRET_KEY_PATTERN.search(leaf_name):
            errors.append(f"state contains a secret-looking key: {key_path} — must never store secrets in loop state")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-file",
        default="docs/operations/AUTONOMOUS_LOOP_STATE.json",
        help="Path to the state JSON file",
    )
    parser.add_argument("--print", dest="do_print", action="store_true",
                        help="Print a human-readable summary instead of just validating")
    args = parser.parse_args()

    path = Path(args.state_file)
    if not path.exists():
        print(f"[validate_state] ERROR: state file not found: {path}", file=sys.stderr)
        return 2

    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        print(f"[validate_state] ERROR: invalid JSON in {path}: {exc}", file=sys.stderr)
        return 2

    errors = validate(state)
    if errors:
        print(f"[validate_state] INVALID — {len(errors)} error(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    if args.do_print:
        iteration = state.get("iteration", {})
        quota = state.get("quota", {})
        runtime = state.get("runtime", {})
        print(f"status:            {state.get('status')}")
        print(f"updated_at:        {state.get('updated_at')}")
        print(f"branch/head:       {state.get('branch')} @ {state.get('head')}")
        print(f"iteration.id:      {iteration.get('id')}")
        print(f"bottleneck:        {iteration.get('bottleneck')}")
        print(f"phase:             {iteration.get('phase')}")
        print(f"next_step:         {iteration.get('next_step')}")
        print(f"auto_execute:      {runtime.get('auto_execute_enabled')}")
        print(f"quota.reset_at:    {quota.get('reset_at')}")
        print(f"blocker:           {state.get('blocker')}")
    else:
        print("[validate_state] OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
