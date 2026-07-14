"""CLI: phê duyệt + phát hành durable command cho ``systemd.restart_unit`` (M1).

Operator surface tối thiểu (Bước 12) — KHÔNG portal redesign. In JSON envelope hoàn
chỉnh ra stdout, sẵn sàng POST tới ``/webhook/agent/rt/commands/enqueue``.

    python -m aoip.console.approve_systemd_restart \
        --unit nginx.service --tenant acme --approver alice \
        --mission-id mis-1 --decision-id dec-1 --incident-id inc-1 \
        --summary "nginx down, restart approved" --ttl-s 300

In ra JSON envelope; operator/CI pipe vào ``curl -d @- .../commands/enqueue``.
"""
from __future__ import annotations

import argparse
import json
import time

from aoip.command_bridge import build_durable_command


def main() -> None:
    p = argparse.ArgumentParser(description="Issue approved systemd.restart_unit command")
    p.add_argument("--unit", required=True)
    p.add_argument("--tenant", required=True)
    p.add_argument("--agent-id", required=True)
    p.add_argument("--approver", required=True)
    p.add_argument("--mission-id", required=True)
    p.add_argument("--decision-id", required=True)
    p.add_argument("--incident-id", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--ttl-s", type=int, default=300, help="Approval validity window (giây)")
    p.add_argument("--diagnosis-confidence", type=float, default=None)
    args = p.parse_args()

    now = time.time()
    command = build_durable_command(
        {
            "mission_id": args.mission_id,
            "decision_id": args.decision_id,
            "incident_id": args.incident_id,
            "capability": "systemd.restart_unit",
            "unit": args.unit,
            "summary": args.summary,
            "confidence": args.diagnosis_confidence if args.diagnosis_confidence is not None else 1.0,
            "evidence_refs": [f"operator:{args.decision_id}"],
        },
        tenant=args.tenant,
        agent_id=args.agent_id,
        approver=args.approver,
        now=now,
        ttl_s=args.ttl_s,
    )
    print(json.dumps(command, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
