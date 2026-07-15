import pytest
import fakeredis.aioredis as aioredis

from aoip.mission import Mission, MissionState
from aoip.mission_store import MissionStore


@pytest.fixture
async def fake_redis():
    return aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_mission_store_is_tenant_scoped(fake_redis):
    store = MissionStore(fake_redis)
    mission = Mission("m-1", "onboard_tenant", "tenant-a").to(MissionState.PLANNED).to(
        MissionState.ASSIGNED).to(MissionState.IN_PROGRESS)
    await store.save("tenant-a", mission, next_action="collect evidence")

    assert (await store.get("tenant-a", "m-1"))["tenant_id"] == "tenant-a"
    assert await store.get("tenant-b", "m-1") is None
    assert len(await store.list("tenant-b")) == 0
    assert (await store.list_all())[0]["tenant_id"] == "tenant-a"


@pytest.mark.asyncio
async def test_mission_store_updates_projection_without_duplicate(fake_redis):
    store = MissionStore(fake_redis)
    mission = Mission("m-1", "goal", "scope").to(MissionState.PLANNED).to(MissionState.ASSIGNED).to(MissionState.IN_PROGRESS)
    await store.save("tenant-a", mission, updated_at=1)
    done = mission.to(MissionState.COMPLETED, completion=1.0)
    await store.save("tenant-a", done, updated_at=2)
    items = await store.list("tenant-a")
    assert len(items) == 1
    assert items[0]["state"] == "completed"
    assert items[0]["completion"] == 1.0
