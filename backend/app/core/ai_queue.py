# 这个文件是资源层限流的下半：AI 任务队列的深度上界。
#
# 和前面三处限流都不同的地方：**这里不能拒**
# --------------------------------------
# 网关层拒一个 HTTP 请求、应用层拒一次建局、房间层拒一个动作，都有个共同前提：
# **对面有客户端，它会拿到错误码、会重试**。AI 回合没有这个前提——
# 一个 AI 回合被丢掉，就没有任何人会再来提交它，那个房间的阶段永远不推进，
# **房间不是变慢而是永久卡死**。所以这一层的过载响应只能是**降级，不能是拒绝**。
#
# 降级降的是"每个任务多贵"，不是"放几个任务进来"
# ------------------------------------------
# 积压的成因不是任务条数多，而是**每条都要等一次 LLM**（十几秒量级）。
# 同样的队列，走规则引擎每条是毫秒级——**队列长度没变，排空时间差三个数量级**。
# 所以过载时把 LLM 摘掉：对局照常进行，AI 说话变套路。这也正是 5 级降级矩阵里
# L2（AI 全切规则引擎）那一级，只不过这里是按队列深度**自动**触发的。
# 一句话：堵车时不是把车赶走，是让每辆车少占路。
#
# 为什么深度自己记，不去问 broker
# ----------------------------
# 问 RabbitMQ 要 `queue_declare(passive=True)` 的 message_count 最准，但那是一次
# **阻塞的 AMQP 往返**，加在每个 AI 回合上就是把 broker 挂进了热路径——
# 保护机制自己成了新的延迟来源。这里改成自己记账：投递时登记、跑完注销。
# 代价是这个数字只覆盖"经由本项目投递的任务"（别的途径塞进队列的看不见），
# 换来的是**一次 Redis 命令就能拿到、且各进程看到同一个数**。
#
# 记账一定会漏，所以必须能自愈
# ------------------------
# worker 被 kill -9 就不会执行注销，漏一个计数就永久多算一个。
# 纯 INCR/DECR 的计数器在这里是错的选择——**它的误差只会单向累积**，
# 攒够阈值之后系统就永久停在降级态，而且从指标上看不出来是漏账还是真积压。
# 所以用 ZSET 存"在飞的任务 → 投递时刻"，计数前先把超过租约时长的清掉：
# 一个任务要么正常注销，要么被租约兜掉，**误差有上界且会自己消失**（同 C06 心跳租约）。
import logging
import uuid
from typing import Optional, Tuple

from app.core import metrics
from app.core.config import settings

logger = logging.getLogger("aivalon.ai_queue")

KEY = "aiq:inflight"

# 「按租约清理 → 记一笔 → 数一下」是读改写序列，多个 API 进程共享同一个 key，
# 拆成多条命令会让两个进程各自读到旧的深度（同 admission / sliding_window）
_ENTER_LUA = """
local key = KEYS[1]
local lease = tonumber(ARGV[1])
local member = ARGV[2]

local now = redis.call('TIME')
local nowf = tonumber(now[1]) + tonumber(now[2]) / 1000000

-- 先按租约清理：漏账（worker 崩了没注销）在这里被兜掉，误差不会累积
redis.call('ZREMRANGEBYSCORE', key, '-inf', nowf - lease)
if member ~= '' then
  redis.call('ZADD', key, nowf, member)
end
-- TTL 兜底：所有在飞任务都过了租约还没人碰这个 key，它整个就该消失
redis.call('EXPIRE', key, math.ceil(lease) + 60)
return redis.call('ZCARD', key)
"""

_script = None


def bind(redis) -> None:
    global _script
    _script = redis.register_script(_ENTER_LUA)


def _resolve(client):
    """拿到可用的 Script 对象。

    **Celery worker 里不会跑 lifespan**，所以没有 `bind` 那一步——
    但 worker 每个任务都自带一个 loop 绑定的 redis 客户端，可以就地注册。
    `Script.__call__` 支持 `client=` 覆盖，所以注册用哪个连接不影响后续调用走哪个。
    """
    global _script
    if _script is None and client is not None:
        _script = client.register_script(_ENTER_LUA)
    return _script


def _fallback_depth() -> int:
    """读不到深度时报 0 = 不降级。

    方向和限流器一致、和 H-1 降级开关相反：**这一层的降级是自动推断出来的，
    不是人做的决定**，所以推断不出来就别擅自降。误降的代价是全站 AI 一起变套路
    （玩家看得见的产品退化），而不降的代价有 H-2 的 LLM 舱壁兜着——
    每次调用有硬超时且会自己回落规则引擎，最坏情况是慢一次，不会卡死。
    """
    return 0


async def _run(member: str, redis=None) -> int:
    script = _resolve(redis)
    if script is None:
        return _fallback_depth()
    try:
        # 不传 client 时用注册脚本时那个连接。**Celery worker 里必须显式传**：
        # 那边每个任务新建 event loop，复用全局客户端会踩到"绑到已关闭 loop 的连接"
        kw = {"client": redis} if redis is not None else {}
        depth = int(await script(
            keys=[KEY], args=[settings.AI_QUEUE_LEASE, member], **kw,
        ))
    except Exception as e:
        logger.warning("读 AI 队列深度失败，按不降级处理: %s", e)
        return _fallback_depth()
    # 各进程都把从 Redis 读回的同一个数字写进 Gauge，所以多进程下不会互相打架
    metrics.ai_queue_depth.set(depth)
    return depth


async def enter(redis=None) -> Tuple[str, int]:
    """登记一个即将投递的 AI 任务，返回 (注销用的 token, 当前深度)。"""
    token = uuid.uuid4().hex
    return token, await _run(token, redis)


async def depth(redis=None) -> int:
    """只读当前深度（顺带做一次租约清理）。"""
    return await _run("", redis)


# ---------------------------------------------------------------------------
# 投递幂等：把"一个 AI 在一个阶段只行动一次"这条不变量直接写出来
#
# 原来的代码是**靠扫描近似**这条不变量的：`_trigger_ai_logic` 遍历所有玩家，
# 挑出"还没投票 / 还没执行任务"的那些投任务。这个近似在重入下必然失真——
# 那个函数挂在**每一次** `process_action` 之后（`game_service.py:721`），
# 而它对 VOTE / MISSION 刻意不 `break`（那两个阶段所有 AI 可以并行）。
# 于是：7 个 AI 要投票 → 投 7 个任务 → AI#1 投完票又触发一次 → 还有 6 个没投
# → 再投 6 个 → …… **7+6+…+1 = 28 次投递换 7 次有效投票**。
# 大白话：老师问"还有谁没交作业"，每收到一份就重新问一遍全班——
# 收到第一份时喊 7 个名字，第二份时喊 6 个。喊的次数是人数的平方，作业还是那几份。
#
# **`break` 那行和调用时机各自都是对的，错在两者的组合**，所以这里不去动它们中的
# 任何一个，而是把不变量本身登记下来：**修补每一处近似它的地方，不如把它直接写出来**。
#
# 键里为什么有 vote_track
# --------------------
# 同一个 `round` 里投票可以重来 5 次（提名被否 → `vote_track += 1` → 回 SPEECH →
# 重新提名 → 再投一轮，见 `game_service.py:462-476`）。只按 `{round}:{phase}` 建键的话，
# 第二次提名的那轮投票会被当成"这个 AI 已经投过了"——**AI 永远不投票，房间永久卡死**。
# 那比 O(n²) 严重得多：浪费可恢复，卡死不可恢复。
#
# 失败方向：**读不到 Redis 就照旧投递**
# ------------------------------
# 和 `_fallback_depth` 同一条理由，但后果更硬：这一层拦掉的是**任务本身**。
# 少投一个 AI 回合没有任何人会再来提交它（AI 回合背后没有会重试的客户端），
# 房间不是变慢而是永久卡死；多投一个只是浪费一次 HTTP。
# **哪边的代价可恢复，就往哪边倒。**
CLAIM_PREFIX = "aiq:dispatched"


def claim_key(game_id: str, round_no: int, vote_track: int,
              phase: str, player_id: int) -> str:
    return f"{CLAIM_PREFIX}:{game_id}:r{round_no}:t{vote_track}:{phase}:{player_id}"


async def claim(key: str, redis=None) -> bool:
    """登记一次投递，返回"这次该不该投"。

    `SET NX` 一条命令完成"查 + 占"，**刻意不拆成 EXISTS + SET**：
    两个 API 进程同时给同一个 AI 投递时，拆开就是两个都读到"没登记过"
    （同 admission / sliding_window / ai_queue 的深度记账，都是同一个理由）。
    """
    if redis is None:
        return True
    try:
        ok = await redis.set(key, "1", nx=True, ex=int(settings.AI_DISPATCH_CLAIM_TTL))
    except Exception as e:
        logger.warning("投递幂等键读写失败，按照旧投递处理 key=%s: %s", key, e)
        return True
    won = bool(ok)
    metrics.ai_dispatch_total.labels(result="dispatched" if won else "deduped").inc()
    return won


async def release_claim(key: str, redis=None) -> None:
    """撤销登记，让这个 AI 回合可以被重新投递。

    **必须有这个出口**：幂等键顺手拿掉了原来那个 O(n²) 重扫的副作用——
    它虽然浪费，却也在无意中充当"投递丢了就再投一次"的兜底。登记之后如果这个回合
    最终没跑成（永久错误、重试用尽），键还占着就没人会再投它，**房间卡死**。
    所以任务确定放弃时要把键放掉，等下一次 `_trigger_ai_logic` 重投。

    放掉之后不会退回 O(n²)：投递侧有**两道独立的闸**——扫描条件（它还没行动）
    和幂等键（这个阶段还没投过它）。真的已经投过票的 AI，`has_voted` 那道就把它挡住了。
    """
    if not key or redis is None:
        return
    try:
        await redis.delete(key)
    except Exception as e:
        logger.warning("撤销投递幂等键失败 key=%s: %s", key, e)


def release_claim_sync(key: str) -> None:
    """同步版撤销，供 Celery 任务收尾用（同 `leave_sync`：那时 AI 逻辑的 loop 已关）。"""
    if not key:
        return
    try:
        from app.core.redis import redis_sync, redis_sync_pool
        client = redis_sync.Redis(connection_pool=redis_sync_pool)
        try:
            client.delete(key)
        finally:
            client.close()
    except Exception as e:
        logger.warning("撤销投递幂等键失败 key=%s: %s", key, e)


async def leave(token: str, redis=None) -> None:
    """任务跑完注销。失败只记日志——**注销失败不能让任务本身失败**，
    否则一次 Redis 抖动会把已经算完的 AI 回合变成一次 Celery 重试。
    漏掉的这笔由租约兜掉。"""
    if not token or redis is None:
        return
    try:
        await redis.zrem(KEY, token)
    except Exception as e:
        logger.warning("注销 AI 任务失败，等租约兜掉 token=%s: %s", token, e)


def leave_sync(token: str) -> None:
    """同步版注销，供 Celery 任务收尾用。

    刻意走同步客户端而不是再起一个 event loop：注销发生在任务最后一步，
    那时候 AI 逻辑那个 loop 已经关掉了。一条 `ZREM` 不值得为它重建一个 loop。
    """
    if not token:
        return
    try:
        from app.core.redis import redis_sync, redis_sync_pool
        client = redis_sync.Redis(connection_pool=redis_sync_pool)
        try:
            client.zrem(KEY, token)
        finally:
            client.close()
    except Exception as e:
        logger.warning("注销 AI 任务失败，等租约兜掉 token=%s: %s", token, e)


def should_degrade(current: int) -> bool:
    """深度是否已经到了该摘掉 LLM 的程度。

    阈值按"一局最多 7 个 AI"给倍数：几局同时在 AI 回合属于正常，
    到几十个就说明 worker 已经跟不上投递速度了。这个数目前是拍的，等 S4 出真实拐点再定。
    """
    return current >= settings.AI_QUEUE_DEGRADE_DEPTH


def note_degraded() -> None:
    metrics.ai_turns_degraded.labels(reason="queue_depth").inc()
