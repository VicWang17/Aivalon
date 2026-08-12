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

    return await _load_once(key, loader, redis=redis, l1_ttl=l1_ttl, l2_ttl=l2_ttl)


# ----------------------------------------------------------------------
# singleflight：热 key 失效瞬间只回源一次
# ----------------------------------------------------------------------
#
# 缓存击穿：一个被高频读的 key 恰好过期（或被失效）的那一瞬间，所有正在读它的请求
# 同时发现未命中，于是**同时**回源。缓存平时挡住的那些 QPS，会在这一刻原封不动地
# 砸到 MySQL 上。key 越热，砸得越狠——热点是它的前提，不是它的缓解因素。
# 好比一家只有一个窗口的银行，牌子上写"暂停营业"的那一秒，排队的人全涌到窗口问
# "什么时候开"——问的人越多，窗口越开不了。
#
# 和穿透（F-3）的区别：穿透查的是**根本不存在**的东西，布隆过滤器能在门口拦掉；
# 击穿查的是**真实存在且很热**的东西，拦不得也拦不掉，只能让它们**共享一次回源**。
#
# 做法就是 singleflight：同一个 key 的并发回源，第一个进来的真去查，
# 后来的都挂在它的 future 上等结果。N 个并发请求 → 1 次查询 + N-1 次等待。
#
# 边界要说清楚：这是**进程内**互斥，不是集群级互斥。
# M 个进程各自回源一次，最坏 M 次而不是 1 次。没上分布式锁是因为代价不对等——
# 分布式锁要在**每次未命中的路径上**多付一次 Redis 往返，还会引入新的故障模式
# （持锁者崩了，其他进程得等锁超时；锁超时设短了又会退化成没锁）。
# 而未命中本来就是低频事件，M（进程数，个位数）远小于 N（并发请求数）。
# **把 N 压到 1 是数量级的改善，把 M 压到 1 是常数级的改善，代价却高得多。**

# key → 正在进行的回源。用 future 而不是锁：锁只能让后来者排队"再查一次"，
# future 能让它们直接拿到第一次的结果——目标是少查，不是排队查。
_inflight: Dict[str, asyncio.Future] = {}


async def _load_once(
    key: str,
    loader: Callable[[], Any],
    redis=None,
    l1_ttl: float = L1_TTL,
    l2_ttl: int = L2_TTL,
) -> Any:
    """回源并回填两级缓存。同一个 key 并发进来时只有第一个真的回源。"""
    # 二次检查 L1。调用方查 L1 未命中之后还 await 过一次 L2 读，那期间足够别的任务
    # 完整跑完一次回源并回填——尤其 loader 是同步函数时，它从头到尾不让出事件循环，
    # 等这个任务被调度回来时"正在回源"的痕迹已经没了，不重查 L1 就会白查一次库。
    hit, value = l1.get(key)
    if hit:
        metrics.cache_reads.labels(level="l1", result="hit").inc()
        return value

    existing = _inflight.get(key)
    if existing is not None:
        metrics.cache_reads.labels(level="singleflight", result="coalesced").inc()
        # 必须 shield：直接 `await existing` 时，如果这个等待者的 task 被取消
        # （比如客户端断开），取消会顺着传到共享的 future 上，把**正在回源的那个人**
        # 和其他所有等待者一起打断。shield 让取消只作用于当前等待者。
        return await asyncio.shield(existing)

    fut = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        metrics.cache_reads.labels(level="db", result="miss").inc()
        value = loader()
        if asyncio.iscoroutine(value):
            value = await value
    except BaseException as e:
        # 用 BaseException 是因为 CancelledError 在 3.8+ 不是 Exception 的子类，
        # 而回源被取消时同样必须把等待者放掉——否则它们会一直挂在这个永不完成的
        # future 上，一个客户端断开就能拖死同一个 key 的所有读请求
        _inflight.pop(key, None)
        if not fut.done():
            fut.set_exception(e)
            # 没有等待者时，未被取用的异常会被 asyncio 打成 "exception was never
            # retrieved" 警告刷日志。这里主动取一次消掉它（fut 只会 set_exception、
            # 不会被 cancel，所以这个调用本身是安全的）
            fut.exception()
        raise

    if not fut.done():
        fut.set_result(value)
    _inflight.pop(key, None)

    # 回填放在结果交付之后：等待者拿的是同一个值，不必等两级缓存写完。
    # 也只有回源者写缓存——等待者跟着写就是 N-1 次重复写。
    if redis is not None:
        try:
            await redis.set(key, json.dumps(value, default=str), ex=l2_ttl)
        except Exception as e:
            logger.warning("L2 回填失败: key=%s %s", key, e)
    l1.set(key, value, ttl=l1_ttl)
    return value


async def invalidate(key: str, redis=None, broadcast: bool = True) -> None:
    """失效一个 key：清本进程 L1 + 删 L2 + 广播通知其他进程清它们的 L1。

    三步都要做，缺一步都会留下旧值：L2 删了但别的进程 L1 还在供旧值，
    就是 F-1 里那个"L1 跨进程失效不了"的洞。
    """
    l1.delete(key)
    if redis is not None:
        try:
            await redis.delete(key)
        except Exception as e:
            logger.warning("L2 失效失败: key=%s %s", key, e)
    if broadcast:
        await _publish_invalidation(key, redis=redis)


# ----------------------------------------------------------------------
# L1 跨进程失效
# ----------------------------------------------------------------------
#
# 这是 F-1 留下的洞：L2 是共享的，删一次全集群立刻看到；而 L1 在每个进程自己的
# 堆里，别的进程改了数据，这个进程的 L1 毫不知情，会继续供旧值直到自己 TTL 到。
# 补法是一条广播通道：谁失效了 key 就喊一声，所有进程听见就清自己的 L1。
#
# 这里刻意用**全集群广播**，和 E-1 的 WS 扇出刻意不广播正好相反。
# 区别在于有没有办法知道该发给谁：WS 扇出有连接路由表，能查出"这个房间的连接在哪几个
# 节点"，定向发就省下无关节点的收发；而"哪个进程的 L1 里存着这个 key"没有任何登记，
# 每个进程都可能有，所以只能广播。**能定向的时候定向，定不了的时候才广播。**
#
# 也不需要防环：E-1 那里收到转发要防止再次扇出（否则 A→B→A 死循环），
# 而这里收到消息只是删本地一个 key，删自己已经删过的 key 是幂等的，
# 发布者收到自己的消息也无所谓。**幂等的操作不需要防环，这是能省掉一层机制的原因。**

INVALIDATE_CHANNEL = "aivalon:cache:invalidate"

_redis = None
_sub_task: Optional[asyncio.Task] = None
_pubsub = None


def bind(redis) -> None:
    """接入失效广播。不调用即为单进程模式：失效只清本地，不发广播。"""
    global _redis
    _redis = redis


async def _publish_invalidation(key: str, redis=None) -> None:
    client = redis if redis is not None else _redis
    if client is None:
        return
    try:
        await client.publish(INVALIDATE_CHANNEL, key)
    except Exception as e:
        # 广播失败只是让别的进程多等一个 L1_TTL，不影响正确性——
        # 这正是 L1 TTL 作为兜底存在的意义，所以这里只记日志
        logger.warning("失效广播发送失败，其他进程将等 L1 过期: key=%s %s", key, e)


async def _subscribe_loop() -> None:
    global _pubsub
    while True:
        try:
            pubsub = _redis.pubsub()
            _pubsub = pubsub
            await pubsub.subscribe(INVALIDATE_CHANNEL)
            logger.info("缓存失效频道已订阅: %s", INVALIDATE_CHANNEL)
            async for raw in pubsub.listen():
                if raw.get("type") != "message":
                    continue
                key = raw.get("data")
                if key:
                    # 只清 L1。L2 是共享的，发布方已经删过了，这里再删一次纯属多余往返
                    l1.delete(key)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 订阅断了必须重连：断开期间本进程的 L1 会一直供旧值，
            # 最坏情况退化成"只有 TTL 兜底"
            logger.warning("缓存失效订阅中断，2s 后重连: %s", e)
            await asyncio.sleep(2)


def start() -> None:
    global _sub_task
    if _redis is not None and _sub_task is None:
        _sub_task = asyncio.create_task(_subscribe_loop())


async def stop() -> None:
    global _sub_task, _pubsub
    if _sub_task:
        _sub_task.cancel()
        _sub_task = None
    if _pubsub is not None:
        try:
            await _pubsub.aclose()
        except Exception:
            pass
        _pubsub = None


async def drop_subscription() -> None:
    """断开底层 socket，订阅循环会自行重连。测试模拟网络抖动用。
    只断 socket 不动 PubSub 对象——理由同 socket_manager.drop_subscription。"""
    conn = getattr(_pubsub, "connection", None) if _pubsub else None
    if conn is not None:
        try:
            await conn.disconnect()
        except Exception:
            pass


async def invalidate_events(game_id: str, redis=None) -> None:
    """失效某局的回放事件流缓存。

    调用时机很关键：必须在**MySQL 真的写进去之后**，不是动作发生时。
    因为事件走 Write-Behind（先进 Redis Stream，flusher 200ms 后批量刷 MySQL），
    而这份缓存回源的是 MySQL。在动作发生时失效，紧随其后的读会回源到一个
    **还没刷进新事件的 MySQL**，然后把这份旧结果缓存 300 秒——比不失效更糟。
    所以失效点挂在 flusher 提交成功之后。
    """
    await invalidate(events_key(game_id), redis=redis)
