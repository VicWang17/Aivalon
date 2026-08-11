"""WS 跨节点扇出测试：连接路由表登记/摘除、定向投递、防环。

用真实 Redis（不满足则 skip）：要验证的正是 Pub/Sub 的投递语义和 SET 成员语义，
自己写个 fake 只能测出 fake 写得对不对（同 test_node_registry.py 的理由）。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import socket_manager
from app.core.socket_manager import ConnectionManager
from app.schemas.protocol import WSMessage, WebSocketOpCode

TEST_ROOM_KEY = "aivalon:test:ws:rooms:{game_id}"
TEST_CHAN_KEY = "aivalon:test:ws:node:{node_id}"
GAME = "game-fanout-1"


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")


class FakeWS:
    """只记下收到的消息。真 WebSocket 在这里没有意义——被测的是投递路径，不是 ASGI 协议"""

    def __init__(self):
        self.sent: list[str] = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_text(self, text: str):
        self.sent.append(text)


@pytest_asyncio.fixture
async def redis(monkeypatch):
    """独立 key 空间，避免碰到运行中服务的真实连接路由表"""
    monkeypatch.setattr(socket_manager, "ROOM_NODES_KEY", TEST_ROOM_KEY)
    monkeypatch.setattr(socket_manager, "NODE_CHANNEL_KEY", TEST_CHAN_KEY)
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    await client.delete(TEST_ROOM_KEY.format(game_id=GAME))
    yield client
    await client.delete(TEST_ROOM_KEY.format(game_id=GAME))
    await client.aclose()


def _msg(text: str) -> WSMessage:
    return WSMessage(type=WebSocketOpCode.STATE_UPDATE, payload={"note": text})


async def _mk(redis, node_id: str) -> ConnectionManager:
    m = ConnectionManager()
    m.bind_cluster(redis, node_id)
    m.start()
    await asyncio.sleep(0.15)  # 等订阅真正建立，否则先发的消息会丢
    return m


async def _members(redis) -> set:
    return await redis.smembers(TEST_ROOM_KEY.format(game_id=GAME))


@pytest.mark.asyncio
async def test_single_node_broadcast_never_touches_redis():
    """单机模式不该为一个用不上的能力多付一次网络往返"""
    m = ConnectionManager()          # 不 bind_cluster
    ws = FakeWS()
    await m.connect(ws, GAME)
    assert not m.clustered
    await m.broadcast(GAME, _msg("hello"))
    assert len(ws.sent) == 1


@pytest.mark.asyncio
async def test_connect_registers_node_in_routing_table(redis):
    m = await _mk(redis, "node-a")
    try:
        await m.connect(FakeWS(), GAME)
        assert await _members(redis) == {"node-a"}
    finally:
        await m.stop()


@pytest.mark.asyncio
async def test_second_connection_on_same_node_does_not_duplicate(redis):
    """路由表是节点粒度不是连接粒度：同节点第二条连接不该再写一次 Redis"""
    m = await _mk(redis, "node-a")
    try:
        await m.connect(FakeWS(), GAME)
        await m.connect(FakeWS(), GAME)
        assert await _members(redis) == {"node-a"}
    finally:
        await m.stop()


@pytest.mark.asyncio
async def test_broadcast_reaches_connection_on_another_node(redis):
    """核心用例：连接在 A，广播从 B 发出，A 上的连接必须收到。

    这正是 D-3 接上房间转发后暴露的漏洞——动作被转发到归属节点并在那里广播，
    而客户端连接挂在入口节点上，广播打给了一屋子空气。
    """
    a = await _mk(redis, "node-a")
    b = await _mk(redis, "node-b")
    try:
        ws = FakeWS()
        await a.connect(ws, GAME)          # 连接只在 A
        await b.broadcast(GAME, _msg("from-b"))   # 广播从 B 发
        await asyncio.sleep(0.2)
        assert len(ws.sent) == 1, "跨节点广播没送达"
        assert "from-b" in ws.sent[0]
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.asyncio
async def test_local_connections_get_message_once_not_twice(redis):
    """本节点直发过就不再经 Redis 绕回来，否则本地连接会收到两遍"""
    a = await _mk(redis, "node-a")
    try:
        ws = FakeWS()
        await a.connect(ws, GAME)
        await a.broadcast(GAME, _msg("once"))
        await asyncio.sleep(0.2)
        assert len(ws.sent) == 1, f"本地连接收到 {len(ws.sent)} 遍"
    finally:
        await a.stop()


@pytest.mark.asyncio
async def test_forwarded_message_is_not_fanned_out_again(redis):
    """防环：收到扇出来的消息只做本地投递，绝不再次扇出。

    不设防的话 A→B→A 无限转发（同 room_router 一跳封顶的道理）。
    判据：B 上的连接只收到一次；若 B 收到后又扇回 A、A 再扇回 B，计数会持续增长。
    """
    a = await _mk(redis, "node-a")
    b = await _mk(redis, "node-b")
    try:
        ws_a, ws_b = FakeWS(), FakeWS()
        await a.connect(ws_a, GAME)
        await b.connect(ws_b, GAME)
        await a.broadcast(GAME, _msg("no-loop"))
        await asyncio.sleep(0.4)          # 给足时间，成环的话这里会涨上去
        assert len(ws_a.sent) == 1, f"A 收到 {len(ws_a.sent)} 遍（疑似成环）"
        assert len(ws_b.sent) == 1, f"B 收到 {len(ws_b.sent)} 遍（疑似成环）"
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.asyncio
async def test_disconnect_removes_node_from_routing_table(redis):
    m = await _mk(redis, "node-a")
    try:
        ws1, ws2 = FakeWS(), FakeWS()
        await m.connect(ws1, GAME)
        await m.connect(ws2, GAME)

        m.disconnect(ws1, GAME)
        await m.unregister_if_empty(GAME)
        assert await _members(redis) == {"node-a"}, "还有连接在，不该摘表"

        m.disconnect(ws2, GAME)
        await m.unregister_if_empty(GAME)
        assert await _members(redis) == set(), "最后一条连接断开后应摘表"
    finally:
        await m.stop()


@pytest.mark.asyncio
async def test_broadcast_to_room_without_connections_is_noop(redis):
    """没人订阅的房间广播不该报错——AI 动作在无人观战的房间里很常见"""
    m = await _mk(redis, "node-a")
    try:
        await m.broadcast(GAME, _msg("nobody"))
    finally:
        await m.stop()


@pytest.mark.asyncio
async def test_routing_table_has_ttl(redis):
    """节点崩溃来不及摘表，残留条目只能靠 key TTL 最终清掉，所以登记时必须带过期"""
    m = await _mk(redis, "node-a")
    try:
        await m.connect(FakeWS(), GAME)
        ttl = await redis.ttl(TEST_ROOM_KEY.format(game_id=GAME))
        assert 0 < ttl <= socket_manager.ROOM_NODES_TTL, f"路由表没设过期，ttl={ttl}"
    finally:
        await m.stop()


@pytest.mark.asyncio
async def test_broadcast_to_dead_node_does_not_drop_live_delivery(redis):
    """表里混进死节点条目时，活节点必须照样收到。

    曾经在扇出前按"存活节点视图"过滤目标并顺手删表项，这是个错误：
    视图只要偏一点（刚启动没同步、或拿到上一轮的旧视图），活节点就被当成死的，
    广播被静默丢弃。而多发一次给死节点的频道是无害的——没有订阅者，Redis 直接丢。
    两种错误代价不对等，所以宁可多发。
    """
    a = await _mk(redis, "node-a")
    b = await _mk(redis, "node-b")
    try:
        ws = FakeWS()
        await a.connect(ws, GAME)
        await redis.sadd(TEST_ROOM_KEY.format(game_id=GAME), "node-never-existed")
        await b.broadcast(GAME, _msg("still-arrives"))
        await asyncio.sleep(0.2)
        assert len(ws.sent) == 1, "表里有死节点条目就把活节点的广播弄丢了"
    finally:
        await a.stop()
        await b.stop()


@pytest.mark.asyncio
async def test_subscription_survives_being_dropped(redis):
    """订阅断了必须重连：断开期间本节点连接会静默收不到广播"""
    a = await _mk(redis, "node-a")
    b = await _mk(redis, "node-b")
    try:
        ws = FakeWS()
        await a.connect(ws, GAME)
        # 只踢掉 A 自己的订阅连接，模拟网络抖动。
        # 不用 CLIENT KILL TYPE pubsub——那会连带杀掉 B 和本机开发服务的订阅。
        await a.drop_subscription()
        await asyncio.sleep(2.6)          # 重连间隔 2s + 余量
        await b.broadcast(GAME, _msg("after-reconnect"))
        await asyncio.sleep(0.3)
        assert any("after-reconnect" in s for s in ws.sent), "订阅断开后没恢复"
    finally:
        await a.stop()
        await b.stop()
