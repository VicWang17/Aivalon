from app.core.celery_app import celery_app
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

@celery_app.task(bind=True, queue="ai_queue", max_retries=3, rate_limit='60/m')
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
                     return None
        
                # 2. 找到当前玩家
                player = next((p for p in game_state.players if p.user_id == player_id), None)
                if not player:
                    print(f"[AI Task] Player {player_id} not found in game {game_id}")
                    return None
                    
                # 3. AI 决策
                action = await AIService.get_action(game_state, player)
                return action
            finally:
                await redis_conn.close()

        action = loop.run_until_complete(run_ai_logic())
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
                error_msg = f"API Error {resp.status_code}: {resp.text}"
                print(f"[AI Task] {error_msg}")
                raise Exception(error_msg)
            
            print(f"[AI Task] Action submitted successfully")

    except Exception as e:
        print(f"[AI Task] Error: {e}")
        db.rollback()
        # 只有在非 API 错误（如网络问题、LLM 错误）时才重试
        # 如果是 403/400 等业务错误，重试可能无意义
        raise self.retry(exc=e, countdown=3)
    finally:
        db.close()
