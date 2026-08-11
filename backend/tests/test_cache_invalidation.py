"""事件驱动失效测试：跨进程 L1 失效广播 + 失效时机 + TTL 兜底。

跨进程用两个独立的 _L1 实例模拟两个进程——真起两个进程只能测出 pytest 会不会跑，
被测的是"广播能不能让另一份 L1 清掉"，一份独立的 L1 就够了。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import inspect

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import cache, event_flusher

GAME = "game-invalidate-1"
KEY = cache.events_key(GAME)


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    await client.delete(KEY)
    cache.l1.clear()
    yield client
    await client.delete(KEY)
    cache.l1.clear()
    await cache.stop()
    cache._redis = None
    await client.aclose()


@pytest.mark.asyncio
async def test_invalidate_clears_l1_in_another_process(redis):
    """核心用例：一个进程失效 key，另一个进程的 L1 也要清掉。

    这是 F-1 留下的洞——L2 是共享的删一次全集群可见，L1 在各进程自己的堆里，
    别的进程改了数据这个进程毫不知情，会一直供旧值到自己 TTL 到。
    """
    cache.bind(redis)
    cache.start()
    await asyncio.sleep(0.15)          # 等订阅真正建立，否则先发的消息会丢

    cache.l1.set(KEY, {"stale": True}, ttl=60)
    assert cache.l1.get(KEY)[0]

    # 模拟"另一个进程"发起失效：不碰本进程的 L1，只发广播
    await cache._publish_invalidation(KEY, redis=redis)
    await asyncio.sleep(0.2)

    assert not cache.l1.get(KEY)[0], "收到失效广播后 L1 没清掉，跨进程还在供旧值"


@pytest.mark.asyncio
async def test_invalidation_only_touches_the_named_key(redis):
    """失效是按 key 的，不能顺手清空整个 L1——那等于每次写都把全部缓存打掉"""
    cache.bind(redis)
    cache.start()
    await asyncio.sleep(0.15)

    other = cache.events_key("some-other-game")
    cache.l1.set(KEY, 1, ttl=60)
    cache.l1.set(other, 2, ttl=60)

    await cache._publish_invalidation(KEY, redis=redis)
    await asyncio.sleep(0.2)

    assert not cache.l1.get(KEY)[0]
    assert cache.l1.get(other) == (True, 2), "把无关的 key 一起清掉了"


@pytest.mark.asyncio
async def test_invalidate_clears_local_l1_without_waiting_for_broadcast(redis):
    """本进程发起失效要立刻清自己的 L1，不能等广播绕回来。

    Redis Pub/Sub 是 fire-and-forget，绕一圈至少一次往返；
    这期间本进程仍供旧值，而它明明是知情的那一方。
    """
    cache.bind(redis)
    cache.l1.set(KEY, {"stale": True}, ttl=60)
    await cache.invalidate(KEY, redis=redis)
    assert not cache.l1.get(KEY)[0], "发起方没有同步清掉自己的 L1"


@pytest.mark.asyncio
async def test_subscription_survives_being_dropped(redis):
    """订阅断了必须重连：断开期间本进程 L1 收不到失效，退化成只有 TTL 兜底"""
    cache.bind(redis)
    cache.start()
    await asyncio.sleep(0.15)

    await cache.drop_subscription()
    await asyncio.sleep(2.6)          # 重连间隔 2s + 余量

    cache.l1.set(KEY, {"stale": True}, ttl=60)
    await cache._publish_invalidation(KEY, redis=redis)
    await asyncio.sleep(0.3)
    assert not cache.l1.get(KEY)[0], "订阅断开后没恢复"


@pytest.mark.asyncio
async def test_broadcast_failure_does_not_raise(redis):
    """广播发不出去只是让别的进程多等一个 L1 TTL，不该让写路径失败。

    这正是 L1 短 TTL 作为兜底存在的意义：失效机制是优化，TTL 才是保证。
    """
    class BrokenRedis:
        async def delete(self, key):
            raise ConnectionError("boom")

        async def publish(self, ch, msg):
            raise ConnectionError("boom")

    cache.l1.set(KEY, 1, ttl=60)
    await cache.invalidate(KEY, redis=BrokenRedis())      # 不该抛
    assert not cache.l1.get(KEY)[0], "本地 L1 仍然要清掉"


@pytest.mark.asyncio
async def test_single_process_mode_needs_no_binding():
    """没 bind 时失效只清本地，不发广播也不报错（单机/测试环境）"""
    cache._redis = None
    cache.l1.set(KEY, 1, ttl=60)
    await cache.invalidate(KEY)
    assert not cache.l1.get(KEY)[0]


# ----------------------------------------------------------------------
# 失效时机
# ----------------------------------------------------------------------

def test_invalidation_happens_after_the_db_commit():
    """失效必须在刷库之后，不能在动作发生时。

    这份缓存回源的是 MySQL，而事件走 Write-Behind：动作发生时事件只进了
    Redis Stream，MySQL 里还没有。提前失效会让紧随其后的读回源到一个
    **还没刷进新事件的 MySQL**，然后把旧结果重新缓存 300 秒——
    比不失效更糟，不失效至少是旧值，提前失效是把旧值重新盖章成新值。

    判据取源码顺序：真正跑一遍 flush 需要 MySQL + 完整环境，
    而这里要钉住的恰恰是"两个调用的先后"，顺序本身就是被测对象。
    """
    src = inspect.getsource(event_flusher.flush_once)
    write = src.index("_flush_batch")
    inval = src.index("invalidate_events")
    cursor = src.index("JOURNAL_CURSOR, new_cursor")
    assert write < inval, "在刷库之前失效了缓存，读会把未刷入的旧结果重新缓存"
    assert inval < cursor, "失效应在游标推进之前，崩溃时宁可重刷重失效（都是幂等的）"


def test_invalidation_is_deduped_per_game():
    """一批 500 条事件可能只涉及几个房间，逐条失效就是几百次无用广播"""
    src = inspect.getsource(event_flusher.flush_once)
    assert "touched = {" in src, "没有按 game_id 去重"


@pytest.mark.asyncio
async def test_flush_invalidation_failure_does_not_break_flushing(redis):
    """失效失败不该让刷库回滚：数据已经落库了，最坏是缓存多留一个 TTL 的旧值"""
    src = inspect.getsource(event_flusher.flush_once)
    block = src[src.index("for game_id in touched"):]
    assert "except Exception" in block, "失效异常会冒泡把刷库一起弄失败"
