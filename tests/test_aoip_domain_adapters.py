"""P3 contract tests: Omni core must remain domain-agnostic."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def _app() -> FastAPI:
    from gateway.routes.onboarding import router

    app = FastAPI()
    app.include_router(router)
    return app


def test_default_registry_treats_kubernetes_as_one_domain_adapter():
    from aoip.domain_adapters import default_registry

    registry = default_registry()
    domains = {item.domain for item in registry.list_adapters()}

    assert "kubernetes" in domains
    assert {"linux", "database", "network"}.issubset(domains)
    assert registry.get("kubernetes") is not None
    assert registry.get("linux") is not None


def test_adapter_capability_contract_is_typed_and_requires_verification():
    from aoip.domain_adapters import AdapterCapability, AdapterDescriptor

    descriptor = AdapterDescriptor(
        name="postgres",
        domain="database",
        version="1",
        capabilities=(
            AdapterCapability(
                name="database.restart",
                operations=("observe", "execute", "verify", "rollback"),
                mutating=True,
                requires_approval=True,
                verification_required=True,
            ),
        ),
    )

    capability = descriptor.capability("database.restart")
    assert capability is not None
    assert capability.mutating is True
    assert capability.requires_approval is True
    assert capability.verification_required is True
    assert set(capability.operations) == {"observe", "execute", "verify", "rollback"}


def test_registry_rejects_duplicate_domains_and_unknown_capabilities():
    from aoip.domain_adapters import AdapterCapability, AdapterDescriptor, AdapterRegistry

    registry = AdapterRegistry()
    adapter = AdapterDescriptor(
        name="linux-systemd",
        domain="linux",
        version="1",
        capabilities=(AdapterCapability(name="service.restart"),),
    )
    registry.register(adapter)

    with pytest.raises(ValueError, match="already registered"):
        registry.register(adapter)
    assert registry.resolve_capability("linux", "service.restart") == adapter.capabilities[0]
    assert registry.resolve_capability("linux", "missing") is None


def test_mutating_capability_must_require_approval_and_verification():
    from aoip.domain_adapters import AdapterCapability

    with pytest.raises(ValueError, match="requires approval"):
        AdapterCapability(name="unsafe.write", mutating=True, requires_approval=False)

    with pytest.raises(ValueError, match="requires verification"):
        AdapterCapability(
            name="unsafe.write",
            operations=("observe", "execute"),
            mutating=True,
            requires_approval=True,
            verification_required=False,
        )


@pytest.mark.asyncio
async def test_adapters_endpoint_exposes_domain_neutral_capabilities():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as client:
        response = await client.get("/onboarding/adapters")

    assert response.status_code == 200
    payload = response.json()
    assert {item["domain"] for item in payload["adapters"]} >= {
        "linux", "kubernetes", "database", "network"
    }
    linux = next(item for item in payload["adapters"] if item["domain"] == "linux")
    restart = next(cap for cap in linux["capabilities"] if cap["name"] == "service.restart")
    assert restart["mutating"] is True
    assert restart["requires_approval"] is True
    assert restart["verification_required"] is True
