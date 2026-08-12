"""LLM 舱壁：整次调用限时 + 超时回落规则引擎 测试。

验收口径三条：
  1. **上界真的是上界**：慢掉的 LLM 必须在 timeout 附近就放手，不能是 timeout × 重试次数
  2. **超时不上抛**：返回 `{"error": ...}` 让调用方回落，抛出去会走 Celery 重试
     ——而重试还是打同一个慢掉的 LLM，一次慢被放大成五次慢
  3. **超时也要记进耗时指标**：只记成功的话，LLM 全在超时的时候曲线反而变好看

判据是"耗时"和"指标涨在哪一档"，不是"函数有没有报错"。
这些测试全部不连真的 LLM：把 `_request` 替掉，验的是舱壁而不是 DeepSeek。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import time

import pytest

from app.core import metrics
from app.services.llm_service import LLMService


def _calls(result: str) -> float:
    return metrics.llm_calls_total.labels(result=result)._value.get()


def _latency_count() -> float:
    """直方图的样本数只能从 collect() 里读，Histogram 没有 `_count` 属性。"""
    for sample in metrics.llm_latency.collect()[0].samples:
        if sample.name.endswith("_count"):
            return sample.value
    raise AssertionError("llm_latency 没有 _count 样本")


@pytest.fixture
def slow_llm(monkeypatch):
    """把真正发请求那一跳换成"永远不返回"，用来验超时。"""
    async def never_returns(*a, **kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(LLMService, "_request", classmethod(lambda cls, *a, **kw: never_returns()))


# ----------------------------------------------------------------------
# 上界
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_is_the_whole_call_not_one_attempt(slow_llm):
    """整次调用的上界就是 timeout，不是 timeout × 尝试次数。

    原来这里把 timeout 直接交给 OpenAI SDK，而 SDK 的 timeout 是**单次尝试**的、
    默认还会重试 2 次、重试之间有退避——`timeout=45` 的真实上界是 140 秒以上。
    **"我传了 timeout"和"这次调用有上界"是两回事。**
    """
    started = time.monotonic()
    result = await LLMService.generate_response("sys", "user", timeout=0.2)
    elapsed = time.monotonic() - started

    assert "error" in result
    assert elapsed < 1.0, f"用了 {elapsed:.1f}s，上界没兜住"


@pytest.mark.asyncio
async def test_timeout_returns_error_instead_of_raising(slow_llm):
    """超时返回 error 字典，不抛。

    抛出去会被 Celery 当任务失败、按指数退避重试 5 次，而重试打的还是那个慢掉的
    LLM——**把一次慢放大成五次慢**，还额外占满 ai_queue 的并发。
    """
    result = await LLMService.generate_response("sys", "user", timeout=0.1)
    assert result["error"].startswith("timeout")


@pytest.mark.asyncio
async def test_client_does_not_retry(monkeypatch):
    """SDK 层的重试必须关掉。

    重试适合"答案很重要且错误是暂态的"。这里**已经有一个立刻可用的次优答案**
    （规则引擎），等 3 次 LLM 的时间里规则引擎能出 3 次结果，玩家也不用干等。
    **有廉价兜底可用时，重试的价值是负的**——它花掉的是玩家的等待时间。
    """
    monkeypatch.setattr("app.core.config.settings.DEEPSEEK_API_KEY", "test-key", raising=False)
    LLMService._client = None
    try:
        client = LLMService.get_client()
        assert client.max_retries == 0, "SDK 还在重试，上界会翻倍"
    finally:
        LLMService._client = None


# ----------------------------------------------------------------------
# 失败分类：四档要能分开，事故里才判断得出该扩容还是该改 prompt
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_counts_as_timeout_not_error(slow_llm):
    """超时记 timeout 档，不能混进 error 档。

    timeout = 依赖**慢了**（该降级或扩容），error = 别的失败（鉴权、配额、网络）。
    混成一档的话，事故里看不出该做哪件事。
    """
    before_t, before_e = _calls("timeout"), _calls("error")
    await LLMService.generate_response("sys", "user", timeout=0.1)
    assert _calls("timeout") == before_t + 1
    assert _calls("error") == before_e


@pytest.mark.asyncio
async def test_exception_counts_as_error(monkeypatch):
    """网络/鉴权之类的失败记 error 档，同样不抛。"""
    async def boom(*a, **kw):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(LLMService, "_request", classmethod(lambda cls, *a, **kw: boom()))

    before = _calls("error")
    result = await LLMService.generate_response("sys", "user", timeout=5)
    assert "error" in result
    assert _calls("error") == before + 1


@pytest.mark.asyncio
async def test_invalid_json_counts_as_invalid(monkeypatch):
    """通了但返回的不是合法 JSON 记 invalid 档。

    这是"依赖坏了"而不是"依赖慢了"——**该去改 prompt，不是该扩容**。
    和 timeout 分开记才看得出来。原始内容要带回去，不然没法排查 prompt 出了什么问题。
    """
    async def garbage(*a, **kw):
        return "这不是 JSON{{"

    monkeypatch.setattr(LLMService, "_request", classmethod(lambda cls, *a, **kw: garbage()))

    before = _calls("invalid")
    result = await LLMService.generate_response("sys", "user", timeout=5)
    assert "error" in result
    assert result["raw_content"] == "这不是 JSON{{"
    assert _calls("invalid") == before + 1


@pytest.mark.asyncio
async def test_success_parses_json(monkeypatch):
    """正常路径：解析出字典，记 success 档。"""
    async def ok(*a, **kw):
        return '{"action_type": "vote", "payload": {"option": "approve"}}'

    monkeypatch.setattr(LLMService, "_request", classmethod(lambda cls, *a, **kw: ok()))

    before = _calls("success")
    result = await LLMService.generate_response("sys", "user", timeout=5)
    assert result["action_type"] == "vote"
    assert _calls("success") == before + 1


# ----------------------------------------------------------------------
# 指标口径
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_latency_is_recorded_even_on_timeout(slow_llm):
    """超时的耗时也要记进直方图。

    只记成功的调用的话，**LLM 全在超时的时候这条曲线反而会变好看**（慢的都没被统计），
    于是最需要告警的那一刻指标一片绿。这类"越坏越好看"的指标比没有指标更危险。
    """
    count_before = _latency_count()
    await LLMService.generate_response("sys", "user", timeout=0.15)

    assert _latency_count() == count_before + 1, "超时没被记进耗时指标"


@pytest.mark.asyncio
async def test_cancellation_propagates(monkeypatch):
    """取消必须能穿过去，不能被当成失败吞掉。

    `CancelledError` 不是 `Exception` 子类，所以 `except Exception` 抓不到它——
    但这里显式写出这个分支，是为了把"取消不是失败"这件事标在代码里（同 DEVLOG 029）。
    吞掉的话上层的超时/关停就失效了。
    """
    async def slow(*a, **kw):
        await asyncio.sleep(10)

    monkeypatch.setattr(LLMService, "_request", classmethod(lambda cls, *a, **kw: slow()))

    task = asyncio.create_task(LLMService.generate_response("sys", "user", timeout=30))
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_timeout_branch_must_precede_generic_except():
    """钉住那个排序依据：`asyncio.TimeoutError` **会**被 `except Exception` 抓走。

    3.11 起 `asyncio.TimeoutError` 就是内置 `TimeoutError`，而它是 `OSError` 的子类。
    所以 llm_service 里超时那个 except 必须排在 `except Exception` 前面，
    顺序写反就分不出"慢了"和"坏了"——**而且不报错，只是指标全记到 error 档**。
    """
    assert asyncio.TimeoutError is TimeoutError
    assert issubclass(TimeoutError, Exception)


# ----------------------------------------------------------------------
# 舱壁接在 AI 决策上（不然上面全是空转）
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ai_falls_back_to_rule_engine_when_llm_times_out(monkeypatch):
    """LLM 超时后 AI 必须给出规则引擎的动作，不能返回 None。

    返回 None 的话这个 AI 就不动了，而对局在等他——**一个玩家卡住等于整局卡住**。
    舱壁的意义不是"调用失败得体面"，是"对局照常推进"。
    """
    from app.services.ai_service import AIService
    from app.models.game_enums import GamePhase

    async def timeout_call(*a, **kw):
        return {"error": "timeout after 20s"}

    monkeypatch.setattr(LLMService, "generate_response", classmethod(
        lambda cls, *a, **kw: timeout_call()))

    # prompt 构建器不是这里要验的东西，替掉以免为了喂它拼一整个合法 GameState
    monkeypatch.setattr(AIService, "_build_system_prompt",
                        staticmethod(lambda *a, **kw: "sys"))
    monkeypatch.setattr(AIService, "_build_user_prompt",
                        staticmethod(lambda *a, **kw: "user"))

    called = {"fallback": 0}

    def fake_fallback(game, player):
        called["fallback"] += 1
        return {"action_type": "vote", "payload": {"option": "approve"}}

    monkeypatch.setattr(AIService, "_fallback_vote", staticmethod(fake_fallback))

    class P:
        is_ai = True
        username = "ai-1"
        user_id = 1
        seat_id = 1
        has_voted = False
        ai_memory = ""

    class G:
        phase = GamePhase.VOTE

    action = await AIService._handle_vote(G(), P())
    assert action is not None, "LLM 超时后 AI 不动了，对局会卡住"
    assert called["fallback"] == 1
