# 这个文件是排行榜和最近对局缓存服务，负责管理 Redis 中的热点数据。
#
# 榜单的**写**路径不在这里，在 `core/rank_buffer.py`：对局结束只把增量攒进合并缓冲，
# 由后台循环批量 ZINCRBY 刷榜。这里只留读路径和从 MySQL 全量重建的兜底。
import json
from typing import List, Optional, Dict, Any
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from app.core.redis import redis_client
from app.core import rank_buffer
from app.models.user import User
from app.models.game import Game
from app.db.base import SessionLocal
import asyncio

# Redis Keys。榜单的 key 定义在 rank_buffer 里（写路径要用），这里引用同一份，
# 不各写一遍——两处字面量早晚会写歪，而写歪的表现是"榜看起来是空的"。
KEY_LEADERBOARD_TOTAL = rank_buffer.BOARD_KEYS[rank_buffer.BOARD_TOTAL]
KEY_LEADERBOARD_GOOD = rank_buffer.BOARD_KEYS[rank_buffer.BOARD_GOOD]
KEY_LEADERBOARD_EVIL = rank_buffer.BOARD_KEYS[rank_buffer.BOARD_EVIL]
KEY_RECENT_GAMES = "global:recent_games"
MAX_RECENT_GAMES = 50

class RankService:
    @staticmethod
    async def record_finished_game(game_id: str, winner: str, player_count: int,
                                   redis_conn=None):
        """把一局的摘要推进最近对局列表。

        只要摘要那几个字段，不要整个 GameState：原来为了调这一步得把 players 反序列化成
        `PlayerState` 再拼一个字段全填默认值的 `GameState`，而用到的只有 game_id、
        winner 和人数三个值。
        """
        client = redis_conn or redis_client

        summary = {
            "id": game_id,
            "winner": winner,               # "good" or "evil"
            "created_at": datetime.now().isoformat(),
            "player_count": player_count,
        }

        # LPUSH + LTRIM：列表定长，不会无界增长
        async with client.pipeline() as pipe:
            pipe.lpush(KEY_RECENT_GAMES, json.dumps(summary))
            pipe.ltrim(KEY_RECENT_GAMES, 0, MAX_RECENT_GAMES - 1)
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
