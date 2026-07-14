"""Domain-neutral adapter contracts for the Omni control plane.

The AOIP core reasons about evidence, decisions, commands and verification.  It
must not know whether a customer target is Kubernetes, Linux, a database or a
network device.  This module is deliberately metadata-only: concrete adapters
live at the domain boundary and are registered by descriptor.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

_OPERATIONS = frozenset({"discover", "observe", "plan", "execute", "verify", "rollback"})


@runtime_checkable
class DomainAdapter(Protocol):
    """Execution seam implemented by a concrete customer-system adapter."""

    async def discover(self, scope: dict[str, Any]) -> dict[str, Any]: ...

    async def observe(self, request: dict[str, Any]) -> dict[str, Any]: ...

    async def plan(self, request: dict[str, Any]) -> dict[str, Any] | None: ...

    async def execute(self, command: dict[str, Any]) -> dict[str, Any]: ...

    async def verify(self, request: dict[str, Any]) -> dict[str, Any]: ...

    async def rollback(self, request: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AdapterCapability:
    """A typed operation exposed by one domain adapter.

    Mutating operations are fail-closed by construction: they must require an
    explicit approval and a verification phase.  The command runtime remains
    responsible for enforcing approval; this object describes the contract that
    the runtime is allowed to resolve.
    """

    name: str
    operations: tuple[str, ...] = ("observe",)
    mutating: bool = False
    requires_approval: bool = False
    verification_required: bool = False

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name or "." not in name:
            raise ValueError("capability name must be namespaced, e.g. database.restart")
        if not self.operations:
            raise ValueError("capability must expose at least one operation")
        invalid = set(self.operations) - _OPERATIONS
        if invalid:
            raise ValueError(f"unsupported capability operations: {sorted(invalid)}")
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("capability operations must be unique")
        if self.mutating and not self.requires_approval:
            raise ValueError("mutating capability requires approval")
        if self.mutating and not self.verification_required:
            raise ValueError("mutating capability requires verification")
        if self.mutating and "execute" not in self.operations:
            raise ValueError("mutating capability must expose execute")


@dataclass(frozen=True, slots=True)
class AdapterDescriptor:
    """Portable identity and capability declaration for one domain adapter."""

    name: str
    domain: str
    version: str
    capabilities: tuple[AdapterCapability, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.domain.strip() or not self.version.strip():
            raise ValueError("adapter name, domain and version are required")
        names = [cap.name for cap in self.capabilities]
        if len(set(names)) != len(names):
            raise ValueError("adapter capability names must be unique")

    def capability(self, name: str) -> AdapterCapability | None:
        return next((cap for cap in self.capabilities if cap.name == name), None)


class AdapterRegistry:
    """In-memory registry used by planning and capability discovery.

    Persistence and tenant policy stay outside this registry.  This keeps the
    domain contract deterministic and makes it safe to use in gateway/worker
    processes and tests alike.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, AdapterDescriptor] = {}

    def register(self, descriptor: AdapterDescriptor) -> None:
        key = descriptor.domain.strip().lower()
        if key in self._adapters:
            raise ValueError(f"adapter domain already registered: {descriptor.domain}")
        self._adapters[key] = descriptor

    def get(self, domain: str) -> AdapterDescriptor | None:
        return self._adapters.get(domain.strip().lower())

    def list_adapters(self) -> tuple[AdapterDescriptor, ...]:
        return tuple(self._adapters[key] for key in sorted(self._adapters))

    def resolve_capability(self, domain: str, capability: str) -> AdapterCapability | None:
        descriptor = self.get(domain)
        return descriptor.capability(capability) if descriptor else None


def default_registry() -> AdapterRegistry:
    """Return the baseline domain set; Kubernetes is intentionally not special."""

    registry = AdapterRegistry()
    registry.register(AdapterDescriptor(
        name="linux",
        domain="linux",
        version="1",
        capabilities=(
            AdapterCapability(name="host.discover", operations=("discover", "observe")),
            AdapterCapability(name="service.restart", operations=("observe", "execute", "verify", "rollback"),
                              mutating=True, requires_approval=True, verification_required=True),
        ),
    ))
    registry.register(AdapterDescriptor(
        name="kubernetes",
        domain="kubernetes",
        version="1",
        capabilities=(
            AdapterCapability(name="workload.discover", operations=("discover", "observe")),
            AdapterCapability(name="workload.restart", operations=("observe", "execute", "verify", "rollback"),
                              mutating=True, requires_approval=True, verification_required=True),
        ),
    ))
    registry.register(AdapterDescriptor(
        name="database",
        domain="database",
        version="1",
        capabilities=(
            AdapterCapability(name="database.discover", operations=("discover", "observe")),
            AdapterCapability(name="database.health", operations=("observe", "verify")),
        ),
    ))
    registry.register(AdapterDescriptor(
        name="network",
        domain="network",
        version="1",
        capabilities=(
            AdapterCapability(name="network.discover", operations=("discover", "observe")),
            AdapterCapability(name="network.verify", operations=("observe", "verify")),
        ),
    ))
    return registry


__all__ = [
    "AdapterCapability",
    "AdapterDescriptor",
    "AdapterRegistry",
    "DomainAdapter",
    "default_registry",
]
