from app.core.celery_app import celery_app
from app.core.config import settings
from app.services.game_service import GameService
from app.services.ai_service import AIService
from app.db.base import SessionLocal
from app.models.game import Game
from app.schemas.game import GameState, PlayerState
from app.models.game_enums import Character, Camp, GamePhase
from app.core.config import settings
from app.core import ai_queue
from app.core.redis import redis_pool
import redis.asyncio as redis
import asyncio
import json
import requests


class AIActionRejected(Exception):
    """回调提交动作被拒。**带着状态码**，因为要不要重试全看它。"""

    def __init__(self, status_code: int, body: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(f"API Error {status_code}: {body[:200]}")


# 服务端说"稍后再来"的那些码。**429/503 是唯一两个真正值得重试的 HTTP 状态**：
# 它们的语义就是"这次不行，等等再来"，而且都带 `Retry-After`。
# 500 刻意不在里面：那是"我们坏了"，重试只是把一个 bug 打成五份流量。
RETRYABLE_STATUS = frozenset({429, 503})


def _is_retryable(exc: Exception) -> bool:
    """判据是**"时间会不会改变答案"**，不是"错误严重不严重"。

    原来这里没有判据——任何非 200 都 `raise Exception` 一路走到指数退避五轮。
    S4 run C 的代价是 18,437 次注定失败的回调（403 一万一千次 + 400 七千次），
    它们和真实玩家动作抢同一条链路、同一个准入桶，**还享受着刚给内部回调开的豁免**。
    **给一条链路开绿灯的前提，是那条链路上跑的东西是有意义的。**

    - 429 / 503 / 超时 / 连接失败 —— 等一下有意义，重试
    - 400 / 403 / 404 / 422 —— **等到宇宙终结也是同一个回答**，一次都不该重
      （「您已经投过票了」等两分钟还是投过了；「您不在执行队伍中」永远不在）

    这和 044 是同一形状但方向相反：044 是压测驱动器把 **429（临时）当成永久**，
    丢掉进度重建对局；这里是 worker 把 **403/400（永久）当成临时**，重试五轮。
    **两个方向相反的误判产生同一个后果：重试放大。**
    大白话：一个人被告知"你不在这支队伍里"，于是每隔两秒再问一遍，问了五遍；
    另一个人被告知"稍等两分钟"，于是立刻从队尾重新排队还顺手领了张新号。
    一个该走不走，一个该等不等。

    默认方向是**重试**（未知异常按临时处理）：这里的未知多半是环境抖动
    （Redis、DB、事件循环），那些等一下确实会好。**已知的永久错误只有一份短名单，
    把它列出来比试图穷举"哪些是临时的"可靠**——名单漏一项的代价是多重试几次，
    而反过来漏一项的代价是那个 AI 回合被永久放弃、房间卡住。
    """
    if isinstance(exc, AIActionRejected):
        # **白名单必须先判**：429 也是 4xx，先按"4xx 不重试"筛就会把它一起毙掉——
        # 而它恰恰是最该重试的那一个。这条顺序写错的话，这个函数就会犯下
        # 它本来要修的那个错误的**镜像版本**：把临时的当成永久的（同 044）。
        if exc.status_code in RETRYABLE_STATUS:
            return True
        # 其余 4xx 一律不重试；5xx 也不重（500 是"我们坏了"，重试只是把一个 bug
        # 打成五份流量，它该在曲线上又快又响地暴露出来）。
        return False
    if isinstance(exc, requests.exceptions.RequestException):
        return True          # 超时、连接失败：等一下有意义
    return True


@celery_app.task(bind=True, queue="ai_queue", max_retries=5, rate_limit=settings.AI_TASK_RATE_LIMIT)
def process_ai_turn(self, game_id: str, player_id: int, queue_token: str = "",
                    claim_key: str = ""):
    """
    处理 AI 玩家的回合

    `queue_token`：投递侧登记的"在飞任务"凭据（见 core/ai_queue.py），跑完注销。
    `claim_key`：投递侧的幂等键（只有 VOTE / MISSION 两个阶段有），**放弃这个回合时
    必须把它放掉**，否则没人会再投递它、房间推不动——见下面 `finally` 里的说明。
    **给默认值是为了兼容已经躺在队列里的老任务**——改任务签名时队列里可能还有
    上一版投递的消息，少一个参数就是一批 TypeError 直接进死信队列。
    """
    print(f"[AI Task] Processing turn for game {game_id}, player {player_id}")
    
    # 0. Broadcast thinking state (Best effort)
    api_base = "http://localhost:8000/api/v1"
    try:
        requests.post(
            f"{api_base}/games/{game_id}/ai_thinking",
            json={"player_id": player_id},
            headers={"X-Internal-Secret": settings.SECRET_KEY},
            timeout=2
        )
    except Exception as e:
        print(f"[AI Task] Failed to broadcast thinking state: {e}")

    db = SessionLocal()
    retrying = False
    failed = False
    try:
        # 运行异步代码
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def run_ai_logic():
            # 创建绑定到当前事件循环的 Redis 客户端
            # 不使用全局连接池，因为 Celery 任务每次创建新的 EventLoop，
            # 全局连接池可能会复用绑定到已关闭 Loop 的连接，导致 "Event loop is closed" 错误
            redis_conn = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                password=settings.REDIS_PASSWORD,
                decode_responses=True,
                encoding="utf-8"
            )
            try:
                # 队列积压到阈值就摘掉 LLM 走规则引擎。**判断放在 worker 里而不是投递侧**：
                # 投递到执行之间隔着排队，积压期间那段间隔正是最长的——
                # 用投递那一刻的深度决定，等于拿几十秒前的旧情况做现在的决定。
                depth = await ai_queue.depth(redis_conn)
                degrade = ai_queue.should_degrade(depth)
                if degrade:
                    ai_queue.note_degraded()
                    print(f"[AI Task] 队列深度 {depth} 已达阈值，本回合走规则引擎")

                # 1. 恢复游戏状态
                game_state = await GameService.restore_game_state(game_id, db, redis_conn=redis_conn)
                
                if not game_state:
                     print(f"[AI Task] Could not restore state for {game_id}")
                     # 无法恢复状态属于致命错误，重试通常无用，但也可能是数据库临时问题
                     # 这里我们选择抛出异常让其重试几次，最终进入死信队列供排查
                     raise ValueError(f"Game state not found for {game_id}")
        
                # 2. 找到当前玩家
                player = next((p for p in game_state.players if p.user_id == player_id), None)
                if not player:
                    print(f"[AI Task] Player {player_id} not found in game {game_id}")
                    # 玩家不存在也是致命错误
                    raise ValueError(f"Player {player_id} not found")
                    
                # 3. AI 决策
                # 把这个 loop 自己的 redis 连接传下去：AI 要读降级开关，
                # 而全局单例客户端在这里会踩到"绑到已关闭 loop 的连接"（同上面那段注释）
                action = await AIService.get_action(game_state, player,
                                                   redis_conn=redis_conn,
                                                   force_fallback=degrade)
                return action
            finally:
                await redis_conn.close()

        try:
            action = loop.run_until_complete(run_ai_logic())
        finally:
            loop.close()
        
        # 4. 提交动作
        if action:
            print(f"[AI Task] Action decided: {action['action_type']}")
            
            # 通过内部 API 回调 Web 进程执行动作
            # 这样可以利用 Web 进程的 WebSocket 广播能力，并保证状态更新的一致性
            # 假设 API 运行在本地 8000 端口
            # 生产环境应通过环境变量配置 API 地址
            api_base = "http://localhost:8000/api/v1"
            url = f"{api_base}/games/{game_id}/ai_action"
            headers = {"X-Internal-Secret": settings.SECRET_KEY}
            payload = {
                "player_id": player_id,
                "action_type": action["action_type"],
                "payload": action["payload"]
            }
            
            resp = requests.post(url, json=payload, headers=headers, timeout=10)

            if resp.status_code != 200:
                # 状态码要**带着**上抛，不能拍平成一句字符串：下面的重试判据全靠它。
                # 原来这里是 `raise Exception(f"API Error {code}: ...")`，
                # 于是 except 里只剩一段人看的文本，代码没法再分辨该不该重试。
                raise AIActionRejected(resp.status_code, resp.text)

            print(f"[AI Task] Action submitted successfully")

    except Exception as e:
        print(f"[AI Task] Error processing game {game_id}: {e}")
        db.rollback()

        # **重试的判据是"时间会不会改变答案"**，不是"错误严重不严重"。
        # 见 _is_retryable 上方的说明。
        if not _is_retryable(e):
            failed = True
            print(f"[AI Task] 永久性错误，不重试: {e}")
            raise

        # 重试用尽也是"彻底放弃"，收尾方式和永久错误一样（注销深度 + 放掉幂等键）。
        # **判在调用 `self.retry` 之前**：传了 `exc=` 的时候 Celery 不抛
        # `MaxRetriesExceededError`，而是把**原异常**重新抛出来
        # （`Task.retry` 里的 `raise_with_context(exc)`），所以 `except` 那条捕不到。
        # 数着次数判反而是精确的：Celery 的条件是 `request.retries + 1 > max_retries`。
        # 原来这条路径上 `retrying` 一直是 True，于是深度里那一笔只能等 120s 租约
        # 兜掉——**放弃得越多，深度虚高得越久，而深度正是降级的判据**。
        if self.request.retries >= self.max_retries:
            failed = True
            print(f"[AI Task] 重试已用尽（{self.max_retries} 次），放弃这个回合: {e}")
            raise

        # 指数退避重试策略
        # 第1次: 2s, 第2次: 4s, 第3次: 8s, 第4次: 16s, 第5次: 32s
        retry_delay = 2 ** (self.request.retries + 1)
        print(f"[AI Task] Retrying in {retry_delay}s (Attempt {self.request.retries + 1}/{self.max_retries})...")

        # 重试意味着这个 AI 回合**还没完成**，所以不注销（下面的 finally 据此跳过）：
        # 注销了的话，等待重试的这段时间它不算在深度里，而正在重试的任务恰恰是
        # 积压的一部分——**漏算会让深度在最需要它的时候偏小**。
        # 幂等键同理不放：重试中的回合已经投出去了，放了键就会被再投一次。
        retrying = True
        raise self.retry(exc=e, countdown=retry_delay)
    finally:
        db.close()
        if not retrying:
            ai_queue.leave_sync(queue_token)
            # 这个回合不再重试了——如果它是**失败**收尾的，幂等键必须放掉，
            # 否则那个 AI 在这个阶段再也不会被投递，房间永远推不动。
            # 成功收尾时放不放都不影响正确性（`has_voted` / `has_acted` 已经翻了，
            # 投递侧的扫描条件那道闸自己就挡住了），但**一起放掉更省事也更保险**：
            # 少一类"要区分成功与失败"的分支，就少一处判错的地方。
            if failed:
                ai_queue.release_claim_sync(claim_key)
