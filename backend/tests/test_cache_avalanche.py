"""逻辑过期异步重建 + TTL 抖动（防雪崩）测试。

两条验收口径：
  1. 逻辑过期的 key 被读到时，**调用方不等回源**——拿旧值立刻返回，重建在后台
  2. 同一批 key 的物理 TTL 不相同——不会在同一秒集体到期
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import json
import time

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import cache, metrics

KEY = f"{cache.KEY_PREFIX}:test:avalanche"


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


requires_redis = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    await client.delete(KEY)
    cache.l1.clear()
    cache._inflight.clear()
    cache._rebuilding.clear()
    yield client
    await client.delete(KEY)
    cache.l1.clear()
    cache._inflight.clear()
    cache._rebuilding.clear()
    await client.aclose()


class SlowLoader:
    def __init__(self, value, delay=0.05):
        self.calls = 0
        self.value = value
        self.delay = delay

    async def __call__(self):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.value


async def _put_stale(redis, value, ago=1.0):
    """手写一个已经逻辑过期、但物理还在的信封。

    直接构造而不是等它自然过期：等 300 秒的测试没人会跑，
    而"过期"这件事在这套设计里就是 exp 字段的一个数字，构造它没有失真。
    """
    await redis.set(KEY, json.dumps({"v": value, "exp": time.time() - ago}), ex=300)


# ----------------------------------------------------------------------
# 逻辑过期：读的人不等回源
# ----------------------------------------------------------------------

@requires_redis
@pytest.mark.asyncio
async def test_stale_read_returns_old_value_without_waiting(redis):
    """逻辑过期时立刻返回旧值，不等回源。

    判据是**耗时**：loader 要睡 200ms，如果这次读等了回源，耗时必然 ≥200ms。
    这就是逻辑过期相比 singleflight 多做的那一步——singleflight 把 N 次回源压成
    1 次，但那 1 次还是有人在等；逻辑过期让谁都不等。
    """
    await _put_stale(redis, {"v": "old"})
    loader = SlowLoader({"v": "new"}, delay=0.2)

    started = time.monotonic()
    value = await cache.get_or_load(KEY, loader, redis=redis)
    elapsed = time.monotonic() - started

    assert value == {"v": "old"}, "应该先把旧值给出去"
    assert elapsed < 0.1, f"读请求等了回源（{elapsed:.3f}s），逻辑过期没生效"


@requires_redis
@pytest.mark.asyncio
async def test_background_rebuild_refreshes_the_value(redis):
    """后台重建要真的把新值写回去，否则旧值会一直被返回。"""
    await _put_stale(redis, {"v": "old"})
    loader = SlowLoader({"v": "new"}, delay=0.02)

    assert await cache.get_or_load(KEY, loader, redis=redis) == {"v": "old"}
    await asyncio.sleep(0.1)                       # 等后台重建跑完
    assert loader.calls == 1, "没有派后台重建"

    cache.l1.clear()                               # 绕开 L1 里那份旧值
    assert await cache.get_or_load(KEY, loader, redis=redis) == {"v": "new"}


@requires_redis
@pytest.mark.asyncio
async def test_fresh_value_does_not_trigger_rebuild(redis):
    """没过期就不该重建。少了这条判断，每次读都会派一个后台回源。"""
    loader = SlowLoader({"v": 1}, delay=0)
    await cache.get_or_load(KEY, loader, redis=redis)
    cache.l1.clear()
    await cache.get_or_load(KEY, loader, redis=redis)
    await asyncio.sleep(0.05)
    assert loader.calls == 1


@requires_redis
@pytest.mark.asyncio
async def test_concurrent_stale_reads_rebuild_only_once(redis):
    """N 个并发读到同一个过期 key，只派一次重建。

    少了这个去重，要防的回源洪峰会原样出现在后台——不再挡着请求，但库照样被打。
    """
    await _put_stale(redis, {"v": "old"})
    loader = SlowLoader({"v": "new"}, delay=0.05)

    results = await asyncio.gather(*[
        cache.get_or_load(KEY, loader, redis=redis) for _ in range(30)
    ])
    await asyncio.sleep(0.15)

    assert all(r == {"v": "old"} for r in results)
    assert loader.calls == 1, f"派了 {loader.calls} 次重建"


@requires_redis
@pytest.mark.asyncio
async def test_rebuild_slot_is_released_after_completion(redis):
    """重建完成要把登记清掉，否则这个 key 再也不会被重建（且 set 无界增长）。"""
    await _put_stale(redis, {"v": "old"})
    await cache.get_or_load(KEY, SlowLoader({"v": "new"}, delay=0), redis=redis)
    await asyncio.sleep(0.05)
    assert KEY not in cache._rebuilding


@requires_redis
@pytest.mark.asyncio
async def test_rebuild_failure_keeps_serving_stale(redis):
    """重建失败不影响任何人：旧值还在，读接口继续服务。

    这是逻辑过期额外白拿的一层韧性——数据源短暂不可用时，
    "当场回源"的写法会让读接口一起失败，而这里只是继续供旧值。
    """
    await _put_stale(redis, {"v": "old"})

    async def boom():
        raise RuntimeError("db down")

    assert await cache.get_or_load(KEY, boom, redis=redis) == {"v": "old"}
    await asyncio.sleep(0.05)
    assert KEY not in cache._rebuilding, "失败也要释放重建登记"

    cache.l1.clear()
    assert await cache.get_or_load(KEY, boom, redis=redis) == {"v": "old"}


@requires_redis
@pytest.mark.asyncio
async def test_stale_reads_are_counted(redis):
    """指标口径：stale 那一档的增速就是"靠旧值免掉的等待次数"。"""
    def _val():
        return metrics.cache_reads.labels(level="l2", result="stale")._value.get()

    await _put_stale(redis, {"v": "old"})
    before = _val()
    await cache.get_or_load(KEY, SlowLoader({"v": "new"}, delay=0), redis=redis)
    assert _val() == before + 1


# ----------------------------------------------------------------------
# TTL 抖动
# ----------------------------------------------------------------------

def test_physical_ttl_outlives_logical_expiry():
    """物理 TTL 必须大于逻辑过期，否则没有宽限窗口，key 一过期就物理消失——
    读到的只能是未命中，逻辑过期完全不起作用。"""
    for _ in range(50):
        assert cache._physical_ttl(300) > 300


def test_physical_ttl_is_jittered():
    """同一批 key 的 TTL 不能相同，否则它们会在同一秒集体到期。

    雪崩最麻烦的地方是**它会自我强化**：第一次集体到期后，重建出来的那批
    又被对齐到同一个到期时刻，下一轮更整齐。抖动让到期时刻自然散开。
    """
    ttls = {cache._physical_ttl(300) for _ in range(200)}
    assert len(ttls) > 10, f"TTL 几乎没有抖动: {sorted(ttls)[:5]}"

    base = 300 + cache.L2_GRACE
    assert all(abs(t - base) <= base * cache.L2_JITTER + 1 for t in ttls), \
        "抖动幅度超出 ±L2_JITTER"


def test_physical_ttl_is_never_zero():
    """TTL 传 0 给 Redis 会报错。小 TTL 叠上负向抖动可能算出 0，得兜住。"""
    assert all(cache._physical_ttl(1) >= 1 for _ in range(50))


# ----------------------------------------------------------------------
# 信封解析
# ----------------------------------------------------------------------

def test_decode_distinguishes_cached_none_from_corruption():
    """缓存下来的 None 是合法值，不能和"解析失败"混为一谈。

    要是拿"值是不是 None"当解析失败的判据，缓存住的空结果每次都会被当成
    坏数据去回源——F-3 防穿透靠的正是缓存空结果，那样等于把它废掉。
    """
    valid, fresh, value = cache._decode(cache._encode(None, 300))
    assert (valid, fresh, value) == (True, True, None)

    valid, fresh, value = cache._decode("not-json{{{")
    assert valid is False and value is None


def test_decode_marks_expired_envelope_as_stale():
    valid, fresh, value = cache._decode(json.dumps({"v": 1, "exp": time.time() - 1}))
    assert valid is True and fresh is False and value == 1


@requires_redis
@pytest.mark.asyncio
async def test_corrupt_l2_value_falls_back_to_loading(redis):
    """L2 里的脏数据不该让读接口失败，回源一次就自愈。"""
    await redis.set(KEY, "not-an-envelope", ex=300)
    loader = SlowLoader({"v": "fresh"}, delay=0)
    assert await cache.get_or_load(KEY, loader, redis=redis) == {"v": "fresh"}
    assert loader.calls == 1
