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
"""
import asyncio
import json
import logging
import time
from typing import Optional, Dict, Any, List

from openai import AsyncOpenAI

from app.core import metrics
from app.core.config import settings

logger = logging.getLogger("aivalon.llm")


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
            logger.warning("LLM 调用失败（%.1fs）: %s", elapsed, e)
            return {"error": str(e)}

        elapsed = time.monotonic() - started
        metrics.llm_latency.observe(elapsed)

        if not json_mode:
            metrics.llm_calls_total.labels(result="success").inc()
            return {"content": content}

        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            # 通了但返回的不是合法 JSON：这是"依赖坏了"而不是"依赖慢了"，
            # 分开记才能在事故里判断该改 prompt 还是该扩容
            metrics.llm_calls_total.labels(result="invalid").inc()
            logger.warning("LLM 返回的不是合法 JSON，回落规则引擎")
            return {"error": "Invalid JSON response", "raw_content": content}

        metrics.llm_calls_total.labels(result="success").inc()
        return parsed
