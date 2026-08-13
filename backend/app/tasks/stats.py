# 这个文件是对局结束后的统计任务（Celery，`stats_queue`）。
#
# 这里刻意**不加熔断器**
# ------------------
# H-5 给 LLM 和邮件都加了熔断，这个依赖（MySQL + Redis）刻意不加。理由不是"来不及做"，
# 是**熔断在这里解决不了问题、还会制造新问题**：
#   1. **熔断的前提是有个可接受的兜底。** LLM 有规则引擎、邮件有"稍后再试"，
#      而统计**没有兜底**——胜场数据不算就是永久丢账，没有"次优答案"可退。
#      **没有兜底的依赖，短路它等于直接丢数据。**
#   2. **没有人在等，所以"不白等"这个收益不存在。** 这是 Celery 任务，慢十分钟
#      不影响任何人的请求；LLM 那边熔断买到的是玩家的等待时间，这里没有这笔账。
#   3. **它已经有正确的机制了：重试 + 幂等。** MySQL 挂了正确的做法是**等它回来再写**，
#      而不是"跳过这一批"。熔断和重试的适用条件恰好相反——
#      **熔断适合"失败了就放弃这一次"，重试适合"这一次不能丢"**。
#      判据不是依赖有多重要，是**丢掉这次调用可不可接受**。
#
# 换成指数退避的理由
# --------------
# 原来是 `countdown=5` 固定值：MySQL 挂 30 秒的话，3 次重试全落在故障窗口里、
# 全部失败，然后任务进死信——**重试次数被固定间隔浪费在同一个故障上了**。
# 退避让这几次重试铺开覆盖更长的窗口，同时**故障期间的重试流量随时间下降**，
# 不至于在数据库刚要缓过来的时候再压一波（同 H-3a：不说等多久，重试自己会变成新峰值）。
# 大白话：敲门没人应，不要每 5 秒敲一次敲三次，要 5 秒、20 秒、80 秒地敲。
import random
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


def retry_countdown(retries: int) -> float:
    """第 `retries` 次重试等多久：指数退避 + 抖动。

    **抖动不是可选项**：一批对局同时结束（一次故障恢复后常常如此）会被投递成
    一批任务，它们的重试时刻会完全对齐、成排打在刚恢复的数据库上——
    **重试自己变成了下一次故障的原因**。同 F-5 缓存 TTL 抖动那条：
    治的是"整齐"本身，而不是"量大"。
    """
    base = settings.STATS_RETRY_BASE * (settings.STATS_RETRY_FACTOR ** retries)
    base = min(base, settings.STATS_RETRY_MAX)
    return round(base * random.uniform(0.8, 1.2), 1)

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
            # **这里刻意不加熔断器**，理由见文件头。要的是重试退避。
            raise self.retry(exc=e, countdown=retry_countdown(self.request.retries))
        finally:
            db.close()

