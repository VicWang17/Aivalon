# 这个文件是集群节点注册表：每个进程启动时把自己登记到 Redis，定期续约心跳，
# 并根据"当前存活节点集合"重建一致性哈希环（app/core/hash_ring.py）。
#
# 有了它，任意节点都能回答同一个问题："房间 xyz 归谁管？"——这是房间 Actor 单写者
# 模型能横向扩容的前提：状态在某个进程内存里，请求必须被路由到那个进程。
#
# 为什么用 ZSET 而不是 SET + 每节点一个 TTL key：
#   - SET 没有成员级 TTL，节点宕机后成员永远留着，环上会挂着一个死节点
#   - 每节点一个 `node:{id}` key 靠 TTL 自动过期是可行的，但列举存活节点要 SCAN 整个
#     keyspace（生产 keyspace 很大时是慢操作）
#   - ZSET 把"成员 + 最后心跳时间"存成 score，一次 ZRANGEBYSCORE 就能取存活集合，
#     一次 ZREMRANGEBYSCORE 清理死节点，单 key、常数次往返
#
# 为什么时间戳必须取 Redis 的时钟（TIME 命令）而不是各节点的本地时间：
#   存活判定是"最后心跳时间 > 现在 - TTL"，写入方和判定方是不同机器。若各写各的本地
#   时间，机器间时钟偏移会直接变成误判——快 10 秒的节点看别人永远像是过期的，慢 10 秒
#   的节点自己写入的心跳一出生就是过期的。Redis 是所有节点共享的单一时间源，
#   把它当作集群唯一时钟就不存在偏移问题。
import asyncio
import logging
import os
import socket
import uuid
from typing import List, Optional

from app.core.hash_ring import HashRing

logger = logging.getLogger("aivalon.cluster")

# 存活节点表：ZSET，member = node_id，score = 最后心跳的 Redis 时间戳（秒）
NODE_SET_KEY = "aivalon:cluster:nodes"

HEARTBEAT_INTERVAL = 2.0
# 判死阈值取心跳间隔的 3 倍：容忍偶发一次心跳丢失（GC 停顿、Redis 抖动）而不误摘节点。
# 这是个明确的取舍——阈值越小故障发现越快，但误判概率越高；误摘一个活节点的代价是
# 它名下所有房间被别的节点认领，比慢几秒发现宕机严重得多，所以宁可保守。
NODE_TTL = HEARTBEAT_INTERVAL * 3


def resolve_node_id() -> str:
    """节点身份：优先取配置/环境变量（部署时显式指定，重启后身份不变，房间会漂回来），
    否则用 主机名-进程号-随机后缀 自动生成。随机后缀是为了同机多进程不撞名。"""
    from app.core.config import settings

    configured = getattr(settings, "NODE_ID", "") or os.getenv("NODE_ID", "")
    if configured:
        return configured
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


class NodeRegistry:
    """节点注册 + 心跳 + 环维护。一个进程一个实例。"""

    def __init__(self, redis, node_id: Optional[str] = None,
                 ttl: float = NODE_TTL, interval: float = HEARTBEAT_INTERVAL):
        self._redis = redis
        self.node_id = node_id or resolve_node_id()
        self._ttl = ttl
        self._interval = interval
        self._ring = HashRing()
        self._task: Optional[asyncio.Task] = None

    async def _now(self) -> float:
        """集群统一时钟：取 Redis 服务端时间，避免各节点本地时钟偏移导致存活误判"""
        secs, usecs = await self._redis.time()
        return secs + usecs / 1_000_000

    async def heartbeat(self) -> List[str]:
        """续约自己 + 清理死节点 + 取存活集合，并按需重建环。返回存活节点列表。"""
        now = await self._now()
        await self._redis.zadd(NODE_SET_KEY, {self.node_id: now})
        # 清理：心跳早于 (now - ttl) 的节点视为已死。幂等，多节点同时清理无副作用。
        await self._redis.zremrangebyscore(NODE_SET_KEY, "-inf", now - self._ttl)
        alive = await self._redis.zrangebyscore(NODE_SET_KEY, now - self._ttl, "+inf")
        self._sync_ring(alive)
        return alive

    def _sync_ring(self, alive: List[str]) -> None:
        """把环调整成与存活集合一致。只增删差集——一致性哈希的价值就在于此，
        整个重建也能得到同样的环（虚拟节点由节点名派生），但按差集操作更能表达意图。"""
        current = set(self._ring.nodes)
        target = set(alive)
        if current == target:
            return
        for gone in current - target:
            self._ring.remove_node(gone)
            logger.warning("节点离开集群: %s（存活 %d）", gone, len(target))
        for joined in target - current:
            self._ring.add_node(joined)
            logger.info("节点加入集群: %s（存活 %d）", joined, len(target))

    async def deregister(self) -> None:
        """优雅下线：主动摘掉自己，不用等 TTL 过期。
        这能把"计划内重启"的路由空窗从 TTL 级别压到一次往返——只有真崩溃才走 TTL 判死。"""
        try:
            await self._redis.zrem(NODE_SET_KEY, self.node_id)
            logger.info("节点已注销: %s", self.node_id)
        except Exception as e:
            # 下线路径不能因为 Redis 抖动阻塞进程退出：留给 TTL 兜底即可
            logger.warning("注销失败，交由 TTL 兜底: %s", e)

    def owner_of(self, game_id: str) -> Optional[str]:
        """房间归属节点。环为空（Redis 不可用或尚未首次心跳）时返回 None，由调用方降级。"""
        return self._ring.get_node(game_id)

    def is_mine(self, game_id: str) -> bool:
        """房间是否归本节点。环为空时返回 True——单机降级：拿不到集群视图就自己扛，
        总比拒绝服务好（单节点部署下环里只有自己，语义也一致）。"""
        owner = self.owner_of(game_id)
        return owner is None or owner == self.node_id

    @property
    def live_nodes(self) -> List[str]:
        return self._ring.nodes

    async def _loop(self) -> None:
        while True:
            try:
                await self.heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 心跳失败不致命：本节点仍在服务，TTL 到了会被别人摘掉，恢复后自动重新加入
                logger.error("心跳失败，下轮重试: %s", e)
            await asyncio.sleep(self._interval)

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="node-heartbeat")
        return self._task

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        await self.deregister()


# 全局单例：由 main.py 的 lifespan 初始化并启动心跳
registry: Optional[NodeRegistry] = None


def init(redis) -> NodeRegistry:
    global registry
    registry = NodeRegistry(redis)
    return registry
