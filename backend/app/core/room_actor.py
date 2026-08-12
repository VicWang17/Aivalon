# 这个文件是房间 Actor 框架：每个活跃房间一个执行单元，动作经队列串行处理（单写者模型）。
# 它替代了原来的 Redis 分布式锁 + 全状态 deepcopy：
#   - 锁防的是"并发改同一房间"，单写者模型下没有并发写，锁失去意义
#   - 串行化天然保证事件顺序，DEVLOG 005 的 TOCTOU 竞态在模型层面消除
#
# 分层限流的第三层（资源层）就落在这里。前两层管的是"谁能进来"——网关层看系统总容量、
# 应用层看单用户配额——但**两层全通过的流量照样能压死一个房间**：同一房间的动作全排在
# 一个 Actor 队列里串行执行，8 个人（或 8 个脚本）对着一局猛点，每个人都没超自己的配额，
# 队列却能排到几千个，而排在队尾的人要等前面全部跑完。这是个纯粹的**资源维度**问题，
# 按用户或按 IP 都统计不出来，只能在持有该资源的地方设上界。
#
# 于是两条上界，缺一不可：
#   - 队列长度有限：满了当场拒，不排进去（排进去就是把延迟无上限地攒起来）
#   - 单个动作的等待有限：超时就不等了（否则一个卡住的动作能挂住所有等待方）
import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from app.core import metrics
from app.core.config import settings

logger = logging.getLogger("aivalon.actor")

# Actor 空闲超时：超过该时间没有新动作，Actor 退出并注销（状态仍在 games 字典，下次动作自动唤醒）
ACTOR_IDLE_TIMEOUT = 300.0

# 处理函数签名：(game_id, *args) -> Awaitable[Any]
ActorHandler = Callable[..., Awaitable[Any]]


class RoomOverloaded(Exception):
    """房间队列已满，动作没有入队（所以一定没有生效，可以安全重试）"""

    def __init__(self, game_id: str, depth: int):
        super().__init__(f"room {game_id} queue full ({depth})")
        self.game_id = game_id
        self.depth = depth


class RoomActionTimeout(Exception):
    """等待超时，提交方不再等。

    注意语义比上面那个弱：动作**可能已经开始执行了**，服务端无法断言它没生效。
    还没出队的会被丢弃（见 _run），已经在跑的拦不住。
    """

    def __init__(self, game_id: str, waited: float):
        super().__init__(f"room {game_id} action timed out after {waited}s")
        self.game_id = game_id
        self.waited = waited


class RoomActor:
    """房间 Actor：绑定一个 game_id，串行消费动作队列"""

    def __init__(self, game_id: str, handler: ActorHandler,
                 on_idle_exit: Callable[["RoomActor"], None],
                 max_queue: Optional[int] = None,
                 action_timeout: Optional[float] = None,
                 idle_timeout: Optional[float] = None):
        self.game_id = game_id
        self._handler = handler
        self._on_idle_exit = on_idle_exit
        self._max_queue = settings.ROOM_QUEUE_MAX if max_queue is None else max_queue
        self._timeout = settings.ROOM_ACTION_TIMEOUT if action_timeout is None else action_timeout
        self._idle_timeout = ACTOR_IDLE_TIMEOUT if idle_timeout is None else idle_timeout
        self._queue: asyncio.Queue[Tuple[tuple, asyncio.Future]] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._closed = False

    @property
    def closed(self) -> bool:
        """已因空闲退出。注册表据此不再把它交出去（见 ActorManager.get_or_create）"""
        return self._closed

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def _ensure_running(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"room-actor-{self.game_id[:8]}")

    async def submit(self, *args, timeout: Optional[float] = None) -> Any:
        """投递一个动作，等待串行处理完成并返回结果（或抛出处理中的异常）。

        两条上界都在这里：队列满抛 `RoomOverloaded`（压根不入队），
        等待超过 timeout 抛 `RoomActionTimeout`（不再等，但动作可能已经在跑）。
        """
        # **先判满再入队**：用有界 asyncio.Queue 靠 await put 挡也能限长度，但那是
        # "让提交方排在队列外面等"，队伍只是从看得见的地方挪到了看不见的地方，
        # 延迟照样无上限。要的是当场拒绝，让上游立刻知道这个房间过载了。
        if self._queue.qsize() >= self._max_queue:
            metrics.room_overload.labels(reason="queue_full").inc()
            logger.warning("房间 %s 队列已满（%d），拒绝新动作", self.game_id, self._queue.qsize())
            raise RoomOverloaded(self.game_id, self._queue.qsize())

        self._ensure_running()
        future = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((args, future))

        wait = self._timeout if timeout is None else timeout
        try:
            # wait_for 超时会**取消它等待的那个 future**，这正是 _run 里判断
            # "提交方还在不在"的依据，所以别换成 gather/shield
            return await asyncio.wait_for(future, timeout=wait)
        except asyncio.TimeoutError:
            metrics.room_overload.labels(reason="timeout").inc()
            logger.warning("房间 %s 动作等待超过 %.1fs，放弃等待", self.game_id, wait)
            raise RoomActionTimeout(self.game_id, wait) from None

    async def _next(self) -> Optional[Tuple[tuple, asyncio.Future]]:
        """取下一个动作；空闲超过阈值返回 None（调用方据此休眠退出）"""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=self._idle_timeout)
        except asyncio.TimeoutError:
            return None

    async def _run(self):
        while True:
            item = await self._next()
            if item is None:
                # 空闲退出前必须再看一眼队列：**"判定空闲"和"新动作入队"能撞在一起**。
                # wait_for 的超时回调已经把 get 取消掉了，紧接着 put_nowait 把动作放进队列，
                # 于是这个动作没人取、它的 future 永远不完成——提交方在有超时之前是**永久挂住**。
                # 这里 continue 掉，交给下一轮取走。
                if not self._queue.empty():
                    continue
                self._closed = True
                self._on_idle_exit(self)
                return

            args, future = item
            # 提交方已经等超时走了（wait_for 取消了它给出的 future）：这个动作还没开始跑，
            # 那就别跑。**超时返回给客户端的必须尽量是"这次没做成"**——否则它按超时重试一次，
            # 服务端又把排队里的那次也执行了，一个动作生效两遍。
            # 这也是队列上界的另一半价值：过载时队里堆的多半都是已经没人等的动作，
            # 挨个执行完只是在给一个已经过载的房间继续加活。
            if future.done():
                self._queue.task_done()
                continue

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
        # closed 的不能再交出去：它的 _run 已经返回，拿到它的调用方会重新
        # create_task 把它救活，于是同一房间存在两个写者——单写者模型就破了
        if actor is None or actor.closed:
            actor = RoomActor(game_id, handler, on_idle_exit=self._deregister)
            self._actors[game_id] = actor
        return actor

    def _deregister(self, actor: RoomActor):
        # **身份比对，不能按 game_id 盲删**：老 Actor 的空闲退出回调可能晚于新 Actor 建好
        # （同一房间又来动作了），按 id 直接 pop 会把**正在用的**那个删掉，
        # 下次动作再建第三个，同一房间短暂存在两个写者。
        if self._actors.get(actor.game_id) is actor:
            self._actors.pop(actor.game_id, None)

    @property
    def active_count(self) -> int:
        return len(self._actors)

    @property
    def game_ids(self) -> list:
        """本进程当前驻留的房间。房间路由的排障依据：状态活在哪个进程内存里，
        看的就是这个列表——转发正确的话，一个房间只应出现在归属节点上。"""
        return list(self._actors.keys())


actor_manager = ActorManager()
