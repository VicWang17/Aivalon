# 这个文件是 L1（进程内）+ L2（Redis）两级缓存，未命中回源 MySQL。
#
# 为什么要两级，而不是只用 Redis
# ------------------------------
# L2 命中已经省掉了 MySQL 查询，但还要付一次网络往返 + 一次 JSON 反序列化。
# L1 存的是**已经反序列化好的对象**，命中时这两笔都不用付——省的不只是网络，
# 还有反序列化的 CPU。热点数据的读放大越高，这一级越值。
#
# L1 的代价：跨进程无法失效
# -------------------------
# Redis 是共享的，一处删掉全集群立刻看到；而 L1 在每个进程自己的堆里，
# 别的节点改了数据，这个进程的 L1 完全不知道。
# **所以 L1 的 TTL 就是它的一致性上限**——写入后最坏要等 L1_TTL 才能看到新值。
# 这是 L1 取 3s 而 L2 取 300s 的唯一理由：不是"3s 比较快"，而是
# 3 秒的脏读可以接受，300 秒不行。（F-2 会给 L2 加事件驱动失效，
# 但 L1 的短 TTL 仍然是它唯一的兜底手段。）
#
# 缓存值必须当只读的用
# --------------------
# L1 命中返回的是**共享引用**，不是副本。调用方一旦就地改它，
# 缓存里那份也跟着变了，而且后面每个命中者都会拿到被改过的数据。
# 拷一份能防住，但那就把"省掉反序列化"这个收益还回去了——所以选择不拷，
# 靠约定：从缓存拿到的东西只读不改。
import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from app.core import metrics

logger = logging.getLogger("aivalon.cache")

# L1 条目上限。必须有：进程内字典没人清就是内存泄漏，
# key 里带 game_id 这类无界维度时尤其明显——房间开一局就多一条，永不释放。
L1_MAX = 512
# L1 存活时间（秒）。见文件头：这个数字是"能容忍多久的脏读"，不是性能参数。
L1_TTL = 3.0
# L2 存活时间（秒）。够长才有意义，失效靠 F-2 的事件驱动，TTL 只是兜底。
L2_TTL = 300


class _L1:
    """进程内缓存。存 (过期时刻, 值)。

    没用 LRU：真正的热点会被反复访问而不断续期，FIFO 淘汰足够。
    上了 LRU 就要维护访问顺序，热路径上多一次链表操作，收益不明显。
    """

    def __init__(self, maxsize: int = L1_MAX):
        self._data: Dict[str, Tuple[float, Any]] = {}
        self._maxsize = maxsize

    def get(self, key: str) -> Tuple[bool, Any]:
        """返回 (是否命中, 值)。用元组而不是返回 None 表示未命中——
        缓存里本来就可能存着 None（空结果也值得缓存，见 F-3 穿透）。"""
        item = self._data.get(key)
        if item is None:
            return False, None
        expire_at, value = item
        if time.monotonic() >= expire_at:
            self._data.pop(key, None)
            return False, None
        return True, value

    def set(self, key: str, value: Any, ttl: float = L1_TTL) -> None:
        if len(self._data) >= self._maxsize and key not in self._data:
            self._evict()
        self._data[key] = (time.monotonic() + ttl, value)

    def _evict(self) -> None:
        """先清过期的；一个都没过期才按插入序丢最旧的（dict 保序）。"""
        now = time.monotonic()
        expired = [k for k, (exp, _) in self._data.items() if now >= exp]
        if expired:
            for k in expired:
                self._data.pop(k, None)
            return
        oldest = next(iter(self._data), None)
        if oldest is not None:
            self._data.pop(oldest, None)

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


l1 = _L1()


# key 命名带版本号前缀：改了缓存值的结构（比如给事件多加一个字段）时，
# 只要 bump 这个前缀，全部旧值自动作废。否则新代码会读到老结构的值，
# 而这种问题在灰度发布期间表现为"一半请求缺字段"，很难定位。
KEY_PREFIX = "aivalon:cache:v1"


def events_key(game_id: str) -> str:
    return f"{KEY_PREFIX}:events:{game_id}"


async def get_or_load(
    key: str,
    loader: Callable[[], Any],
    redis=None,
    l1_ttl: float = L1_TTL,
    l2_ttl: int = L2_TTL,
) -> Any:
    """按 L1 → L2 → 回源 的顺序取值，并把结果回填到上面两级。

    loader 可以是同步函数或协程函数：回源大多是同步的 ORM 查询，
    强制要求 async 只会让调用方多包一层。
    """
    hit, value = l1.get(key)
    if hit:
        metrics.cache_reads.labels(level="l1", result="hit").inc()
        return value

    if redis is not None:
        try:
            raw = await redis.get(key)
        except Exception as e:
            # 缓存挂了要能穿透到数据源，不能把读请求一起弄挂——
            # 缓存是加速手段，不是可用性依赖
            logger.warning("L2 读失败，回源: key=%s %s", key, e)
            raw = None
        if raw is not None:
            metrics.cache_reads.labels(level="l2", result="hit").inc()
            value = json.loads(raw)
            l1.set(key, value, ttl=l1_ttl)
            return value

    metrics.cache_reads.labels(level="db", result="miss").inc()
    value = loader()
    if asyncio.iscoroutine(value):
        value = await value

    if redis is not None:
        try:
            await redis.set(key, json.dumps(value, default=str), ex=l2_ttl)
        except Exception as e:
            logger.warning("L2 回填失败: key=%s %s", key, e)
    l1.set(key, value, ttl=l1_ttl)
    return value


async def invalidate(key: str, redis=None) -> None:
    """失效一个 key。

    注意这只清得掉**本进程**的 L1——别的节点的 L1 只能等它自己的 TTL 到。
    要做到全集群立即失效，需要一条广播通道通知每个进程清 L1（F-2 的事）。
    """
    l1.delete(key)
    if redis is not None:
        try:
            await redis.delete(key)
        except Exception as e:
            logger.warning("L2 失效失败: key=%s %s", key, e)
