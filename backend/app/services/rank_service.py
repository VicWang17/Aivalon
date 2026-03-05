# 这个文件是排行榜和最近对局缓存服务，负责管理 Redis 中的热点数据。
import json
from typing import List, Optional, Dict, Any
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from app.core.redis import redis_client
from app.models.user import User
from app.models.game import Game
from app.schemas.game import GameState, GamePhase
from app.db.base import SessionLocal
import asyncio

# Redis Keys
KEY_LEADERBOARD_TOTAL = "leaderboard:total_wins"
KEY_LEADERBOARD_GOOD = "leaderboard:wins_good"
KEY_LEADERBOARD_EVIL = "leaderboard:wins_evil"
KEY_RECENT_GAMES = "global:recent_games"
MAX_RECENT_GAMES = 50

class RankService:
    @staticmethod
    async def update_after_game_finish(game_state: GameState, winner: str, redis_conn=None):
        """
        游戏结束时调用：更新排行榜和最近对局缓存
        """
        # 1. 更新最近对局列表
        await RankService._add_recent_game(game_state, winner, redis_conn)
        
        # 2. 更新排行榜 (全量更新或增量更新)
        # 鉴于 User 表已经更新了 total_wins，我们可以直接查询最新的 User 数据来更新 ZSET
        await RankService._update_leaderboard_for_users(game_state, redis_conn)

    @staticmethod
    async def _add_recent_game(game: GameState, winner: str, redis_conn=None):
        """
        将游戏摘要推入 Redis List
        """
        client = redis_conn or redis_client
        
        # 构建摘要
        summary = {
            "id": game.game_id,
            "winner": winner, # "good" or "evil"
            "created_at": datetime.now().isoformat(), # 使用当前时间作为完成时间
            "player_count": len(game.players),
        }
        
        # 序列化
        data = json.dumps(summary)
        
        # LPUSH + LTRIM
        async with client.pipeline() as pipe:
            pipe.lpush(KEY_RECENT_GAMES, data)
            pipe.ltrim(KEY_RECENT_GAMES, 0, MAX_RECENT_GAMES - 1)
            await pipe.execute()

    @staticmethod
    async def _update_leaderboard_for_users(game: GameState, redis_conn=None):
        """
        更新参与本局玩家的排行榜分数
        """
        client = redis_conn or redis_client
        
        user_ids = [p.user_id for p in game.players if not p.is_ai]
        if not user_ids:
            return

        # 在线程池中执行 DB 查询，避免阻塞事件循环
        def query_users():
            db = SessionLocal()
            try:
                return db.query(User).filter(User.id.in_(user_ids)).all()
            finally:
                db.close()

        users = await asyncio.to_thread(query_users)
        
        if not users:
            return

        async with client.pipeline() as pipe:
            for user in users:
                # 更新总胜场
                pipe.zadd(KEY_LEADERBOARD_TOTAL, {str(user.id): user.total_wins})
                # 更新阵营胜场
                pipe.zadd(KEY_LEADERBOARD_GOOD, {str(user.id): user.wins_good})
                pipe.zadd(KEY_LEADERBOARD_EVIL, {str(user.id): user.wins_evil})
            await pipe.execute()

    @staticmethod
    async def get_leaderboard(board_type: str = "total", limit: int = 10) -> List[User]:
        """
        获取排行榜 (返回 User 对象列表，按排名排序)
        board_type: total | good | evil
        """
        key_map = {
            "total": KEY_LEADERBOARD_TOTAL,
            "good": KEY_LEADERBOARD_GOOD,
            "evil": KEY_LEADERBOARD_EVIL
        }
        key = key_map.get(board_type, KEY_LEADERBOARD_TOTAL)
        
        # 1. 尝试从 Redis 获取
        items = await redis_client.zrevrange(key, 0, limit - 1, withscores=True)
        
        if not items:
            # 2. 如果 Redis 为空，尝试重建缓存
            await RankService._rebuild_leaderboard(board_type)
            items = await redis_client.zrevrange(key, 0, limit - 1, withscores=True)
            
        if not items:
            return []

        # 3. 填充用户信息
        user_ids = [int(item[0]) for item in items]
        
        def query_users_by_ids(ids):
            db = SessionLocal()
            try:
                users = db.query(User).filter(User.id.in_(ids)).all()
                return users
            finally:
                db.close()

        users = await asyncio.to_thread(query_users_by_ids, user_ids)
        
        # 4. 按 Redis 顺序重排
        user_map = {u.id: u for u in users}
        result = []
        for uid in user_ids:
            if uid in user_map:
                result.append(user_map[uid])
                
        return result

    @staticmethod
    async def get_recent_games(limit: int = 10) -> List[Dict[str, Any]]:
        """
        获取最近对局
        """
        # 1. 查 Redis
        raw_list = await redis_client.lrange(KEY_RECENT_GAMES, 0, limit - 1)
        
        if not raw_list:
            # 2. 重建
            await RankService._rebuild_recent_games()
            raw_list = await redis_client.lrange(KEY_RECENT_GAMES, 0, limit - 1)
            
        return [json.loads(s) for s in raw_list]

    @staticmethod
    async def _rebuild_leaderboard(board_type: str):
        """
        从 DB 重建排行榜缓存
        """
        def query_all_users():
            db = SessionLocal()
            try:
                return db.query(User).all()
            finally:
                db.close()

        users = await asyncio.to_thread(query_all_users)
        
        async with redis_client.pipeline() as pipe:
            for user in users:
                if board_type == "total":
                    pipe.zadd(KEY_LEADERBOARD_TOTAL, {str(user.id): user.total_wins})
                elif board_type == "good":
                    pipe.zadd(KEY_LEADERBOARD_GOOD, {str(user.id): user.wins_good})
                elif board_type == "evil":
                    pipe.zadd(KEY_LEADERBOARD_EVIL, {str(user.id): user.wins_evil})
            
            # 设置过期时间
            key = KEY_LEADERBOARD_TOTAL
            if board_type == "good": key = KEY_LEADERBOARD_GOOD
            if board_type == "evil": key = KEY_LEADERBOARD_EVIL
            pipe.expire(key, 3600 * 24) 
            await pipe.execute()

    @staticmethod
    async def _rebuild_recent_games():
        """
        从 DB 重建最近对局缓存
        """
        def query_recent_games():
            db = SessionLocal()
            try:
                return db.query(Game).filter(Game.status == "finished")\
                    .order_by(Game.finished_at.desc())\
                    .limit(MAX_RECENT_GAMES).all()
            finally:
                db.close()

        games = await asyncio.to_thread(query_recent_games)
        
        if not games:
            return

        # 倒序遍历（最旧的先 LPUSH，最新的最后 LPUSH，这样最新的在 List 头部）
        games_reversed = games[::-1]
        
        async with redis_client.pipeline() as pipe:
            pipe.delete(KEY_RECENT_GAMES) 
            for game in games_reversed:
                summary = {
                    "id": game.id,
                    "winner": game.winner,
                    "created_at": str(game.finished_at or game.created_at),
                    "player_count": len(game.player_ids) if game.player_ids else 0
                }
                pipe.lpush(KEY_RECENT_GAMES, json.dumps(summary))
            await pipe.execute()
