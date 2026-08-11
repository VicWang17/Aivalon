# 这个文件是房间 Actor 框架：每个活跃房间一个执行单元，动作经队列串行处理（单写者模型）。
# 它替代了原来的 Redis 分布式锁 + 全状态 deepcopy：
#   - 锁防的是"并发改同一房间"，单写者模型下没有并发写，锁失去意义
#   - 串行化天然保证事件顺序，DEVLOG 005 的 TOCTOU 竞态在模型层面消除
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

logger = logging.getLogger("aivalon.actor")

# Actor 空闲超时：超过该时间没有新动作，Actor 退出并注销（状态仍在 games 字典，下次动作自动唤醒）
ACTOR_IDLE_TIMEOUT = 300.0

# 处理函数签名：(game_id, *args) -> Awaitable[Any]
ActorHandler = Callable[..., Awaitable[Any]]


class RoomActor:
    """房间 Actor：绑定一个 game_id，串行消费动作队列"""

    def __init__(self, game_id: str, handler: ActorHandler, on_idle_exit: Callable[[str], None]):
        self.game_id = game_id
        self._handler = handler
        self._on_idle_exit = on_idle_exit
        self._queue: asyncio.Queue[Tuple[tuple, asyncio.Future]] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    def _ensure_running(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"room-actor-{self.game_id[:8]}")

    async def submit(self, *args) -> Any:
        """投递一个动作，等待串行处理完成并返回结果（或抛出处理中的异常）"""
        self._ensure_running()
        future = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((args, future))
        return await future

    async def _run(self):
        while True:
            try:
                args, future = await asyncio.wait_for(self._queue.get(), timeout=ACTOR_IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                # 空闲超时：退出并注销（休眠）；下次 submit 会重建 Actor
                self._on_idle_exit(self.game_id)
                return
            try:
                result = await self._handler(self.game_id, *args)
                if not future.done():
                    future.set_result(result)
            except Exception as e:
                # 单个动作失败不影响队列后续动作
                if not future.done():
                    future.set_exception(e)
            finally:
                self._queue.task_done()


class ActorManager:
    """房间 Actor 注册表：game_id -> RoomActor，按需创建"""

    def __init__(self):
        self._actors: Dict[str, RoomActor] = {}

    def get_or_create(self, game_id: str, handler: ActorHandler) -> RoomActor:
        actor = self._actors.get(game_id)
        if actor is None:
            actor = RoomActor(game_id, handler, on_idle_exit=self._deregister)
            self._actors[game_id] = actor
        return actor

    def _deregister(self, game_id: str):
        self._actors.pop(game_id, None)

    @property
    def active_count(self) -> int:
        return len(self._actors)

    @property
    def game_ids(self) -> list:
        """本进程当前驻留的房间。房间路由的排障依据：状态活在哪个进程内存里，
        看的就是这个列表——转发正确的话，一个房间只应出现在归属节点上。"""
        return list(self._actors.keys())


actor_manager = ActorManager()
