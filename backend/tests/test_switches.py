"""降级开关：运行时可切 + 失败方向 测试。

验收口径三条：
  1. **不重启就生效**：一次写入，所有进程最迟 LOCAL_TTL 秒后读到新值
  2. **不自己弹回来**：开关本身没有 TTL，降级是人做的决定
  3. **读不到时保持已生效的决定**，不退回出厂默认值——Redis 抖动往往和事故同时发生

判据是"开关读出来是什么"和"AI 走了哪条分支"，不是"函数有没有报错"。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import switches


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")

NAME = switches.AI_USE_LLM


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)

    async def _clean():
        await client.delete(switches._key(NAME))
        switches.clear_local_cache()

    await _clean()
    yield client
    await _clean()
    await client.aclose()


# ----------------------------------------------------------------------
# 运行时可切
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_absent_switch_falls_back_to_config(redis):
    """没人动过开关时用配置默认值。

    这条保证**环境变量那条路照旧有效**：压测和集成测试都靠 `AI_USE_LLM=false`
    起环境，开关只是多了一条运行时覆盖的通路，不是把原来那条换掉。
    """
    assert await switches.get_bool(NAME, redis) == switches.default_of(NAME)


@pytest.mark.asyncio
async def test_switch_takes_effect_without_restart(redis):
    """切开关后立刻能读到新值——这是整件事的全部意义。

    原来 `AI_USE_LLM` 是启动时读的，要改就得重启所有 Celery worker，
    而 AI 决策就跑在 worker 里，重启会打断正在处理的 AI 回合。
    **一个需要重启才能生效的降级开关，在事故里等于没有。**
    """
    await switches.set_bool(NAME, False, redis)
    assert await switches.get_bool(NAME, redis) is False

    await switches.set_bool(NAME, True, redis)
    assert await switches.get_bool(NAME, redis) is True


@pytest.mark.asyncio
async def test_other_process_sees_new_value_within_local_ttl(redis):
    """别的进程最迟 LOCAL_TTL 秒后读到新值。

    本地缓存是为了不把 Redis 挂在 AI 热路径上（开关每个 AI 回合都要读一次），
    代价就是切换有个最长 LOCAL_TTL 的延迟——同 cache.py 的口径：**TTL 就是一致性上限**。
    """
    await switches.get_bool(NAME, redis)                 # 预热本地缓存
    await redis.set(switches._key(NAME), "0")            # 模拟另一个进程切了开关

    # 缓存还没过期，本进程读到的仍是旧值（这是有意的，不是 bug）
    assert await switches.get_bool(NAME, redis) == switches.default_of(NAME)

    await asyncio.sleep(switches.LOCAL_TTL + 0.05)
    assert await switches.get_bool(NAME, redis) is False


@pytest.mark.asyncio
async def test_switch_has_no_ttl(redis):
    """开关本身不能有 TTL。

    半夜降级完去睡觉，TTL 一到 AI 又开始打 LLM——那就是同一个故障再来一遍。
    **降级是人做的决定，只能由人显式撤销。**
    """
    await switches.set_bool(NAME, False, redis)
    assert await redis.ttl(switches._key(NAME)) == -1, "开关被设了 TTL，会自己弹回来"


@pytest.mark.asyncio
async def test_reset_returns_to_config_default(redis):
    """复位 = 删掉运行时覆盖，回到配置默认值。"""
    await switches.set_bool(NAME, not switches.default_of(NAME), redis)
    await switches.reset(NAME, redis)
    assert await switches.get_bool(NAME, redis) == switches.default_of(NAME)
    assert await redis.exists(switches._key(NAME)) == 0


@pytest.mark.asyncio
async def test_unknown_switch_is_rejected(redis):
    """只认登记过的开关名。

    拼错名字要当场报错，不能默默写进一个没人读的 key——那样操作的人以为切了，
    实际什么都没发生，而事故中这种"以为切了"比报错危险得多。
    """
    with pytest.raises(KeyError):
        await switches.set_bool("no-such-switch", False, redis)
    with pytest.raises(KeyError):
        await switches.reset("no-such-switch", redis)


# ----------------------------------------------------------------------
# 失败方向：这一节是这个模块最要紧的地方
# ----------------------------------------------------------------------

class BrokenRedis:
    async def get(self, key):
        raise ConnectionError("redis down")


@pytest.mark.asyncio
async def test_redis_failure_keeps_the_last_known_value(redis):
    """Redis 读失败时保持**本进程上次读到的值**，哪怕它已经过期。

    这个方向很要紧：开关被切到"已降级"往往正是因为线上在着火，
    而 Redis 抖动本身就是着火的一部分——这时候退回配置默认值（LLM 开着）
    等于**开关在最需要它的那一刻自己失效了**。
    **开关的失败方向要偏向"保持当前已生效的决定"，不是偏向出厂设置。**
    """
    await switches.set_bool(NAME, False, redis)          # 已降级
    await asyncio.sleep(switches.LOCAL_TTL + 0.05)       # 本地缓存过期

    assert await switches.get_bool(NAME, BrokenRedis()) is False, "降级态被 Redis 抖动弄丢了"


@pytest.mark.asyncio
async def test_redis_failure_without_any_prior_read_uses_default(redis):
    """从来没读到过（进程刚起来 Redis 就挂了）只能用配置默认值。

    没有"上次的值"可保持，这时候退回默认值是唯一选择——
    但绝不能因此抛异常把业务路径带崩：读不到开关不该让 AI 回合失败。
    """
    switches.clear_local_cache()
    assert await switches.get_bool(NAME, BrokenRedis()) == switches.default_of(NAME)


@pytest.mark.asyncio
async def test_get_bool_never_raises(redis):
    """redis 传 None 也得给出一个值，不能抛。"""
    switches.clear_local_cache()

    class NoGet:
        async def get(self, key):
            raise RuntimeError("boom")

    assert isinstance(await switches.get_bool(NAME, NoGet()), bool)


# ----------------------------------------------------------------------
# 开关真的接在 AI 决策上（不然上面全是空转）
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ai_takes_rule_engine_branch_when_degraded(redis, monkeypatch):
    """开关切成降级后，AI 必须走规则引擎而不是 LLM。

    这条是把开关和它要控制的行为**连起来**的断言：前面那些只验开关本身读写对不对，
    真正要保证的是"切了开关，AI 的分支真的变了"。
    """
    from app.services.ai_service import AIService

    called = {"fallback": 0, "llm": 0}

    def fake_fallback(game, player):
        called["fallback"] += 1
        return {"action_type": "vote", "payload": {}}

    async def fake_speech(game, player):
        called["llm"] += 1
        return {"action_type": "speech", "payload": {}}

    monkeypatch.setattr(AIService, "_get_fallback_action", staticmethod(fake_fallback))
    monkeypatch.setattr(AIService, "_handle_speech", staticmethod(fake_speech))

    class P:
        is_ai = True
        username = "ai-1"

    class G:
        from app.models.game_enums import GamePhase
        phase = GamePhase.SPEECH

    await switches.set_bool(NAME, False, redis)
    await AIService.get_action(G(), P(), redis_conn=redis)
    assert called == {"fallback": 1, "llm": 0}, "开关切了但 AI 还在打 LLM"

    await switches.set_bool(NAME, True, redis)
    await AIService.get_action(G(), P(), redis_conn=redis)
    assert called == {"fallback": 1, "llm": 1}, "开关开着却没走 LLM"


@pytest.mark.asyncio
async def test_snapshot_reports_value_and_default(redis):
    """开关中心要能同时看到"现在是什么"和"默认是什么"。

    事故里第一件事就是确认到底切没切——只显示当前值的话，
    分不清"这是默认值"还是"有人切成了这个值"。
    """
    await switches.set_bool(NAME, False, redis)
    snap = await switches.snapshot(redis)
    assert snap[NAME]["value"] is False
    assert snap[NAME]["default"] == switches.default_of(NAME)
