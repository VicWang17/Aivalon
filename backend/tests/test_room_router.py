"""房间路由接入请求路径的测试：什么时候本地处理、什么时候转发、以及防环。

这一层的 bug 特征是"单机永远正常、多节点才炸"，所以分支判断必须逐条锁死。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.core import node_registry, room_router
from app.core.hash_ring import HashRing
from app.core.room_router import FORWARD_HEADER


class FakeRequest:
    """只用到 headers / url.path / method，不值得起真 ASGI 请求"""

    def __init__(self, headers=None, path="/api/v1/games/g1/action", method="POST"):
        self.headers = headers or {}
        self.method = method
        self.url = type("U", (), {"path": path})()
        self.query_params = {}


class FakeRegistry:
    """替身注册表：直接指定环内容与地址表，绕开 Redis 与心跳"""

    def __init__(self, node_id, nodes, addrs=None):
        self.node_id = node_id
        self._ring = HashRing(nodes)
        self._addrs = addrs or {}

    def owner_of(self, game_id):
        return self._ring.get_node(game_id)

    def addr_of(self, node_id):
        return self._addrs.get(node_id)


@pytest.fixture
def restore_registry():
    original = node_registry.registry
    yield
    node_registry.registry = original


def _install(monkeypatch, registry):
    monkeypatch.setattr(node_registry, "registry", registry)


def _find_game_owned_by(registry, owner, limit=5000):
    """构造一个归属指定节点的 game_id——路由是哈希决定的，不能随手编一个"""
    for i in range(limit):
        gid = f"game-{i}"
        if registry.owner_of(gid) == owner:
            return gid
    raise AssertionError(f"未找到归属 {owner} 的 game_id")


def test_no_registry_means_local(monkeypatch, restore_registry):
    """注册表未初始化（单机/测试环境）：一律本地处理，不能因为集群没起来就拒绝服务"""
    _install(monkeypatch, None)
    assert room_router.should_forward("g1", FakeRequest()) is None


def test_own_room_is_local(monkeypatch, restore_registry):
    """房间归本节点：正常路径，本地处理"""
    reg = FakeRegistry("node-a", ["node-a", "node-b"],
                       {"node-a": "http://a:8000", "node-b": "http://b:8000"})
    _install(monkeypatch, reg)
    mine = _find_game_owned_by(reg, "node-a")
    assert room_router.should_forward(mine, FakeRequest()) is None


def test_foreign_room_is_forwarded(monkeypatch, restore_registry):
    """房间归别的节点：转发到该节点，且保留原始请求路径"""
    reg = FakeRegistry("node-a", ["node-a", "node-b"],
                       {"node-a": "http://a:8000", "node-b": "http://b:8000"})
    _install(monkeypatch, reg)
    theirs = _find_game_owned_by(reg, "node-b")

    target = room_router.should_forward(
        theirs, FakeRequest(path=f"/api/v1/games/{theirs}/action"))
    assert target == f"http://b:8000/api/v1/games/{theirs}/action"


def test_already_forwarded_never_forwards_again(monkeypatch, restore_registry):
    """防环的核心：带转发标记的请求一律本地处理，一跳封顶。

    两节点心跳间隙视图可能短暂不一致（A 认为归 B，B 认为归 A），
    不设防就是 A→B→A→B 无限转发，一次请求打爆两个节点。"""
    reg = FakeRegistry("node-a", ["node-a", "node-b"],
                       {"node-a": "http://a:8000", "node-b": "http://b:8000"})
    _install(monkeypatch, reg)
    theirs = _find_game_owned_by(reg, "node-b")

    # 不带标记会转发，带标记必须本地处理——同一个 game_id 对比，排除是 id 选得巧
    assert room_router.should_forward(theirs, FakeRequest()) is not None
    assert room_router.should_forward(
        theirs, FakeRequest(headers={FORWARD_HEADER: "1"})) is None


def test_empty_ring_falls_back_to_local(monkeypatch, restore_registry):
    """环为空（Redis 不可用或尚未首次心跳）：单机降级，自己扛"""
    _install(monkeypatch, FakeRegistry("node-a", []))
    assert room_router.should_forward("g1", FakeRequest()) is None


def test_owner_without_address_falls_back_to_local(monkeypatch, restore_registry):
    """归属节点在环上但地址表里没它（刚加入、地址还没同步）：
    本地兜底优于报错——转发不出去不该让玩家的动作失败"""
    reg = FakeRegistry("node-a", ["node-a", "node-b"], {"node-a": "http://a:8000"})
    _install(monkeypatch, reg)
    theirs = _find_game_owned_by(reg, "node-b")
    assert room_router.should_forward(theirs, FakeRequest()) is None


def test_addr_trailing_slash_does_not_double_up(monkeypatch, restore_registry):
    """地址配成 http://b:8000/ 时不能拼出 //api/v1/...——配置容错"""
    reg = FakeRegistry("node-a", ["node-a", "node-b"],
                       {"node-b": "http://b:8000/"})
    _install(monkeypatch, reg)
    theirs = _find_game_owned_by(reg, "node-b")
    target = room_router.should_forward(
        theirs, FakeRequest(path=f"/api/v1/games/{theirs}/action"))
    assert target == f"http://b:8000/api/v1/games/{theirs}/action"


def test_routing_is_consistent_for_same_room(monkeypatch, restore_registry):
    """同一房间的每次请求都必须给出同一个决定，否则动作会散落到两个节点"""
    reg = FakeRegistry("node-a", ["node-a", "node-b", "node-c"],
                       {n: f"http://{n}:8000" for n in ["node-a", "node-b", "node-c"]})
    _install(monkeypatch, reg)
    decisions = {room_router.should_forward("game-stable", FakeRequest())
                 for _ in range(50)}
    assert len(decisions) == 1


@pytest.mark.asyncio
async def test_forward_passes_through_critical_headers(monkeypatch, restore_registry):
    """转发必须带上鉴权、幂等键、trace_id：
    少了鉴权下游 401；少了幂等键重试语义在转发后失效；少了 trace_id 跨节点排障断链。
    同时必须打上转发标记，否则防环失效。"""
    captured = {}

    class FakeResponse:
        status_code = 200
        content = b'{"ok":true}'
        headers = {"content-type": "application/json"}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, content=None, headers=None, params=None):
            captured.update(method=method, url=url, headers=headers, content=content)
            return FakeResponse()

    monkeypatch.setattr(room_router.httpx, "AsyncClient", FakeClient)

    req = FakeRequest(headers={
        "authorization": "Bearer tok",
        "x-internal-secret": "sec",
        "x-idempotency-key": "idem-1",
        "x-request-id": "trace-1",
        "content-type": "application/json",
        "host": "should-not-be-forwarded",
    })
    resp = await room_router.forward("http://b:8000/api/v1/games/g1/action", req, b"{}")

    assert resp.status_code == 200
    h = captured["headers"]
    assert h[FORWARD_HEADER] == "1"          # 防环标记
    assert h["authorization"] == "Bearer tok"
    # 内部密钥：AI worker 的 ai_action 靠它鉴权，漏了会固定 403、对局静默卡死
    assert h["x-internal-secret"] == "sec"
    assert h["x-idempotency-key"] == "idem-1"
    assert h["x-request-id"] == "trace-1"
    # 连接级头不能透传，由 httpx 自己算
    assert "host" not in h
    assert captured["method"] == "POST"
    assert captured["content"] == b"{}"


@pytest.mark.asyncio
async def test_forward_omits_absent_optional_headers(monkeypatch, restore_registry):
    """没有幂等键/trace_id 时不要塞空值进去——空 Authorization 会让下游把请求当匿名"""
    captured = {}

    class FakeResponse:
        status_code = 200
        content = b"{}"
        headers = {"content-type": "application/json"}

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, content=None, headers=None, params=None):
            captured.update(headers=headers)
            return FakeResponse()

    monkeypatch.setattr(room_router.httpx, "AsyncClient", FakeClient)
    await room_router.forward("http://b:8000/x", FakeRequest(), b"{}")

    for absent in ("authorization", "x-idempotency-key", "x-request-id"):
        assert absent not in captured["headers"]


@pytest.mark.asyncio
async def test_rate_limiter_skips_forwarded_requests():
    """限流只在入口节点算一次：转发后的请求再算一次会让用户配额凭空减半，
    且计几次取决于房间落在哪个节点，行为不可预期。"""
    from app.core.rate_limit import SkipForwardedRateLimiter

    limiter = SkipForwardedRateLimiter(times=1, seconds=1)
    called = {"n": 0}

    async def boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("转发请求不应进入限流逻辑")

    # 带转发标记：直接放行，不触碰父类逻辑（父类未初始化 FastAPILimiter，一碰就炸）
    import app.core.rate_limit as rl
    monkey = rl.RateLimiter.__call__
    try:
        rl.RateLimiter.__call__ = boom
        assert await limiter(FakeRequest(headers={FORWARD_HEADER: "1"}), None) is None
        assert called["n"] == 0
        # 不带标记：必须走父类限流逻辑
        with pytest.raises(AssertionError):
            await limiter(FakeRequest(), None)
    finally:
        rl.RateLimiter.__call__ = monkey
