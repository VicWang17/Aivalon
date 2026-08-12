from app.core.celery_app import celery_app
from app.models.game_enums import Camp, camp_of
from app.models.user import User
from app.services.rank_service import RankService
from app.core import rank_buffer
from app.db.base import SessionLocal
from app.core.config import settings
import redis.asyncio as redis
from typing import List, Optional
import asyncio

from app.core.idempotency import CeleryIdempotencyManager

@celery_app.task(bind=True, queue="stats_queue", max_retries=3)
def process_game_result(self, game_id: str, winner: str, players_data: List[dict]):
    """
    异步处理游戏结束后的统计任务：
    1. 更新 User 表胜场统计
    2. 更新 Redis 排行榜
    3. 更新最近对局缓存
    """
    print(f"[Stats Task] Processing game result for {game_id}, winner: {winner}")
    
    # 幂等性检查
    # 使用 Redis SETNX 确保同一个 game_id 只处理一次
    # Key 过期时间设为 24 小时
    with CeleryIdempotencyManager(f"game_result:{game_id}", expire=86400) as is_new:
        if not is_new:
            print(f"[Stats Task] DUPLICATE: Game result for {game_id} already processed. Skipping.")
            return

        db = SessionLocal()
        try:
            # 1. 更新用户胜场统计
            # 先获取所有用户
            user_ids = [p["user_id"] for p in players_data if not p.get("is_ai", True)] # 只统计真实玩家
            users = db.query(User).filter(User.id.in_(user_ids)).all()
            user_map = {u.id: u for u in users}
            
            # 转换 winner
            winner_camp = Camp(winner)

            # 榜单增量：(榜名, user_id, 增量)。在这里就能算出来，不用查库——
            # 这正是能改用 ZINCRBY 的前提，见 core/rank_buffer.py 的文件头。
            deltas = []

            for p_data in players_data:
                # 只有真实玩家才计入 UserStats
                if p_data.get("is_ai", True):
                    continue

                user = user_map.get(p_data["user_id"])
                if not user:
                    continue

                # 更新总场次
                user.total_games = (user.total_games or 0) + 1

                # 判定阵营：比枚举不比字符串（`camp_of` 里说了原因）
                camp = camp_of(p_data["character"])
                is_evil = camp == Camp.EVIL

                if camp == winner_camp:
                    user.total_wins = (user.total_wins or 0) + 1
                    if is_evil:
                        user.wins_evil = (user.wins_evil or 0) + 1
                    else:
                        user.wins_good = (user.wins_good or 0) + 1
                    deltas.append((rank_buffer.BOARD_TOTAL, user.id, 1))
                    deltas.append((
                        rank_buffer.BOARD_EVIL if is_evil else rank_buffer.BOARD_GOOD,
                        user.id, 1,
                    ))

                db.add(user)

            db.commit()
            print(f"[Stats Task] Updated user stats for game {game_id}")
            
            # 2. 榜单增量入合并缓冲 + 最近对局列表。
            # 榜不再在这里直接写 ZSET：增量攒进缓冲，由 API 进程的后台循环批量刷
            # （见 core/rank_buffer.py）。原来那句"查库拿绝对胜场再 ZADD 覆盖"整段没了——
            # 增量上面已经算出来了，不需要再读一次数据源。
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
                    await rank_buffer.record(deltas, redis_conn)
                    await RankService.record_finished_game(
                        game_id=game_id,
                        winner=winner,
                        player_count=len(players_data),
                        redis_conn=redis_conn,
                    )
                finally:
                    await redis_conn.close()

            loop.run_until_complete(run_stats())
            loop.close()

            print(f"[Stats Task] Buffered {len(deltas)} rank updates for {game_id}")

        except Exception as e:
            print(f"[Stats Task] Error processing game {game_id}: {e}")
            db.rollback()
            # 抛出异常让 Celery 重试
            raise self.retry(exc=e, countdown=5)
        finally:
            db.close()

