"""资源层限流·下：AI 队列深度上限 测试。

这一层和前面三处限流**方向相反：它不能拒**。
网关层拒 HTTP、应用层拒建局、房间层拒动作，都有个共同前提——
**对面有客户端，它会拿到错误码、会重试**。AI 回合没有这个前提：
一个 AI 回合被丢掉就没有任何人会再提交它，那个房间的阶段永远不推进，
**房间不是变慢而是永久卡死**。所以过载响应只能是降级。

验收口径四条：
  1. 深度确实随投递涨、随完成落
  2. 到阈值触发降级（摘掉 LLM 走规则引擎），压力退了自动恢复
  3. **漏账要能自愈**：worker 被 kill -9 不会注销，误差必须有上界
  4. 读不到 Redis 时**不降级**（方向和 H-1 降级开关相反）
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import ai_queue, metrics
from app.core.config import settings


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    await client.delete(ai_queue.KEY)
    ai_queue.bind(client)
    yield client
    await client.delete(ai_queue.KEY)
    ai_queue._script = None
    await client.aclose()


# ----------------------------------------------------------------------
# 记账
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_depth_tracks_dispatch_and_completion(redis):
    """投递让深度涨，完成让它落。"""
    tokens = []
    for expected in (1, 2, 3):
        token, depth = await ai_queue.enter(redis)
        tokens.append(token)
        assert depth == expected

    await ai_queue.leave(tokens[0], redis)
    assert await ai_queue.depth(redis) == 2


@pytest.mark.asyncio
async def test_depth_counts_queued_tasks_not_just_running_ones(redis):
    """记账在**投递侧**，不是等 worker 领到任务才记。

    等领到才记的话，积压期间躺在队列里的任务一个都不算，
    **深度永远看着很小，而那恰恰是要看的东西**——积压的定义就是"投了还没被处理"。
    """
    for _ in range(5):
        await ai_queue.enter(redis)      # 模拟只投递、没有 worker 在消费
    assert await ai_queue.depth(redis) == 5


@pytest.mark.asyncio
async def test_concurrent_dispatch_counts_every_task(redis):
    """并发投递不能漏计。

    「按租约清理 → 记一笔 → 数一下」是读改写序列，多个 API 进程共享同一个 key，
    拆成多条命令会让两个进程各自读到旧的深度。member 用 uuid 也是必须的
    （同 H-3b：ZSET 里 member 相同会被覆盖成一条，并发越高漏计越多）。
    """
    await asyncio.gather(*[ai_queue.enter(redis) for _ in range(30)])
    assert await redis.zcard(ai_queue.KEY) == 30


@pytest.mark.asyncio
async def test_leave_is_idempotent_and_tolerates_unknown_token(redis):
    """重复注销、注销不存在的 token 都不该出错——
    Celery 的 at-least-once 语义下同一个任务可能被执行两次。"""
    token, _ = await ai_queue.enter(redis)
    await ai_queue.leave(token, redis)
    await ai_queue.leave(token, redis)
    await ai_queue.leave("never-existed", redis)
    assert await ai_queue.depth(redis) == 0


# ----------------------------------------------------------------------
# 漏账自愈：这是选 ZSET 而不是 INCR/DECR 计数器的全部理由
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_leaked_entries_expire_by_lease(redis, monkeypatch):
    """worker 崩了没注销的那笔，被租约兜掉。

    **纯 INCR/DECR 计数器在这里是错的选择**：worker 被 kill -9 就不会 DECR，
    误差**只会单向累积**，攒够阈值之后系统永久停在降级态——
    而且从指标上看不出来是漏账还是真积压。
    ZSET 存"在飞任务 → 投递时刻"，计数前按租约清理：
    一个任务要么正常注销、要么被租约兜掉，**误差有上界且会自己消失**（同 C06 心跳租约）。
    """
    monkeypatch.setattr(settings, "AI_QUEUE_LEASE", 0.3)
    await ai_queue.enter(redis)          # 拿到 token 就"崩了"，永不注销
    assert await ai_queue.depth(redis) == 1

    await asyncio.sleep(0.4)
    assert await ai_queue.depth(redis) == 0, "漏账没有被租约兜掉，误差会永久累积"


@pytest.mark.asyncio
async def test_key_expires_when_everything_drains(redis, monkeypatch):
    """全部注销后 key 自己消失，不留残骸。"""
    monkeypatch.setattr(settings, "AI_QUEUE_LEASE", 0.3)
    await ai_queue.enter(redis)
    ttl = await redis.ttl(ai_queue.KEY)
    assert 0 < ttl <= 61


# ----------------------------------------------------------------------
# 降级判定
# ----------------------------------------------------------------------

def test_degrade_triggers_at_threshold(monkeypatch):
    """到阈值才降，没到不降。"""
    monkeypatch.setattr(settings, "AI_QUEUE_DEGRADE_DEPTH", 10)
    assert not ai_queue.should_degrade(9)
    assert ai_queue.should_degrade(10)
    assert ai_queue.should_degrade(50)


def test_degrade_is_not_sticky(monkeypatch):
    """压力退了自动恢复：判定只看当前深度，不留状态。

    这和 H-1 那个**人手切的开关刻意不设 TTL**（降级是人做的决定，不能自己弹回来）
    恰好相反——**自动推断出来的降级必须能自动撤销**，
    否则一次瞬时积压会让 AI 永久说套话，而没有人知道该去关掉它。
    """
    monkeypatch.setattr(settings, "AI_QUEUE_DEGRADE_DEPTH", 10)
    assert ai_queue.should_degrade(20)
    assert not ai_queue.should_degrade(3)     # 同一进程，深度掉下来就不降了


def test_degrade_is_counted(monkeypatch):
    """自动降级必须上报。

    人手切开关时至少有人知道自己切了；**按深度自动触发的降级没人按过按钮**——
    不上报的话"AI 怎么突然开始说套话了"在复盘时无从查起（同 H-1：不可观测就是静默变更）。
    """
    before = metrics.ai_turns_degraded.labels(reason="queue_depth")._value.get()
    ai_queue.note_degraded()
    after = metrics.ai_turns_degraded.labels(reason="queue_depth")._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_depth_is_published_as_a_gauge(redis):
    """深度上 Gauge。各进程写的都是从 Redis 读回来的同一个数，多进程下不会互相打架。"""
    await ai_queue.enter(redis)
    await ai_queue.enter(redis)
    await ai_queue.depth(redis)
    assert metrics.ai_queue_depth._value.get() == 2


# ----------------------------------------------------------------------
# 失败方向
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_failure_reports_zero_so_nothing_degrades(redis):
    """Redis 挂了报 0 = 不降级。

    方向和限流器一致、**和 H-1 降级开关相反**：开关是人做的决定，读不到要保持已生效的
    降级态；这里的降级是**自动推断**出来的，推断不出来就别擅自降。
    误降的代价是全站 AI 一起说套话（玩家看得见的产品退化），
    而不降的代价有 H-2 的 LLM 舱壁兜着——每次调用有硬超时且自己回落规则引擎，
    最坏是慢一次，不会卡死。**哪边的代价可恢复，就往哪边倒。**
    """
    class Broken:
        async def __call__(self, *a, **kw):
            raise ConnectionError("redis is down")

    ai_queue._script = Broken()
    assert await ai_queue.depth(redis) == 0
    token, depth = await ai_queue.enter(redis)
    assert depth == 0
    assert not ai_queue.should_degrade(depth)


@pytest.mark.asyncio
async def test_depth_without_bind_reports_zero():
    """没注册脚本时报 0：import 到 app 但没起 lifespan 的场景不该被误判成积压。"""
    ai_queue._script = None
    assert await ai_queue.depth(None) == 0


@pytest.mark.asyncio
async def test_leave_failure_does_not_raise(redis):
    """注销失败只记日志。

    上抛的话，一次 Redis 抖动会把**已经算完的** AI 回合变成一次 Celery 重试，
    于是同一个回合又打一次 LLM——收敛压力的机制反过来制造压力。
    漏掉的这笔由租约兜掉。
    """
    class Broken:
        async def zrem(self, *a):
            raise ConnectionError("redis is down")

    await ai_queue.leave("some-token", Broken())     # 不抛就算过


@pytest.mark.asyncio
async def test_lease_must_exceed_worst_case_turn():
    """租约必须大于单个 AI 回合的最坏耗时。

    小了会把**正在跑的**任务当成漏账清掉，于是深度永远上不去、降级永不触发——
    一个"看起来在工作"的保护机制，实际从来没生效过。
    """
    assert settings.AI_QUEUE_LEASE > settings.AI_LLM_TIMEOUT_SPEECH


# ----------------------------------------------------------------------
# 接到 AI 决策上
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_force_fallback_skips_llm_entirely(monkeypatch):
    """`force_fallback` 直接走规则引擎，一次都不碰 LLM。

    降级降的是**每个任务多贵**，不是放几个任务进来：同样长的队列，
    走 LLM 每条十几秒、走规则引擎每条毫秒级——
    **队列长度没变，排空时间差三个数量级**。
    """
    from app.schemas.game import GameState, PlayerState
    from app.models.game_enums import GamePhase
    from app.services import ai_service as ai_mod

    called = []

    async def boom(*a, **kw):
        called.append(1)
        raise AssertionError("降级时不该调用 LLM")

    monkeypatch.setattr(ai_mod.AIService, "_call_llm", boom)

    game = GameState(
        game_id="g1", phase=GamePhase.VOTE,
        players=[PlayerState(user_id=1, username="AI-1", seat_id=0, is_ai=True)],
        leader_id=1, proposed_team=[1],
    )
    action = await ai_mod.AIService.get_action(game, game.players[0],
                                              force_fallback=True)
    assert called == []
    assert action is not None, "降级后必须仍然给出一个动作——否则房间的阶段永远不推进"


@pytest.mark.asyncio
async def test_force_fallback_is_separate_from_the_manual_switch(monkeypatch):
    """自动降级和人手开关是两个独立入口。

    混成一个的话：要么自动降级把人手切的开关覆盖掉（人的决定被机器撤销），
    要么反过来自动降级撤不回去。**一个每回合重新判断、一个要留到人来撤销**，
    生命周期不同的东西不能共用一个状态位。
    """
    import inspect
    from app.services.ai_service import AIService

    sig = inspect.signature(AIService.get_action)
    assert "force_fallback" in sig.parameters
    assert sig.parameters["force_fallback"].default is False
