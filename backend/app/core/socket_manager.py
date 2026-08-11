# 这个文件定义了WebSocket连接管理器，负责管理所有活跃的WebSocket连接，支持按房间（game_id）广播消息。
#
# 跨节点扇出（E 组）：连接和房间归属是两件独立的事。
# 房间路由（room_router）保证"动作只在归属节点处理"，但客户端的 WS 连接挂在哪个节点
# 是它自己连上来的那个，跟归属节点没有关系。D-3 接上转发之后就出现了这个漏洞：
# 动作被转发到归属节点、在那里广播，而连接在入口节点上——active_connections 是进程
# 本地字典，广播打给了一屋子空气，客户端收不到任何更新。
#
# 解法是一张连接路由表：房间 → 持有该房间连接的节点集合。广播时查表，只发给真正有
# 连接的那几个节点（而不是 Pub/Sub 广播给全集群——那样每个节点都要收下并丢弃与自己
# 无关的消息，节点越多浪费越大）。每个节点订阅自己的专属频道。
from typing import Dict, List, Optional
import asyncio
import logging
from fastapi import WebSocket
from app.schemas.protocol import WSMessage, WebSocketOpCode

logger = logging.getLogger("aivalon.ws")

# 房间 → 持有其连接的节点集合
ROOM_NODES_KEY = "aivalon:ws:rooms:{game_id}"
# 节点专属广播频道
NODE_CHANNEL_KEY = "aivalon:ws:node:{node_id}"
# 连接路由表兜底过期时间。节点崩溃时来不及摘表，只能靠 TTL 让残留条目最终消失。
# 取 1 小时：远长于一局游戏，房间活着就会被不断续期；房间死了一小时后自然清掉。
ROOM_NODES_TTL = 3600


class ConnectionManager:
    def __init__(self):
        # 存储格式: {game_id: [websocket1, websocket2, ...]}
        # 也可以考虑更细粒度: {game_id: {user_id: websocket}} 以便定向发送
        self.active_connections: Dict[str, List[WebSocket]] = {}
        self._redis = None
        self._node_id: Optional[str] = None
        self._sub_task: Optional[asyncio.Task] = None
        # 存下当前订阅句柄：stop() 要显式关掉它，否则连接只能等 GC
        self._pubsub = None

    # ------------------------------------------------------------------
    # 集群接入
    # ------------------------------------------------------------------

    def bind_cluster(self, redis, node_id: str) -> None:
        """接入集群模式。不调用即为单机模式：广播只走进程内，不碰 Redis。
        单机不该为了一个用不上的能力多付一次网络往返。"""
        self._redis = redis
        self._node_id = node_id

    @property
    def clustered(self) -> bool:
        return self._redis is not None and self._node_id is not None

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------

    async def connect(self, websocket: WebSocket, game_id: str):
        await websocket.accept()
        first_on_this_node = game_id not in self.active_connections
        if first_on_this_node:
            self.active_connections[game_id] = []
        self.active_connections[game_id].append(websocket)

        # 只在本节点第一条连接时登记：路由表记的是"节点"粒度，不是"连接"粒度
        if first_on_this_node and self.clustered:
            key = ROOM_NODES_KEY.format(game_id=game_id)
            try:
                await self._redis.sadd(key, self._node_id)
                # 每次登记都续期：节点崩溃时来不及摘表，靠 TTL 让整张表最终消失。
                # 一局游戏远短于 1 小时，房间还活着就一直有新连接进来续期。
                await self._redis.expire(key, ROOM_NODES_TTL)
            except Exception as e:
                logger.warning("连接路由表登记失败，该房间跨节点广播可能收不到: game=%s %s", game_id, e)

    def disconnect(self, websocket: WebSocket, game_id: str):
        """同步方法（WebSocketDisconnect 的处理路径上不便 await），
        路由表摘除交给 unregister_if_empty。"""
        if game_id in self.active_connections:
            if websocket in self.active_connections[game_id]:
                self.active_connections[game_id].remove(websocket)
            if not self.active_connections[game_id]:
                del self.active_connections[game_id]

    async def unregister_if_empty(self, game_id: str) -> None:
        """本节点已无该房间连接时，从路由表摘掉自己。
        摘不掉的后果只是多收几条无用广播，不影响正确性，所以失败只记日志。"""
        if not self.clustered or game_id in self.active_connections:
            return
        try:
            await self._redis.srem(ROOM_NODES_KEY.format(game_id=game_id), self._node_id)
        except Exception as e:
            logger.warning("连接路由表摘除失败: game=%s %s", game_id, e)

    # ------------------------------------------------------------------
    # 广播
    # ------------------------------------------------------------------

    async def broadcast(self, game_id: str, message: WSMessage):
        """向指定房间的所有连接广播消息（含其他节点上的连接）"""
        msg_json = message.model_dump_json()
        await self._send_local(game_id, msg_json)
        if self.clustered:
            await self._fanout_remote(game_id, msg_json)

    async def _send_local(self, game_id: str, msg_json: str) -> None:
        if game_id not in self.active_connections:
            return
        # 复制列表进行迭代，防止发送过程中连接断开导致修改列表报错
        for connection in list(self.active_connections[game_id]):
            try:
                await connection.send_text(msg_json)
            except Exception:
                # 发送失败通常意味着连接已断开，可以在这里处理，或者依赖 disconnect 回调
                pass

    async def _fanout_remote(self, game_id: str, msg_json: str) -> None:
        """查路由表，只发给持有该房间连接的其他节点。
        本节点已在 _send_local 直发过，不再经 Redis 绕一圈——省一次往返，
        也让"Redis 挂了本地广播还能用"成立。"""
        try:
            nodes = await self._redis.smembers(ROOM_NODES_KEY.format(game_id=game_id))
        except Exception as e:
            logger.warning("查连接路由表失败，跨节点广播本次跳过: game=%s %s", game_id, e)
            return

        targets = nodes - {self._node_id}
        if not targets:
            return

        # 刻意不在这里按"存活节点"过滤目标。
        # 曾经这么写过，是个错误：存活视图只要偏一点（刚启动还没同步、或拿到的是上一轮
        # 的旧视图），就会把活节点当成死的、连表项一起删掉，结果**广播被静默丢弃**。
        # 而多发一次给已死节点的频道是无害的——没有订阅者，Redis 直接丢。
        # 两种错误的代价完全不对等，所以宁可多发。
        # 崩溃节点的残留表项靠 key TTL 兜底（见 connect），且一个房间的成员数天然
        # 被集群规模封顶，不会无限增长。
        for node_id in targets:
            try:
                await self._redis.publish(
                    NODE_CHANNEL_KEY.format(node_id=node_id),
                    f"{game_id}\n{msg_json}",
                )
            except Exception as e:
                logger.warning("跨节点扇出失败: game=%s target=%s %s", game_id, node_id, e)

    async def broadcast_game_update(self, game_id: str, game_state: object):
        """
        广播对局状态更新消息
        前端收到此消息后，应立即调用 GET /games/{game_id} 拉取最新状态
        """
        # 注意：这里 game_state 的类型是 GameState，为了避免循环导入，用了 object
        # 实际上我们只需要 game_id，但为了扩展性可以传更多信息
        # 目前主要通知前端去拉取，所以 payload 可以简化
        try:
            update_msg = WSMessage(
                type=WebSocketOpCode.STATE_UPDATE,
                payload={
                    "game_id": game_id,
                    "phase": game_state.phase if hasattr(game_state, 'phase') else None,
                    "timestamp": getattr(game_state, 'phase_start_time', 0.0)
                }
            )
            await self.broadcast(game_id, update_msg)
        except Exception as e:
            # 广播失败不应影响主流程
            print(f"Broadcast failed: {e}")

    # ------------------------------------------------------------------
    # 订阅本节点频道
    # ------------------------------------------------------------------

    async def _subscribe_loop(self) -> None:
        """收其他节点扇出过来的消息，投给本地连接。
        收到的消息一律只做本地投递，绝不再次扇出——否则两个节点互相转发就是死循环
        （同 room_router 一跳封顶的道理）。"""
        channel = NODE_CHANNEL_KEY.format(node_id=self._node_id)
        while True:
            try:
                pubsub = self._redis.pubsub()
                self._pubsub = pubsub
                await pubsub.subscribe(channel)
                logger.info("WS 广播频道已订阅: %s", channel)
                async for raw in pubsub.listen():
                    if raw.get("type") != "message":
                        continue
                    data = raw.get("data") or ""
                    game_id, _, msg_json = data.partition("\n")
                    if msg_json:
                        await self._send_local(game_id, msg_json)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # 订阅断了必须重连：断开期间本节点上的连接会静默收不到广播
                logger.warning("WS 广播订阅中断，2s 后重连: %s", e)
                await asyncio.sleep(2)

    def start(self) -> None:
        if self.clustered and self._sub_task is None:
            self._sub_task = asyncio.create_task(self._subscribe_loop())

    async def drop_subscription(self) -> None:
        """断开当前订阅的底层连接，订阅循环会自行重连。测试模拟网络抖动用。

        提供这个方法有两个原因：
        一是别在测试里用 CLIENT KILL TYPE pubsub——那会杀掉整个 Redis 上所有的
        订阅连接，包括同时在跑的其他节点和本机开着的开发服务；
        二是只断底层 socket、不动 PubSub 对象本身。对着正在 listen 的 PubSub 调
        aclose() 会和读协程抢同一个 socket（readuntil() 冲突），还会污染共享连接池。
        """
        pubsub = self._pubsub
        conn = getattr(pubsub, "connection", None) if pubsub else None
        if conn is not None:
            try:
                await conn.disconnect()
            except Exception:
                pass

    async def stop(self) -> None:
        if self._sub_task:
            self._sub_task.cancel()
            self._sub_task = None
        if self._pubsub is not None:
            try:
                await self._pubsub.aclose()
            except Exception:
                pass
            self._pubsub = None
        # 退出时把本节点从所有房间的路由表里摘掉，否则残留条目要等下次广播才被清
        if self.clustered:
            for game_id in list(self.active_connections.keys()):
                try:
                    await self._redis.srem(
                        ROOM_NODES_KEY.format(game_id=game_id), self._node_id)
                except Exception:
                    pass


manager = ConnectionManager()
