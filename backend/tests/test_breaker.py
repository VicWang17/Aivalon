"""依赖熔断 测试。

**舱壁和熔断不是一件事**，这份测试大半在钉这条区别：
舱壁（H-2）保证单次调用最多等 45 秒，熔断保证**学到依赖不行之后一次都不等**。
所以第一条测试量的是"短路那次花了多少时间"，不是"返回值对不对"。

验收口径五条：
  1. 跳闸后的调用**不花时间**，且和超时分开计数
  2. 判定按窗口内比例、且有最小样本门槛（不是"连续 N 次"，也不能一次抖动就摘依赖）
  3. **非法 JSON 不算依赖失败**——那是我们自己的 prompt 问题，算进去就永远合不回来
  4. 半开只放一个探针；探针不回报也必须能放下一个（不能永久停在半开）
  5. 熔断推断的降级和人手拧的档位**各存各的、读时取 max**，互不覆盖
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import time

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.core import breaker, degrade, metrics
from app.services.llm_service import LLMService, llm_breaker


def _redis_ok() -> bool:
    import redis as sync_redis
    try:
        return sync_redis.Redis(host="localhost", port=6379, socket_timeout=1).ping()
    except Exception:
        return False


@pytest.fixture(autouse=True)
def clean_breakers():
    """每个用例前后都把熔断器复位。

    进程内状态最容易在测试之间串味：一个用例留下个 open 的熔断器，
    下一个用例（甚至别的测试文件里的降级用例）就会莫名读到"已降到 2 档"。
    """
    breaker.reset_all()
    yield
    breaker.reset_all()


def _mk(**kw) -> breaker.Breaker:
    opts = dict(window=10.0, min_samples=4, failure_ratio=0.5, open_for=0.2)
    opts.update(kw)
    return breaker.Breaker("test-dep", **opts)


# ----------------------------------------------------------------------
# 熔断到底买到了什么：不是"少出错"，是"不白等"
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_open_breaker_costs_no_time(monkeypatch):
    """跳闸后的调用**一秒都不等**，这才是熔断相对舱壁的全部增量。

    舱壁（H-2）已经保证单次不超过上界，但 LLM 挂掉时**每个** AI 回合照旧
    要付满那个上界才回落——对局能推进，可每步慢几十秒，和卡死没区别。
    所以这条断言量的是时间，不是返回值。
    """
    async def slow(*a, **kw):
        await asyncio.sleep(5)          # 模拟挂掉的依赖：慢到必然超时

    monkeypatch.setattr(LLMService, "_request", slow)

    # 先把它打到跳闸：min_samples 次超时（上界给 0.05s，别真等）
    for _ in range(llm_breaker.min_samples):
        await LLMService.generate_response("s", "u", timeout=0.05)
    assert llm_breaker.state == breaker.OPEN, "连续超时没能让熔断器跳闸"

    started = time.monotonic()
    result = await LLMService.generate_response("s", "u", timeout=5)
    elapsed = time.monotonic() - started

    assert "error" in result, "熔断中必须给调用方一个错误让它回落，不能返回半个答案"
    assert elapsed < 0.05, f"熔断中还等了 {elapsed:.3f}s，等于熔断没生效"


@pytest.mark.asyncio
async def test_short_circuit_is_counted_apart_from_timeout(monkeypatch):
    """`breaker_open` 和 `timeout` 分开计数。

    熔断生效后 **timeout 那一档应该停止增长、breaker_open 接着涨**。
    两个一起涨就说明熔断器没挂在真正的调用点上——
    合成一档的话这个错法完全看不出来。
    """
    async def slow(*a, **kw):
        await asyncio.sleep(5)

    monkeypatch.setattr(LLMService, "_request", slow)
    for _ in range(llm_breaker.min_samples):
        await LLMService.generate_response("s", "u", timeout=0.05)

    timeout_label = metrics.llm_calls_total.labels(result="timeout")
    open_label = metrics.llm_calls_total.labels(result="breaker_open")
    reject_label = metrics.breaker_rejects.labels(name="llm")
    t_before, o_before, r_before = (timeout_label._value.get(),
                                   open_label._value.get(),
                                   reject_label._value.get())

    await LLMService.generate_response("s", "u", timeout=0.05)

    assert timeout_label._value.get() == t_before, "熔断中还在记 timeout，说明真去调了"
    assert open_label._value.get() == o_before + 1
    assert reject_label._value.get() == r_before + 1


@pytest.mark.asyncio
async def test_short_circuit_is_not_observed_in_latency(monkeypatch):
    """短路那次**不进耗时直方图**。

    压根没发生的等待记进去会把 P99 一路拉绿——**依赖挂着的时候延迟曲线反而变好看**。
    这和 H-2 那条"失败和超时的耗时也要记"是同一个陷阱的两面：
    **真实发生过的等待必须记，没发生的调用不能记。**
    """
    async def slow(*a, **kw):
        await asyncio.sleep(5)

    monkeypatch.setattr(LLMService, "_request", slow)
    for _ in range(llm_breaker.min_samples):
        await LLMService.generate_response("s", "u", timeout=0.05)

    before = metrics.llm_latency._sum.get()
    await LLMService.generate_response("s", "u", timeout=0.05)
    assert metrics.llm_latency._sum.get() == before


# ----------------------------------------------------------------------
# 判定口径：比例 + 最小样本
# ----------------------------------------------------------------------

def test_one_failure_never_trips():
    """一次失败不跳闸。

    **最小样本数是比例判定的前提**：1 次调用里失败 1 次是 100% 失败率，
    少了这条门槛，第一次网络抖动就能把整个依赖摘掉（同 C07 分位数的样本量前提）。
    """
    b = _mk(min_samples=4)
    b.record(False)
    assert b.state == breaker.CLOSED
    assert b.snapshot()["samples"] == 1


def test_alternating_failures_still_trip():
    """成功失败交替也要能跳闸——这是不用"连续 N 次"的理由。

    "连续 5 次失败"在依赖半死不活时**永远不跳**：偶尔成功一次就把连续计数清零，
    而实际上一半的请求都在付满超时。按窗口比例判才拦得住这种最常见的故障形态。
    """
    b = _mk(min_samples=4, failure_ratio=0.5)
    for ok in (True, False, True, False):
        b.record(ok)
    assert b.state == breaker.OPEN, "半数失败没跳闸"


def test_healthy_traffic_does_not_trip():
    """失败率没到阈值就不许跳——熔断器误跳的代价是白白降级。"""
    b = _mk(min_samples=4, failure_ratio=0.5)
    for ok in (True, True, True, False):
        b.record(ok)
    assert b.state == breaker.CLOSED


def test_old_failures_age_out_of_the_window():
    """窗口外的失败不算。

    不按窗口滑动的话，一次半夜的故障会永远留在计数里，攒够阈值直接跳闸——
    **凭几小时前的失败拒绝现在的请求**。同 H-3b 换掉固定窗口的动机。
    """
    b = _mk(window=0.05, min_samples=2, failure_ratio=0.5)
    b.record(False)
    b.record(False)
    b.reset()                       # 先清掉这次跳闸，只验证过期逻辑
    b.record(False)
    time.sleep(0.06)
    b.record(False)                 # 上一条已经滑出窗口，样本只剩这 1 条
    assert b.state == breaker.CLOSED
    assert b.snapshot()["samples"] == 1


@pytest.mark.asyncio
async def test_invalid_json_does_not_trip_the_breaker(monkeypatch):
    """**非法 JSON 不算依赖失败。**

    这条最反直觉，也最要紧。依赖是活的、答得也快，只是答得不对——多半是我们
    自己的 prompt 有问题。算成失败的话，**一个 prompt bug 会把熔断器跳开，
    而半开探测同样会拿回非法 JSON，于是它永远合不回来**：
    我们自己的 bug 变成了"依赖不可用"，而且再也修不回来（除非重启）。
    判据是这次失败**有没有白花时间**，不是"我拿到想要的了没有"。
    """
    async def garbage(*a, **kw):
        return "not json at all"

    monkeypatch.setattr(LLMService, "_request", garbage)
    for _ in range(llm_breaker.min_samples * 2):
        result = await LLMService.generate_response("s", "u")
        assert "error" in result          # 调用方照旧回落
    assert llm_breaker.state == breaker.CLOSED, "非法 JSON 把熔断器跳开了"


@pytest.mark.asyncio
async def test_cancellation_is_not_a_dependency_failure(monkeypatch):
    """取消不记账。取消是我们自己的决定，不是依赖的问题（同 DEVLOG 029）。

    记成失败的话，客户端断连、任务被取消这类正常事件会把依赖误判成挂了。
    """
    async def cancelled(*a, **kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(LLMService, "_request", cancelled)
    for _ in range(llm_breaker.min_samples):
        with pytest.raises(asyncio.CancelledError):
            await LLMService.generate_response("s", "u")
    assert llm_breaker.snapshot()["samples"] == 0
    assert llm_breaker.state == breaker.CLOSED


# ----------------------------------------------------------------------
# 半开：恢复探测不能自己把依赖再打挂
# ----------------------------------------------------------------------

def test_half_open_lets_exactly_one_probe_through():
    """半开**只放一个请求过去**。

    全放过去的话，依赖刚有点起色就被积压的流量一次打回原形——
    **恢复探测本身把依赖又打挂了**。其余请求立刻短路回落、不等待，所以不吃亏。
    """
    b = _mk(min_samples=2, failure_ratio=0.5, open_for=0.05)
    b.record(False)
    b.record(False)
    assert not b.allow()                     # 冷却期内一律拒

    time.sleep(0.06)
    assert b.allow(), "冷却结束后第一个探针必须放行"
    assert not b.allow(), "半开放行了第二个请求"
    assert not b.allow()


def test_probe_success_closes_and_clears_the_window():
    """探针成功就闭合，**并且必须清空窗口**。

    留着跳闸前那批失败记录的话，闭合后第一次失败就会把比例重新算到阈值上——
    **刚合上就又跳开**，于是依赖已经好了却还在半瘫。
    """
    b = _mk(min_samples=2, failure_ratio=0.5, open_for=0.05)
    b.record(False)
    b.record(False)
    time.sleep(0.06)

    assert b.allow()
    b.record(True)
    assert b.state == breaker.CLOSED
    assert b.snapshot()["samples"] == 0, "闭合后旧的失败记录还留着"

    b.record(False)
    assert b.state == breaker.CLOSED, "刚闭合就被一次失败重新跳开"


def test_probe_failure_trips_again_immediately():
    """探针失败立刻重新跳开，**不等窗口攒够**。

    半开状态下总共只有这一个样本，等"够不够 min_samples"就是永远不够——
    于是每个请求都成了探针，等价于熔断器没生效。
    """
    b = _mk(min_samples=5, failure_ratio=0.5, open_for=0.05)
    b.record(False)
    for _ in range(4):
        b.record(False)
    assert b.state == breaker.OPEN
    time.sleep(0.06)

    assert b.allow()
    b.record(False)
    assert b.state == breaker.OPEN
    assert not b.allow(), "探针失败后还在放行"


def test_a_probe_that_never_reports_does_not_wedge_the_breaker():
    """探针没回报时**必须能放下一个**。

    调用方漏了 `record`、进程被打断、任务被取消，都会留下一个永远不回报的探针。
    只认"上一个探针回报了才放下一个"的话，这个熔断器就永久停在半开、
    再也不探测了——**依赖早好了，它自己不知道**。
    同 ai_queue 的租约：**凡是登记出去等对方回报的东西，都必须假设对方不会来**（同 C06）。
    """
    b = _mk(min_samples=2, failure_ratio=0.5, open_for=0.05)
    b.record(False)
    b.record(False)
    time.sleep(0.06)

    assert b.allow()                 # 探针出去了，然后我们故意不 record
    assert not b.allow()             # 探针期限内不放第二个
    time.sleep(0.06)
    assert b.allow(), "探针不回报把熔断器锁死在半开了"


def test_trips_are_counted_not_just_stated():
    """跳闸次数上 Counter。

    **`state` 是瞬时值，抓取间隔里跳闸又恢复就完全看不到**；跳闸次数只增不减、抓不丢。
    短时间反复跳闸（次数在涨但 state 一直是 0）说明依赖半死不活或阈值太敏感——
    只看 state 会以为一切正常。
    """
    label = metrics.breaker_trips.labels(name="test-dep")
    before = label._value.get()
    b = _mk(min_samples=2, failure_ratio=0.5)
    b.record(False)
    b.record(False)
    assert label._value.get() == before + 1
    assert metrics.breaker_state.labels(name="test-dep")._value.get() == 2


# ----------------------------------------------------------------------
# 和降级矩阵的关系：各存各的，读时取 max
# ----------------------------------------------------------------------

pytestmark_redis = pytest.mark.skipif(not _redis_ok(), reason="需要本机 Redis 在线")


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis(host="localhost", port=6379, decode_responses=True)
    await client.delete(degrade.KEY)
    degrade.clear_local_cache()
    yield client
    await client.delete(degrade.KEY)
    degrade.clear_local_cache()
    await client.aclose()


def test_implied_level_only_counts_open_breakers():
    """只有 `open` 才压降级档位。

    半开必须**先把推断出来的降级撤掉**：调用方读到"还在降级"就不会去调依赖，
    探针也就永远发不出去。闭合后自然回 0——**自动触发的降级必须能自动恢复**。
    """
    b = breaker.register(_mk(min_samples=2, failure_ratio=0.5,
                             open_for=0.05, implies_level=2))
    assert breaker.implied_level() == 0

    b.record(False)
    b.record(False)
    assert breaker.implied_level() == 2

    time.sleep(0.06)                      # 进半开
    assert breaker.implied_level() == 0, "半开时还压着降级，探针就永远发不出去"

    b.allow()
    b.record(True)
    assert breaker.implied_level() == 0


@pytest.mark.asyncio
@pytestmark_redis
async def test_breaker_degradation_never_writes_the_manual_knob(redis):
    """熔断器**绝不去写人手拧的那个档位**。

    跳闸后 `set_level(2)` 只有一行，很诱人，但它会**让机器覆盖人的决定**，
    而且依赖恢复后没人能把它撤回来——"到底是谁拧的"这个信息在一个整数里存不下。
    所以两个来源各存各的、读时取 max（同 H-3c·下：**生命周期不同的东西不能共用状态位**）。
    """
    b = breaker.register(_mk(min_samples=2, failure_ratio=0.5,
                             open_for=10.0, implies_level=2))
    b.record(False)
    b.record(False)

    assert await degrade.effective_level(redis) == 2, "熔断跳闸没反映到生效档位上"
    assert await degrade.level(redis) == 0, "熔断器写了人手那个 key"
    assert await redis.get(degrade.KEY) is None


@pytest.mark.asyncio
@pytestmark_redis
async def test_breaker_recovery_does_not_erase_the_human_decision(redis):
    """熔断器恢复不能把人拧的档位一起撤掉。

    这正是"直接写那个 key"最隐蔽的后果：人本来拧在 3 档，熔断器跳闸写 2、
    恢复时写 0，**人半夜拧的那一档就没了**，而且没人会注意到。
    """
    await degrade.set_level(degrade.L3_SLOW_COLD_PATH, redis)
    b = breaker.register(_mk(min_samples=2, failure_ratio=0.5,
                             open_for=0.05, implies_level=5))
    b.record(False)
    b.record(False)
    assert await degrade.effective_level(redis) == 5

    time.sleep(0.06)
    b.allow()
    b.record(True)                        # 依赖恢复
    assert await degrade.effective_level(redis) == degrade.L3_SLOW_COLD_PATH
    assert await degrade.level(redis) == degrade.L3_SLOW_COLD_PATH


@pytest.mark.asyncio
@pytestmark_redis
async def test_manual_level_wins_when_it_is_higher(redis):
    """取 max 而不是"熔断器说了算"。

    两个来源的意图都是"这一刀砍下去"，**谁都不该被别人的"不用降"覆盖掉**
    （同 AI 侧四条通路取"或"）。人拧到 5 档时，一个 implies_level=2 的熔断器
    恢复了也不该把 L5 放开。
    """
    await degrade.set_level(degrade.L5_REJECT_NEW_GAME, redis)
    b = breaker.register(_mk(min_samples=2, failure_ratio=0.5,
                             open_for=10.0, implies_level=2))
    b.record(False)
    b.record(False)
    assert await degrade.effective_level(redis) == degrade.L5_REJECT_NEW_GAME


@pytest.mark.asyncio
@pytestmark_redis
async def test_breaker_implied_level_actually_gates_features(redis):
    """熔断推断出的档位要**真的让那一级的措施生效**，不能只是个数字。

    只更新指标不影响 `at_least` 的话，这条链路等于只是"报告了一下"——
    LLM 挂了但 AI 路径照旧一个个去打它，熔断白熔。
    """
    b = breaker.register(_mk(min_samples=2, failure_ratio=0.5,
                             open_for=10.0, implies_level=2))
    assert not await degrade.at_least(degrade.L2_AI_RULE_ENGINE, redis)
    b.record(False)
    b.record(False)
    assert await degrade.at_least(degrade.L2_AI_RULE_ENGINE, redis)
    assert not await degrade.at_least(degrade.L3_SLOW_COLD_PATH, redis)


@pytest.mark.asyncio
@pytestmark_redis
async def test_breaker_recovers_even_when_its_own_degradation_hides_the_call_site(redis):
    """**熔断器不能被自己推断出的降级锁死。**

    这是这一版最容易埋进去的死锁：跳闸把档位顶到 L2，而 AI 路径读到"已降到 L2"
    就直接走规则引擎、**一次都不会再调 LLM**——于是没人来调用 `allow()`，
    冷却期过了也没人推进状态，熔断器永远停在 open。
    **它推断出的降级，反过来挡住了它自己唯一的恢复途径。**
    所以状态推进挂在"任何人读状态"这一步（`_advance`），
    `implied_level()` 那次读就顺手把它推进到半开了。
    """
    b = breaker.register(_mk(min_samples=2, failure_ratio=0.5,
                             open_for=0.05, implies_level=2))
    b.record(False)
    b.record(False)
    assert await degrade.at_least(degrade.L2_AI_RULE_ENGINE, redis)

    time.sleep(0.06)
    # 全程没有人调过 allow()，只是读了一次生效档位
    assert not await degrade.at_least(degrade.L2_AI_RULE_ENGINE, redis), \
        "冷却期已过但档位还压着，熔断器把自己锁死了"
    assert b.allow(), "锁死状态下探针发不出去"


@pytest.mark.asyncio
@pytestmark_redis
async def test_effective_level_is_a_separate_gauge(redis):
    """生效档位单独一条曲线，和人手档位分开。

    **看到之后要做的事不同**：只有 `degrade_level` 高 = 有人拧了闸，该问"还没恢复吗"；
    `effective` 高于它 = **没人拧过，是熔断器在压着**，该去看哪个依赖挂了。
    合成一条的话，复盘时分不清这次降级是人的决定还是机器的推断，
    而这两件事的下一步动作完全相反（一个去撤销，一个去修依赖）。
    """
    b = breaker.register(_mk(min_samples=2, failure_ratio=0.5,
                             open_for=10.0, implies_level=4))
    b.record(False)
    b.record(False)
    await degrade.effective_level(redis)

    assert metrics.degrade_level_effective._value.get() == 4
    assert metrics.degrade_level._value.get() == degrade.L0_NORMAL


# ----------------------------------------------------------------------
# 运维面
# ----------------------------------------------------------------------

def test_snapshot_tells_the_operator_when_it_will_retry():
    """快照要给出 `cooldown_left`。

    没有这个数，看到一个 open 的熔断器只能干等着猜"它还会不会自己好"。
    事故里第二分钟要能回答的是"再过多久会自动探测一次"。
    """
    b = breaker.register(_mk(min_samples=2, failure_ratio=0.5, open_for=30.0))
    b.record(False)
    b.record(False)
    snap = breaker.snapshot()["test-dep"]
    assert snap["state"] == breaker.OPEN
    assert 0 < snap["cooldown_left"] <= 30.0
    assert snap["failures"] == 2


def test_breaker_endpoint_is_read_only_guarded_and_hidden():
    """熔断接口必须鉴权、不进 OpenAPI，而且**只读**。

    刻意不给"手动跳闸"的入口：想关 LLM 有 H-1 的开关，想整体降级有降级矩阵，
    再加一个手动跳闸就是**第三个能达到同样效果的入口**——
    同一件事有三个开关，事故里就没人知道该撤销哪一个。
    """
    import inspect
    from app.routers import admin

    src = inspect.getsource(admin.list_breakers)
    assert "_guard(x_internal_secret)" in src
    assert "include_in_schema=False" in src

    module_src = inspect.getsource(admin)
    assert '@router.post("/breakers"' not in module_src, "给熔断器加了写入口"
    assert '@router.delete("/breakers"' not in module_src


def test_llm_breaker_cooldown_outlasts_a_single_call():
    """冷却期必须比一次 LLM 调用的上界更长。

    冷却比一次调用还短的话，等于刚跳闸就又去打那个正在过载的依赖——
    **保护机制变成了新的压力源**。这条钉的是关系不是具体数值
    （同 ai_queue 租约必须大于舱壁上界那条）。
    """
    from app.core.config import settings
    assert llm_breaker.open_for > settings.AI_LLM_TIMEOUT_SPEECH
    assert llm_breaker.min_samples >= 2, "没有样本门槛，一次抖动就摘掉 LLM"
    assert 0 < llm_breaker.failure_ratio <= 1
    assert llm_breaker.implies_level == 2, "LLM 不可用的实际效果就是 AI 全走规则引擎"
