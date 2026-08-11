"""集群节点注册表测试：心跳续约、宕机判死、多节点路由一致。

用真实 Redis（不满足则 skip）：这里要验证的正是 ZSET score 区间与 TIME 命令的真实语义，
自己写个 fake 只能测出 fake 写得对不对。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import node_registry
from app.core.node_registry import NodeRegistry

TEST_KEY = "aivalon:test:cluster:nodes"
# 测试用的短参数：默认 TTL 6s 会让测试跑太久
FAST_TTL = 0.4
FAST_INTERVAL = 0.1


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")


@pytest_asyncio.fixture
async def redis(monkeypatch):
    """独立 key 空间，避免碰到运行中服务的真实节点表"""
    monkeypatch.setattr(node_registry, "NODE_SET_KEY", TEST_KEY)
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    await client.delete(TEST_KEY)
    yield client
    await client.delete(TEST_KEY)
    await client.aclose()


def _mk(redis, node_id: str) -> NodeRegistry:
    return NodeRegistry(redis, node_id=node_id, ttl=FAST_TTL, interval=FAST_INTERVAL)


@pytest.mark.asyncio
async def test_heartbeat_registers_self(redis):
    """心跳把自己登记进存活集合，并把自己加进环"""
    node = _mk(redis, "node-a")
    assert node.live_nodes == []  # 心跳前环是空的

    alive = await node.heartbeat()
    assert alive == ["node-a"]
    assert node.live_nodes == ["node-a"]
    assert node.owner_of("game-1") == "node-a"
    assert node.is_mine("game-1")


@pytest.mark.asyncio
async def test_two_nodes_agree_on_ownership(redis):
    """最关键的一条：两个节点必须对"房间归谁"给出完全一致的答案。
    答案不一致就意味着同一房间被两个节点认领，单写者模型作废。"""
    a, b = _mk(redis, "node-a"), _mk(redis, "node-b")
    await a.heartbeat()
    await b.heartbeat()
    await a.heartbeat()  # a 再心跳一次，才能看到后加入的 b

    assert a.live_nodes == b.live_nodes == ["node-a", "node-b"]

    games = [f"game-{i}" for i in range(500)]
    assert [a.owner_of(g) for g in games] == [b.owner_of(g) for g in games]
    # 且两个节点各自认领的房间恰好互补，没有重叠、没有遗漏
    mine_a = {g for g in games if a.is_mine(g)}
    mine_b = {g for g in games if b.is_mine(g)}
    assert mine_a & mine_b == set()
    assert mine_a | mine_b == set(games)
    assert mine_a and mine_b  # 两边都分到了房间，不是一边独吞


@pytest.mark.asyncio
async def test_dead_node_expires_and_rooms_are_taken_over(redis):
    """B 停止心跳 → 超过 TTL 后被 A 摘除 → B 名下房间转由 A 接管。
    这就是"节点宕机迁移"的路由侧依据。"""
    a, b = _mk(redis, "node-a"), _mk(redis, "node-b")
    await a.heartbeat()
    await b.heartbeat()
    await a.heartbeat()
    assert len(a.live_nodes) == 2

    games = [f"game-{i}" for i in range(500)]
    orphaned = [g for g in games if a.owner_of(g) == "node-b"]
    assert orphaned, "测试数据里应有归属 B 的房间"

    # B 不再续约，等到超过判死阈值
    await asyncio.sleep(FAST_TTL + 0.15)
    await a.heartbeat()

    assert a.live_nodes == ["node-a"]
    # 原属 B 的房间全部转到 A，且 A 认领了全部房间
    assert all(a.owner_of(g) == "node-a" for g in orphaned)
    assert all(a.is_mine(g) for g in games)


@pytest.mark.asyncio
async def test_heartbeat_keeps_node_alive_past_ttl(redis):
    """持续心跳的节点不会被误摘——判死看的是"最后心跳时间"，不是"注册时间"。
    误摘活节点比慢几秒发现宕机严重得多（房间会被两边同时认领）。"""
    a, b = _mk(redis, "node-a"), _mk(redis, "node-b")
    await a.heartbeat()

    deadline = asyncio.get_running_loop().time() + FAST_TTL * 2
    while asyncio.get_running_loop().time() < deadline:
        await b.heartbeat()
        await asyncio.sleep(FAST_INTERVAL)

    alive = await a.heartbeat()
    assert set(alive) == {"node-a", "node-b"}


@pytest.mark.asyncio
async def test_deregister_removes_immediately(redis):
    """优雅下线不等 TTL：计划内重启的路由空窗压到一次往返"""
    a, b = _mk(redis, "node-a"), _mk(redis, "node-b")
    await a.heartbeat()
    await b.heartbeat()

    await b.deregister()
    alive = await a.heartbeat()
    assert alive == ["node-a"]


@pytest.mark.asyncio
async def test_rejoin_restores_original_routing(redis):
    """节点重启后用同一身份回来，原来的房间会漂回去——虚拟节点由节点名派生的直接结果。
    这条是"NODE_ID 建议显式配置"的理由：随机身份会导致每次重启都是一次全新的再平衡。"""
    a, b = _mk(redis, "node-a"), _mk(redis, "node-b")
    await a.heartbeat()
    await b.heartbeat()
    await a.heartbeat()

    games = [f"game-{i}" for i in range(500)]
    before = [a.owner_of(g) for g in games]

    await b.deregister()
    await a.heartbeat()
    assert a.live_nodes == ["node-a"]

    await b.heartbeat()          # 同名重新加入
    await a.heartbeat()
    assert [a.owner_of(g) for g in games] == before


@pytest.mark.asyncio
async def test_empty_ring_falls_back_to_self(redis):
    """Redis 不可用或尚未首次心跳时环为空：is_mine 返回 True 走单机降级，
    宁可自己扛也不拒绝服务（单节点部署下语义也一致）"""
    node = _mk(redis, "node-a")
    assert node.owner_of("game-1") is None
    assert node.is_mine("game-1") is True


@pytest.mark.asyncio
async def test_heartbeat_loop_survives_redis_failure(redis):
    """心跳循环不能被 Redis 抖动打死：报错记日志后下轮重试。
    本节点仍在服务，TTL 到了会被别人摘掉，Redis 恢复后自动重新加入。"""
    node = _mk(redis, "node-a")
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ConnectionError("redis down")
        return ["node-a"]

    node.heartbeat = flaky
    task = node.start()
    await asyncio.sleep(FAST_INTERVAL * 5)
    task.cancel()

    assert calls["n"] > 2, "循环应在失败后继续重试"


@pytest.mark.asyncio
async def test_uses_redis_clock_not_local_clock(redis):
    """存活判定的时间戳取自 Redis 服务端时钟，不是各节点本地时钟。
    否则机器间时钟偏移会直接变成误判：快 10 秒的节点看别人永远像过期。"""
    node = _mk(redis, "node-a")
    await node.heartbeat()

    score = await redis.zscore(TEST_KEY, "node-a")
    secs, usecs = await redis.time()
    redis_now = secs + usecs / 1_000_000
    # 心跳写入的 score 必须贴着 Redis 自己的时钟
    assert abs(score - redis_now) < 1.0


@pytest.mark.asyncio
async def test_node_id_defaults_to_unique_value():
    """未显式配置 NODE_ID 时自动生成，且同机多进程不会撞名"""
    ids = {node_registry.resolve_node_id() for _ in range(5)}
    assert len(ids) == 5
