"""应用层滑动窗口限流 测试。

验收口径四条：
  1. **没有可利用的窗口边界**——这是换掉固定窗口的全部理由
  2. **超限时不写入**：被拒的请求不该把窗口往后推，否则重试的客户端把自己永久锁死
  3. **scope 互相独立**：一个接口被刷不能连带限死无关功能
  4. **读不到 Redis 要放行**：限流器是保护机制，不是新的单点

判据是"放行了几次"和"什么时候恢复"，不是"函数有没有报错"。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import HTTPException

from app.core import metrics, sliding_window


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")

SCOPES = ("t1", "t2", "action", "create_game")


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    keys = [f"{sliding_window.KEY_PREFIX}{s}" for s in SCOPES]
    keys += [f"{sliding_window.KEY_PREFIX}{s}:ip:unknown" for s in SCOPES]
    await client.delete(*keys)
    sliding_window.bind(client)
    yield client
    await client.delete(*keys)
    sliding_window._script = None
    await client.aclose()


class FakeRequest:
    """限流器只用到 headers 和 client.host"""

    def __init__(self, headers=None, host="1.2.3.4"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": host})()


# ----------------------------------------------------------------------
# 窗口边界：这是换掉固定窗口的全部理由
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_window_has_no_exploitable_boundary(redis):
    """窗口过半时仍然拒——固定窗口在这里已经清零了。

    固定窗口（`SET px` + `INCR`）的 key 从**第一个请求**起算过期，所以到点一次性清零，
    卡着边界前后各来一波就能挤进 **2 倍**：上小时最后一秒建 10 局、下小时第一秒再建
    10 局，**2 秒内建了 20 局**，而每次都"没超过 10 局/小时"。
    滑动窗口是"往前看一整个窗口"，没有"整点"这个可利用的东西。
    """
    for _ in range(3):
        assert (await sliding_window.check("t1", 1.0, 3))[0] >= 0

    remaining, retry_ms = await sliding_window.check("t1", 1.0, 3)
    assert remaining == -1
    assert retry_ms > 0

    # 过半：固定窗口此刻多半已经清零，滑动窗口必须还在拒
    await asyncio.sleep(0.5)
    assert (await sliding_window.check("t1", 1.0, 3))[0] == -1, "窗口边界可以被利用"

    # 最早那批滑出窗口后才恢复
    await asyncio.sleep(0.6)
    assert (await sliding_window.check("t1", 1.0, 3))[0] >= 0, "配额没有随时间滑出"


@pytest.mark.asyncio
async def test_quota_recovers_gradually_not_all_at_once(redis):
    """配额是一条条滑出来的，不是到点整批返还。

    这决定了被限之后的体验：滑动窗口下客户端能持续以限定速率通过，
    固定窗口下它得干等到下个窗口、然后瞬间放行一整批（那一批本身就是个小峰值）。
    """
    # 三次请求间隔开，让它们在不同时刻滑出
    for _ in range(3):
        await sliding_window.check("t2", 0.6, 3)
        await asyncio.sleep(0.15)

    # 此刻最早那条（0.45 秒前）还在窗口内
    assert (await sliding_window.check("t2", 0.6, 3))[0] == -1

    await asyncio.sleep(0.2)  # 只等第一条滑出
    assert (await sliding_window.check("t2", 0.6, 3))[0] >= 0
    # 紧接着再来一次：只滑出一条，所以只放行一条
    assert (await sliding_window.check("t2", 0.6, 3))[0] == -1, "一次滑出了不止一条配额"


# ----------------------------------------------------------------------
# 超限时的行为
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rejected_requests_do_not_extend_the_window(redis):
    """被拒的请求不写入，所以不会把窗口往后推。

    写了的话，**一个还在重试的客户端会把自己永久锁死**：每次被拒都刷新一次窗口，
    永远等不到解封。惩罚性延长是另一回事（要做也该显式做），不该是限流的副作用。
    """
    for _ in range(2):
        await sliding_window.check("t1", 1.0, 2)

    # 在窗口内持续重试（8 × 0.05 ≈ 0.4s，明显短于 1s 窗口，期间不该有配额滑出）
    for _ in range(8):
        assert (await sliding_window.check("t1", 1.0, 2))[0] == -1
        await asyncio.sleep(0.05)

    # 等最早那批滑出。若被拒的请求也写了进去，此刻窗口已被重试推到更晚，这里会红
    await asyncio.sleep(0.7)
    assert (await sliding_window.check("t1", 1.0, 2))[0] >= 0, "重试把自己永久锁死了"


@pytest.mark.asyncio
async def test_zset_never_grows_beyond_limit(redis):
    """ZSET 长度上界就是 limit，不随打进来的请求数增长。

    滑动窗口日志的代价是内存 O(窗口内请求数)，看着像个风险——
    但**先 trim 再计数、超了就不写**，所以上界是 `times` 本身。
    这也划定了适用范围：`times` 小（1/秒、10/小时）时很划算，
    `times` 很大（网关层 200/s）就该用令牌桶——**令牌桶只存 2 个数字，与流量无关**。
    """
    for _ in range(50):
        await sliding_window.check("t1", 10, 3)

    assert await redis.zcard(f"{sliding_window.KEY_PREFIX}t1") == 3


@pytest.mark.asyncio
async def test_key_expires_so_keys_do_not_pile_up(redis):
    """key 带 TTL。按用户维度计数时，没 TTL 的话
    **key 的数量等于历史用户数而不是活跃用户数**。"""
    await sliding_window.check("t1", 2, 5)
    ttl = await redis.pttl(f"{sliding_window.KEY_PREFIX}t1")
    assert 0 < ttl <= 3100


# ----------------------------------------------------------------------
# 原子性与隔离
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_checks_allow_exactly_the_limit(redis):
    """并发打进来时放行数**恰好**等于 limit。

    「trim → 计数 → 判断 → 写入」是读改写序列，多进程共享一个 key。
    分成多条命令的话，两个进程同时数到"还剩 1 个"，各自都放行。
    Lua 在 Redis 里单线程整段执行才原子。
    """
    results = await asyncio.gather(*[sliding_window.check("t1", 10, 5) for _ in range(40)])
    allowed = sum(1 for remaining, _ in results if remaining >= 0)
    assert allowed == 5, f"放行了 {allowed} 次，配额只有 5"


@pytest.mark.asyncio
async def test_unique_members_so_same_instant_requests_all_count(redis):
    """同一瞬间的并发请求要各计一条，不能互相覆盖。

    ZSET 的 member 相同会被当成同一条覆盖掉。如果 member 取"时间戳"，
    同微秒的两个请求就只算一条——**并发越高漏计越多**，限流器恰好在压力最大时失效。
    所以 member 用 uuid：只需唯一，不参与比较。
    """
    await asyncio.gather(*[sliding_window.check("t1", 10, 100) for _ in range(20)])
    assert await redis.zcard(f"{sliding_window.KEY_PREFIX}t1") == 20, "同瞬间的请求被合并了"


@pytest.mark.asyncio
async def test_scopes_are_independent(redis):
    """一个接口打满不影响另一个。

    共用 key 的话，**查榜单会吃掉建对局的配额**——一个接口被刷会连带限死无关功能。
    """
    for _ in range(3):
        await sliding_window.check("t1", 10, 3)
    assert (await sliding_window.check("t1", 10, 3))[0] == -1
    assert (await sliding_window.check("t2", 10, 3))[0] >= 0


# ----------------------------------------------------------------------
# 失败方向
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_failure_allows_through(redis):
    """Redis 挂了要放行。

    限流器是**保护机制，不是可用性依赖**——抖一下就拒全站的话它自己成了新单点。
    注意这和 H-1 降级开关的方向相反（开关读不到要保持已生效的降级态）。
    """
    class Broken:
        async def __call__(self, *a, **kw):
            raise ConnectionError("redis is down")

    sliding_window._script = Broken()
    remaining, retry_ms = await sliding_window.check("t1", 1, 1)
    assert remaining == 1
    assert retry_ms == 0


@pytest.mark.asyncio
async def test_check_without_bind_allows_through():
    """没注册脚本时放行：import 到 app 但没起 lifespan 的场景不该被卡住。"""
    sliding_window._script = None
    assert (await sliding_window.check("t1", 1, 7))[0] == 7


# ----------------------------------------------------------------------
# 依赖层：挂到路由上之后的行为
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_limiter_raises_429_with_retry_after(redis):
    """超限抛 429 且**必须带 `Retry-After`**。

    不告诉客户端等多久，它会立刻重试，**过载期间的重试本身变成新的流量峰值**。
    """
    limiter = sliding_window.SlidingWindowLimiter("action", 10, 1)
    await limiter(FakeRequest())

    with pytest.raises(HTTPException) as exc:
        await limiter(FakeRequest())
    assert exc.value.status_code == 429
    assert int(exc.value.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_limiter_counts_per_user_not_globally(redis):
    """按调用方分别计数。

    按 IP 统计的话，NAT / 公司出口下多用户共享配额会被误伤，
    且压测流量来自单一 IP 完全无法构造负载——这是 v2 改成 user_id 维度的起因。
    """
    limiter = sliding_window.SlidingWindowLimiter("action", 10, 1)
    await limiter(FakeRequest(host="1.1.1.1"))

    with pytest.raises(HTTPException):
        await limiter(FakeRequest(host="1.1.1.1"))
    # 换个来源：自己的配额还是满的
    await limiter(FakeRequest(host="2.2.2.2"))


@pytest.mark.asyncio
async def test_forwarded_requests_are_exempt(redis):
    """跨节点转发的请求不再计一次。

    计两次的话同一个用户动作占两份配额，**且占多少取决于房间落在哪台机器**，
    行为不可预期。限流只在入口节点发生一次。
    """
    from app.core.room_router import FORWARD_HEADER

    limiter = sliding_window.SlidingWindowLimiter("action", 10, 1)
    for _ in range(5):
        assert await limiter(FakeRequest(headers={FORWARD_HEADER: "1"})) is None


@pytest.mark.asyncio
async def test_rejects_metric_carries_scope(redis):
    """拒绝要上报，且 label 只放 scope。

    scope 是低基数（接口数量固定）可以进 label；user_id 绝不能进——
    一万个用户就是一万条时间序列，Prometheus 内存打爆（C02）。
    """
    limiter = sliding_window.SlidingWindowLimiter("create_game", 10, 1)
    before = metrics.rate_limit_rejects.labels(scope="create_game")._value.get()

    await limiter(FakeRequest())
    with pytest.raises(HTTPException):
        await limiter(FakeRequest())

    after = metrics.rate_limit_rejects.labels(scope="create_game")._value.get()
    assert after == before + 1


def test_identifier_ignores_forwarded_header():
    """限流键不能读 `X-Forwarded-For`。

    这是这次顺手修掉的一个真问题：`send-code` / `login` 原来没传 identifier，
    用的是 fastapi-limiter 的 `default_identifier`,**而它读 `X-Forwarded-For`**——
    那是个客户端可以随便填的请求头。于是**最需要限流的两个接口，限流键可以伪造**：
    每个请求换一个假 IP 就能无限发验证码（每封都花真钱）、无限试密码。
    """
    import asyncio as _asyncio
    from app.core.rate_limit import user_or_ip_identifier

    req = FakeRequest(headers={"X-Forwarded-For": "9.9.9.9"}, host="1.1.1.1")
    assert _asyncio.run(user_or_ip_identifier(req)) == "ip:1.1.1.1"
