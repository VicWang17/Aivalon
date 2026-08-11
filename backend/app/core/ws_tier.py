# 这个文件负责判定一条 WS 连接的推送等级（玩家 / 旁观者）。
#
# 为什么要单独一个模块，而不是直接调 GameService.load_game：
# 网关进程的职责只有"连接维持、握手鉴权、消息转发"，一旦它 import 了 GameService，
# 就顺带把 ORM 模型、规则引擎、Celery 任务全都拉进了这个只该拿着 socket 的进程里
# ——它会因为业务代码的一个 import 错误而起不来，也会跟着业务代码一起重启，
# 而重启网关的代价是所有长连接一起掉线。拆分的意义正在于此，不能被一行 import 抵消。
#
# 所以这里只做一件事：把 Redis 快照当只读数据看，用 json.loads 抠出座位表。
# 不反序列化成 GameState——那需要 app.schemas.game，而它又牵着一串业务依赖。
import json
import logging
from typing import Optional

from app.core.socket_manager import Tier

logger = logging.getLogger("aivalon.ws")

# 与 event_journal.append_with_snapshot 写入的 key 保持一致
SNAPSHOT_KEY = "game:{game_id}:state"


async def resolve_tier(redis, game_id: str, user_id: int) -> Tier:
    """按"是否在这局里占着座位"判定等级。

    判据必须是服务端查出来的，不能让客户端自己声明角色——否则任何人都会声明成玩家，
    把本该聚合的旁观流量提成即时流量，分级就白做了。

    查不到房间时按旁观者接入：可能是刚建局还没落盘。宁可把玩家误判成旁观者
    （只是慢半秒），也不要因为一次读失败把连接挡在门外。
    """
    if redis is None:
        return Tier.SPECTATOR
    try:
        raw = await redis.get(SNAPSHOT_KEY.format(game_id=game_id))
    except Exception as e:
        logger.warning("读对局快照失败，按旁观者接入: game=%s %s", game_id, e)
        return Tier.SPECTATOR
    return Tier.PLAYER if _holds_seat(raw, user_id) else Tier.SPECTATOR


def _holds_seat(raw: Optional[str], user_id: int) -> bool:
    if not raw:
        return False
    try:
        players = json.loads(raw).get("players") or []
    except (json.JSONDecodeError, AttributeError, TypeError):
        return False
    return any(p.get("user_id") == user_id for p in players)
