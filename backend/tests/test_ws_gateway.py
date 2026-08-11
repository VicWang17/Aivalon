"""独立 WS 网关测试：职责边界 + 不进哈希环 + 等级判定只读快照。

这一组测的不是"功能对不对"，而是"拆分有没有真的拆开"。
职责边界是靠约定维持的，而约定会在某次"就加一行 import"里悄悄失效——
所以必须有测试把它钉住，否则网关会慢慢长回一个完整的业务进程。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ast
import json

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import node_registry
from app.core.socket_manager import Tier
from app.core.ws_tier import SNAPSHOT_KEY, resolve_tier

GAME = "game-gateway-1"


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    await client.delete(SNAPSHOT_KEY.format(game_id=GAME))
    yield client
    await client.delete(SNAPSHOT_KEY.format(game_id=GAME))
    await client.aclose()


def _snapshot(*user_ids) -> str:
    """只造 resolve_tier 真正会读的字段。

    刻意不构造完整 GameState：网关判等级只需要座位表，
    用完整对象反而会掩盖"它其实不需要业务模型"这个事实。
    """
    return json.dumps({"players": [{"user_id": uid, "seat_id": i}
                                   for i, uid in enumerate(user_ids)]})


# ----------------------------------------------------------------------
# 职责边界：网关不能背着业务层跑
# ----------------------------------------------------------------------

def _imported_modules(*rel_parts) -> set:
    """取一个文件真正 import 了哪些模块（含函数体内的延迟 import）。

    用 AST 而不是 grep 源码文本：注释和文档字符串里会正常提到这些模块名
    （本文件的说明就提到了），grep 会把说明当成违规。
    也不用 sys.modules：主进程里业务模块早被别处导入过了，查模块表分辨不出是谁导的。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, *rel_parts), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    return names


# 网关不该碰的东西：业务服务层、房间 Actor、AI、Celery 任务。
# app.models.user / app.db.base 不在列——握手鉴权要查用户，这是网关的本职工作。
BUSINESS_MARKERS = ("app.services", "app.core.room_actor", "app.core.celery_app",
                    "app.tasks", "ai_service", "rule_engine")


def test_ws_router_does_not_import_business_layer():
    """ws 路由不许 import 业务层。

    拆分的收益全在这一条上：网关只拿 socket，才能不跟着业务代码重启。
    一旦这里导入了服务层，网关就会因为业务层的任何 import 错误起不来，
    也会跟着业务发版一起重启——而重启网关等于所有长连接同时掉线。
    职责边界靠约定维持，而约定会在某次"就加一行 import"里悄悄失效，所以要钉住。
    """
    imported = _imported_modules("app", "routers", "ws.py")
    for mod in imported:
        assert not any(bad in mod for bad in BUSINESS_MARKERS), \
            f"ws 路由把业务层拉进来了: {mod}"


def test_gateway_does_not_join_the_hash_ring():
    """网关绝不能进一致性哈希环。

    环的数据源是 node_registry 的 NODE_SET_KEY，而进环的唯一入口是 init() + 心跳。
    网关一旦进环，就会有 1/N 的房间被分配给一个没有业务逻辑、没有 Actor 的进程，
    转发过去的动作无人处理。身份和"是否参与房间归属"是两件事，网关只要前者。
    判据取"根本不导入 node_registry"，比"没调 init" 更难绕过。
    """
    imported = _imported_modules("app", "gateway.py")
    assert not any("node_registry" in m for m in imported), \
        "网关导入了节点注册，有进环风险——房间会被分给没有业务逻辑的进程"
    for mod in imported:
        assert not any(bad in mod for bad in BUSINESS_MARKERS), \
            f"网关把业务层拉进来了: {mod}"


def test_gateway_only_mounts_ws_router():
    """网关只挂 WS 路由：挂上业务路由等于把业务流量又引回这个进程，拆分就没意义了"""
    from app import gateway

    paths = [r.path for r in gateway.app.routes]
    assert any("/api/v1/ws" in p for p in paths), "WS 路由没挂上"
    for business in ("/api/v1/games", "/api/v1/auth", "/api/v1/users"):
        assert not any(p.startswith(business) for p in paths), \
            f"网关挂了业务路由: {business}"


def test_gateway_id_is_distinct_from_node_id(monkeypatch):
    """网关身份必须与业务节点身份分开。

    同机部署时若共用一个 id，两个进程会订阅同一个专属频道、都以为自己持有该房间连接。
    """
    from app.gateway import resolve_gateway_id

    monkeypatch.setattr("app.core.config.settings.GATEWAY_ID", "", raising=False)
    monkeypatch.delenv("GATEWAY_ID", raising=False)
    gw = resolve_gateway_id()
    assert gw.startswith("gw-"), f"网关 id 没有可辨识前缀: {gw}"
    assert gw != node_registry.resolve_node_id()


# ----------------------------------------------------------------------
# 等级判定
# ----------------------------------------------------------------------

pytestmark_redis = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")


@pytest.mark.asyncio
@pytestmark_redis
async def test_seat_holder_is_a_player(redis):
    await redis.set(SNAPSHOT_KEY.format(game_id=GAME), _snapshot(7, 8, 9))
    assert await resolve_tier(redis, GAME, 8) is Tier.PLAYER


@pytest.mark.asyncio
@pytestmark_redis
async def test_non_seat_holder_is_a_spectator(redis):
    await redis.set(SNAPSHOT_KEY.format(game_id=GAME), _snapshot(7, 8, 9))
    assert await resolve_tier(redis, GAME, 99) is Tier.SPECTATOR


@pytest.mark.asyncio
@pytestmark_redis
async def test_missing_snapshot_falls_back_to_spectator(redis):
    """查不到房间按旁观者接入，而不是拒连。

    刚建局还没落盘就是这种情况。宁可把玩家误判成旁观者（只慢半秒），
    也不要因为一次读失败把人挡在门外。
    """
    assert await resolve_tier(redis, GAME, 8) is Tier.SPECTATOR


@pytest.mark.asyncio
@pytestmark_redis
async def test_corrupt_snapshot_does_not_break_handshake(redis):
    """快照是坏数据也不能让握手抛异常——降级成旁观者即可"""
    await redis.set(SNAPSHOT_KEY.format(game_id=GAME), "not-json{{{")
    assert await resolve_tier(redis, GAME, 8) is Tier.SPECTATOR


@pytest.mark.asyncio
async def test_redis_unavailable_falls_back_to_spectator():
    """Redis 不可用时握手仍要能完成：连接层不该被一次读失败拖死"""
    class BrokenRedis:
        async def get(self, key):
            raise ConnectionError("boom")

    assert await resolve_tier(BrokenRedis(), GAME, 8) is Tier.SPECTATOR
    assert await resolve_tier(None, GAME, 8) is Tier.SPECTATOR
