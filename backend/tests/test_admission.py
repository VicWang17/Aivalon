"""网关层准入：全局令牌桶 + IP 令牌桶 测试。

验收口径四条：
  1. **突发和稳态是两个独立旋钮**：攒满的桶能一次放过 capacity 个，之后按 rate 恢复
  2. **多进程共享同一个桶**：读改写必须原子，两个进程不能各自放行同一个令牌
  3. **两层分开判**：被 IP 层拒掉的请求不该消耗全局配额
  4. **读不到 Redis 要放行**：限流器是保护机制，不是新的单点

判据是"放行了几个"和"拒在哪一层"，不是"函数有没有报错"。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import inspect

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import admission
from app.core.config import settings


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")

ALLOW, REJECT_GLOBAL, REJECT_IP = 0, 1, 2


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    keys = [admission.GLOBAL_KEY] + [f"{admission.IP_KEY_PREFIX}{ip}"
                                     for ip in ("1.1.1.1", "2.2.2.2", "3.3.3.3")]
    await client.delete(*keys)
    admission.bind(client)
    yield client
    await client.delete(*keys)
    admission._script = None
    await client.aclose()


@pytest.fixture
def limits(monkeypatch):
    """把配额调小到能数得清。真实值（200/s）在单测里既慢又不稳。"""
    def _set(g_cap, g_rate, i_cap, i_rate):
        for k, v in (("RATE_LIMIT_GLOBAL_CAPACITY", g_cap), ("RATE_LIMIT_GLOBAL_RATE", g_rate),
                     ("RATE_LIMIT_IP_CAPACITY", i_cap), ("RATE_LIMIT_IP_RATE", i_rate)):
            monkeypatch.setattr(f"app.core.config.settings.{k}", v)
    return _set


# ----------------------------------------------------------------------
# 令牌桶的两个旋钮
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_bucket_absorbs_a_burst(redis, limits):
    """攒满的桶能一次放过 capacity 个，第 capacity+1 个才被拒。

    **这就是选令牌桶而不是固定窗口的理由**：固定窗口只有"N 次/秒"一个旋钮，
    表达不了"允许突发到 5、但稳态只给 2/s"。真实流量就是这个形状——
    平时很闲，偶尔一波集中涌入。
    """
    limits(100, 100, 5, 2)  # IP 桶 5 个令牌，每秒补 2 个

    verdicts = [(await admission.check("1.1.1.1"))[0] for _ in range(5)]
    assert verdicts == [ALLOW] * 5, "满桶没放过整个突发"

    verdict, retry_ms = await admission.check("1.1.1.1")
    assert verdict == REJECT_IP
    assert retry_ms > 0, "拒了却没告诉客户端等多久"


@pytest.mark.asyncio
async def test_tokens_refill_at_the_configured_rate(redis, limits):
    """桶空之后按 rate 恢复，不是等一整个窗口才一次性全放。

    固定窗口是"到点清零"，所以窗口边界能挤进 2 倍流量；令牌桶是连续补充，
    没有边界这个概念。
    """
    limits(100, 100, 2, 20)  # 每秒补 20 个 = 50ms 一个

    assert (await admission.check("2.2.2.2"))[0] == ALLOW
    assert (await admission.check("2.2.2.2"))[0] == ALLOW
    assert (await admission.check("2.2.2.2"))[0] == REJECT_IP

    await asyncio.sleep(0.12)  # 够补 2 个
    assert (await admission.check("2.2.2.2"))[0] == ALLOW, "令牌没在补"


@pytest.mark.asyncio
async def test_refill_never_exceeds_capacity(redis, limits):
    """闲很久也只能攒到 capacity，不能无限攒。

    不封顶的话，一个通宵没流量的接口早上会积压出几万个令牌，
    **突发上限就成了"闲了多久"的函数**，等于没有上限。
    """
    limits(100, 100, 3, 20)  # 3 个令牌，每秒补 20 个

    # 先消耗一个，把桶建出来——桶不存在时 peek 直接返回 cap，
    # 那种情况下无论闲多久都测不出"攒过头"，得先有个带时间戳的桶
    assert (await admission.check("3.3.3.3"))[0] == ALLOW

    # 闲 0.4 秒。不封顶的话按 rate 该攒到 2 + 0.4×20 = 10 个
    await asyncio.sleep(0.4)

    verdicts = [(await admission.check("3.3.3.3"))[0] for _ in range(4)]
    assert verdicts == [ALLOW, ALLOW, ALLOW, REJECT_IP], "令牌攒过了 capacity"


# ----------------------------------------------------------------------
# 原子性：这是必须用 Lua 的全部理由
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_checks_do_not_oversubscribe(redis, limits):
    """并发打进来时，放行数**恰好**等于令牌数，不能超发。

    "读桶 → 算补了多少 → 判断 → 写回"是个读改写序列，而多个 API 进程共享同一个桶。
    用 GET 再 SET 的话，两个进程同时读到"还剩 1 个"，各自都放行——桶被超发。
    Lua 在 Redis 里单线程整段执行，这个序列才是原子的。
    """
    limits(100, 0.01, 10, 0.01)  # rate 压到几乎不补，避免测试期间桶自己长回来

    results = await asyncio.gather(*[admission.check("1.1.1.1") for _ in range(30)])
    allowed = sum(1 for v, _ in results if v == ALLOW)
    assert allowed == 10, f"放行了 {allowed} 个，令牌只有 10 个"


# ----------------------------------------------------------------------
# 两层的关系
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ip_rejection_does_not_burn_global_quota(redis, limits):
    """被 IP 层拒掉的请求不消耗全局配额。

    扣了的话，**一个刷接口的 IP 就能把全局配额一起烧掉**——而那正是它想干的事：
    它自己被拒无所谓，只要能顺带把别人的容量也吃掉。
    """
    limits(100, 0.01, 2, 0.01)

    for _ in range(2):
        assert (await admission.check("1.1.1.1"))[0] == ALLOW
    for _ in range(20):
        assert (await admission.check("1.1.1.1"))[0] == REJECT_IP

    # 全局只该被扣掉最初放行的那 2 个
    remaining = float(await redis.hget(admission.GLOBAL_KEY, "t"))
    assert remaining > 97, f"全局桶被 IP 层的拒绝扣掉了，还剩 {remaining}"

    # 别的 IP 照常能进——受影响的只有刷的那个来源
    assert (await admission.check("2.2.2.2"))[0] == ALLOW


@pytest.mark.asyncio
async def test_global_bucket_rejects_regardless_of_source(redis, limits):
    """全局桶空了，来源再干净也拒。

    这一层管的是"这台系统一共能吃多少"——**到顶了就该无条件卸载，
    不必再管来源是谁**。这也是它和按 user_id 的业务限流的根本区别：
    一万个用户每人只建一局完全合规，机器照样倒。
    """
    limits(3, 0.01, 100, 0.01)

    for _ in range(3):
        assert (await admission.check("1.1.1.1"))[0] == ALLOW

    verdict, _ = await admission.check("2.2.2.2")  # 全新的 IP，自己的桶是满的
    assert verdict == REJECT_GLOBAL, "全局到顶了却还在放行"


@pytest.mark.asyncio
async def test_ip_buckets_are_independent(redis, limits):
    """一个 IP 打满不影响别的 IP。IP 维度存在的意义就是隔离故障来源。"""
    limits(100, 0.01, 2, 0.01)

    for _ in range(2):
        await admission.check("1.1.1.1")
    assert (await admission.check("1.1.1.1"))[0] == REJECT_IP
    assert (await admission.check("3.3.3.3"))[0] == ALLOW


# ----------------------------------------------------------------------
# 失败方向 + 运维细节
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_failure_allows_through(redis, limits):
    """Redis 挂了要放行，不是拒绝。

    限流器是**保护机制，不是可用性依赖**。Redis 抖一下就把全站拒了的话，
    这个限流器本身成了新的单点，它造成的故障比它防的还大。
    注意这和 H-1 降级开关的失败方向**相反**（开关读不到要保持已生效的降级态）：
    开关表达"人做的决定不能自己失效"，限流器表达"别帮着把故障放大"。
    """
    class Broken:
        async def __call__(self, *a, **kw):
            raise ConnectionError("redis is down")

    admission._script = Broken()
    verdict, retry_ms = await admission.check("1.1.1.1")
    assert verdict == ALLOW
    assert retry_ms == 0


@pytest.mark.asyncio
async def test_check_without_bind_allows_through(redis):
    """没注册脚本时放行。单测/脚本里 import 到 app 但没起 lifespan 的场景不该被卡住。"""
    admission._script = None
    assert (await admission.check("1.1.1.1"))[0] == ALLOW


@pytest.mark.asyncio
async def test_bucket_expires_so_ip_keys_do_not_pile_up(redis, limits):
    """桶带 TTL，否则 IP 桶的数量随访客数无限增长。

    TTL 设成"恰好攒满"是无损的：**一个过期消失的桶和一个满桶完全等价**
    （都是下次来了给满配额）。顺手解决了内存增长。
    """
    limits(100, 100, 4, 2)  # 4 / 2 = 2 秒攒满
    await admission.check("1.1.1.1")

    ttl = await redis.pttl(f"{admission.IP_KEY_PREFIX}1.1.1.1")
    assert ttl > 0, "IP 桶没设过期，key 会随访客数无限增长"
    assert ttl <= 3000 + 50, f"TTL {ttl}ms 比攒满时间长太多"


@pytest.mark.asyncio
async def test_health_check_is_exempt():
    """/health 必须豁免。

    突发流量下把健康检查也拒了，负载均衡会认为节点已死并摘掉它——
    于是剩下的节点承接更多流量、更快被拒，**限流器亲手把一次过载放大成雪崩**。
    /metrics 同理：正过载时最需要看指标。
    """
    assert "/health" in admission.EXEMPT_PATHS
    assert "/metrics" in admission.EXEMPT_PATHS


def _req(headers: dict):
    """最小请求替身：`is_internal_request` 只读 headers。

    刻意不用 `TestClient`——那会跑真实 lifespan、要碰 Redis，而模块级连接池在
    全量跑时已绑到别的事件循环上（同 test_redis_pool.py 那条）。
    """
    class Req:
        pass

    r = Req()
    r.headers = headers
    return r


def test_internal_callback_is_exempt_from_admission():
    """带**验过的**内部密钥的请求豁免准入。

    这条是 S4 run B 撞出来的（DEVLOG 044）：AI worker 跑完一个回合要回调
    `/games/{id}/ai_action` 把动作交回 Web 进程，而那条路径过准入、和用户流量
    抢同一个桶。压测里被拒 54 次，后果是**回调被拒 → AI 任务退避重试 →
    房间推进不了 → 玩家以为这局没了去建新局**——过载自己长出一条正反馈。

    **判据和 /health 豁免是同一条**：拒健康检查会让 LB 摘节点、剩下的更快被拒；
    拒 AI 回调会让房间卡住、玩家重建局。**凡是"系统为了自愈而发给自己的请求"
    都不该和外部流量抢配额**——它被拒的代价不是少服务一个人，
    而是**已经服务到一半的那些人全部卡住**。
    """
    assert admission.is_internal_request(
        _req({admission.INTERNAL_SECRET_HEADER: settings.SECRET_KEY})
    )


def test_exemption_requires_the_right_secret_not_just_the_header():
    """光有头不算，密钥必须对。

    只看头在不在的话，加一个请求头就能绕过**整层**准入——那是
    `X-Forwarded-For` 那个错误再犯一次：**一个可伪造的凭据等于没有凭据**。
    这条压舱：它一红就说明准入层被开了个人人可走的后门。
    """
    assert not admission.is_internal_request(
        _req({admission.INTERNAL_SECRET_HEADER: "wrong-secret"})
    )
    assert not admission.is_internal_request(_req({}))
    assert not admission.is_internal_request(
        _req({admission.INTERNAL_SECRET_HEADER: ""})
    )


def test_secret_comparison_is_constant_time():
    """密钥比较必须用 `compare_digest`，不能用 `==`。

    这个比较发生在**每一个请求**上。朴素比较在第一个不同的字节上就短路返回，
    逐字节试探能把耗时差异变成一次密钥泄露——**能被计时的比较就是能被问出来的秘密**。
    源码断言，因为行为上测不出时序差异。
    """
    src = inspect.getsource(admission.is_internal_request)
    assert "compare_digest" in src, "内部密钥比较要用 hmac.compare_digest"


def test_internal_exemption_is_by_credential_not_by_path_prefix():
    """豁免必须按凭据判，不能按路径前缀。

    回调路径带 `{game_id}`，`EXEMPT_PATHS` 是精确匹配的集合、加不进去；
    改成前缀匹配 `/api/v1/games` 则等于**把整个对局接口从准入里摘出去**，
    那正是最需要保护的地方。所以豁免的判据只能是"证明得了自己是内部请求"。
    """
    for p in admission.EXEMPT_PATHS:
        assert "ai_action" not in p and "ai_thinking" not in p, (
            "内部回调不该靠路径豁免，见 is_internal_request"
        )


def test_client_ip_ignores_forwarded_header():
    """不能信 X-Forwarded-For。

    它是个请求头，客户端可以随便填。信了它，每个请求换一个伪造 IP 就能让 IP 桶
    形同虚设——**一个可伪造的限流键等于没有限流键**。
    """
    class Req:
        headers = {"X-Forwarded-For": "9.9.9.9"}

        class client:
            host = "1.1.1.1"

    assert admission.client_ip(Req()) == "1.1.1.1"
