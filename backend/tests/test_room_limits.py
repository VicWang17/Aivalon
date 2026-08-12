"""资源层限流：单房间事件风暴保护 测试。

这是分层限流的第三层。前两层都统计不到它要防的东西——
**八个人对着同一局猛点，每个人都没超自己的配额，那一个房间的队列却排到几千个**。

验收口径四条：
  1. 队列有上界，满了当场拒（而不是让提交方排在队列外面等）
  2. 等待有上界，超时不再等（队列不满但处理慢时，队尾一样在无限等）
  3. 超时后**还没出队**的动作不执行（否则客户端重试一次，动作生效两遍）
  4. 两种拒绝语义必须分开：queue_full 一定没生效，timeout 不能断言

顺带钉住两个原来存在的竞态（todo 里"Actor 空闲退出竞态"那条）：
空闲判定与新动作入队撞车、老 Actor 的退出回调把新 Actor 删掉。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import pytest

from app.core import metrics
from app.core.room_actor import (
    ActorManager,
    RoomActionTimeout,
    RoomActor,
    RoomOverloaded,
)


def _mk(handler, **kw) -> RoomActor:
    kw.setdefault("max_queue", 3)
    kw.setdefault("action_timeout", 0.3)
    return RoomActor("g1", handler, on_idle_exit=lambda _: None, **kw)


async def _never(game_id, *args):
    await asyncio.sleep(3600)


# ----------------------------------------------------------------------
# 队列上界
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_queue_full_rejects_instead_of_queueing():
    """队列满了当场拒，不是排在外面等。

    有界 `asyncio.Queue` 靠 `await put` 也能限长度，但那只是把队伍从看得见的地方
    挪到看不见的地方——**延迟照样无上限**。要的是立刻告诉上游"这个房间过载了"。
    """
    actor = _mk(_never, max_queue=2, action_timeout=5)
    first = asyncio.create_task(actor.submit("a1"))   # 会被取走开始执行
    await asyncio.sleep(0.05)
    queued = [asyncio.create_task(actor.submit(f"q{i}")) for i in range(2)]
    await asyncio.sleep(0.05)

    with pytest.raises(RoomOverloaded) as exc:
        await actor.submit("overflow")
    assert exc.value.depth == 2

    for t in [first, *queued]:
        t.cancel()


@pytest.mark.asyncio
async def test_rejection_is_immediate_not_after_a_wait():
    """拒绝必须是立刻的。**过载时最不能做的事就是让调用方多等**——
    上游连接、线程、协程都还占着，等待本身就是资源。"""
    actor = _mk(_never, max_queue=1, action_timeout=5)
    running = asyncio.create_task(actor.submit("a1"))
    await asyncio.sleep(0.05)
    filler = asyncio.create_task(actor.submit("a2"))
    await asyncio.sleep(0.05)

    started = asyncio.get_running_loop().time()
    with pytest.raises(RoomOverloaded):
        await actor.submit("a3")
    assert asyncio.get_running_loop().time() - started < 0.05

    running.cancel()
    filler.cancel()


@pytest.mark.asyncio
async def test_queue_recovers_after_drain():
    """拒绝不是粘性的：房间处理完积压就该恢复接单。

    这一层拦的是**瞬时风暴**，不是惩罚房间。粘住不放就变成了"一次抖动导致对局打不下去"。
    """
    gate = asyncio.Event()

    async def handler(game_id, name):
        await gate.wait()
        return name

    actor = _mk(handler, max_queue=1, action_timeout=5)
    running = asyncio.create_task(actor.submit("a1"))
    await asyncio.sleep(0.05)
    queued = asyncio.create_task(actor.submit("a2"))
    await asyncio.sleep(0.05)

    with pytest.raises(RoomOverloaded):
        await actor.submit("a3")

    gate.set()
    assert await running == "a1"
    assert await queued == "a2"
    assert await actor.submit("a4") == "a4"


# ----------------------------------------------------------------------
# 等待上界
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_slow_action_times_out_instead_of_hanging():
    """等待有上界。

    原来 `submit` 是裸 `await future`：处理函数卡住（LLM 慢、DB 卡、下游超时）
    就**永久挂住**，而挂住的是一个 HTTP 请求，连接一直占着。
    队列不满也会发生——所以队列上界不能替代等待上界，两个都要。
    """
    actor = _mk(_never, action_timeout=0.2)
    with pytest.raises(RoomActionTimeout) as exc:
        await actor.submit("slow")
    assert exc.value.waited == 0.2


@pytest.mark.asyncio
async def test_timed_out_action_is_dropped_if_not_yet_started():
    """超时后还没出队的动作不执行。

    执行了的话：客户端拿到超时 → 按超时语义重试 → 服务端把排队那次也跑了，
    **一个动作生效两遍**。投票、提名这类动作重复执行会直接把对局算错。
    """
    executed = []
    gate = asyncio.Event()

    async def handler(game_id, name):
        executed.append(name)
        if name == "blocker":
            await gate.wait()
        return name

    actor = _mk(handler, max_queue=5, action_timeout=0.2)
    # blocker 自己给足等待时间：它要一直占着串行位，不能跟着一起超时
    blocker = asyncio.create_task(actor.submit("blocker", timeout=5))
    await asyncio.sleep(0.05)

    with pytest.raises(RoomActionTimeout):
        await actor.submit("gives_up")     # 排在 blocker 后面，等不到就走

    gate.set()
    await blocker
    await asyncio.sleep(0.05)
    assert executed == ["blocker"], f"放弃等待的动作仍被执行了：{executed}"


@pytest.mark.asyncio
async def test_timeout_does_not_poison_the_queue():
    """一个动作超时后，Actor 还能继续服务后面的动作。

    超时是常态不是故障，处理循环不能因此退出——否则一次慢调用让整个房间失能。
    """
    async def handler(game_id, name):
        if name == "slow":
            await asyncio.sleep(1)
        return name

    actor = _mk(handler, action_timeout=0.15)
    with pytest.raises(RoomActionTimeout):
        await actor.submit("slow")
    await asyncio.sleep(1)     # 等那个慢动作自己跑完，腾出串行位
    assert await actor.submit("fast") == "fast"


# ----------------------------------------------------------------------
# 两种拒绝的语义差别
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_rejections_are_distinct_types():
    """queue_full 和 timeout 必须能分辨。

    **前者一定没生效**（压根没入队），可以安全重试；
    **后者不能断言**（可能已经在跑）。合成一个异常的话，
    路由层就只能给出一个笼统的错误码，客户端也就只能瞎猜要不要重发。
    """
    assert not issubclass(RoomOverloaded, RoomActionTimeout)
    assert not issubclass(RoomActionTimeout, RoomOverloaded)


@pytest.mark.asyncio
async def test_both_rejections_are_counted_with_distinct_reasons():
    """两种拒绝分别上报。正常对局下这两个计数应该一直是 0，
    一涨就说明有房间被打爆了（或处理链路慢了）——这是资源层唯一的验收口径。

    label 只放 reason：**game_id 绝不能进 label**，房间数无上限（C02 基数爆炸）。
    """
    def _read(reason):
        return metrics.room_overload.labels(reason=reason)._value.get()

    full_before, to_before = _read("queue_full"), _read("timeout")

    actor = _mk(_never, max_queue=1, action_timeout=0.15)
    # 占位的那个给足等待时间，否则它也会超时、把 timeout 计数多加一次
    running = asyncio.create_task(actor.submit("a1", timeout=30))
    await asyncio.sleep(0.05)
    filler = asyncio.create_task(actor.submit("a2"))
    await asyncio.sleep(0.05)
    with pytest.raises(RoomOverloaded):
        await actor.submit("a3")
    with pytest.raises(RoomActionTimeout):
        await filler

    assert _read("queue_full") == full_before + 1
    assert _read("timeout") == to_before + 1
    running.cancel()


# ----------------------------------------------------------------------
# 空闲退出竞态（原来就存在的两个坑）
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_action_arriving_at_idle_exit_is_still_processed():
    """"判定空闲"和"新动作入队"撞在一起时，动作不能被吞掉。

    原来的写法里，`wait_for` 超时已经取消了 `queue.get()`，紧接着 `put_nowait`
    把动作放进队列 —— 于是**没人取它，它的 future 永远不完成**。
    在 submit 有超时之前这是个永久挂起；有超时之后是一次无理由的 504。
    修法是退出前再看一眼队列。
    """
    async def handler(game_id, name):
        return name

    actor = _mk(handler, idle_timeout=0.1)
    # 让 Actor 起来并进入空闲等待，然后恰好在超时边界投递
    assert await actor.submit("warmup") == "warmup"
    await asyncio.sleep(0.1)
    result = await asyncio.wait_for(actor.submit("racy"), timeout=1.0)
    assert result == "racy"


@pytest.mark.asyncio
async def test_closed_actor_is_not_handed_out_again():
    """已退出的 Actor 不能再被交出去。

    交出去的话，调用方一 submit 就把它的 `_run` 重新 create_task 救活，
    于是**同一房间同时存在两个写者**——单写者模型（去掉分布式锁的全部依据）就破了。
    """
    async def handler(game_id, name):
        return name

    mgr = ActorManager()
    first = mgr.get_or_create("g1", handler)
    first._idle_timeout = 0.05
    assert await first.submit("x") == "x"
    await asyncio.sleep(0.15)          # 等它空闲退出

    assert first.closed
    second = mgr.get_or_create("g1", handler)
    assert second is not first, "拿到了已退出的 Actor"


@pytest.mark.asyncio
async def test_stale_idle_exit_does_not_evict_the_live_actor():
    """老 Actor 的退出回调不能把新 Actor 从注册表里删掉。

    回调按 game_id 盲删的话，"老的退出"晚于"新的建好"就会删掉**正在用的**那个，
    下次动作再建第三个——同一房间短暂存在两个写者。所以注销要比对身份。
    """
    async def handler(game_id, name):
        return name

    mgr = ActorManager()
    old = mgr.get_or_create("g1", handler)
    old._closed = True
    live = mgr.get_or_create("g1", handler)     # closed 的被替换掉

    mgr._deregister(old)                        # 老的回调迟到
    assert mgr.get_or_create("g1", handler) is live, "迟到的注销把在用的 Actor 删了"
