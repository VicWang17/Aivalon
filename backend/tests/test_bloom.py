"""布隆过滤器测试：无假阴性、位图为空放行、Redis 故障放行、跨进程哈希稳定。

核心判据是**无假阴性**——登记过的值绝不能被答成"不存在"。
假阳性（把不存在的答成可能存在）不断言具体结果，只断言整体误判率在量级上说得过去：
单个值命中哪些位是哈希决定的，钉死某个具体 id 的结果等于把实现写进测试。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subprocess

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import bloom, metrics

KEY = "aivalon:bloom:test:v1"


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


needs_redis = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    await client.delete(KEY)
    yield client
    await client.delete(KEY)
    await client.aclose()


class _BrokenRedis:
    """所有操作都抛异常，用来验证"过滤器坏了不能影响可用性"。"""
    def pipeline(self, transaction=False):
        raise RuntimeError("redis down")

    async def exists(self, key):
        raise RuntimeError("redis down")


# ---- 无假阴性：这是整个组件唯一不能破的性质 ----

@needs_redis
@pytest.mark.asyncio
async def test_registered_values_are_never_rejected(redis):
    """登记过的一律放行。假阴性会把真实房间 404 掉，是功能级故障。"""
    ids = [f"game-real-{i}" for i in range(200)]
    for gid in ids:
        await bloom.add(redis, gid, key=KEY)
    for gid in ids:
        assert await bloom.might_contain(redis, gid, key=KEY) is True


@needs_redis
@pytest.mark.asyncio
async def test_unregistered_value_is_rejected(redis):
    """位图非空时，没登记过的值应该被拦住——这才是防穿透真正省下的回源。"""
    await bloom.add(redis, "game-real-1", key=KEY)
    assert await bloom.might_contain(redis, "game-never-created", key=KEY) is False


@needs_redis
@pytest.mark.asyncio
async def test_false_positive_rate_stays_low(redis):
    """假阳性率在量级上说得过去：m=2^20、k=7、n=1000 时理论值远低于 1%。

    不断言某个具体 id 的结果（那是把哈希实现写进测试），只断言整体比例。
    """
    for i in range(1000):
        await bloom.add(redis, f"game-real-{i}", key=KEY)
    probes = [f"game-fake-{i}" for i in range(1000)]
    passed = 0
    for gid in probes:
        if await bloom.might_contain(redis, gid, key=KEY):
            passed += 1
    assert passed / len(probes) < 0.05, f"假阳性率过高: {passed}/{len(probes)}"


# ---- 位图为空一律放行：无假阴性是"登记齐全"的性质，不是算法的性质 ----

@needs_redis
@pytest.mark.asyncio
async def test_empty_bitmap_allows_everything(redis):
    """位图不存在时，它给出的任何"不存在"都是假的（老房间没登记 / Redis 被清），
    此时必须一路放行，否则全站房间集体 404。"""
    assert await redis.exists(KEY) == 0
    assert await bloom.might_contain(redis, "game-anything", key=KEY) is True


@needs_redis
@pytest.mark.asyncio
async def test_warm_registers_existing_values(redis):
    """预热之后老房间才拦不住——这是正确性前提，不是性能优化。"""
    old = [f"game-old-{i}" for i in range(50)]

    # 没预热就先建立位图，老房间会被误判成不存在
    await bloom.add(redis, "game-new-1", key=KEY)
    assert await bloom.might_contain(redis, old[0], key=KEY) is False

    await redis.delete(KEY)
    count = await bloom.warm(redis, lambda: old, key=KEY)
    assert count == 50
    for gid in old:
        assert await bloom.might_contain(redis, gid, key=KEY) is True


@needs_redis
@pytest.mark.asyncio
async def test_warm_skips_when_bitmap_already_exists(redis):
    """位图已在（它不设 TTL，绝大多数次启动都在），就不该再扫一遍 games 表。"""
    await bloom.add(redis, "game-real-1", key=KEY)
    calls = []

    def loader():
        calls.append(1)
        return ["game-old-1"]

    assert await bloom.warm(redis, loader, key=KEY) == 0
    assert calls == [], "位图已存在却还是查了库"


@needs_redis
@pytest.mark.asyncio
async def test_warm_failure_leaves_bitmap_empty(redis):
    """灌不进去就别开始拦：位图保持空，`might_contain` 一路放行。"""
    def broken_loader():
        raise RuntimeError("db down")

    assert await bloom.warm(redis, broken_loader, key=KEY) == 0
    assert await redis.exists(KEY) == 0
    assert await bloom.might_contain(redis, "game-anything", key=KEY) is True


@needs_redis
@pytest.mark.asyncio
async def test_bitmap_key_has_no_ttl(redis):
    """过期归零和被清空是同一种事故：位图一旦到期消失，真实房间会被集体判死。"""
    await bloom.add(redis, "game-real-1", key=KEY)
    assert await redis.ttl(KEY) == -1, "位图不该有 TTL"


# ---- 过滤器是优化，不能变成可用性依赖 ----

@pytest.mark.asyncio
async def test_no_redis_allows_everything():
    assert await bloom.might_contain(None, "game-anything", key=KEY) is True
    await bloom.add(None, "game-anything", key=KEY)          # 不抛
    assert await bloom.warm(None, lambda: ["x"], key=KEY) == 0


@pytest.mark.asyncio
async def test_redis_failure_allows_everything():
    """读位图失败要放行。放行的代价是多查一次库，错拦的代价是真实房间 404。"""
    broken = _BrokenRedis()
    assert await bloom.might_contain(broken, "game-real-1", key=KEY) is True
    await bloom.add(broken, "game-real-1", key=KEY)          # 不抛


# ---- 位置计算必须跨进程一致 ----

def test_offsets_are_deterministic():
    a = bloom._offsets("game-abc")
    b = bloom._offsets("game-abc")
    assert a == b
    assert len(a) == bloom.HASHES
    assert all(0 <= off < bloom.BITS for off in a)


def test_offsets_do_not_depend_on_pythonhashseed():
    """位图是跨进程共享的，各进程算出的位必须一致。

    内置 hash() 受 `PYTHONHASHSEED` 随机化影响（同 DEVLOG C05 那个哈希盐的坑），
    所以这里用 md5。用两个不同的 seed 各起一个进程实测，比读代码可靠。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from app.core import bloom\n"
        "print(bloom._offsets('game-abc'))\n" % root
    )
    outs = []
    for seed in ("0", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        outs.append(subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        ).stdout.strip())
    assert outs[0] == outs[1], f"不同 PYTHONHASHSEED 下位置不一致: {outs}"


# ---- 验收口径 ----

@needs_redis
@pytest.mark.asyncio
async def test_rejects_are_counted(redis):
    """被拦下的次数就是省下的无效回源次数，这是防穿透唯一的验收口径。"""
    await bloom.add(redis, "game-real-1", key=KEY)
    before = metrics.bloom_rejects._value.get()
    assert await bloom.might_contain(redis, "game-never-created", key=KEY) is False
    assert metrics.bloom_rejects._value.get() == before + 1

    # 放行的不该计数
    await bloom.might_contain(redis, "game-real-1", key=KEY)
    assert metrics.bloom_rejects._value.get() == before + 1
