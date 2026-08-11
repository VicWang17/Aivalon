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
from app.core import metrics
from app.schemas.protocol import WSMessage, WebSocketOpCode

logger = logging.getLogger("aivalon.ws")

# 同 Tick 合并帧的窗口（秒）。
# 这里没有全局 tick 循环，动作是事件驱动的，所以"同 Tick"落地成一个合并窗口：
# 窗口内同一房间的多个 STATE_UPDATE 只下发最后一帧。
# 取 50ms：人眼分辨不出，而一次人类动作触发的 AI 连锁刚好落在这个量级内。
TICK_INTERVAL = 0.05

# 可以合并的消息类型。
# STATE_UPDATE 的 payload 只是"去拉最新状态"的通知，同一房间连发 N 帧与发 1 帧
# 对客户端完全等价，所以可以安全丢掉中间帧。
# 其余类型都带独有信息（AI_THINKING 带 player_id、ERROR 带原因），合并就是丢信息。
COALESCIBLE = frozenset({WebSocketOpCode.STATE_UPDATE})

# 每条连接的写缓冲上限（帧数）。超过即判定为慢消费者，主动断开。
# 取 32：配合 50ms 合并窗口，32 帧约等于 1.6s 的积压量。偶发网络抖动填不满，
# 能填满说明客户端真的跟不上了（或者根本不读了）。
SEND_QUEUE_MAX = 32


class _Conn:
    """一条连接 + 它自己的写缓冲和写协程。

    为什么每条连接要单独一个队列和协程：原来的写法是在广播循环里逐个
    `await ws.send_text()`。客户端不读数据时 TCP 窗口会填满，那个 await 就会挂住，
    **整个房间的广播卡在这一个慢客户端上**（队头阻塞）。
    改成"广播只往队列里塞、写协程负责真正发出去"，广播路径就再也不会等socket。
    """

    __slots__ = ("ws", "game_id", "queue", "task", "dropped")

    def __init__(self, ws: WebSocket, game_id: str):
        self.ws = ws
        self.game_id = game_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=SEND_QUEUE_MAX)
        self.task: Optional[asyncio.Task] = None
        self.dropped = False

    async def offer(self, msg_json: str) -> bool:
        """入队，返回 False 表示这条连接真的跟不上了。

        满了不能立刻判死刑：**某一瞬间缓冲是满的，不代表消费者慢**。
        广播循环里连续塞几十帧时中间没有任何真正的挂起点，写协程根本没被调度过，
        缓冲自然是满的——这时判死会把健康连接一起误杀（实测 96 帧全部误杀）。
        所以先让出一次事件循环给写协程一个排水的机会，再试一次：
          - 消费者健康：写协程这一让就把队列抽空了，第二次入队成功
          - 消费者卡死：写协程正挂在 send 上，让也没用，第二次仍失败 → 判定为慢消费者
        `sleep(0)` 只等一个事件循环 tick，不依赖任何 socket，不会把队头阻塞放回来。
        """
        try:
            self.queue.put_nowait(msg_json)
            return True
        except asyncio.QueueFull:
            pass
        await asyncio.sleep(0)
        try:
            self.queue.put_nowait(msg_json)
            return True
        except asyncio.QueueFull:
            return False

    async def _writer(self) -> None:
        """单协程顺序发送：保证同一连接上的帧序与入队顺序一致"""
        while True:
            msg_json = await self.queue.get()
            try:
                await self.ws.send_text(msg_json)
            except Exception:
                # 发送失败基本等于连接已断，退出写协程，清理交给 disconnect
                self.queue.task_done()
                return
            self.queue.task_done()

    def start(self) -> None:
        self.task = asyncio.create_task(self._writer())

    def stop(self) -> None:
        if self.task:
            self.task.cancel()
            self.task = None

# 房间 → 持有其连接的节点集合
ROOM_NODES_KEY = "aivalon:ws:rooms:{game_id}"
# 节点专属广播频道
NODE_CHANNEL_KEY = "aivalon:ws:node:{node_id}"
# 连接路由表兜底过期时间。节点崩溃时来不及摘表，只能靠 TTL 让残留条目最终消失。
# 取 1 小时：远长于一局游戏，房间活着就会被不断续期；房间死了一小时后自然清掉。
ROOM_NODES_TTL = 3600


class ConnectionManager:
    def __init__(self):
        # 存储格式: {game_id: [_Conn, _Conn, ...]}
        # 存的是 _Conn 而不是裸 WebSocket：每条连接要带自己的写缓冲，
        # 否则一个慢客户端会卡住整个房间的广播
        self.active_connections: Dict[str, List["_Conn"]] = {}
        self._redis = None
        self._node_id: Optional[str] = None
        self._sub_task: Optional[asyncio.Task] = None
        # 存下当前订阅句柄：stop() 要显式关掉它，否则连接只能等 GC
        self._pubsub = None
        # 合并窗口内挂起的最后一帧：{game_id: msg_json}
        self._pending: Dict[str, str] = {}
        # 每个房间最多一个在跑的窗口定时器
        self._tick_tasks: Dict[str, asyncio.Task] = {}

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
        conn = _Conn(websocket, game_id)
        conn.start()
        self.active_connections[game_id].append(conn)

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
        conns = self.active_connections.get(game_id)
        if conns is None:
            return
        for conn in list(conns):
            if conn.ws is websocket:
                conn.stop()          # 写协程必须停，否则连接没了协程还挂在那儿
                conns.remove(conn)
        if not conns:
            del self.active_connections[game_id]

    def _drop(self, conn: "_Conn") -> None:
        """摘掉一条连接并异步关闭它。

        关闭动作丢进后台 Task 而不是在这里 await：慢消费者的 socket 本来就写不动，
        await 它的 close 等于把广播路径重新卡回去——那就白做背压了。
        """
        conn.stop()
        conns = self.active_connections.get(conn.game_id)
        if conns and conn in conns:
            conns.remove(conn)
            if not conns:
                del self.active_connections[conn.game_id]
        asyncio.create_task(self._close_quietly(conn))

    async def _drain_all(self, timeout: float = 1.0) -> None:
        """等所有写缓冲排空，最多等 timeout。

        必须有超时：慢消费者的队列永远排不空，无限等会把整个进程的退出流程挂住。
        """
        waits = [c.queue.join()
                 for conns in self.active_connections.values() for c in conns]
        if not waits:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*waits), timeout=timeout)
        except Exception:
            pass

    @staticmethod
    async def _close_quietly(conn: "_Conn") -> None:
        try:
            await conn.ws.close(code=1013)   # 1013 Try Again Later：告诉客户端可以重连
        except Exception:
            pass

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
        """向指定房间的所有连接广播消息（含其他节点上的连接）。

        STATE_UPDATE 走 TICK_INTERVAL 合并窗口，窗口内只下发最后一帧；
        其余类型立即下发。合并在源头做，跨节点也就只发一帧而不是 N 帧。
        """
        msg_json = message.model_dump_json()
        if message.type in COALESCIBLE:
            self._enqueue(game_id, msg_json)
            return
        # 不可合并的帧要立即发，但不能越过已挂起的帧——那样客户端会先收到
        # "AI 正在思考"再收到上一次的状态更新，顺序反了
        await self._flush(game_id)
        await self._dispatch(game_id, msg_json)

    def _enqueue(self, game_id: str, msg_json: str) -> None:
        """放入合并窗口。窗口里已有一帧就直接顶掉它——被顶掉的那帧永远不会下发。"""
        if game_id in self._pending:
            metrics.ws_frames_merged.inc()
        self._pending[game_id] = msg_json
        if game_id not in self._tick_tasks:
            self._tick_tasks[game_id] = asyncio.create_task(self._tick(game_id))

    async def _tick(self, game_id: str) -> None:
        try:
            await asyncio.sleep(TICK_INTERVAL)
        except asyncio.CancelledError:
            return
        # 先注销自己再 flush：flush 里有 await，期间新来的帧必须能开一个新窗口，
        # 否则那一帧会挂在 _pending 里等不到任何定时器来发它
        self._tick_tasks.pop(game_id, None)
        try:
            await self._flush(game_id)
        except Exception as e:
            logger.warning("合并帧下发失败: game=%s %s", game_id, e)

    async def _flush(self, game_id: str) -> None:
        msg_json = self._pending.pop(game_id, None)
        if msg_json is not None:
            await self._dispatch(game_id, msg_json)

    async def _dispatch(self, game_id: str, msg_json: str) -> None:
        metrics.ws_frames_sent.inc()
        await self._send_local(game_id, msg_json)
        if self.clustered:
            await self._fanout_remote(game_id, msg_json)

    async def _send_local(self, game_id: str, msg_json: str) -> None:
        """只往各连接的写缓冲里塞，不等 socket。

        这里一律非阻塞：广播路径上一旦 await 某条 socket，那条连接读得慢就会
        拖住整个房间（队头阻塞）。真正的发送由每条连接自己的写协程负责。
        """
        conns = self.active_connections.get(game_id)
        if not conns:
            return
        # 复制列表进行迭代：慢消费者会在循环里被摘掉，边遍历边改会漏元素
        for conn in list(conns):
            if await conn.offer(msg_json):
                continue
            # 缓冲塞满 = 客户端跟不上或根本不读了。留着它只会一直吃内存，
            # 而且它永远追不上——积压的帧本身就已经是过期状态。直接断开，
            # 让客户端重连后一次拉到最新状态，比慢慢补一堆旧帧更快也更省。
            conn.dropped = True
            metrics.ws_slow_consumers_dropped.inc()
            logger.warning("慢消费者断开: game=%s 写缓冲积压超过 %d 帧", game_id, SEND_QUEUE_MAX)
            self._drop(conn)

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
        # 挂起的帧先发掉再收摊：合并窗口最多只欠 TICK_INTERVAL 的量，
        # 直接丢掉等于让客户端停在一个过期状态上
        for game_id in list(self._tick_tasks.keys()):
            self._tick_tasks.pop(game_id).cancel()
        for game_id in list(self._pending.keys()):
            try:
                await self._flush(game_id)
            except Exception:
                pass
        # 给写协程一次把队列排空的机会：上面 flush 只是把帧塞进了缓冲，
        # 真正发出去是写协程的事，直接 cancel 等于白 flush 一场
        await self._drain_all()
        # 再停掉所有写协程，否则进程退出时它们还挂在 queue.get() 上
        for conns in self.active_connections.values():
            for conn in conns:
                conn.stop()
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
