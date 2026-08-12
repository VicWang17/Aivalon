# 这个文件是排行榜和最近对局缓存服务，负责管理 Redis 中的热点数据。
#
# 榜单的**写**路径不在这里，在 `core/rank_buffer.py`：对局结束只把增量攒进合并缓冲，
# 由后台循环批量 ZINCRBY 刷榜。这里只留读路径和从 MySQL 全量重建的兜底。
import asyncio
import json
import logging
from typing import Any, Dict, List

from datetime import datetime

from app.core import cache
from app.core import degrade
from app.core import metrics
from app.core import rank_buffer
from app.core.redis import redis_client
from app.db.base import SessionLocal
from app.models.game import Game
from app.models.user import User

logger = logging.getLogger("aivalon.rank")

KEY_RECENT_GAMES = "global:recent_games"
MAX_RECENT_GAMES = 50

# 归并快照：合并好的 Top N（已带用户名等展示字段）整体存一个 key。
# 读接口只读这个 key，不碰分片也不碰 MySQL。
SNAPSHOT_PREFIX = "leaderboard:snapshot:"
# 快照里存多少条。读接口的 limit 上限是 100（见 routers/users.py），存满 100 条
# 就能覆盖任何 limit，切片在应用侧做——**按最大 limit 存一份，而不是每个 limit 存一份**，
# 否则 limit 是个无界维度，缓存 key 会跟着爆开。
SNAPSHOT_TOP_N = 100
# 快照 TTL：比归并间隔长很多。归并循环每 MERGE_INTERVAL 覆盖一次，TTL 只是兜底——
# 万一所有节点的归并循环都停了，让快照过期总比一直供一份不知多旧的数据好。
SNAPSHOT_TTL = 300
# 归并间隔（秒）。榜单晚几秒更新没人看得出来，而这个间隔直接决定归并的开销频率：
# **归并次数由它决定，和读 QPS 无关**——这正是"定时归并"相对"每次读都归并"的全部意义。
MERGE_INTERVAL = 5.0
# 本地缓存 TTL（秒）。同 cache.py 文件头：这个数字是"能容忍多久的脏读"，
# 而榜单本身就已经是 MERGE_INTERVAL 级别的旧数据了，再多 2 秒无所谓。
SNAPSHOT_L1_TTL = 2.0

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

    # ------------------------------------------------------------------
    # 读路径：本地缓存 → 归并快照，一次 MySQL 都不查
    # ------------------------------------------------------------------
    #
    # 原来每次请求都要：查 Redis 排序 → **拿着这批 id 去 MySQL 查用户名和场次**。
    # Redis 只用来排序，展示字段还是每次回源，所以榜单接口的 QPS 是**直接压在 MySQL 上的**。
    # 现在展示字段在归并时就一起烤进快照，读接口只读一个 key。
    #
    # 三层各治一件事，别混着说：
    #   - **定时归并**：把"归并 8 个分片"的开销从每次读挪到每 MERGE_INTERVAL 一次。
    #     **归并次数从此和读 QPS 无关**——这是"定时归并"相对"读时归并"的全部意义。
    #   - **快照**：把展示字段一起存下来，读路径彻底不碰 MySQL。
    #   - **本地缓存**：省掉那一次 Redis 往返和 JSON 反序列化（同 cache.py 文件头）。

    @staticmethod
    def snapshot_key(board_type: str) -> str:
        return f"{SNAPSHOT_PREFIX}{board_type}"

    @staticmethod
    async def get_leaderboard(board_type: str = "total", limit: int = 10) -> List[Dict[str, Any]]:
        """取榜单 Top `limit`。返回可直接展示的条目列表，按排名降序。

        走 `cache.get_or_load` 拿 L1 + singleflight，但**刻意传 `redis=None`**：
        快照本身就是这份数据的 L2，再让 cache 往 Redis 写一份信封就是同一份数据存两处，
        而且两处的失效时机还不一样。这里要复用的只是 F-1 的 L1 和 F-4 的 singleflight。
        """
        if board_type not in rank_buffer.BOARD_BASE:
            board_type = rank_buffer.BOARD_TOTAL

        entries = await cache.get_or_load(
            RankService.snapshot_key(board_type),
            lambda: RankService._read_snapshot(board_type),
            redis=None,
            l1_ttl=SNAPSHOT_L1_TTL,
        )
        # 切片在应用侧做：快照按最大 limit 存一份，不按 limit 分别存
        # （limit 是无界维度，每个 limit 存一份就是让 key 跟着请求参数爆开）
        return entries[:limit]

    @staticmethod
    async def _read_snapshot(board_type: str) -> List[Dict[str, Any]]:
        """读归并快照。快照不在（冷启动 / 所有归并循环都停了）就当场归并一次兜底。"""
        try:
            raw = await redis_client.get(RankService.snapshot_key(board_type))
        except Exception as e:
            logger.warning("读榜单快照失败，当场归并: %s", e)
            raw = None

        if raw is not None:
            metrics.rank_reads.labels(result="snapshot").inc()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("榜单快照不是合法 JSON，当场归并")

        # 兜底：当场归并一次并把快照建起来。singleflight 已经保证同一时刻只有一个人做这件事。
        metrics.rank_reads.labels(result="merged").inc()
        return await RankService.rebuild_snapshot(board_type, redis_client)

    @staticmethod
    async def _load_users(ids: List[int]) -> List[User]:
        """按 id 批量取用户，用来给榜单补展示字段。

        单独抽出来是因为这是读路径上**唯一**碰 MySQL 的地方，抽出来才好在测试里替掉，
        也好一眼看出它只在归并时被调一次、不在每次读上。
        """
        if not ids:
            return []

        def query_users():
            db = SessionLocal()
            try:
                return db.query(User).filter(User.id.in_(ids)).all()
            finally:
                db.close()

        # 同步 ORM 查询丢线程池：它不让出事件循环，直接在协程里跑会把整个
        # 事件循环卡住那么久，而归并循环和请求处理跑在同一个循环上
        return await asyncio.to_thread(query_users)

    @staticmethod
    async def rebuild_snapshot(board_type: str, redis_conn=None) -> List[Dict[str, Any]]:
        """归并分片 → 补齐展示字段 → 写快照。返回归并结果。

        这是唯一查 MySQL 的地方，而且每 MERGE_INTERVAL 最多一次（读多少次都一样）。
        """
        client = redis_conn or redis_client
        ranked = await rank_buffer.merge_shards(client, board_type, SNAPSHOT_TOP_N)
        if not ranked:
            return []

        user_ids = [int(m) for m, _ in ranked if str(m).isdigit()]
        users = await RankService._load_users(user_ids)
        user_map = {u.id: u for u in users}

        entries = []
        for member, score in ranked:
            user = user_map.get(int(member)) if str(member).isdigit() else None
            if user is None:
                # 榜上有人但库里查不到（账号注销等）：跳过而不是留个空位。
                # 榜是派生数据，以 MySQL 为准。
                continue
            total_games = user.total_games or 0
            entries.append({
                "user_id": user.id,
                "username": user.username,
                "total_games": total_games,
                "wins_good": user.wins_good or 0,
                "wins_evil": user.wins_evil or 0,
                "total_wins": user.total_wins or 0,
                "win_rate": round((user.total_wins or 0) / total_games * 100, 1) if total_games else 0.0,
                "score": score,
            })

        try:
            await client.set(
                RankService.snapshot_key(board_type),
                json.dumps(entries),
                ex=SNAPSHOT_TTL,
            )
        except Exception as e:
            # 快照写不上去只是让下一个读的人再归并一次，不影响这次的返回值
            logger.warning("写榜单快照失败: %s", e)

        return entries

    @staticmethod
    async def merge_loop(redis_conn) -> None:
        """定时归并循环（随 API 进程生命周期运行）。

        多个节点各归并一遍，最后一个写的赢。**这里刻意不做互斥**，和写路径的 RENAME 不同：
        归并是纯粹的读 + 覆盖写，重复做只是浪费一点 CPU，不会算错；
        而写路径的换出重复做会丢增量，那才必须互斥。
        **该不该加互斥，看重复执行会不会改变结果，不看它是不是"后台任务"。**
        """
        logger.info("leaderboard merger started (interval=%.1fs)", MERGE_INTERVAL)
        while True:
            try:
                for board in rank_buffer.BOARDS:
                    await RankService.rebuild_snapshot(board, redis_conn)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("榜单归并失败，下轮重试: %s", e)
            # 降级矩阵 L3：到档就把间隔乘上倍数（见 core/degrade.py）。
            # **每轮重新读、不在循环外读一次**：那样拧了档位要等到进程重启才生效,
            # 而这个循环的生命周期和进程一样长——**一个要重启才生效的降级等于没有**（同 H-1）。
            # 归并本身是"读 8 个分片 + 一次可能的查库",降频直接按倍数省掉这些。
            interval = await degrade.cold_path_interval(MERGE_INTERVAL, redis_conn)
            await asyncio.sleep(interval)

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
    async def _load_all_scores(column: str) -> List[tuple]:
        """全表取 (user_id, 该榜的权威胜场)。只在人工对账时被调用。"""
        def query_all_users():
            db = SessionLocal()
            try:
                return [(u.id, getattr(u, column) or 0) for u in db.query(User).all()]
            finally:
                db.close()

        return await asyncio.to_thread(query_all_users)

    @staticmethod
    async def rebuild_from_db(board_type: str, redis_conn=None) -> int:
        """从 MySQL 全量重刷分片。返回写入的 member 数。

        这是榜单的对账路径，也是 G-1 敢用不幂等的 ZINCRBY 的**前提**：
        增量重放会让分数偏高，跑一次这个就正回来了。**"可以接受 at-least-once"
        不是因为多算无害，而是因为存在这条能把它抹平的路径**——没有它就只能上幂等方案。

        用 ZADD 而不是 ZINCRBY：这里写的是权威绝对值，就该覆盖。
        和写路径要用 ZINCRBY 不冲突——那边没有绝对值可用，这边有。
        """
        client = redis_conn or redis_client
        column = {
            rank_buffer.BOARD_TOTAL: "total_wins",
            rank_buffer.BOARD_GOOD: "wins_good",
            rank_buffer.BOARD_EVIL: "wins_evil",
        }[board_type]

        rows = await RankService._load_all_scores(column)

        pipe = client.pipeline(transaction=False)
        # 先删干净再重刷：不删的话，库里已经不存在的 member 会永远留在榜上。
        # 这中间有个空窗，读到空榜的人会当场归并出一份空快照——可以接受，
        # 因为这条路径是人工对账时才跑的，不在正常读写路径上。
        for key in rank_buffer.shard_keys(board_type):
            pipe.delete(key)
        written = 0
        for user_id, score in rows:
            if not score:
                continue    # 0 分的不占榜位，也省得把全库用户都塞进去
            pipe.zadd(rank_buffer.key_for_member(board_type, str(user_id)),
                      {str(user_id): float(score)})
            written += 1
        await pipe.execute()

        # 重刷完必须让快照跟着更新，否则读接口还在供旧快照，对账看起来"没生效"
        await RankService.rebuild_snapshot(board_type, client)
        return written

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
