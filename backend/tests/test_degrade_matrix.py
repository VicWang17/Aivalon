"""5 级降级矩阵 + 开关中心 测试。

矩阵的本质是**这些措施有序且累积**：越往上不仅多关一样，而且必然包含下面所有级别。
所以它是一个整数旋钮，不是 5 个复选框——**5 个复选框要求操作的人在压力下自己
记住依赖顺序**，记错一步就是白降一轮。

验收口径四条：
  1. **判定必须是 `>=` 不是 `==`**：拧到更严重的档位，绝不能把低档位的措施放开
  2. 每一级砍的东西各自生效，且只砍自己那一刀（L5 不能挡进行中的对局）
  3. 拧完不重启就生效、不自己弹回来、能一键恢复
  4. 失败方向和 H-1 开关一致（人做的决定，读不到保持现状），
     和 ai_queue 的自动降级相反（机器推断的，推断不出来就别降）
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from fastapi import HTTPException

from app.core import degrade, metrics, sliding_window
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

    async def _clean():
        await client.delete(degrade.KEY)
        await client.delete(f"{sliding_window.KEY_PREFIX}degrade:create_game")
        degrade.clear_local_cache()

    await _clean()
    sliding_window.bind(client)
    yield client
    await _clean()
    await client.aclose()


# ----------------------------------------------------------------------
# 累积语义：这是这类"档位"最容易写错的地方
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_levels_are_cumulative_not_exclusive(redis):
    """拧到高档位，低档位的措施必须**仍然生效**。

    写成 `if level == 2: 走规则引擎` 看着没问题，但升到 3 档时 LLM 会自己开回来——
    **往更严重的方向拧旋钮，反而恢复了更贵的功能**。
    这条是整个矩阵的地基，所以逐级都验一遍。
    """
    await degrade.set_level(degrade.L5_REJECT_NEW_GAME, redis)
    for lv in range(degrade.L1_NO_AI_SPEECH, degrade.MAX_LEVEL + 1):
        assert await degrade.at_least(lv, redis), f"L{lv} 在 5 档下被放开了"


@pytest.mark.asyncio
async def test_lower_levels_are_not_active_yet(redis):
    """没拧到的档位不能提前生效——否则降一级等于降到底。"""
    await degrade.set_level(degrade.L2_AI_RULE_ENGINE, redis)
    assert await degrade.at_least(degrade.L1_NO_AI_SPEECH, redis)
    assert await degrade.at_least(degrade.L2_AI_RULE_ENGINE, redis)
    assert not await degrade.at_least(degrade.L3_SLOW_COLD_PATH, redis)
    assert not await degrade.at_least(degrade.L5_REJECT_NEW_GAME, redis)


@pytest.mark.asyncio
async def test_zero_level_activates_nothing(redis):
    """0 档什么都不关。默认状态必须是"什么都没降"，不能靠人去确认。"""
    for lv in range(degrade.L1_NO_AI_SPEECH, degrade.MAX_LEVEL + 1):
        assert not await degrade.at_least(lv, redis)


# ----------------------------------------------------------------------
# 旋钮本身：不重启生效 / 不自己弹回来 / 一键恢复
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_level_takes_effect_without_restart(redis):
    """一次写入，别的进程最迟 LOCAL_TTL 秒后读到。

    模拟"另一个进程"：清掉本进程缓存后重读，走的就是别的进程会走的那条路。
    """
    await degrade.set_level(degrade.L3_SLOW_COLD_PATH, redis)
    degrade.clear_local_cache()
    assert await degrade.level(redis) == degrade.L3_SLOW_COLD_PATH


@pytest.mark.asyncio
async def test_level_has_no_ttl(redis):
    """档位不设 TTL：降级是人做的决定，不能自己到期弹回来。

    有 TTL 的话，半夜降完级去睡觉，TTL 一到系统自己把闸放开——**同一个故障再来一遍**，
    而且这次没人在看。同 H-1 的开关。
    """
    await degrade.set_level(degrade.L4_QUEUE_CREATE_GAME, redis)
    assert await redis.ttl(degrade.KEY) == -1


@pytest.mark.asyncio
async def test_reset_recovers_in_one_step(redis):
    """一键恢复到 0 档。

    **恢复必须和降级一样一步到位**：要求逐级往下退，就是要求人在最累的时候
    多做 5 次操作、每次都可能记错顺序。
    """
    await degrade.set_level(degrade.L5_REJECT_NEW_GAME, redis)
    await degrade.reset(redis)
    assert await degrade.level(redis) == degrade.L0_NORMAL
    assert await redis.get(degrade.KEY) is None


@pytest.mark.asyncio
async def test_out_of_range_raises_instead_of_clamping(redis):
    """越界报错，**刻意不夹逼**。

    把手滑输入的 50 夹成 5，就是静默地把全站新开局拒了——
    操作的人以为自己填错了会收到报错，实际系统照他没打算的方式执行了。
    **夹逼是在替用户猜意图，而这个场景猜错的代价是全站不可用。**
    """
    for bad in (-1, degrade.MAX_LEVEL + 1, 50):
        with pytest.raises(ValueError):
            await degrade.set_level(bad, redis)
    assert await degrade.level(redis) == degrade.L0_NORMAL


@pytest.mark.asyncio
async def test_garbage_value_falls_back_to_zero(redis):
    """Redis 里是脏值时按 0 档处理。

    注意这和"读不到保持上次的值"不冲突：**读不到**是通路故障（保持现状），
    **读到了但是垃圾**是数据错误（这个值不可信，按最轻的来）。
    宁可不降，也不要因为一个坏值把全站拒了。
    """
    for bad in ("abc", "", "9", "-2", "2.5"):
        await redis.set(degrade.KEY, bad)
        degrade.clear_local_cache()
        assert await degrade.level(redis) == degrade.L0_NORMAL, f"脏值 {bad!r} 没兜住"


# ----------------------------------------------------------------------
# 失败方向：和 H-1 一致，和 ai_queue 相反
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_failure_keeps_the_last_known_level(redis, monkeypatch):
    """读不到时**保持上次读到的档位**，不退回 0 档。

    拧到高档位往往正因为线上在着火，而 Redis 抖动本身就是着火的一部分——
    这时候退回 0 档等于**旋钮在最需要它的那一刻自己弹回去了**。
    方向和 H-1 开关一致；和 `ai_queue` 那个自动降级恰好相反（那个是机器推断的，
    推断不出来就别擅自降）——**判据是这个决定由谁做出的**。
    """
    monkeypatch.setattr(degrade, "LOCAL_TTL", 0.0)   # 让本地缓存必然过期，逼它走 Redis
    await degrade.set_level(degrade.L4_QUEUE_CREATE_GAME, redis)

    class Broken:
        async def get(self, *a):
            raise ConnectionError("redis is down")

    assert await degrade.level(Broken()) == degrade.L4_QUEUE_CREATE_GAME


@pytest.mark.asyncio
async def test_never_read_and_redis_down_reports_zero(monkeypatch):
    """从没读到过、且 Redis 也挂了，只能报 0 档。

    没有"上次的值"可保持时，唯一安全的假设是"没人降过级"——
    这时候擅自认为在降级中，会让一个刚重启的进程凭空开始拒请求。
    """
    degrade.clear_local_cache()

    class Broken:
        async def get(self, *a):
            raise ConnectionError("redis is down")

    assert await degrade.level(Broken()) == degrade.L0_NORMAL


# ----------------------------------------------------------------------
# 各级的实际效果
# ----------------------------------------------------------------------

async def _ai_action(monkeypatch, redis, phase, level):
    """跑一次 AI 决策，返回 (是否碰了 LLM, 动作)。"""
    from app.schemas.game import GameState, PlayerState
    from app.models.game_enums import GamePhase
    from app.services import ai_service as ai_mod

    called = []

    async def spy(*a, **kw):
        called.append(1)
        return None                        # 让它回落，只关心"碰没碰"

    monkeypatch.setattr(ai_mod.AIService, "_call_llm", spy)
    # 把人手开关钉在"开着"，否则分不清是开关关的还是档位关的
    async def _on(*a, **kw):
        return True
    monkeypatch.setattr(ai_mod.switches, "ai_use_llm", _on)

    await degrade.set_level(level, redis)
    game = GameState(
        game_id="g-degrade", phase=phase,
        players=[PlayerState(user_id=1, username="AI-1", seat_id=0, is_ai=True)],
        leader_id=1, speaker_id=1, proposed_team=[1],
    )
    action = await ai_mod.AIService.get_action(game, game.players[0], redis_conn=redis)
    return bool(called), action


@pytest.mark.asyncio
async def test_l1_cuts_speech_only(redis, monkeypatch):
    """L1 只砍 AI 发言，投票照旧走 LLM。

    发言先挨刀是因为**它是最贵的一次 LLM 调用**（输出长、温度高、超时上界 45s
    是投票的两倍多），而且它说什么都不改变胜负——
    **排序依据是"这一刀砍掉多少成本"除以"玩家有多疼"**。
    """
    from app.models.game_enums import GamePhase

    touched, action = await _ai_action(monkeypatch, redis,
                                      GamePhase.SPEECH, degrade.L1_NO_AI_SPEECH)
    assert not touched, "L1 下发言还在打 LLM"
    assert action is not None, "降级后必须仍然给出动作——否则房间的阶段永远不推进"

    touched, _ = await _ai_action(monkeypatch, redis,
                                 GamePhase.VOTE, degrade.L1_NO_AI_SPEECH)
    assert touched, "L1 不该连投票一起砍，那是 L2 的事"


@pytest.mark.asyncio
async def test_l2_cuts_every_decision(redis, monkeypatch):
    """L2 全部决策走规则引擎，一次都不碰 LLM。"""
    from app.models.game_enums import GamePhase

    for phase in (GamePhase.SPEECH, GamePhase.VOTE):
        touched, action = await _ai_action(monkeypatch, redis, phase,
                                          degrade.L2_AI_RULE_ENGINE)
        assert not touched, f"L2 下 {phase} 还在打 LLM"
        assert action is not None


@pytest.mark.asyncio
async def test_degrade_level_is_counted_on_the_ai_path(redis, monkeypatch):
    """按档位摘掉 LLM 也要上报，reason 和自动降级分开。

    四档 reason 要人做的事完全不同：`queue_depth` 该看 worker 够不够，
    `switch` 是有人点名关了，`level_l1/l2` 该问的是"还没恢复吗"。
    """
    from app.models.game_enums import GamePhase

    label = metrics.ai_turns_degraded.labels(reason="level_l2")
    before = label._value.get()
    await _ai_action(monkeypatch, redis, GamePhase.VOTE, degrade.L2_AI_RULE_ENGINE)
    assert label._value.get() == before + 1


@pytest.mark.asyncio
async def test_l3_stretches_cold_path_intervals(redis):
    """L3 把冷路径的间隔按倍数拉长，**不是关掉**。

    榜单晚十几秒更新没人投诉，查不到榜单会被当成故障——
    关掉省不了更多成本，却把一个正常功能变成了报错。
    """
    assert await degrade.cold_path_interval(5.0, redis) == 5.0

    await degrade.set_level(degrade.L3_SLOW_COLD_PATH, redis)
    assert await degrade.cold_path_interval(5.0, redis) == 5.0 * settings.DEGRADE_COLD_PATH_FACTOR
    # 拧得更狠时也不能把降频撤销掉（同 test_levels_are_cumulative）
    await degrade.set_level(degrade.L5_REJECT_NEW_GAME, redis)
    assert await degrade.cold_path_interval(5.0, redis) > 5.0


@pytest.mark.asyncio
async def test_l4_queues_new_games_with_a_global_quota(redis, monkeypatch):
    """L4 收一个**全局**配额：还能建，但得排队。

    这个配额刻意不按用户算——按用户的那个（10 局/小时）一直都在生效，
    而它保护不了系统容量：**一万个用户每人只建 1 局完全合规，机器照样倒**。
    """
    monkeypatch.setattr(settings, "DEGRADE_CREATE_GAME_TIMES", 2)
    monkeypatch.setattr(settings, "DEGRADE_CREATE_GAME_SECONDS", 60.0)
    await degrade.set_level(degrade.L4_QUEUE_CREATE_GAME, redis)

    await degrade.guard_new_game(redis)
    await degrade.guard_new_game(redis)
    with pytest.raises(HTTPException) as e:
        await degrade.guard_new_game(redis)
    assert e.value.status_code == 429
    # 不说等多久，客户端就立刻重试，重试本身变成新的峰值（同 H-3a）
    assert int(e.value.headers["Retry-After"]) >= 1


@pytest.mark.asyncio
async def test_l4_quota_is_not_zero(redis):
    """L4 的配额必须是正数。

    配成 0 就等于 L5 了，**那这一级白设**——矩阵里两级效果一样，
    等于把可用的档位从 5 个变成 4 个，而操作的人还以为自己留了余地。
    """
    assert settings.DEGRADE_CREATE_GAME_TIMES > 0


@pytest.mark.asyncio
async def test_l5_rejects_new_games_with_503(redis):
    """L5 直接拒新开局。

    和 L4 的差别不只是数字大小，**是给客户端的语义不同**：
    L4 是 429（等一会儿能建，该排队重试），L5 是 503（这功能现在整个关了）。
    """
    await degrade.set_level(degrade.L5_REJECT_NEW_GAME, redis)
    with pytest.raises(HTTPException) as e:
        await degrade.guard_new_game(redis)
    assert e.value.status_code == 503
    assert "Retry-After" in e.value.headers


@pytest.mark.asyncio
async def test_new_game_passes_below_l4(redis):
    """L3 及以下不该挡建局——降级要一级一级来，不能提前砍。"""
    for lv in (degrade.L0_NORMAL, degrade.L1_NO_AI_SPEECH,
               degrade.L2_AI_RULE_ENGINE, degrade.L3_SLOW_COLD_PATH):
        await degrade.set_level(lv, redis)
        await degrade.guard_new_game(redis)      # 不抛就算过


@pytest.mark.asyncio
async def test_l5_gate_is_only_on_game_creation(redis):
    """L5 的闸只挂在建局上，**不挂在对局内的动作上**。

    "只服务进行中的房间"这句话的全部含义就在这里：已经开局的人还在玩，
    停的只是新流量进来。挡错地方就是把正在打的对局一起掀了——
    **那不是降级，那是故障**。用 AST 钉住调用点，不靠"记得别加"。
    """
    import ast
    import inspect
    from app.routers import game as game_router

    tree = ast.parse(inspect.getsource(game_router))
    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "guard_new_game"):
                guarded.add(node.name)

    assert guarded == {"create_game"}, f"降级闸挂错了地方: {guarded}"


@pytest.mark.asyncio
async def test_degrade_rejects_are_counted_apart_from_rate_limits(redis):
    """降级拒绝和限流拒绝**分开计数**。

    同一个 429 背后是两件完全不同的事：限流拒绝说明"有人打得太猛"，
    降级拒绝说明"是我们自己主动关掉的"。混在一个计数器里，
    事故里就分不清"流量涨了"和"我们把闸关了"。
    """
    label = metrics.degrade_rejects.labels(reason="level_l5")
    before = label._value.get()
    await degrade.set_level(degrade.L5_REJECT_NEW_GAME, redis)
    with pytest.raises(HTTPException):
        await degrade.guard_new_game(redis)
    assert label._value.get() == before + 1


@pytest.mark.asyncio
async def test_level_is_published_as_a_gauge(redis):
    """档位上 Gauge。

    **刻意用一个 Gauge 而不是每级一个**：档位是累积的，看曲线的人要的是
    "什么时候拧到了几档"这一条线，拆成 5 条布尔曲线反而要自己拼出这个信息。
    """
    await degrade.set_level(degrade.L3_SLOW_COLD_PATH, redis)
    assert metrics.degrade_level._value.get() == degrade.L3_SLOW_COLD_PATH
    await degrade.reset(redis)
    assert metrics.degrade_level._value.get() == degrade.L0_NORMAL


# ----------------------------------------------------------------------
# 开关中心
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_snapshot_carries_the_matrix_itself(redis):
    """快照带上整张矩阵和每一级的生效状态。

    **开关中心的价值主要在"看得清"而不是"切得动"**：切一个数字任何人都会写，
    难的是事故里第二分钟能一眼确认"现在几档、这一档到底关了什么"，
    而不是去翻代码——没人在半夜记得住 3 档砍的是什么。
    """
    await degrade.set_level(degrade.L2_AI_RULE_ENGINE, redis)
    snap = await degrade.snapshot(redis)

    assert snap["level"] == degrade.L2_AI_RULE_ENGINE
    assert snap["description"] == degrade.MATRIX[degrade.L2_AI_RULE_ENGINE]
    assert len(snap["matrix"]) == degrade.MAX_LEVEL + 1
    active = [row["level"] for row in snap["matrix"] if row["active"]]
    assert active == [degrade.L1_NO_AI_SPEECH, degrade.L2_AI_RULE_ENGINE]


def test_every_level_has_a_description():
    """每一级都得有人话说明，否则开关中心返回的矩阵会缺行。"""
    for lv in range(degrade.L0_NORMAL, degrade.MAX_LEVEL + 1):
        assert degrade.MATRIX.get(lv), f"L{lv} 没有说明"


def test_admin_endpoints_are_guarded_and_hidden():
    """改运行时行为的接口必须鉴权且不进 OpenAPI。

    **任何人都能把线上新开局拒掉的话，这个接口本身就是个可用性漏洞**——
    降级矩阵比单项开关更需要这条，因为它的 L5 是"全站不让进"。
    """
    import inspect
    from app.routers import admin

    for fn in (admin.get_degrade_level, admin.set_degrade_level,
               admin.reset_degrade_level):
        src = inspect.getsource(fn)
        assert "_guard(x_internal_secret)" in src, f"{fn.__name__} 没鉴权"
        assert "include_in_schema=False" in src, f"{fn.__name__} 进了 OpenAPI"
