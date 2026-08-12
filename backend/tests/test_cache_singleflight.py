"""singleflight 防击穿测试。

验收口径只有一条：**热 key 失效的那一瞬间来 N 个并发请求，回源只发生 1 次**。
所以判据全是"loader 被调了几次"，不是"返回值对不对"——返回值对只说明没崩。

不用压测：击穿是并发时序问题而不是吞吐问题，`asyncio.gather` 起 50 个协程
就能精确复现"同时未命中"这个瞬间，比压测可靠得多也快得多。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import pytest

from app.core import cache, metrics


@pytest.fixture(autouse=True)
def clean_cache():
    """L1 和 _inflight 都是模块级单例，测试之间必须清干净。"""
    cache.l1.clear()
    cache._inflight.clear()
    yield
    cache.l1.clear()
    cache._inflight.clear()


class SlowLoader:
    """记调用次数的慢回源。

    必须是 async 且真的 await：同步 loader 从头到尾不让出事件循环，
    并发请求根本没机会在"回源进行中"这个窗口里挤进来——
    那样测出来的 1 次回源是假的，是串行执行的结果，不是 singleflight 的功劳。
    """

    def __init__(self, value=None, delay=0.05):
        self.calls = 0
        self.value = value if value is not None else {"v": 1}
        self.delay = delay

    async def __call__(self):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return self.value


# ----------------------------------------------------------------------
# 核心验收
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_misses_load_only_once():
    """50 个并发同时未命中，只有 1 次回源。这是防击穿唯一的验收口径。"""
    loader = SlowLoader()
    results = await asyncio.gather(*[
        cache.get_or_load("sf:k1", loader) for _ in range(50)
    ])
    assert loader.calls == 1, f"回源了 {loader.calls} 次，singleflight 没生效"
    assert all(r == {"v": 1} for r in results), "等待者没拿到回源结果"


@pytest.mark.asyncio
async def test_waiters_share_the_same_object():
    """等待者拿到的是回源那一份，不是各自回源的副本。

    共享引用是刻意的（见 cache.py 文件头：拷一份就把省下的反序列化还回去了），
    这条用例把这个约定钉住——顺带说明为什么缓存值必须当只读的用。
    """
    loader = SlowLoader()
    results = await asyncio.gather(*[
        cache.get_or_load("sf:k1", loader) for _ in range(5)
    ])
    assert all(r is results[0] for r in results)


@pytest.mark.asyncio
async def test_different_keys_do_not_block_each_other():
    """互斥必须是 per-key 的。做成全局锁会把不相关的回源串起来，
    等于自己造了一个吞吐瓶颈——比击穿更糟，因为它每次未命中都在生效。"""
    a, b = SlowLoader({"v": "a"}), SlowLoader({"v": "b"})
    ra, rb = await asyncio.gather(
        cache.get_or_load("sf:ka", a),
        cache.get_or_load("sf:kb", b),
    )
    assert a.calls == 1 and b.calls == 1
    assert ra == {"v": "a"} and rb == {"v": "b"}


@pytest.mark.asyncio
async def test_second_wave_after_completion_hits_l1():
    """第一波回源完成并回填之后，第二波应该命中 L1 而不是再回源。
    这条是在确认 singleflight 没有把回填搞丢。"""
    loader = SlowLoader()
    await asyncio.gather(*[cache.get_or_load("sf:k1", loader) for _ in range(10)])
    await asyncio.gather(*[cache.get_or_load("sf:k1", loader) for _ in range(10)])
    assert loader.calls == 1


# ----------------------------------------------------------------------
# 失败与取消：不能把等待者永久挂住
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loader_failure_propagates_to_all_waiters():
    """回源失败要让所有等待者都拿到异常，不能有人永远挂着。"""
    calls = []

    async def boom():
        calls.append(1)
        await asyncio.sleep(0.02)
        raise RuntimeError("db down")

    results = await asyncio.gather(
        *[cache.get_or_load("sf:k1", boom) for _ in range(10)],
        return_exceptions=True,
    )
    assert len(calls) == 1
    assert all(isinstance(r, RuntimeError) for r in results)


@pytest.mark.asyncio
async def test_failed_flight_is_cleared_so_next_call_retries():
    """失败的回源必须把 _inflight 里的痕迹清掉。

    留着的后果不是"多查一次"而是**永久故障**：后来的请求会挂到一个已经完成
    （且带着异常）的 future 上，这个 key 从此再也回源不成功。
    """
    async def boom():
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await cache.get_or_load("sf:k1", boom)
    assert "sf:k1" not in cache._inflight

    ok = SlowLoader({"v": 2}, delay=0)
    assert await cache.get_or_load("sf:k1", ok) == {"v": 2}


@pytest.mark.asyncio
async def test_cancelling_a_waiter_does_not_kill_the_loader():
    """一个等待者被取消（比如客户端断开），不能连带打断正在回源的那个人。

    这是 `asyncio.shield` 唯一的作用。没有它，共享 future 会被取消传播打断，
    **一个客户端断开就能让其他所有等待者一起失败**——放大故障而不是收敛故障。
    """
    loader = SlowLoader(delay=0.1)
    first = asyncio.create_task(cache.get_or_load("sf:k1", loader))
    await asyncio.sleep(0.01)                      # 让 first 先占住这次回源
    waiter = asyncio.create_task(cache.get_or_load("sf:k1", loader))
    await asyncio.sleep(0.01)                      # 让 waiter 挂上去
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert await first == {"v": 1}                 # 回源方不受影响
    assert loader.calls == 1


@pytest.mark.asyncio
async def test_cancelling_the_loader_releases_waiters():
    """回源方被取消时，等待者必须被放掉而不是永久挂着。

    否则一个客户端断开就能拖死同一个 key 的所有读请求——
    这也是 `_load_once` 里捕获 `BaseException` 而不是 `Exception` 的原因
    （`CancelledError` 在 3.8+ 不是 `Exception` 的子类）。
    """
    loader = SlowLoader(delay=1.0)
    first = asyncio.create_task(cache.get_or_load("sf:k1", loader))
    await asyncio.sleep(0.01)
    waiter = asyncio.create_task(cache.get_or_load("sf:k1", loader))
    await asyncio.sleep(0.01)
    first.cancel()

    # 等待者不该挂死：给它一个远小于 loader delay 的超时
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, timeout=0.3)
    assert "sf:k1" not in cache._inflight


@pytest.mark.asyncio
async def test_inflight_is_empty_after_success():
    """回源完就得把登记清掉，不然 _inflight 是个按 key 无界增长的内存泄漏。"""
    await asyncio.gather(*[
        cache.get_or_load(f"sf:k{i}", SlowLoader(delay=0)) for i in range(20)
    ])
    assert cache._inflight == {}


# ----------------------------------------------------------------------
# 验收口径
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metric_counts_coalesced_loads():
    """指标口径：N 个并发未命中应该看到 db +1、singleflight +(N-1)，
    两者的比值就是防击穿省下的回源量。"""
    def _val(level, result):
        return metrics.cache_reads.labels(level=level, result=result)._value.get()

    db_before = _val("db", "miss")
    sf_before = _val("singleflight", "coalesced")

    loader = SlowLoader()
    await asyncio.gather(*[cache.get_or_load("sf:k1", loader) for _ in range(20)])

    assert _val("db", "miss") == db_before + 1
    assert _val("singleflight", "coalesced") == sf_before + 19
