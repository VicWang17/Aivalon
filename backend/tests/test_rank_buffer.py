"""热榜写路径：批量合并 ZINCRBY 测试。

验收口径两条：
  1. **合并**：同一个 (榜, 玩家) 在一个窗口里被改 N 次，只打出 1 次 ZINCRBY，且分数等于总增量
  2. **不丢**：换出的那一瞬间进来的写不能被抹掉，崩在中途的批次要能被下一轮捡回来

判据都是"ZSET 上最终的分数"和"发出去几条命令"，不是"函数有没有报错"。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import metrics, rank_buffer

NODE = "test-node"
BOARDS = list(rank_buffer.BOARD_KEYS.values())


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

    async def _clean():
        await client.delete(
            rank_buffer.PENDING_KEY, f"{rank_buffer.DRAINING_PREFIX}{NODE}", *BOARDS
        )

    await _clean()
    yield client
    await _clean()
    await client.aclose()


async def _score(redis, board, member) -> float:
    raw = await redis.zscore(rank_buffer.BOARD_KEYS[board], str(member))
    return float(raw) if raw is not None else 0.0


# ----------------------------------------------------------------------
# 合并
# ----------------------------------------------------------------------

@requires_redis
@pytest.mark.asyncio
async def test_multiple_changes_to_same_member_merge_into_one_command(redis):
    """同一个玩家在窗口内赢了 5 次，只发 1 条 ZINCRBY，分数是 5。

    这是"合并"的定义：合并不是少加分，是**少发命令**。
    分数必须还是 5，否则就不是合并而是丢数据了。
    """
    for _ in range(5):
        await rank_buffer.record([(rank_buffer.BOARD_TOTAL, 7, 1)], redis)

    # 缓冲里就只有一条：HINCRBY 在服务端已经叠好了
    assert await redis.hlen(rank_buffer.PENDING_KEY) == 1

    applied = await rank_buffer.drain_once(redis, NODE)
    assert applied == 1, f"发了 {applied} 条 ZINCRBY，没有合并"
    assert await _score(redis, rank_buffer.BOARD_TOTAL, 7) == 5.0


@requires_redis
@pytest.mark.asyncio
async def test_different_members_and_boards_stay_separate(redis):
    """合并只在同一个 (榜, 玩家) 上发生，不能把不同人或不同榜叠到一起。"""
    await rank_buffer.record([
        (rank_buffer.BOARD_TOTAL, 1, 1),
        (rank_buffer.BOARD_GOOD, 1, 1),
        (rank_buffer.BOARD_TOTAL, 2, 1),
    ], redis)
    await rank_buffer.drain_once(redis, NODE)

    assert await _score(redis, rank_buffer.BOARD_TOTAL, 1) == 1.0
    assert await _score(redis, rank_buffer.BOARD_GOOD, 1) == 1.0
    assert await _score(redis, rank_buffer.BOARD_TOTAL, 2) == 1.0
    assert await _score(redis, rank_buffer.BOARD_EVIL, 1) == 0.0


@requires_redis
@pytest.mark.asyncio
async def test_zincrby_accumulates_across_windows(redis):
    """跨窗口要累加，不是覆盖。

    这条在盯 ZINCRBY / ZADD 的区别：用 ZADD 写的话第二个窗口会把第一个窗口的分数
    覆盖掉，最终分数是 3 而不是 5——而且**只有跨窗口才看得出来**，单窗口测不出。
    """
    await rank_buffer.record([(rank_buffer.BOARD_TOTAL, 7, 2)], redis)
    await rank_buffer.drain_once(redis, NODE)
    await rank_buffer.record([(rank_buffer.BOARD_TOTAL, 7, 3)], redis)
    await rank_buffer.drain_once(redis, NODE)

    assert await _score(redis, rank_buffer.BOARD_TOTAL, 7) == 5.0


@requires_redis
@pytest.mark.asyncio
async def test_concurrent_records_do_not_lose_increments(redis):
    """50 局并发结束，一分都不能少。HINCRBY 是原子的，不需要加锁。"""
    await asyncio.gather(*[
        rank_buffer.record([(rank_buffer.BOARD_TOTAL, 7, 1)], redis) for _ in range(50)
    ])
    await rank_buffer.drain_once(redis, NODE)
    assert await _score(redis, rank_buffer.BOARD_TOTAL, 7) == 50.0


# ----------------------------------------------------------------------
# 换出：不能丢，也不能重复刷
# ----------------------------------------------------------------------

@requires_redis
@pytest.mark.asyncio
async def test_writes_during_drain_survive(redis):
    """换出之后进来的写落进新缓冲，下一轮刷出去，不能被连带抹掉。

    这是 RENAME 而不是"HGETALL 之后 DEL"的理由：DEL 会把两步之间进来的写一起删掉，
    那些增量既没上榜也不在缓冲里了。这里模拟那个时序：先换出，再写，再刷。
    """
    await rank_buffer.record([(rank_buffer.BOARD_TOTAL, 7, 1)], redis)
    tmp = f"{rank_buffer.DRAINING_PREFIX}{NODE}"
    await redis.rename(rank_buffer.PENDING_KEY, tmp)      # 手动换出，模拟刷到一半

    await rank_buffer.record([(rank_buffer.BOARD_TOTAL, 7, 1)], redis)   # 窗口边界上的写

    await rank_buffer.drain_once(redis, NODE)             # 先捡残批，再换出新的
    assert await _score(redis, rank_buffer.BOARD_TOTAL, 7) == 2.0, "换出瞬间的写丢了"


@requires_redis
@pytest.mark.asyncio
async def test_leftover_batch_is_picked_up_next_round(redis):
    """崩在 RENAME 之后、ZINCRBY 之前的残批，下一轮必须捡回来。

    临时 key 按节点分开就是为了这个：残批只有原来那个节点知道该怎么处理。
    """
    tmp = f"{rank_buffer.DRAINING_PREFIX}{NODE}"
    await redis.hset(tmp, rank_buffer._field(rank_buffer.BOARD_TOTAL, "9"), 4)

    applied = await rank_buffer.drain_once(redis, NODE)
    assert applied == 1
    assert await _score(redis, rank_buffer.BOARD_TOTAL, 9) == 4.0
    assert await redis.exists(tmp) == 0, "刷完要把临时 key 删掉"


@requires_redis
@pytest.mark.asyncio
async def test_drain_is_idempotent_when_buffer_is_empty(redis):
    """缓冲空的时候刷一次什么都不该发生，更不能报错——
    这个循环每秒都在跑，绝大多数时候缓冲就是空的。"""
    assert await rank_buffer.drain_once(redis, NODE) == 0
    assert await rank_buffer.drain_once(redis, NODE) == 0


@requires_redis
@pytest.mark.asyncio
async def test_only_one_node_wins_the_swap(redis):
    """多节点同时刷，RENAME 只有一个能成功，增量不会被打两次。

    换出这个动作本身就是原子的，所以不用再套一层分布式锁。
    """
    await rank_buffer.record([(rank_buffer.BOARD_TOTAL, 7, 1)], redis)

    results = await asyncio.gather(*[
        rank_buffer.drain_once(redis, f"node-{i}") for i in range(5)
    ])
    try:
        assert sum(results) == 1, f"增量被打了 {sum(results)} 次"
        assert await _score(redis, rank_buffer.BOARD_TOTAL, 7) == 1.0
    finally:
        await redis.delete(*[f"{rank_buffer.DRAINING_PREFIX}node-{i}" for i in range(5)])


# ----------------------------------------------------------------------
# 边界
# ----------------------------------------------------------------------

@requires_redis
@pytest.mark.asyncio
async def test_zero_delta_is_not_buffered(redis):
    """0 增量不入缓冲：输的那些人占一条 field 只是让批次白白变大。"""
    await rank_buffer.record([(rank_buffer.BOARD_TOTAL, 7, 0)], redis)
    assert await redis.exists(rank_buffer.PENDING_KEY) == 0


@requires_redis
@pytest.mark.asyncio
async def test_unknown_board_does_not_drop_the_whole_batch(redis):
    """认不出的榜名只跳过它自己，同批里正常的增量必须照样上榜。"""
    tmp = f"{rank_buffer.DRAINING_PREFIX}{NODE}"
    await redis.hset(tmp, mapping={
        rank_buffer._field("no-such-board", "1"): 1,
        rank_buffer._field(rank_buffer.BOARD_TOTAL, "2"): 3,
    })

    await rank_buffer.drain_once(redis, NODE)
    assert await _score(redis, rank_buffer.BOARD_TOTAL, 2) == 3.0


@requires_redis
@pytest.mark.asyncio
async def test_record_survives_redis_failure(redis):
    """Redis 挂了不能让对局结算失败：榜是派生数据，丢一批下次全量重建就补回来了。"""
    class Broken:
        def pipeline(self, transaction=False):
            raise ConnectionError("redis down")

    assert await rank_buffer.record([(rank_buffer.BOARD_TOTAL, 7, 1)], Broken()) == 0
    assert await rank_buffer.record([(rank_buffer.BOARD_TOTAL, 7, 1)], None) == 0


@requires_redis
@pytest.mark.asyncio
async def test_merge_ratio_metrics(redis):
    """指标口径：buffered / applied 就是合并率，这是批量合并写唯一的验收口径。"""
    buffered_before = metrics.rank_updates_buffered._value.get()
    applied_before = metrics.rank_updates_applied._value.get()

    for _ in range(6):
        await rank_buffer.record([(rank_buffer.BOARD_TOTAL, 7, 1)], redis)
    await rank_buffer.drain_once(redis, NODE)

    assert metrics.rank_updates_buffered._value.get() == buffered_before + 6
    assert metrics.rank_updates_applied._value.get() == applied_before + 1


def test_boards_are_defined_in_one_place():
    """读路径的 key 必须和写路径同源。两处各写一遍字面量早晚写歪，
    而写歪的表现是"榜看起来是空的"——写进了一个没人读的 key。"""
    from app.services import rank_service
    assert rank_service.KEY_LEADERBOARD_TOTAL == rank_buffer.BOARD_KEYS["total"]
    assert rank_service.KEY_LEADERBOARD_GOOD == rank_buffer.BOARD_KEYS["good"]
    assert rank_service.KEY_LEADERBOARD_EVIL == rank_buffer.BOARD_KEYS["evil"]
