from app.core.celery_app import celery_app
from app.schemas.game import GameState, PlayerState
from app.models.game_enums import Camp, Character, GamePhase
from app.models.user import User
from app.services.rank_service import RankService
from app.db.base import SessionLocal
from app.core.config import settings
import redis.asyncio as redis
from typing import List, Optional
import asyncio

# 坏人角色集合
EVIL_CHARACTERS = {"ASSASSIN", "MORGANA", "MINION"}

@celery_app.task(bind=True, queue="stats_queue", max_retries=3)
def process_game_result(self, game_id: str, winner: str, players_data: List[dict]):
    """
    异步处理游戏结束后的统计任务：
    1. 更新 User 表胜场统计
    2. 更新 Redis 排行榜
    3. 更新最近对局缓存
    """
    print(f"[Stats Task] Processing game result for {game_id}, winner: {winner}")
    
    db = SessionLocal()
    try:
        # 1. 更新用户胜场统计
        # 先获取所有用户
        user_ids = [p["user_id"] for p in players_data if not p.get("is_ai", True)] # 只统计真实玩家
        users = db.query(User).filter(User.id.in_(user_ids)).all()
        user_map = {u.id: u for u in users}
        
        # 转换 winner
        winner_camp = Camp(winner)

        for p_data in players_data:
            # 只有真实玩家才计入 UserStats
            if p_data.get("is_ai", True):
                continue
                
            user = user_map.get(p_data["user_id"])
            if not user:
                continue
            
            # 更新总场次
            user.total_games = (user.total_games or 0) + 1
            
            # 判定阵营
            # p_data["character"] 是字符串
            char_str = p_data["character"]
            is_evil = char_str in EVIL_CHARACTERS
            
            # 判定胜负
            is_winner = False
            if winner_camp == Camp.GOOD and not is_evil:
                is_winner = True
            elif winner_camp == Camp.EVIL and is_evil:
                is_winner = True
                
            if is_winner:
                user.total_wins = (user.total_wins or 0) + 1
                if is_evil:
                    user.wins_evil = (user.wins_evil or 0) + 1
                else:
                    user.wins_good = (user.wins_good or 0) + 1
            
            db.add(user)
        
        db.commit()
        print(f"[Stats Task] Updated user stats for game {game_id}")
        
        # 2. 更新排行榜 & 最近对局 (调用 RankService)
        # 还原 PlayerState 对象列表
        restored_players = []
        for p in players_data:
            # 构造 PlayerState
            # 注意：p["character"] 是字符串，需要转成枚举
            player = PlayerState(
                user_id=p["user_id"],
                username=p["username"],
                seat_id=p["seat_id"],
                is_ai=p.get("is_ai", False),
                character=Character(p["character"]),
                is_connected=p.get("is_connected", True),
                has_voted=p.get("has_voted", False),
                has_acted=p.get("has_acted", False)
            )
            restored_players.append(player)
            
        # 构造最小化 GameState
        fake_game_state = GameState(
            game_id=game_id,
            phase=GamePhase.FINISHED,
            phase_start_time=0.0,
            players=restored_players,
            leader_id=0,
            speaker_id=0,
            winner=winner_camp,
            # 其他必填字段给默认值
            round=1,
            vote_track=0,
            mission_results=[],
            vote_history=[],
            mission_history=[],
            speech_history=[],
            proposed_team=[],
            pending_mission_results=[],
            votes={}
        )
        
        # 在同步环境中运行异步代码
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def run_stats():
            redis_conn = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                password=settings.REDIS_PASSWORD,
                decode_responses=True,
                encoding="utf-8"
            )
            try:
                await RankService.update_after_game_finish(fake_game_state, winner, redis_conn)
            finally:
                await redis_conn.close()

        loop.run_until_complete(run_stats())
        loop.close()
        
        print(f"[Stats Task] Updated rank and recent games for {game_id}")

    except Exception as e:
        print(f"[Stats Task] Error processing game {game_id}: {e}")
        db.rollback()
        # 抛出异常让 Celery 重试
        raise self.retry(exc=e, countdown=5)
    finally:
        db.close()

