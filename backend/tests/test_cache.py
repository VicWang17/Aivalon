"""L1 + L2 两级缓存测试：命中层级、回源次数、失效、降级。

判据统一用"回源了几次"，而不是"返回值对不对"——返回值对只说明代码没崩，
缓存有没有真的生效得看数据源被打了几下。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import cache

KEY = "aivalon:cache:v1:test:k1"


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    await client.delete(KEY)
    cache.l1.clear()          # L1 是模块级单例，测试之间必须清干净
    yield client
    await client.delete(KEY)
    cache.l1.clear()
    await client.aclose()


class Counter:
    """记回源次数。缓存生效的唯一硬判据就是这个数字不再涨。"""

    def __init__(self, value=None):
        self.calls = 0
        self.value = value if value is not None else {"v": 1}

    def __call__(self):
        self.calls += 1
        return self.value


# ----------------------------------------------------------------------
# L1 本身
# ----------------------------------------------------------------------

def test_l1_expires_by_ttl():
    l1 = cache._L1()
    l1.set("k", "v", ttl=0.05)
    assert l1.get("k") == (True, "v")
    import time
    time.sleep(0.08)
    assert l1.get("k") == (False, None), "L1 没按 TTL 过期"


def test_l1_distinguishes_cached_none_from_miss():
    """缓存里存着 None 和"没缓存"是两件事。

    返回 None 表示未命中的写法，会让"空结果缓存"（F-3 防穿透要用）永远失效——
    每次都判定未命中、每次都回源，防的那个穿透原封不动。
    """
    l1 = cache._L1()
    l1.set("k", None)
    assert l1.get("k") == (True, None)
    assert l1.get("missing") == (False, None)


def test_l1_is_bounded():
    """进程内缓存必须有上限：key 里带 game_id 这种无界维度时，不淘汰就是内存泄漏"""
    l1 = cache._L1(maxsize=8)
    for i in range(50):
        l1.set(f"k{i}", i)
    assert len(l1) <= 8, f"L1 涨到了 {len(l1)} 条"


def test_l1_evicts_expired_before_live_entries():
    """淘汰先清过期的，别把还有效的热条目挤掉"""
    l1 = cache._L1(maxsize=3)
    l1.set("stale", 1, ttl=0.01)
    l1.set("hot", 2, ttl=60)
    l1.set("hot2", 3, ttl=60)
    import time
    time.sleep(0.03)
    l1.set("new", 4, ttl=60)
    assert l1.get("hot") == (True, 2), "有效条目被过期条目挤掉了"


# ----------------------------------------------------------------------
# 两级协作
# ----------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")
async def test_first_read_loads_from_source_then_serves_from_cache(redis):
    loader = Counter()
    a = await cache.get_or_load(KEY, loader, redis=redis)
    b = await cache.get_or_load(KEY, loader, redis=redis)
    assert a == b == {"v": 1}
    assert loader.calls == 1, f"缓存没挡住回源，回源了 {loader.calls} 次"


@pytest.mark.asyncio
@pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")
async def test_l2_serves_when_l1_is_cold(redis):
    """L1 空了但 L2 还在时不该回源——这正是两级缓存的意义：
    进程重启或 L1 过期后，仍然有一层挡在数据源前面。"""
    loader = Counter()
    await cache.get_or_load(KEY, loader, redis=redis)
    cache.l1.clear()                       # 模拟 L1 过期/进程重启
    value = await cache.get_or_load(KEY, loader, redis=redis)
    assert value == {"v": 1}
    assert loader.calls == 1, "L1 冷了就回源，L2 白设了"


@pytest.mark.asyncio
@pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")
async def test_l2_hit_refills_l1(redis):
    """命中 L2 要回填 L1，否则热点每次都要付一次网络往返 + 反序列化"""
    await cache.get_or_load(KEY, Counter(), redis=redis)
    cache.l1.clear()
    await cache.get_or_load(KEY, Counter(), redis=redis)
    assert cache.l1.get(KEY)[0], "L2 命中后没回填 L1"


@pytest.mark.asyncio
@pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")
async def test_invalidate_clears_both_levels(redis):
    loader = Counter()
    await cache.get_or_load(KEY, loader, redis=redis)
    await cache.invalidate(KEY, redis=redis)
    await cache.get_or_load(KEY, loader, redis=redis)
    assert loader.calls == 2, "失效后没有回源，两级里还留着旧值"


@pytest.mark.asyncio
@pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")
async def test_l1_ttl_bounds_cross_process_staleness(redis):
    """L1 的 TTL 就是它的一致性上限。

    别的节点改了数据、连 L2 一起清了，本进程的 L1 仍会返回旧值直到自己过期。
    这不是 bug，是 L1 的固有代价——也正因如此 L1 的 TTL 必须取"能容忍多久脏读"，
    而不是随手取个大数字。
    """
    loader = Counter()
    await cache.get_or_load(KEY, loader, redis=redis, l1_ttl=0.05)
    await redis.delete(KEY)                # 模拟别的节点失效了 L2
    stale = await cache.get_or_load(KEY, loader, redis=redis, l1_ttl=0.05)
    assert stale == {"v": 1} and loader.calls == 1, "L1 应该还在供旧值"
    await asyncio.sleep(0.08)
    await cache.get_or_load(KEY, loader, redis=redis, l1_ttl=0.05)
    assert loader.calls == 2, "L1 过期后应该重新回源"


# ----------------------------------------------------------------------
# 降级
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_down_falls_through_to_source():
    """L2 挂了要能穿透到数据源。

    缓存是加速手段，不是可用性依赖——Redis 故障时读接口应该变慢，而不是报错。
    """
    class BrokenRedis:
        async def get(self, key):
            raise ConnectionError("boom")

        async def set(self, key, value, ex=None):
            raise ConnectionError("boom")

    cache.l1.clear()
    loader = Counter()
    value = await cache.get_or_load(KEY, loader, redis=BrokenRedis())
    assert value == {"v": 1} and loader.calls == 1


@pytest.mark.asyncio
async def test_works_without_redis_at_all():
    """单机/测试环境不给 redis 也要能跑，退化成只有 L1"""
    cache.l1.clear()
    loader = Counter()
    await cache.get_or_load(KEY, loader)
    await cache.get_or_load(KEY, loader)
    assert loader.calls == 1, "无 L2 时 L1 也该挡住第二次回源"


@pytest.mark.asyncio
async def test_async_loader_is_supported():
    """回源大多是同步 ORM 查询，但异步的也要能用，不该逼调用方多包一层"""
    cache.l1.clear()
    calls = []

    async def loader():
        calls.append(1)
        return {"v": 2}

    assert await cache.get_or_load(KEY, loader) == {"v": 2}
    assert await cache.get_or_load(KEY, loader) == {"v": 2}
    assert len(calls) == 1
