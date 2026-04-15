"""In-memory cluster simulator for E2E ReAct integration tests."""

from __future__ import annotations

from typing import Any


class SimulatedClusterState:
    """
    Stateful fake cluster: Secret credential drift → patch → healthy describe.

    Tool outputs are derived from current fields (not static per-tool magic strings).
    """

    def __init__(
        self,
        *,
        namespace: str = "multi-agent",
        secret_name: str = "chaos-pg-secret",
        secret_key: str = "APP_PASSWORD",
    ) -> None:
        self.namespace = namespace
        self.secret_name = secret_name
        self.secret_key = secret_key
        self.secret_patched = False
        self.describe_calls = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "secret_patched": self.secret_patched,
            "describe_calls": self.describe_calls,
            "namespace": self.namespace,
            "secret_name": self.secret_name,
        }

    def execute_readonly(self, tool_name: str, args: dict[str, Any]) -> str:
        """Return dynamic observation from current secret_patched flag."""
        if tool_name != "k8s_describe_resource":
            return f"[sim] unsupported readonly tool in harness: {tool_name!r}"
        rt = str(args.get("resource_type") or "")
        name = str(args.get("name") or "")
        ns = str(args.get("namespace") or "")
        self.describe_calls += 1
        if rt == "Secret" and name == self.secret_name and ns == self.namespace:
            if not self.secret_patched:
                return (
                    f"Secret {ns}/{name} type=Opaque; keys=[{self.secret_key}]; "
                    "sync_generation=7; workload reports: password authentication failed for user app "
                    "(credential mismatch vs live DB)."
                )
            return (
                f"Secret {ns}/{name} type=Opaque; keys=[{self.secret_key}]; "
                "sync_generation=8; lastRotation=ok; workload DB auth succeeds (healthy)."
            )
        return f"[sim] describe {rt} {ns}/{name}: snapshot ok (generic)."

    def apply_patch_secret(self, args: dict[str, Any]) -> None:
        """Simulate omni-executor applying k8s_patch_secret."""
        ns = str(args.get("namespace") or "").strip()
        name = str(args.get("name") or "").strip()
        if ns == self.namespace and name == self.secret_name:
            self.secret_patched = True
