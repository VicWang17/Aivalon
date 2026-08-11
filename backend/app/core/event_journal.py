# 这个文件是 Write-Behind 事件日志：事件先写 Redis Stream（微秒级），MySQL 由 flusher 批量补写。
#
# 核心设计：事件入 Stream 与状态快照写在同一个 Redis 事务（MULTI/EXEC）里——
# 两者要么同时成功要么同时失败，崩溃任意时刻都不可能出现"有状态无事件"或"有事件无状态"。
# 这一个 Redis 事务就是热路径上唯一的持久化点（ durability point ），替代原来的同步 MySQL 写。
import json
from typing import Any, Dict, Optional

from app.core.redis import redis_client
from app.schemas.game import GameState

JOURNAL_STREAM = "journal:game_events"      # 全局事件流（所有房间共用，flusher 顺序消费）
JOURNAL_CURSOR = "journal:last_flushed_id"  # flusher 的刷库游标（Stream entry id）

SNAPSHOT_TTL = 86400  # 快照保留 24h（覆盖一局游戏的生命周期）


async def append_with_snapshot(
    game_id: str,
    seq: int,
    event_type: str,
    player_id: Optional[int],
    payload: Dict[str, Any],
    phase: str,
    winner: Optional[str],
    game_state: GameState,
) -> None:
    """
    原子持久化一个动作：事件入 Stream + 最新状态快照，同一 MULTI/EXEC 事务。
    失败则整体失败（调用方不应更新内存状态），由调用方返回错误。
    """
    entry = {
        "game_id": game_id,
        "seq": str(seq),
        "event_type": event_type,
        "player_id": "" if player_id is None else str(player_id),
        "phase": phase,
        "winner": winner or "",
        "payload": json.dumps(payload, ensure_ascii=False, default=str),
    }
    pipe = redis_client.pipeline(transaction=True)  # MULTI/EXEC 事务管道
    pipe.xadd(JOURNAL_STREAM, entry)
    pipe.set(f"game:{game_id}:state", game_state.model_dump_json(), ex=SNAPSHOT_TTL)
    await pipe.execute()
