# 这个文件是降级开关的运行时读写：开关值存在 Redis 上，各进程带一层短 TTL 的本地缓存。
#
# 为什么开关不能是环境变量
# ----------------------
# 原来 AI 走不走 LLM 由 `settings.AI_USE_LLM` 决定，而它是**启动时**从 .env 读一次的。
# 想在事故中把 AI 切到规则引擎，就得改配置 + **重启所有 Celery worker**——
# 而 AI 决策跑在 worker 里，重启会把正在处理的 AI 回合一起打断。
# **一个需要重启才能生效的降级开关，在事故里等于没有**：降级的全部价值就是
# "不重启就能生效"，需要重启的话不如直接扩容。
# 开关放 Redis 上，各进程（API / worker / 网关）读同一个值，一次写入全局生效。
#
# 两个 TTL，方向相反
# ----------------
#   - **开关本身不设 TTL**。降级是人做的决定，不能自己到期弹回来——半夜降级完去睡觉，
#     TTL 一到 AI 又开始打 LLM，那就是同一个故障再来一遍。想恢复就显式删掉这个 key。
#   - **本地缓存必须设 TTL**。开关会被每个 AI 回合读一次，每次都往 Redis 打一个 GET
#     是把 Redis 挂在了热路径上。TTL 就是"切换后最久多久生效"（同 cache.py 的口径：
#     TTL 是一致性上限），1 秒足够——人手动切开关，等 1 秒和等 0 秒没区别。
#
# 读不到的时候往哪边倒
# ------------------
# Redis 读失败时**优先用本进程上一次读到的值，哪怕它已经过期了**，只有从来没读到过
# 才退回配置默认值。这个顺序很要紧：开关被切到"已降级"往往正是因为线上在着火，
# 而 Redis 抖动本身就是着火的一部分——这时候退回配置默认值（LLM 开着）
# 等于**开关在最需要它的那一刻自己失效了**。
# **开关的失败方向要偏向"保持当前已生效的决定"，不是偏向出厂设置。**
import logging
import time
from typing import Any, Dict, Optional, Tuple

from app.core import metrics
from app.core.config import settings

logger = logging.getLogger("aivalon.switches")

KEY_PREFIX = "switch:"

# 本地缓存 TTL（秒）= 切换后最久多久全局生效
LOCAL_TTL = 1.0

# ---- 开关登记表 ----
# name -> 取配置里的哪个字段当默认值。
# **默认值仍然取自 `settings`，所以 .env / 环境变量照旧有效**——
# 压测脚本和集成测试都靠 `AI_USE_LLM=false` 起环境（见 bench/README.md），
# 开关只是多了一条"运行时覆盖"的通路，不是把原来那条换掉。
AI_USE_LLM = "ai_use_llm"

_DEFAULT_FROM = {
    AI_USE_LLM: "AI_USE_LLM",
}

# name -> (上次读到的值, 过期时刻)
_local: Dict[str, Tuple[Any, float]] = {}


def _key(name: str) -> str:
    return f"{KEY_PREFIX}{name}"


def default_of(name: str) -> bool:
    return bool(getattr(settings, _DEFAULT_FROM[name]))


def _observe(name: str, value: bool) -> bool:
    """把开关当前状态同步到指标上。

    降级中不上报的话，事故复盘时分不清"AI 没说话"是因为降级了还是因为坏了——
    **降级动作本身必须是可观测的**，否则它就是一次没人知道的静默变更。
    """
    metrics.degrade_switch.labels(name=name).set(0 if value else 1)
    return value


async def get_bool(name: str, redis=None) -> bool:
    """读开关。永不抛异常：开关读不到不该让业务路径跟着挂。"""
    cached = _local.get(name)
    now = time.monotonic()
    if cached is not None and now < cached[1]:
        return _observe(name, cached[0])

    client = redis or _client()
    raw = None
    if client is not None:
        try:
            raw = await client.get(_key(name))
        except Exception as e:
            logger.warning("读降级开关失败 name=%s: %s", name, e)
            if cached is not None:
                # 过期的旧值也比出厂默认值可信：见文件头"读不到的时候往哪边倒"
                return _observe(name, cached[0])
            return _observe(name, default_of(name))

    # key 不存在 = 没人动过开关 = 用配置默认值（环境变量那条路照旧有效）
    value = default_of(name) if raw is None else raw == "1"
    _local[name] = (value, now + LOCAL_TTL)
    return _observe(name, value)


async def set_bool(name: str, value: bool, redis=None) -> None:
    """切开关。刻意不设 TTL：降级是人做的决定，不能自己到期弹回来。"""
    if name not in _DEFAULT_FROM:
        raise KeyError(f"未登记的开关: {name}")
    client = redis or _client()
    await client.set(_key(name), "1" if value else "0")
    # 本进程立刻生效，别让切开关的人自己还要等 LOCAL_TTL 才看到变化
    _local[name] = (value, time.monotonic() + LOCAL_TTL)
    logger.warning("降级开关已切换 name=%s value=%s", name, value)
    _observe(name, value)


async def reset(name: str, redis=None) -> None:
    """删掉运行时覆盖，回到配置默认值。"""
    if name not in _DEFAULT_FROM:
        raise KeyError(f"未登记的开关: {name}")
    client = redis or _client()
    await client.delete(_key(name))
    _local.pop(name, None)
    logger.warning("降级开关已复位 name=%s -> 配置默认值 %s", name, default_of(name))


async def snapshot(redis=None) -> Dict[str, Dict[str, Any]]:
    """所有开关的当前值 + 配置默认值，供开关中心展示。"""
    out = {}
    for name in _DEFAULT_FROM:
        out[name] = {
            "value": await get_bool(name, redis),
            "default": default_of(name),
        }
    return out


def clear_local_cache() -> None:
    """丢掉本进程的开关缓存（测试用，也可用于强制立刻重读）。"""
    _local.clear()


def _client():
    # 延迟导入：core.redis 在导入时就会建连接池，让这个模块能被单独 import
    from app.core.redis import redis_client
    return redis_client


async def ai_use_llm(redis=None) -> bool:
    return await get_bool(AI_USE_LLM, redis)
