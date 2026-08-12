# 这个文件是热榜写路径的合并缓冲：对局结束的胜场变更先攒进 Redis HASH，
# 由后台循环周期性批量 ZINCRBY 打到榜上。
#
# 原来的写法有什么问题
# --------------------
# 每局结束都去 MySQL 查一遍这几个玩家的**绝对**胜场，再 `ZADD` 覆盖榜上的分数。
# 一局 8 个人 × 3 个榜 = 24 次 ZADD，外加一次查库，全都发生在对局结束那一刻。
# 200 局同时结束就是 200 次查库 + 几千次往返，而且这些往返打的是**读路径正在用的 ZSET**。
#
# 为什么改成 ZINCRBY
# -----------------
# `ZADD` 写的是绝对值，所以**必须先知道现在是多少**——这就是那次查库的来源。
# `ZINCRBY` 写的是增量，而增量在对局结束的那一刻就已经知道了（赢了就 +1），
# 根本不用查库。**"用增量还是绝对值"决定了要不要读一次数据源**，不只是 API 差别。
#
# 代价是增量不幂等：绝对值 ZADD 重复执行没事，ZINCRBY 重复执行就多加一次。
# 这里接受 at-least-once，因为热榜是**可以从 MySQL 重建的派生数据**
# （`RankService._rebuild_leaderboard` 就是那条兜底路径），偶尔多算一次下次重建就正回来了。
# 换成账户余额这种权威数据就绝不能这么做。
#
# 合并缓冲又是为什么
# -----------------
# 缓冲是一个 HASH，field 是 `榜|玩家`，值是累积增量，用 HINCRBY 攒。
# 收益分两笔，得分开说清楚：
#   - **批量化**（这是主要收益）：一个窗口里结束的所有对局，攒成一次 pipeline 打出去。
#     N 次往返变 1 次，和 Write-Behind 把 N 次 MySQL 写攒成一个事务是同一个套路。
#   - **同 member 合并**：同一个 (榜, 玩家) 在窗口内被改多次，HINCRBY 会自动叠成一条。
#     这一笔在本项目里其实不大——一个人没法同时结束两局——所以别把它当卖点。
#     `rank_updates_buffered / rank_updates_applied` 会如实反映合并率是多少。
# 除此之外还顺带把 ZSET 的写从对局结束路径上摘下来了：读路径要用的 ZSET 只在
# 每个窗口被碰一次，而不是每局结束都碰。
import asyncio
import logging
from typing import Iterable, Tuple

from redis.exceptions import ResponseError

from app.core import metrics

logger = logging.getLogger("aivalon.rank")

# 待刷缓冲（HASH）：field = "榜|玩家id"，value = 累积增量
PENDING_KEY = "rank:pending"
# 换出后的临时 key 前缀，按节点分开（理由见 drain_once）
DRAINING_PREFIX = "rank:draining:"

# 刷榜间隔（秒）。比 Write-Behind 的 200ms 宽松得多：热榜晚一秒更新没人看得出来，
# 而事件流的间隔直接是 RPO。**间隔该取多少由"晚一点的后果是什么"决定，不是越小越好**——
# 间隔越大，攒进一批的变更越多，合并率越高。
DRAIN_INTERVAL = 1.0

BOARD_TOTAL = "total"
BOARD_GOOD = "good"
BOARD_EVIL = "evil"

# 榜名 → ZSET key。key 定义放在 core 这一层，让 services/rank_service.py 反过来引用，
# 避免 core → services 的反向依赖成环。
BOARD_KEYS = {
    BOARD_TOTAL: "leaderboard:total_wins",
    BOARD_GOOD: "leaderboard:wins_good",
    BOARD_EVIL: "leaderboard:wins_evil",
}

_SEP = "|"


def _field(board: str, member: str) -> str:
    return f"{board}{_SEP}{member}"


async def record(entries: Iterable[Tuple[str, int, int]], redis) -> int:
    """把若干条胜场变更并进缓冲。entries: [(榜名, user_id, 增量), ...]。返回入缓冲条数。

    刻意不在这里做任何合并：HINCRBY 本身就是服务端的合并操作，
    同一个 field 攒多少次都只是一条。客户端再攒一层只是重复劳动。
    """
    items = [(board, user_id, delta) for board, user_id, delta in entries if delta]
    if not items or redis is None:
        return 0

    try:
        pipe = redis.pipeline(transaction=False)
        for board, user_id, delta in items:
            pipe.hincrby(PENDING_KEY, _field(board, str(user_id)), delta)
        await pipe.execute()
    except Exception as e:
        # 榜没更新上不该让对局结算失败：胜场的权威值在 MySQL 的 User 表里，
        # 榜是派生数据，丢了这一批下次重建就补回来了
        logger.warning("热榜变更入缓冲失败，本批丢弃: %s", e)
        return 0

    metrics.rank_updates_buffered.inc(len(items))
    return len(items)


async def _apply(redis, tmp: str) -> int:
    """把临时 key 里攒着的增量批量打到榜上。返回实际执行的 ZINCRBY 条数。"""
    items = await redis.hgetall(tmp)
    if not items:
        return 0

    pipe = redis.pipeline(transaction=False)
    applied = 0
    for field, delta in items.items():
        board, _, member = field.partition(_SEP)
        key = BOARD_KEYS.get(board)
        if key is None:
            # 认不出的榜名（比如换版本前的残留）只跳过这一条，不能让整批失败——
            # 抛出去的话，同批里正常的那些增量会跟着一起丢
            logger.warning("热榜缓冲里有认不出的榜名，跳过: %s", field)
            continue
        pipe.zincrby(key, float(delta), member)
        applied += 1
    await pipe.execute()

    # 删除放在 ZINCRBY 之后：这中间宕机会导致这一批被重放一次（at-least-once）。
    # 反过来先删再打就是 at-most-once，会**永久少算**——两害之间选可以自愈的那个，
    # 多算的下次全量重建就正回来了，少算的除了重建没有别的办法发现。
    await redis.delete(tmp)

    metrics.rank_updates_applied.inc(applied)
    return applied


async def drain_once(redis, node_id: str) -> int:
    """换出缓冲并批量刷榜。返回实际执行的 ZINCRBY 条数。

    用 RENAME 换出而不是"HGETALL 之后 DEL"：后者在两步之间进来的写会被 DEL 连带抹掉，
    那些变更既没打到榜上也不在缓冲里了。RENAME 是原子的，换出之后的写落进一个新的
    空缓冲，下一轮再刷。

    RENAME 顺带解决了多节点的互斥：每个节点都跑这个循环，但一个 key 只能被
    RENAME 成功一次，抢输的节点直接拿到 "no such key"。
    **不用加分布式锁，因为换出这个动作本身就是原子的**——能靠单条命令的原子性
    做到互斥时就别再套一层锁。
    """
    tmp = f"{DRAINING_PREFIX}{node_id}"

    # 先捡上一轮崩溃留下的残批：RENAME 成功之后、ZINCRBY 打完之前宕机的话，
    # 增量就躺在这个临时 key 里，不先处理就永久丢了。
    # 临时 key 按节点分开正是为了这个——别的节点的残批不该由我来猜。
    applied = await _apply(redis, tmp)

    try:
        await redis.rename(PENDING_KEY, tmp)
    except ResponseError:
        return applied  # 缓冲是空的，或者被别的节点抢先换走了

    return applied + await _apply(redis, tmp)


async def drain_loop(redis, node_id: str) -> None:
    """后台刷榜循环（随 API 进程生命周期运行）。"""
    logger.info("rank flusher started (interval=%.1fs)", DRAIN_INTERVAL)
    while True:
        try:
            count = await drain_once(redis, node_id)
            if count:
                logger.info("flushed %d rank updates", count)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 刷不动不致命：增量留在缓冲里，下轮重来
            logger.error("rank flush failed, will retry: %s", e)
        await asyncio.sleep(DRAIN_INTERVAL)
