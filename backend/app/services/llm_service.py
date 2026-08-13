"""
这个文件是 LLM 服务的封装，负责调用 DeepSeek API 生成内容，并把这次调用**限时**。

原来"有超时"是个误会
------------------
原来这里把 `timeout` 直接传给 OpenAI SDK，看着像是"最多等 45 秒"。实际不是：
  - SDK 的 `timeout` 是**单次尝试**的超时，不是整次调用的；
  - `openai` 默认 `max_retries=2`，也就是最多打 3 次；
  - 每次重试之间还有退避等待。
所以 `timeout=45` 的真实上界是 3 × 45 + 退避 ≈ 140 秒以上。
**"我传了 timeout" 和 "这次调用有上界" 是两回事**——重试次数不算进去的话，
超时参数只约束了其中一跳。这类误会的代价是：调用方按 45 秒做的容量估算全是错的。

两处改动
--------
  1. **`asyncio.wait_for` 包住整次调用**，它才是权威上界。放在最外层的好处是
     它不依赖 SDK 的任何语义——SDK 换版本、重试策略变了，这个上界都还成立。
     **超时要卡在自己的代码里，不能外包给依赖库的参数。**
  2. **`max_retries=0`**：在一局游戏里重试慢掉的 LLM 是反的。重试适合"答案很重要
     且错误是暂态的"，而这里**已经有一个立刻可用的次优答案**（规则引擎）。
     等 3 次 LLM 的时间里，规则引擎能出 3 次结果，玩家也不用干等。
     **有廉价兜底可用时，重试的价值就变成负的**：它花掉的是玩家的等待时间。

超时了往哪边倒
------------
返回 `{"error": ...}`，调用方（`ai_service`）据此回落规则引擎——AI 说句套话，
对局照常推进。**超时不能上抛成任务失败**：抛出去会走 Celery 重试，
而重试还是打同一个慢掉的 LLM，等于把一次慢放大成五次慢。

上面这些都是"单次"的事，熔断管的是"总共"
--------------------------------
舱壁保证每次最多等 45 秒，但 LLM 真挂了的时候，**每个** AI 回合都要付满这 45 秒
才回落——对局能推进，但每步慢 45 秒，玩家看着和卡死没区别。所以再包一层熔断器
（`core/breaker.py`）：窗口内失败过半就直接短路，后面的调用一次都不等。
**熔断挂在这里而不是挂在 `ai_service` 里**：挂在调用方的话，下一个调 LLM 的人
不会知道要先问一句，这道防线就只护住了一条路径。
"""
import asyncio
import json
import logging
import time
from typing import Optional, Dict, Any, List

from openai import AsyncOpenAI

from app.core import breaker, metrics
from app.core.config import settings

logger = logging.getLogger("aivalon.llm")

# LLM 熔断器。`implies_level=L2`：LLM 整个不可用时，实际效果就是 AI 全部决策
# 走规则引擎，也就是降级矩阵的 2 档——**把它上报成 L2，事故里那条
# `degrade_level_effective` 曲线才对得上现实**，而不是"档位显示 0 档但 AI 全在说套话"。
# 注意它只是"推断该到几档"，不会去写人手拧的那个 key（见 degrade.effective_level）。
llm_breaker = breaker.register(breaker.Breaker(
    "llm",
    window=settings.BREAKER_LLM_WINDOW,
    min_samples=settings.BREAKER_LLM_MIN_SAMPLES,
    failure_ratio=settings.BREAKER_LLM_FAILURE_RATIO,
    open_for=settings.BREAKER_LLM_OPEN_FOR,
    implies_level=2,
))


class LLMService:
    _client: Optional[AsyncOpenAI] = None

    @classmethod
    def get_client(cls) -> AsyncOpenAI:
        if cls._client is None:
            if not settings.DEEPSEEK_API_KEY:
                raise ValueError("DEEPSEEK_API_KEY is not set in configuration")

            cls._client = AsyncOpenAI(
                api_key=settings.DEEPSEEK_API_KEY,
                base_url=settings.DEEPSEEK_BASE_URL,
                # 不重试：见文件头。有规则引擎兜底时，重试花掉的是玩家的等待时间
                max_retries=0,
            )
        return cls._client

    @classmethod
    async def _request(cls, system_prompt: str, user_prompt: str,
                       json_mode: bool, temperature: float, timeout: float) -> str:
        """真正发出去的那一跳。超时由外层 wait_for 兜，这里的 timeout 只是让 SDK 也早点放手。"""
        client = cls.get_client()
        response = await client.chat.completions.create(
            model=settings.DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            response_format={"type": "json_object"} if json_mode else None,
            temperature=temperature,
            max_tokens=1024,
            timeout=timeout,
        )
        return response.choices[0].message.content

    @classmethod
    async def generate_response(
        cls,
        system_prompt: str,
        user_prompt: str,
        json_mode: bool = True,
        temperature: float = 0.7,
        timeout: float = 30,
    ) -> Dict[str, Any]:
        """
        调用 LLM 生成响应。**永不抛异常**：失败一律返回 `{"error": ...}`，
        由调用方决定回落什么——把异常抛给 Celery 会触发重试，而重试只是把一次慢放大成五次慢。

        :param timeout: **整次调用**的上界（秒），含建连、传输、解析
        :return: 解析后的字典；失败时是 `{"error": ...}`
        """
        # 熔断中：**一秒都不等**，直接给调用方一个错误让它回落规则引擎。
        # 返回的形状和超时那条完全一样，所以调用方一行都不用改——
        # 对它来说"这次拿不到 LLM"是同一件事，区别只在我们没有为此付时间。
        if not llm_breaker.allow():
            metrics.llm_calls_total.labels(result="breaker_open").inc()
            # **刻意不 observe 耗时**：短路的这次几乎不花时间，记进耗时直方图
            # 会把 P99 一路拉绿——**依赖挂着的时候延迟曲线反而变好看**，
            # 和 H-2 那条"失败的耗时也要记"是同一个陷阱的两面：
            # 真实发生过的等待必须记，压根没发生的调用不能记。
            return {"error": "llm breaker open"}

        started = time.monotonic()

        try:
            content = await asyncio.wait_for(
                cls._request(system_prompt, user_prompt, json_mode, temperature, timeout),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # 注意这个分支必须排在 `except Exception` 前面：3.11 起
            # `asyncio.TimeoutError` 就是内置 `TimeoutError`，而它是 `OSError` 的子类，
            # 也就是说它**会被 `except Exception` 抓走**，顺序写反就分不出超时和别的错
            elapsed = time.monotonic() - started
            metrics.llm_calls_total.labels(result="timeout").inc()
            metrics.llm_latency.observe(elapsed)
            # 超时是熔断最该拦的那种失败：**它白花掉了 45 秒**
            llm_breaker.record(False)
            logger.warning("LLM 调用超时 %.1fs（上界 %.1fs），回落规则引擎", elapsed, timeout)
            return {"error": f"timeout after {timeout}s"}
        except asyncio.CancelledError:
            # 取消不是失败，不记指标也不能吞：吞掉会让上层的取消失效
            # （`CancelledError` 不是 `Exception` 子类，所以下面那个 except 抓不到它，
            #  这里显式写出来只为把这件事标在代码里，同 DEVLOG 029）
            raise
        except Exception as e:
            elapsed = time.monotonic() - started
            metrics.llm_calls_total.labels(result="error").inc()
            metrics.llm_latency.observe(elapsed)
            # 鉴权、配额、连不上：马上重试也不会成功，算失败
            llm_breaker.record(False)
            logger.warning("LLM 调用失败（%.1fs）: %s", elapsed, e)
            return {"error": str(e)}

        elapsed = time.monotonic() - started
        metrics.llm_latency.observe(elapsed)

        # 这一跳通了，对熔断器来说就是成功——它判的是"这个依赖还能不能用"，
        # 不是"这次答得对不对"。下面非法 JSON 那条分支刻意也走这个 `True`。
        llm_breaker.record(True)

        if not json_mode:
            metrics.llm_calls_total.labels(result="success").inc()
            return {"content": content}

        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            # 通了但返回的不是合法 JSON：这是"依赖坏了"而不是"依赖慢了"，
            # 分开记才能在事故里判断该改 prompt 还是该扩容。
            # **但对熔断器来说这次算成功**（上面已经记了）：依赖是活的、答得也快，
            # 只是答得不对，多半是我们自己的 prompt 问题。算成失败的话，
            # 一个 prompt bug 会把熔断器跳开，而半开探测同样会拿回非法 JSON——
            # **它就永远合不回来了**，一个我们自己的 bug 变成了依赖不可用。
            metrics.llm_calls_total.labels(result="invalid").inc()
            logger.warning("LLM 返回的不是合法 JSON，回落规则引擎")
            return {"error": "Invalid JSON response", "raw_content": content}

        metrics.llm_calls_total.labels(result="success").inc()
        return parsed
