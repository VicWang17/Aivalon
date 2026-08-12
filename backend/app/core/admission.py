"""
这个文件是**网关层准入控制**：全局令牌桶 + IP 维度令牌桶，挂在中间件上，所有请求先过它。

和已有的 `rate_limit.py` 是两回事
--------------------------------
`rate_limit.py` 是**业务层**限流：按 user_id 管"这个用户建局别太频繁"，挂在具体路由上，
表达的是业务规则。它保护不了系统——一万个用户每人都只建 1 局，完全合规，机器照样倒。
这一层管的是另一件事：**这台系统一共能吃多少**。所以它按容量算，不按用户算，
且必须挂在最外层——被它拒掉的请求不应该消耗任何后端资源（DB 连接、Actor、AI 队列）。
大白话：业务层是"每人限购两件"，网关层是"店里只能站 200 人，满了在门外等"。

为什么是令牌桶，不是 fastapi-limiter 那种固定窗口
----------------------------------------------
固定窗口在窗口边界能放进 **2 倍** 流量（上一窗口末尾 N 个 + 下一窗口开头 N 个），
而准入层的存在意义恰恰是给出一个可信的容量上界。更关键的是固定窗口**表达不了
"允许突发到 400、但稳态只给 200/s"** 这件事——而这正是真实流量的形状：
平时很闲，偶尔一波集中涌入。令牌桶用两个参数把这两件事分开：
  - `capacity` = 攒得下多少令牌 = **允许多大的突发**
  - `rate` = 每秒补多少 = **稳态速率**
桶空了就拒，攒满了能一次放过 capacity 个。**突发容忍度和稳态速率是两个独立的旋钮**，
这是固定窗口只有"N 次/秒"一个旋钮时做不到的。

为什么必须用 Lua
--------------
"读桶 → 算补了多少 → 判断 → 写回"是个读改写序列，而多个 API 进程共享同一个桶。
用 `GET` 再 `SET` 的话，两个进程同时读到"还剩 1 个令牌"，各自都放行——桶被超发。
Lua 脚本在 Redis 里单线程整段执行，这个序列才是原子的。

时间取 Redis 的，不取调用方的
--------------------------
补令牌要算"距上次多久了"，用哪个时钟是个选择。取各 API 节点自己的 `time.time()`，
节点间时钟偏差会直接变成令牌数偏差——快的那台会算出更多令牌，等于它给自己多发配额。
脚本里用 `redis.call('TIME')`，桶的时间基准和桶本身在同一个地方。
**共享状态的时间基准也是共享状态**，和 C05 那条"跨进程比较的哈希不能用带随机盐的 `hash()`"
是同一类问题：凡是多个进程要算出同一个答案的输入，都不能各自本地取。

读不到 Redis 往哪边倒：放行
------------------------
限流器是**保护机制，不是可用性依赖**。Redis 抖一下就把全站拒了的话，
这个限流器本身成了新的单点，它造成的故障比它防的还大。所以异常一律放行。
注意这和 H-1 的降级开关**方向相反**（开关读不到要保持已生效的降级态）：
开关表达的是"人做的决定不能自己失效"，限流器表达的是"别帮着把故障放大"。
**每个有兜底的地方都要单独想一遍失败方向，照抄隔壁就会写反。**
"""
import logging
from typing import Optional, Tuple

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core import metrics
from app.core.config import settings

logger = logging.getLogger("aivalon.admission")

GLOBAL_KEY = "admit:global"
IP_KEY_PREFIX = "admit:ip:"

# 不限流的路径。**健康检查必须豁免**：突发流量下把 /health 也拒了，
# 负载均衡会认为节点已死并把它摘掉——于是剩下的节点承接更多流量、更快被拒，
# 限流器亲手把一次过载放大成雪崩。/metrics 同理，正过载时最需要看指标。
EXEMPT_PATHS = frozenset({"/health", "/metrics", "/", "/cluster"})

# 桶：HASH{t=剩余令牌, ts=上次补令牌的时刻}
# 返回 {判定, 建议重试毫秒}；判定 0=放行 1=全局桶空 2=IP 桶空
_BUCKET_LUA = """
local now = redis.call('TIME')
local nowf = tonumber(now[1]) + tonumber(now[2]) / 1000000

local function peek(key, cap, rate)
  local b = redis.call('HMGET', key, 't', 'ts')
  local tokens, ts = tonumber(b[1]), tonumber(b[2])
  if tokens == nil then return cap end
  return math.min(cap, tokens + (nowf - ts) * rate)
end

local function save(key, cap, rate, tokens)
  redis.call('HSET', key, 't', tokens, 'ts', nowf)
  -- TTL 不是随手取的：桶从空到攒满要 cap/rate 秒，而**一个过期消失的桶和一个满桶
  -- 完全等价**（都是下次来了给满配额）。所以按"恰好攒满"设过期是无损的，
  -- 顺手解决了 IP 桶随访客数无限增长的问题。多给 1 秒余量防边界抖动。
  redis.call('PEXPIRE', key, math.ceil(cap / rate * 1000) + 1000)
end

local g_cap, g_rate = tonumber(ARGV[1]), tonumber(ARGV[2])
local i_cap, i_rate = tonumber(ARGV[3]), tonumber(ARGV[4])
local cost = tonumber(ARGV[5])

-- 先判全局：全局满了就该无条件卸载，不必再管来源是谁
local g = peek(KEYS[1], g_cap, g_rate)
if g < cost then
  save(KEYS[1], g_cap, g_rate, g)
  return {1, math.ceil((cost - g) / g_rate * 1000)}
end

local i = peek(KEYS[2], i_cap, i_rate)
if i < cost then
  -- 刻意不扣全局桶：被 IP 层拒掉的请求没有消耗系统容量，
  -- 扣了的话一个刷接口的 IP 就能把全局配额也一起烧掉——那正是它想干的事
  save(KEYS[1], g_cap, g_rate, g)
  save(KEYS[2], i_cap, i_rate, i)
  return {2, math.ceil((cost - i) / i_rate * 1000)}
end

save(KEYS[1], g_cap, g_rate, g - cost)
save(KEYS[2], i_cap, i_rate, i - cost)
return {0, 0}
"""

_script = None


def bind(redis) -> None:
    """注册脚本。用 register_script 而不是每次 eval：走 EVALSHA 只发 40 字节摘要，
    不必每个请求都把整段脚本推给 Redis——这一层每个请求都要过，省的是带宽也是延迟。"""
    global _script
    _script = redis.register_script(_BUCKET_LUA)


def client_ip(request: Request) -> str:
    """取客户端 IP。

    **刻意不读 `X-Forwarded-For`**：它是个请求头，客户端可以随便填。信了它，
    每个请求换一个伪造 IP 就能让 IP 桶形同虚设——**一个可伪造的限流键等于没有限流键**。
    只有在确实有一层自己可信、且会**覆写**该头的代理时才能信它，那属于部署配置，
    不该由应用默认假设。
    """
    return request.client.host if request.client else "unknown"


async def check(ip: str, cost: int = 1) -> Tuple[int, int]:
    """返回 (判定, 建议重试毫秒)。判定 0=放行 1=全局桶空 2=IP 桶空。异常一律放行。"""
    if _script is None:
        return 0, 0
    try:
        verdict, retry_ms = await _script(
            keys=[GLOBAL_KEY, f"{IP_KEY_PREFIX}{ip}"],
            args=[
                settings.RATE_LIMIT_GLOBAL_CAPACITY,
                settings.RATE_LIMIT_GLOBAL_RATE,
                settings.RATE_LIMIT_IP_CAPACITY,
                settings.RATE_LIMIT_IP_RATE,
                cost,
            ],
        )
        return int(verdict), int(retry_ms)
    except Exception as e:
        # 失败放行：限流器是保护机制不是可用性依赖，见文件头
        logger.warning("准入检查失败，放行: %s", e)
        return 0, 0


class AdmissionMiddleware(BaseHTTPMiddleware):
    """全局准入。挂在最外层，被拒的请求不碰任何后端资源。"""

    async def dispatch(self, request: Request, call_next):
        if not settings.RATE_LIMIT_ADMISSION_ENABLED or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        # 跨节点转发豁免：转发后的请求会在第二个节点再过一次准入，同一个用户动作
        # 占两份容量，且占多少取决于房间落在哪台机器。准入只在**入口节点**发生一次。
        # 同 rate_limit.py 的 SkipForwardedRateLimiter，不构成绕过途径：
        # 该头只在集群内部转发时添加，外部伪造它的请求在入口节点已经被计过一次了。
        from app.core.room_router import FORWARD_HEADER

        if request.headers.get(FORWARD_HEADER):
            return await call_next(request)

        verdict, retry_ms = await check(client_ip(request))
        if verdict == 0:
            return await call_next(request)

        # 分层上报：两档要采取的行动完全不同——global 是**系统到顶了**（该扩容或降级），
        # ip 是**某个来源在打我**（该封或该查）。合成一个 429 计数就分不出来了。
        layer = "global" if verdict == 1 else "ip"
        metrics.admission_rejects.labels(layer=layer).inc()
        retry_after = max(1, -(-retry_ms // 1000))  # 向上取整到秒
        return JSONResponse(
            status_code=429,
            content={"code": 429, "message": "请求过于频繁，请稍后再试", "data": None},
            # **必须带 Retry-After**：不告诉客户端等多久，它会立刻重试，
            # 于是过载期间的重试本身变成新的流量峰值——限流器又把故障放大了一轮
            headers={"Retry-After": str(retry_after)},
        )
