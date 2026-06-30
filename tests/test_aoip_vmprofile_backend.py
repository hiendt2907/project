"""Tests: real VMProfile → understand_host loop (P1 Discovery → P3 Model → P4 Interview).

Runtime ép framework: dữ liệu discovery THẬT (shape của ``run_vm_discovery``) chảy
qua đúng pipeline understand_host đã có — không verb mới, không noun mới. Join
listeners↔services; running-service-không-có-cổng và cổng-không-chủ → Unknown
(interview, never assume).
"""
from __future__ import annotations

import pytest

from aoip.capabilities.understand_host import understand_host
from aoip.capability import CapabilityState
from aoip.discovery_backend import VMProfileDiscoveryBackend
from aoip.system_model import SystemModel
from aoip.understanding import UnderstandingContext


def _ctx(backend: VMProfileDiscoveryBackend, host: str = "db-01") -> UnderstandingContext:
    return UnderstandingContext(
        host=host,
        scope=f"acme/{host}",
        backend=backend,
        capability=CapabilityState(capability_id="understand_host", scope=f"acme/{host}"),
        model=SystemModel(scope=f"acme/{host}"),
    )


# Profile shape khớp run_vm_discovery() output.
_PROFILE = {
    "agent_id": "ag-1",
    "hostname": "db-01",
    "role": "database_server",
    "services": [
        {"name": "mariadb", "status": "running"},
        {"name": "nginx", "status": "running"},
        {"name": "cron", "status": "running"},  # chạy nhưng không listen cổng nào
    ],
    "listeners": [
        {"port": 3306, "service": "mariadbd"},
        {"port": 80, "service": "nginx"},
        {"port": 9999, "service": ""},  # cổng mở nhưng không rõ chủ
    ],
}


async def test_listeners_become_verified_facts():
    ctx = _ctx(VMProfileDiscoveryBackend(_PROFILE))
    await understand_host(ctx)

    ports = {f.obj for f in ctx.model.facts if f.predicate == "exposes_port"}
    assert "3306" in ports and "80" in ports
    # entity host được xây từ Fact thật.
    assert ctx.model.entities == {"host:db-01"}
    assert all(f.provenance for f in ctx.model.facts)


async def test_running_service_without_port_triggers_interview():
    ctx = _ctx(VMProfileDiscoveryBackend(_PROFILE))
    await understand_host(ctx)

    unknowns = {c.blocking_unknown for c in ctx.communications}
    # cron chạy nhưng không có listener → không tự bịa cổng, phải hỏi.
    assert "service_port:cron" in unknowns
    # cổng 9999 mở nhưng không rõ process chủ → hỏi.
    assert "port_owner:9999" in unknowns


async def test_no_hallucinated_ownership():
    ctx = _ctx(VMProfileDiscoveryBackend(_PROFILE))
    await understand_host(ctx)
    assert not any(f.predicate == "owned_by" for f in ctx.model.facts)


async def test_empty_profile_yields_no_facts_no_crash():
    ctx = _ctx(VMProfileDiscoveryBackend({"hostname": "x", "services": [], "listeners": []}), host="x")
    await understand_host(ctx)
    assert ctx.model.facts == ()
    assert ctx.communications == []


async def test_open_ports_alias_supported():
    # run_vm_discovery phát cả "open_ports" lẫn "listeners" (cùng nội dung).
    profile = {"hostname": "h", "services": [], "open_ports": [{"port": 6379, "service": "redis-server"}]}
    ctx = _ctx(VMProfileDiscoveryBackend(profile), host="h")
    await understand_host(ctx)
    ports = {f.obj for f in ctx.model.facts if f.predicate == "exposes_port"}
    assert ports == {"6379"}
