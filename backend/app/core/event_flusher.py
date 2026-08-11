# 这个文件是 Write-Behind 的批量刷库器：周期性把 Redis Stream 里的事件批量写入 MySQL。
#
# 可靠性设计：
#   - 游标（last_flushed_id）在批量事务提交成功后才推进 → 崩溃会导致重刷 → 用 INSERT IGNORE 幂等去重
#     （game_events 有 (game_id, seq) 唯一约束兜底）
#   - Outbox 表同事务写入，保持"事件 + 发件箱"的原子性（只是从同步变成了延迟 ≤flush 间隔）
import asyncio
import json
import logging

from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.sql import func

from app.core import event_journal
from app.core.redis import get_new_redis_client
from app.db.base import SessionLocal
from app.models.game import Game as GameModel, GameEvent as GameEventModel
from app.models.outbox import OutboxEvent

logger = logging.getLogger("aivalon.flusher")

FLUSH_INTERVAL = 0.2      # 刷库间隔（秒）：RPO 与 MySQL 写压力的平衡点
FLUSH_BATCH_SIZE = 500    # 单次最多刷多少条


def _flush_batch(entries: list) -> None:
    """同步批量落库（在线程池里执行）。entries: [(entry_id, fields), ...]"""
    db = SessionLocal()
    try:
        for entry_id, fields in entries:
            payload = json.loads(fields.get("payload", "{}"))
            player_id = int(fields["player_id"]) if fields.get("player_id") else None

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

            # 开局事件：补建 games 表记录（创建路径已移出 MySQL 热路径）
            if fields.get("event_type") == "GAME_START":
                stmt = mysql_insert(GameModel).prefix_with("IGNORE").values(
                    id=fields["game_id"],
                    status="playing",
                    player_ids=payload.get("player_ids", []),
                    winner=None,
                    user_id=payload.get("creator_id"),
                )
                db.execute(stmt)
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
