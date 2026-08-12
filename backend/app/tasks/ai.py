from app.core.celery_app import celery_app
from app.core.config import settings
from app.services.game_service import GameService
from app.services.ai_service import AIService
from app.db.base import SessionLocal
from app.models.game import Game
from app.schemas.game import GameState, PlayerState
from app.models.game_enums import Character, Camp, GamePhase
from app.core.config import settings
from app.core.redis import redis_pool
import redis.asyncio as redis
import asyncio
import json
import requests

@celery_app.task(bind=True, queue="ai_queue", max_retries=5, rate_limit=settings.AI_TASK_RATE_LIMIT)
def process_ai_turn(self, game_id: str, player_id: int):
    """
    处理 AI 玩家的回合
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
                                                   redis_conn=redis_conn)
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
                # 如果是 4xx 错误，可能是业务逻辑问题，重试可能无用，但为了保险起见还是抛出
                error_msg = f"API Error {resp.status_code}: {resp.text}"
                print(f"[AI Task] {error_msg}")
                raise Exception(error_msg)
            
            print(f"[AI Task] Action submitted successfully")

    except Exception as e:
        print(f"[AI Task] Error processing game {game_id}: {e}")
        db.rollback()
        
        # 指数退避重试策略
        # 第1次: 2s, 第2次: 4s, 第3次: 8s, 第4次: 16s, 第5次: 32s
        retry_delay = 2 ** (self.request.retries + 1)
        print(f"[AI Task] Retrying in {retry_delay}s (Attempt {self.request.retries + 1}/{self.max_retries})...")
        
        raise self.retry(exc=e, countdown=retry_delay)
    finally:
        db.close()
