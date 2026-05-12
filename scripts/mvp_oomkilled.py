#!/usr/bin/env python3
"""
MVP: OOMKilled alert → kubectl execution plan.

Usage:
    python scripts/mvp_oomkilled.py
    VLLM_BASE_URL=http://localhost:11434/v1 python scripts/mvp_oomkilled.py

Dependencies (pip install):
    pydantic>=2.0
    httpx
"""

import json
import os
import sys
import uuid

import httpx
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# 1. Input: simulated OOMKilled alert
# ---------------------------------------------------------------------------

SAMPLE_ALERT = {
    "alertname": "KubePodOOMKilled",
    "namespace": "production",
    "pod": "api-server-7d9f8b6c4-xk2pq",
    "container": "api-server",
    "severity": "critical",
    "memory_limit": "512Mi",
    "message": "Container was OOMKilled. It exceeded its memory limit.",
}


# ---------------------------------------------------------------------------
# 2. Output schema (Pydantic)
# ---------------------------------------------------------------------------

class KubectlPatch(BaseModel):
    api_version: str = "apps/v1"
    kind: str = "Deployment"
    namespace: str
    name: str
    patch: dict = Field(description="Strategic merge patch body")


class ExecutionPlan(BaseModel):
    trace_id: str
    alert_type: str = "KubePodOOMKilled"
    severity: str
    rationale: str = Field(description="One-sentence explanation of the fix")
    action: str = Field(description="Human-readable action label")
    kubectl_patch: KubectlPatch
    dry_run: bool = True

    @model_validator(mode="after")
    def patch_must_set_memory(self) -> "ExecutionPlan":
        resources = (
            self.kubectl_patch.patch
            .get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [{}])[0]
            .get("resources", {})
        )
        if not resources.get("limits", {}).get("memory"):
            raise ValueError("patch must set containers[0].resources.limits.memory")
        return self


# ---------------------------------------------------------------------------
# 3. Prompt builder
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an SRE automation agent. Given a Kubernetes OOMKilled alert, you must
respond with ONLY a JSON object — no markdown, no explanation, no code fences.

The JSON must match this exact schema:
{
  "trace_id": "<uuid>",
  "alert_type": "KubePodOOMKilled",
  "severity": "<critical|warning>",
  "rationale": "<one sentence>",
  "action": "increase_memory_limit",
  "kubectl_patch": {
    "api_version": "apps/v1",
    "kind": "Deployment",
    "namespace": "<namespace>",
    "name": "<deployment-name derived from pod name>",
    "patch": {
      "spec": {
        "template": {
          "spec": {
            "containers": [
              {
                "name": "<container-name>",
                "resources": {
                  "limits": { "memory": "<new-limit>" },
                  "requests": { "memory": "<new-request>" }
                }
              }
            ]
          }
        }
      }
    }
  },
  "dry_run": true
}

Rules:
- Derive the Deployment name by stripping the pod hash suffix (last two dash-separated segments).
- Increase memory limit by 50% rounded to the nearest 64Mi.
- Set memory request to 75% of the new limit.
- dry_run must always be true.
"""


def build_user_message(alert: dict) -> str:
    return f"Alert:\n{json.dumps(alert, indent=2)}\n\nTrace ID: {uuid.uuid4()}"


# ---------------------------------------------------------------------------
# 4. vLLM call (OpenAI-compatible)
# ---------------------------------------------------------------------------

VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"))
VLLM_MODEL = os.getenv("VLLM_MODEL", os.getenv("OLLAMA_MODEL", "qwen3.6"))


def call_vllm(system: str, user: str) -> str:
    url = f"{VLLM_BASE_URL}/v1/chat/completions"
    payload = {
        "model": VLLM_MODEL,
        "stream": False,
        "temperature": 0,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        response = httpx.post(url, json=payload, timeout=120)
        response.raise_for_status()
    except httpx.ConnectError:
        print(f"[ERROR] Cannot reach vLLM at {VLLM_BASE_URL}", file=sys.stderr)
        print("        Set VLLM_BASE_URL env var or ensure vLLM is running.", file=sys.stderr)
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(f"[ERROR] vLLM returned {e.response.status_code}: {e.response.text}", file=sys.stderr)
        sys.exit(1)

    return response.json()["choices"][0]["message"]["content"]


# ---------------------------------------------------------------------------
# 5. Parse + validate
# ---------------------------------------------------------------------------

def parse_plan(raw: str) -> ExecutionPlan:
    # Strip accidental markdown fences if the model adds them
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        print(f"[ERROR] LLM returned non-JSON:\n{raw}", file=sys.stderr)
        raise SystemExit(1) from e

    try:
        return ExecutionPlan.model_validate(data)
    except Exception as e:
        print(f"[ERROR] Schema validation failed:\n{e}", file=sys.stderr)
        raise SystemExit(1) from e


# ---------------------------------------------------------------------------
# 6. Main
# ---------------------------------------------------------------------------

def main() -> None:
    alert = SAMPLE_ALERT
    print(f"[INPUT]  Alert: {alert['alertname']} — pod={alert['pod']}")
    print(f"         Model: {VLLM_MODEL} @ {VLLM_BASE_URL}\n")

    user_msg = build_user_message(alert)
    raw = call_vllm(SYSTEM_PROMPT, user_msg)

    plan = parse_plan(raw)

    print("[OUTPUT] Validated execution plan:")
    print(json.dumps(plan.model_dump(), indent=2))

    # Print the equivalent kubectl command for humans
    patch_json = json.dumps(plan.kubectl_patch.patch)
    dry = " --dry-run=client" if plan.dry_run else ""
    print("\n[KUBECTL COMMAND]")
    print(
        f"kubectl patch deployment {plan.kubectl_patch.name}"
        f" -n {plan.kubectl_patch.namespace}"
        f" --type=strategic --patch '{patch_json}'"
        f"{dry}"
    )


if __name__ == "__main__":
    main()
