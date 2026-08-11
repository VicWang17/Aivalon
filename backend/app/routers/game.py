"""
这个文件定义了对局相关的路由接口。
"""
import asyncio
import random
import time
from typing import List, Annotated
from fastapi import APIRouter, Depends, HTTPException, status, Header
from redis.asyncio import Redis
from app.core.rate_limit import create_game_rate_limit, action_rate_limit
from app.schemas.game import GameCreateRequest, GameCreateResponse, GameState, GameActionRequest, GameSummary, GameEventSchema, RecentGameSummary
from app.services.game_service import GameService
from app.services.rank_service import RankService
from app.core.deps import get_current_user
from app.core.redis import get_redis
from app.core.idempotency import IdempotencyManager
from app.models.user import User
from app.db.base import SessionLocal
from app.schemas.base import ResponseModel
from app.core.socket_manager import manager
from app.schemas.protocol import WSMessage, WebSocketOpCode

router = APIRouter()

# AI 随机名字库
AI_NAMES = [
    "杰克", "罗恩", "詹姆斯", "哈利", "露娜", "纳威", "金妮", "弗雷德", "乔治",
    "托马斯", "爱德华", "理查德", "亨利", "伊丽莎白", "维多利亚", "玛丽", "安妮",
    "大卫", "迈克尔", "约翰", "威廉", "罗伯特", "查尔斯", "约瑟夫", "保罗", "马克",
    "艾米", "艾丽", "艾玛", "奥利维亚", "索菲亚", "伊莎贝拉", "米娅", "夏洛特", "阿米莉亚",
    "亚历山大", "本杰明", "卡尔", "丹尼尔", "埃里克", "弗兰克", "加布里埃尔", "雨果", "伊万",
    "凯文", "利奥", "马克斯", "尼古拉斯", "奥斯卡", "彼得", "昆廷", "山姆", "蒂姆",
    "爱丽丝", "贝蒂", "卡罗尔", "戴安娜", "伊娃", "菲奥娜", "格蕾丝", "海伦", "艾瑞斯",
    "朱莉", "凯特", "莉莉", "莫莉", "诺拉", "佩妮", "瑞秋", "萨拉", "蒂娜", "温蒂"
]

def _load_user_map(player_ids: list[int]) -> dict[int, str]:
    """短生命周期会话查用户名（在线程池执行）：只取 ID→用户名 映射，拿到立即释放连接，
    不把连接持有过后续的 await（创建对局 90s 超时复盘：连接被持有着跨 Redis 写等待）。"""
    db = SessionLocal()
    try:
        users = db.query(User).filter(User.id.in_(player_ids)).all()
        return {u.id: u.username for u in users}
    finally:
        db.close()


@router.post("/", response_model=ResponseModel[GameCreateResponse], dependencies=[Depends(create_game_rate_limit())])
async def create_game(
    request: GameCreateRequest,
    current_user: User = Depends(get_current_user),
):
    """
    创建新对局。
    需要提供 player_ids 列表。
    """
    # 1. 验证玩家 ID 是否存在
    if not request.player_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Player IDs cannot be empty"
        )

    # 2. 从数据库获取用户信息
    user_map = await asyncio.to_thread(_load_user_map, request.player_ids)
    
    # 4. 确定需要命名的 ID
    # 策略：
    # 1. 数据库没查到的 ID -> 必须随机命名
    # 2. 数据库查到了，但名字是 User_ 开头的 -> 也随机命名（覆盖掉测试残留）
    # 3. 排除当前用户（如果是 User_ 开头也不动它，除非用户自己改名）
    
    ids_to_rename = list(set(request.player_ids) - set(user_map.keys()))
    
    for uid, name in user_map.items():
        # 如果不是自己，且名字看起来是默认生成的
        if uid != current_user.id and name.startswith("User_"):
            ids_to_rename.append(uid)
    
    # 去重
    ids_to_rename = list(set(ids_to_rename))
    
    # 准备名字池
    available_names = list(AI_NAMES)
    random.shuffle(available_names)
    
    for uid in ids_to_rename:
        if available_names:
            user_map[uid] = available_names.pop()
        else:
            user_map[uid] = f"User_{uid}" # 名字不够用时回退到默认

    # 5. 调用 Service 创建对局
    game_state = await GameService.create_game(request.player_ids, user_map, creator_id=current_user.id)
    
    return ResponseModel(
        data=GameCreateResponse(
            game_id=game_state.game_id,
            initial_state=game_state
        )
    )

@router.get("/recent", response_model=ResponseModel[List[RecentGameSummary]])
async def get_recent_games(
    limit: int = 10
):
    """
    获取最近已完成的对局摘要（全站范围，Redis缓存加速）
    """
    games = await RankService.get_recent_games(limit)
    return ResponseModel(data=games)

from app.core.config import settings
from app.models.game_enums import ActionType
from pydantic import BaseModel

class AIActionRequest(BaseModel):
    player_id: int
    action_type: ActionType
    payload: dict = {}

class AIThinkingRequest(BaseModel):
    player_id: int

@router.post("/{game_id}/ai_thinking", include_in_schema=False)
async def broadcast_ai_thinking(
    game_id: str,
    request: AIThinkingRequest,
    x_internal_secret: str = Header(None)
):
    """
    Internal API for AI Worker to broadcast thinking state
    """
    if x_internal_secret != settings.SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid internal secret")
        
    msg = WSMessage(
        type=WebSocketOpCode.AI_THINKING,
        # ts 为广播发出时间戳：客户端据此计算端到端广播延迟（S3 压测与 E 组广播延迟埋点共用）
        payload={"player_id": request.player_id, "ts": time.time()}
    )
    await manager.broadcast(game_id, msg)
    
    return {"status": "ok"}

@router.post("/{game_id}/ai_action", include_in_schema=False)
async def process_ai_action(
    game_id: str, 
    request: AIActionRequest,
    x_internal_secret: str = Header(None)
):
    """
    Internal API for AI Worker to submit actions
    """
    if x_internal_secret != settings.SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid internal secret")
        
    await GameService.process_action(
        game_id, 
        request.player_id, 
        request.action_type, 
        request.payload
    )
    
    return {"status": "ok"}

@router.get("/history", response_model=ResponseModel[List[GameSummary]])
async def get_my_game_history(
    skip: int = 0,
    limit: int = 20,
    current_user: User = Depends(get_current_user)
):
    """
    获取当前用户的对局历史（按时间倒序）
    """
    # 直接调用 Service 获取
    games = GameService.get_user_games(current_user.id, skip=skip, limit=limit)
    return ResponseModel(data=games)

@router.get("/{game_id}/events", response_model=ResponseModel[List[GameEventSchema]])
async def get_game_events(
    game_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    获取对局的所有事件流（回放用）
    """
    # 1. 检查游戏是否存在（Service 会查库）
    events = GameService.get_game_events(game_id)
    if not events:
        # 如果没有事件，可能游戏未开始或不存在
        # 再确认一下游戏是否存在
        game = GameService.get_game(game_id)
        if not game:
             # 如果内存也没有，那就真没有了
             # 实际上 get_game_events 是查库，如果库里没事件，但游戏在进行中，也是空列表
             # 这里简单返回空列表即可，或者抛出 404 如果游戏ID无效
             pass
    
    return ResponseModel(data=events)

@router.get("/{game_id}", response_model=ResponseModel[GameState])
async def get_game_state(
    game_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    获取对局快照（支持断线重连/刷新）。
    返回的数据已根据当前玩家身份进行脱敏。
    """
    # 1. 获取全局状态
    game = GameService.get_game(game_id)
    if not game:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Game not found"
        )
    
    # 2. 获取视角视图
    # Service 层会处理脱敏逻辑
    player_view = GameService.get_player_view(game, current_user.id)
    
    return ResponseModel(data=player_view)

@router.post("/{game_id}/action", response_model=ResponseModel[GameState], dependencies=[Depends(action_rate_limit())])
async def submit_action(
    game_id: str,
    action: GameActionRequest,
    current_user: User = Depends(get_current_user),
    x_idempotency_key: Annotated[str | None, Header()] = None,
    redis: Redis = Depends(get_redis)
):
    """
    提交玩家动作（统一入口）。
    根据 action_type 和 payload 执行相应的业务逻辑。
    支持幂等性校验：通过 Header 'x-idempotency-key' 传递唯一请求ID。
    """
    
    # 封装核心处理逻辑
    async def _process():
        # 调用 Service 处理动作
        # Service 层会负责：
        # 1. 校验动作是否合法（规则引擎）
        # 2. 更新游戏状态（状态机）
        # 3. 返回更新后的全局状态（Service 返回的是全局状态）
        updated_state = await GameService.process_action(
            game_id=game_id,
            user_id=current_user.id,
            action_type=action.action_type,
            payload=action.payload
        )
        
        # 返回给前端时，同样需要进行视角脱敏
        # 这样前端操作完后能立即拿到最新的、符合自己视角的快照
        return GameService.get_player_view(updated_state, current_user.id)

    # 幂等性控制
    if x_idempotency_key:
        async with IdempotencyManager(redis, x_idempotency_key, current_user.id):
            player_view = await _process()
    else:
        player_view = await _process()
    
    return ResponseModel(data=player_view)
