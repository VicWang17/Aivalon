"""
这个文件是**应用层滑动窗口限流**：按 user_id（未认证回退 IP）计数，挂在具体路由上。

三层限流各管什么
--------------
  - **网关层**（`admission.py`，令牌桶）：管"这台系统一共能吃多少"，按容量算
  - **应用层**（本文件，滑动窗口）：管"这个用户能做多少次这件事"，按业务规则算
  - **资源层**（后续）：管"单个房间/队列别被打爆"

为什么应用层用滑动窗口，而网关层用令牌桶
------------------------------------
不是随手换个算法，两层要的性质不同：
  - 网关层要的是**突发容忍度和稳态速率能分开调**（平时闲、偶尔涌入），令牌桶恰好两个旋钮。
  - 应用层承载的是**业务承诺**："10 局/小时"必须真的是 10 局/小时。
固定窗口（项目原来用的 `fastapi-limiter`，其 Lua 是 `SET px` + `INCR`）做不到后者：
key 从**第一个请求**起算过期，所以在窗口边界能挤进 **2 倍**——
上一小时最后一秒建 10 局，下一小时第一秒再建 10 局，**2 秒内建了 20 局**，
而每一次都"没超过 10 局/小时"。业务规则被算法的实现细节打了个对折。
大白话：固定窗口是"每到整点清零",所以卡着点前后各来一波就能翻倍;
滑动窗口是"往前看一小时",没有"整点"这个可利用的边界。

代价与它的上界
------------
滑动窗口日志把窗口内每次请求都记一条（ZSET 一个 member），所以内存是 O(窗口内请求数)。
**但先 trim 再计数，所以上界就是 `times` 本身**——超了就被拒、不再写入。
这也划定了它的适用范围：`times` 小的时候（本项目 1/秒、10/小时）很划算；
`times` 很大时（网关层那种 200/s）就该用令牌桶——**令牌桶只存 2 个数字，与流量无关**。
选算法的依据是"要什么性质 + 代价能不能接受"，不是哪个更先进。

score 取 Redis 的时间，member 由调用方给
------------------------------------
score 是**跨进程比较的基准**，必须取 `redis.call('TIME')`——节点时钟偏差会直接变成
窗口边界偏差（同 `admission.py` 与 C05）。member 只需要**唯一**，不参与比较，
所以由调用方给一个随机串就行。两个请求落在同一微秒时，member 相同会被 ZSET 当成
同一条覆盖掉，**于是并发越高、漏计越多**——限流器恰好在压力最大时失效。
"""
import logging
import uuid
from typing import Optional, Tuple

from fastapi import HTTPException, Request
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

from app.core import metrics

logger = logging.getLogger("aivalon.ratelimit")

KEY_PREFIX = "slide:"

# 滑动窗口日志：ZSET{member=唯一串, score=Redis 时刻}
# 返回 {剩余配额, 建议重试毫秒}；剩余为 -1 表示已超限
_WINDOW_LUA = """
local key = KEYS[1]
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local member = ARGV[3]

local now = redis.call('TIME')
local nowf = tonumber(now[1]) + tonumber(now[2]) / 1000000
local cutoff = nowf - window

-- 先清掉滑出窗口的，再计数。**顺序很要紧**：先计数的话会把已经过期的算进来，
-- 于是限流比设定的更严；而且不 trim 的话 ZSET 会随请求数无限长大
redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local used = redis.call('ZCARD', key)

if used >= limit then
  -- 超限时**刻意不写入**：写了的话每次被拒都往后推一次窗口，
  -- 一个还在重试的客户端会把自己永久锁死（惩罚性延长不是这一层该做的事）
  local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
  local retry = window
  if oldest[2] then retry = tonumber(oldest[2]) + window - nowf end
  if retry < 0 then retry = 0 end
  return {-1, math.ceil(retry * 1000)}
end

redis.call('ZADD', key, nowf, member)
-- TTL 兜底清理：key 里最后一条滑出窗口后它就该消失。
-- 没有 TTL 的话，一个只来过一次的用户会永久留一个 ZSET——按用户维度计数时，
-- **key 的数量等于历史用户数而不是活跃用户数**
redis.call('PEXPIRE', key, math.ceil(window * 1000) + 1000)
return {limit - used - 1, 0}
"""

_script = None


def bind(redis) -> None:
    """注册脚本。走 EVALSHA 只发 40 字节摘要，不必每个请求推整段脚本。"""
    global _script
    _script = redis.register_script(_WINDOW_LUA)


async def check(key: str, window: float, limit: int) -> Tuple[int, int]:
    """返回 (剩余配额, 建议重试毫秒)。剩余 -1 表示超限。异常一律放行。"""
    if _script is None:
        return limit, 0
    try:
        remaining, retry_ms = await _script(
            keys=[f"{KEY_PREFIX}{key}"],
            # member 用 uuid：只需唯一、不参与比较。同微秒的两个请求若 member 相同，
            # ZSET 会当成同一条覆盖掉，并发越高漏计越多
            args=[window, limit, uuid.uuid4().hex],
        )
        return int(remaining), int(retry_ms)
    except Exception as e:
        # 失败放行：限流器是保护机制，不是可用性依赖（同 admission.py）
        logger.warning("滑动窗口检查失败，放行 key=%s: %s", key, e)
        return limit, 0


class SlidingWindowLimiter:
    """路由依赖。用法：`dependencies=[Depends(SlidingWindowLimiter("scope", 60, 10))]`

    `scope` 进 key，所以不同接口的配额互相独立——共用一个 key 的话，
    查榜单会吃掉建对局的配额，**一个接口被刷会连带限死无关的功能**。
    """

    def __init__(self, scope: str, seconds: float, times: int):
        self.scope = scope
        self.seconds = seconds
        self.times = times

    async def __call__(self, request: Request):
        # 跨节点转发豁免：转发后的请求会在第二个节点再计一次，同一个用户动作被计两次，
        # 配额凭空减半、且减多少取决于房间落在哪台机器。限流只在**入口节点**发生一次。
        # 同 rate_limit.py 的 SkipForwardedRateLimiter
        from app.core.room_router import FORWARD_HEADER

        if request.headers.get(FORWARD_HEADER):
            return

        from app.core.rate_limit import user_or_ip_identifier

        who = await user_or_ip_identifier(request)
        remaining, retry_ms = await check(f"{self.scope}:{who}", self.seconds, self.times)
        if remaining >= 0:
            return

        # scope 是低基数（接口数量固定），可以进 label；who 绝不能进（C02 基数爆炸）
        metrics.rate_limit_rejects.labels(scope=self.scope).inc()
        retry_after = max(1, -(-retry_ms // 1000))
        raise HTTPException(
            status_code=HTTP_429_TOO_MANY_REQUESTS,
            detail="请求过于频繁，请稍后再试",
            # 带 Retry-After：不说等多久，客户端会立刻重试，重试本身变成新的峰值
            headers={"Retry-After": str(retry_after)},
        )
