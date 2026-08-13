"""依赖熔断·下半：邮件 与 统计 测试。

**同一个模式在不同依赖上的收益和适用性都不同**，这份测试大半在钉这条：
  - **邮件要熔断，但省的不是时间而是槽位**：它跑在 BackgroundTask 里没人等，
    可邮件挂掉时每个发码请求都占住一个协程和一条 SMTP 连接直到超时，
    而服务商给的连接配额只有个位数——占满之后连正常的信也发不出去。
  - **统计刻意不熔断**：熔断的前提是有个可接受的兜底，而胜场数据没有兜底，
    短路它等于直接丢数据。它要的是重试退避——**熔断适合"失败了就放弃这一次"，
    重试适合"这一次不能丢"**。

验收口径四条：
  1. 邮件有超时上界、永不抛、失败可观测（原来三样全没有）
  2. 熔断中**在承诺之前就拒**，不能让用户既收不到信又被 60s 间隔锁在外面
  3. 邮件熔断的 `implies_level` 必须是 0——邮件不在对局链路上
  4. 统计不加熔断，但退避必须是指数 + 抖动
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import time

import pytest

from app.core import breaker, email as email_mod, metrics
from app.core.config import settings


@pytest.fixture(autouse=True)
def clean_breakers():
    breaker.reset_all()
    yield
    breaker.reset_all()


# ----------------------------------------------------------------------
# 邮件：原来三个洞（没超时 / 会抛 / 不可观测）
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_send_has_an_upper_bound(monkeypatch):
    """发信有超时上界。

    原来这里**压根没有超时**：`fm.send_message` 卡住就是永久卡住，而它跑在
    API 进程的事件循环里——挂住的任务只增不减。同 H-2 那条：
    **上界要卡在自己的代码里，不能指望依赖库的参数。**
    """
    async def hang(*a, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(email_mod.fm, "send_message", hang)
    monkeypatch.setattr(settings, "MAIL_SEND_TIMEOUT", 0.05)

    started = time.monotonic()
    ok = await email_mod.send_verification_email("a@example.com", "123456")
    elapsed = time.monotonic() - started

    assert ok is False
    assert elapsed < 1.0, f"发信卡了 {elapsed:.2f}s，超时上界没生效"


@pytest.mark.asyncio
async def test_send_never_raises(monkeypatch):
    """发信永不抛异常。

    调用方是 `background_tasks.add_task`——**异常抛到那里没有任何人能处理**，
    只会变成一行没人看的服务器日志。**没人能据此行动的异常，抛出去就只是噪音**，
    所以失败在这里就地收敛成 返回值 + 日志 + 指标 + 熔断记账。
    """
    async def boom(*a, **kw):
        raise RuntimeError("smtp is down")

    monkeypatch.setattr(email_mod.fm, "send_message", boom)
    assert await email_mod.send_verification_email("a@example.com", "1") is False


@pytest.mark.asyncio
async def test_failures_are_observable(monkeypatch):
    """发信失败要上指标。

    这条补的是**一个"用户知道坏了、我们不知道"的洞**：原来失败只会在
    BackgroundTask 里抛个没人看的异常，用户拿到"验证码已发送"然后永远收不到信，
    而我们这边一个计数器都不涨——**最坏的一种故障就是只有用户看得见的故障**。
    """
    async def boom(*a, **kw):
        raise RuntimeError("smtp is down")

    monkeypatch.setattr(email_mod.fm, "send_message", boom)
    label = metrics.email_sends.labels(result="error")
    before = label._value.get()
    await email_mod.send_verification_email("a@example.com", "1")
    assert label._value.get() == before + 1


@pytest.mark.asyncio
async def test_timeout_is_counted_apart_from_error(monkeypatch):
    """超时和其他错误分开计数。

    要做的事不同：超时是**邮件服务慢了或挂了**（该看熔断和服务商状态），
    error 常常是**单个收件地址的问题**（对方邮箱满了、域名不存在）。
    混一档的话，几个坏地址和一次真故障在曲线上完全一样。
    """
    async def hang(*a, **kw):
        await asyncio.sleep(30)

    monkeypatch.setattr(email_mod.fm, "send_message", hang)
    monkeypatch.setattr(settings, "MAIL_SEND_TIMEOUT", 0.05)

    t_label = metrics.email_sends.labels(result="timeout")
    e_label = metrics.email_sends.labels(result="error")
    t_before, e_before = t_label._value.get(), e_label._value.get()

    await email_mod.send_verification_email("a@example.com", "1")

    assert t_label._value.get() == t_before + 1
    assert e_label._value.get() == e_before


@pytest.mark.asyncio
async def test_cancellation_is_not_a_dependency_failure(monkeypatch):
    """取消不记账，也不能吞（同 DEVLOG 029）。"""
    async def cancelled(*a, **kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(email_mod.fm, "send_message", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await email_mod.send_verification_email("a@example.com", "1")
    assert email_mod.email_breaker.snapshot()["samples"] == 0


# ----------------------------------------------------------------------
# 邮件熔断：省的是槽位，不是时间
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_breaker_stops_touching_smtp_at_all(monkeypatch):
    """跳闸后**一次都不去碰 SMTP**。

    邮件跑在 BackgroundTask 里没人等，所以这里的收益**不是** LLM 那边的"不白等"，
    而是**并发槽位和 SMTP 连接**：邮件服务挂掉时每个发码请求都占住一个协程和
    一条连接直到超时，而服务商给的配额只有个位数——**占满之后连正常的信也发不出去**。
    所以这条断言的是"调用次数"，不是"花了多少时间"。
    """
    calls = []

    async def boom(*a, **kw):
        calls.append(1)
        raise RuntimeError("smtp is down")

    monkeypatch.setattr(email_mod.fm, "send_message", boom)

    for _ in range(email_mod.email_breaker.min_samples):
        await email_mod.send_verification_email("a@example.com", "1")
    assert email_mod.email_breaker.state == breaker.OPEN
    touched = len(calls)

    for _ in range(5):
        assert await email_mod.send_verification_email("a@example.com", "1") is False
    assert len(calls) == touched, "熔断后还在建 SMTP 连接"


@pytest.mark.asyncio
async def test_email_breaker_does_not_move_the_degrade_matrix(monkeypatch):
    """邮件熔断的 `implies_level` 必须是 0。

    **不是每个熔断器都该挂降级级别。** 矩阵每一级砍的都是对局链路的成本，
    而邮件不在那条链路上——为了发不出验证码就把全站往下拧一档是荒谬的。
    挂之前要问的是："这个依赖不可用，和'对局链路要省成本'是同一件事吗？"
    LLM 是（AI 全走规则引擎就是 L2），邮件不是。
    """
    async def boom(*a, **kw):
        raise RuntimeError("smtp is down")

    monkeypatch.setattr(email_mod.fm, "send_message", boom)
    for _ in range(email_mod.email_breaker.min_samples):
        await email_mod.send_verification_email("a@example.com", "1")

    assert email_mod.email_breaker.state == breaker.OPEN
    assert email_mod.email_breaker.implies_level == 0
    assert breaker.implied_level() == 0, "邮件挂了把降级矩阵拧动了"


def test_email_breaker_threshold_fits_a_low_traffic_endpoint():
    """样本门槛要低于这个接口的实际流量。

    **门槛定得比流量还高的熔断器等于没有**：发码是低频接口，照 LLM 那样
    要 8 个样本可能要等很久，期间每个请求都在付满超时。
    反过来失败比例要求更高——发信失败有时只是单个收件地址的问题，
    比例定高才不会被几个坏地址带跳闸。
    """
    assert email_mod.email_breaker.min_samples <= 3
    assert email_mod.email_breaker.failure_ratio >= 0.8
    assert email_mod.email_breaker.open_for > settings.MAIL_SEND_TIMEOUT


# ----------------------------------------------------------------------
# 承诺：闸必须在写 Redis 之前
# ----------------------------------------------------------------------

def test_availability_gate_runs_before_the_promise():
    """熔断检查必须排在写验证码和 60s 发送间隔**之前**。

    发码接口先写这两个 key，再把发信丢进后台。邮件挂着的时候，用户
    **既收不到信、又被那个 60s 锁在外面**，而他收到的响应是"验证码已发送"——
    **在依赖已知不可用时承诺出去，换来的是用户以为自己收得到、然后重试还被拒**。
    顺序错了不报错，所以按源码顺序钉死。
    """
    import inspect
    from app.routers import auth

    src = inspect.getsource(auth.send_code)
    gate = src.index("email_is_available()")
    code_key = src.index("verification_code:")
    limit_key = src.index("email_limit:{email}\", \"1\"")

    assert gate < code_key, "熔断检查排在了写验证码之后"
    assert gate < limit_key, "熔断检查排在了写 60s 发送间隔之后"


def test_availability_gate_says_how_long_to_wait():
    """拒的时候要带 `Retry-After`。

    不说等多久，客户端就立刻重试，而重试本身会变成新的峰值（同 H-3a）。
    """
    import inspect
    from app.routers import auth

    src = inspect.getsource(auth.send_code)
    assert "Retry-After" in src
    assert "503" in src or "SERVICE_UNAVAILABLE" in src


def test_is_available_is_not_the_real_gate():
    """`is_available` 不是那道闸，真正的闸在依赖边界上。

    只在路由里判的话，任何别的地方调 `send_verification_email` 都绕过了熔断——
    **熔断挂在依赖边界上，不挂在调用方里**（同 LLM 那条）。
    这里验的是两处都有：路由里挡"承诺"，发送函数里挡"调用"。
    """
    import inspect
    src = inspect.getsource(email_mod.send_verification_email)
    assert "email_breaker.allow()" in src


@pytest.mark.asyncio
async def test_is_available_recovers_with_the_breaker(monkeypatch):
    """熔断进半开后，`is_available` 必须重新放行。

    否则发码接口会一直拒，**而它拒的正是那个能触发探测的请求**——
    同 LLM 那条自锁死：自动恢复机制不能依赖一条被它自己关掉的路径。
    """
    async def boom(*a, **kw):
        raise RuntimeError("smtp is down")

    monkeypatch.setattr(email_mod.fm, "send_message", boom)
    monkeypatch.setattr(email_mod.email_breaker, "open_for", 0.05)

    for _ in range(email_mod.email_breaker.min_samples):
        await email_mod.send_verification_email("a@example.com", "1")
    assert not email_mod.is_available()

    time.sleep(0.06)
    assert email_mod.is_available(), "冷却期过了还在拒，探测请求永远进不来"


# ----------------------------------------------------------------------
# 统计：刻意不熔断，改退避
# ----------------------------------------------------------------------

def test_stats_task_has_no_breaker():
    """统计任务**刻意不加熔断器**。

    熔断的前提是有个可接受的兜底：LLM 有规则引擎、邮件有"稍后再试"，
    而统计**没有兜底**——胜场数据不算就是永久丢账。
    **没有兜底的依赖，短路它等于直接丢数据。**
    而且这里没有人在等（Celery 任务慢十分钟不影响任何人的请求），
    所以"不白等"这个收益压根不存在。
    **判据不是依赖有多重要，是丢掉这次调用可不可接受。**
    """
    import inspect
    from app.tasks import stats

    src = inspect.getsource(stats)
    assert "breaker" not in src.replace("熔断", ""), "给统计任务加了熔断器"
    assert "self.retry" in src, "统计任务必须靠重试而不是熔断"


def test_stats_retry_backs_off_exponentially():
    """重试间隔指数增长。

    原来是固定 `countdown=5`：MySQL 挂 30 秒的话，3 次重试全落在故障窗口里、
    全部失败然后进死信——**重试次数被固定间隔浪费在同一个故障上了**。
    退避让这几次重试铺开覆盖更长的窗口。
    """
    from app.tasks.stats import retry_countdown

    waits = [retry_countdown(i) for i in range(3)]
    assert waits[0] < waits[1] < waits[2], f"重试间隔没有递增: {waits}"
    # 三次重试要能覆盖到远超一次固定 5s×3 的窗口
    assert sum(waits) > 60, f"三次重试只覆盖了 {sum(waits):.0f}s，跨不过一次短故障"


def test_stats_retry_is_jittered():
    """退避必须带抖动。

    **抖动不是可选项**：一批对局同时结束（故障恢复后常常如此）会被投递成一批任务，
    它们的重试时刻会完全对齐、成排打在刚恢复的数据库上——
    **重试自己变成了下一次故障的原因**。同 F-5 缓存 TTL 抖动：治的是"整齐"本身。
    """
    from app.tasks.stats import retry_countdown

    samples = {retry_countdown(1) for _ in range(20)}
    assert len(samples) > 1, "退避没有抖动，一批任务的重试会完全对齐"


def test_stats_retry_is_capped():
    """退避有上界。

    不设上界的话，指数涨几轮就是几小时后才重试——**那个时候数据早就没人关心了**，
    而任务还占着队列和幂等 key。
    """
    from app.tasks.stats import retry_countdown

    assert retry_countdown(20) <= settings.STATS_RETRY_MAX * 1.2
