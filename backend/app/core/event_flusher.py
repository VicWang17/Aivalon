# 这个文件是 Write-Behind 的批量刷库器：周期性把 Redis Stream 里的事件批量写入 MySQL。
#
# 可靠性设计：
#   - 游标（last_flushed_id）在批量事务提交成功后才推进 → 崩溃会导致重刷 → 用 INSERT IGNORE 幂等去重
#     （game_events 有 (game_id, seq) 唯一约束兜底）
#   - Outbox 表同事务写入，保持"事件 + 发件箱"的原子性（只是从同步变成了延迟 ≤flush 间隔）
import asyncio
import json
import logging

from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import func

from app.core import cache
from app.core import event_journal
from app.core.redis import get_new_redis_client
from app.db.base import SQLALCHEMY_DATABASE_URL
from app.models.game import Game as GameModel, GameEvent as GameEventModel
from app.models.outbox import OutboxEvent

logger = logging.getLogger("aivalon.flusher")

FLUSH_INTERVAL = 0.2      # 刷库间隔（秒）：RPO 与 MySQL 写压力的平衡点
FLUSH_BATCH_SIZE = 500    # 单次最多刷多少条

# 独立引擎 + 单连接池：后台刷库是常驻写者，不与前台请求路径共享连接池（舱壁隔离），
# 避免大批量刷库事务占满共享池导致请求路径拿不到连接（S2 复测创建对局 90s 超时的根因之一）。
_flush_engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_size=1,
    max_overflow=0,
    pool_pre_ping=True,
    pool_recycle=3600,
    isolation_level="READ COMMITTED",
)
FlushSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_flush_engine)


def _flush_batch(entries: list) -> None:
    """同步批量落库（在线程池里执行）。entries: [(entry_id, fields), ...]"""
    db = FlushSessionLocal()
    try:
        for entry_id, fields in entries:
            payload = json.loads(fields.get("payload", "{}"))
            player_id = int(fields["player_id"]) if fields.get("player_id") else None

            # 开局事件：先补建 games 表记录（创建路径已移出 MySQL 热路径）。
            # 顺序要紧：game_events.game_id 有外键指向 games.id，若先插事件行，
            # 外键校验失败会被 INSERT IGNORE 降级成 warning 静默丢弃——GAME_START
            # 就此永久丢失（全库 372 局零条 GAME_START，见 DEVLOG 014）。
            if fields.get("event_type") == "GAME_START":
                stmt = mysql_insert(GameModel).prefix_with("IGNORE").values(
                    id=fields["game_id"],
                    status="playing",
                    player_ids=payload.get("player_ids", []),
                    winner=None,
                    user_id=payload.get("creator_id"),
                )
                db.execute(stmt)

            # 事件表：INSERT IGNORE 幂等（崩溃重刷不产生重复行）
            stmt = mysql_insert(GameEventModel).prefix_with("IGNORE").values(
                game_id=fields["game_id"],
                seq=int(fields["seq"]),
                event_type=fields["event_type"],
                player_id=player_id,
                payload=payload,
            )
            db.execute(stmt)

            # Outbox：保持事务性发件箱语义（极端崩溃重刷可能产生重复行，消费端幂等兜底）
            db.add(OutboxEvent(
                aggregate_type="game",
                aggregate_id=fields["game_id"],
                event_type=fields["event_type"],
                payload=payload,
                status="pending",
            ))

            # 结算事件：同步更新 games 表元信息
            if fields.get("phase") == "finished":
                game_record = db.query(GameModel).filter(GameModel.id == fields["game_id"]).first()
                if game_record:
                    game_record.status = "finished"
                    game_record.winner = fields.get("winner") or None
                    game_record.finished_at = func.now()

        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


async def flush_once(redis) -> int:
    """刷一批：读游标之后的事件 → 批量写 MySQL → 推进游标。返回刷入条数。"""
    last_id = await redis.get(event_journal.JOURNAL_CURSOR) or "0-0"
    entries = await redis.xrange(
        event_journal.JOURNAL_STREAM, min=f"({last_id}", count=FLUSH_BATCH_SIZE
    )
    if not entries:
        return 0

    await asyncio.to_thread(_flush_batch, entries)

    # 事件驱动失效：刷库成功之后才失效回放事件流缓存。
    #
    # 时机是这里唯一要紧的事。这份缓存回源的是 MySQL，而事件走 Write-Behind——
    # 动作发生时事件只进了 Redis Stream，MySQL 里还没有。如果在动作发生时失效，
    # 紧随其后的读会回源到一个**还没刷进新事件的 MySQL**，拿到旧结果再缓存 300 秒,
    # 比不失效更糟：不失效至少是旧值，提前失效是"把旧值重新盖章成新值"。
    # 所以失效点挂在 commit 之后。**失效要对齐数据源的可见时刻，不是业务动作的发生时刻。**
    #
    # 按 game_id 去重：一批 500 条事件可能只涉及几个房间，逐条发就是几百次无用广播。
    touched = {fields.get("game_id") for _, fields in entries if fields.get("game_id")}
    for game_id in touched:
        try:
            await cache.invalidate_events(game_id, redis=redis)
        except Exception as e:
            # 失效失败不该让刷库回滚：数据已经落库了，最坏是缓存多留一个 TTL 的旧值
            logger.warning("失效回放缓存失败: game=%s %s", game_id, e)

    new_cursor = entries[-1][0]
    await redis.set(event_journal.JOURNAL_CURSOR, new_cursor)
    # 删除已刷入的条目，控制 Stream 长度
    await redis.xdel(event_journal.JOURNAL_STREAM, *[eid for eid, _ in entries])
    return len(entries)


async def flusher_loop() -> None:
    """后台刷库循环（随 API 进程生命周期运行）"""
    redis = get_new_redis_client()
    logger.info("event flusher started (interval=%.1fs)", FLUSH_INTERVAL)
    while True:
        try:
            count = await flush_once(redis)
            if count:
                logger.info("flushed %d events to MySQL", count)
        except Exception as e:
            # 刷库失败不致命：事件留在 Stream 里，下轮重试（INSERT IGNORE 幂等）
            logger.error("flush failed, will retry: %s", e)
        await asyncio.sleep(FLUSH_INTERVAL)
