"""
这个文件定义了对局相关的路由接口。
"""
import asyncio
import random
import time
from typing import List, Annotated
from fastapi import APIRouter, Depends, HTTPException, status, Header, Request, Response
from redis.asyncio import Redis
from app.core import bloom
from app.core import room_router
from app.core.rate_limit import create_game_rate_limit, action_rate_limit
from app.schemas.game import GameCreateRequest, GameCreateResponse, GameState, GameActionRequest, GameSummary, GameEventSchema, RecentGameSummary
from app.services.game_service import GameService
from app.services.rank_service import RankService
from app.core.deps import get_current_user
from app.core.redis import get_redis, redis_client
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
    http_request: Request,
    x_internal_secret: str = Header(None)
):
    """
    Internal API for AI Worker to submit actions

    这也是一条写路径：AI worker 提交的动作同样要落到房间归属节点的 Actor 上。
    worker 通常只认一个入口地址，请求打到哪个节点是随机的，所以必须同样走路由转发。
    """
    if x_internal_secret != settings.SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid internal secret")

    target = room_router.should_forward(game_id, http_request)
    if target:
        forwarded = await room_router.forward(target, http_request, await http_request.body())
        return Response(
            content=forwarded.content,
            status_code=forwarded.status_code,
            media_type=forwarded.headers.get("content-type", "application/json"),
        )

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

    走 L1 进程内 + L2 Redis 两级缓存，未命中回源 MySQL（见 app/core/cache.py）。
    这条路径是全站唯一每次都真的查 MySQL 的读接口，而回放事件只增不改，
    缓存起来没有一致性负担——新事件追加时由写路径失效（F-2）。
    """
    # 布隆过滤器挡在缓存前面：这条路径的空结果虽然也会被缓存，但攻击者每次换一个
    # 随机 id，每个 id 都是全新的 key、都要穿到 MySQL，缓存等于不存在（见 core/bloom.py）
    if not await bloom.might_contain(redis_client, game_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Game not found"
        )

    events = await GameService.get_game_events_cached(game_id)
    # 空列表照样返回 200：库里没事件不代表房间不存在（可能刚建局还没落盘），
    # 这里答 404 会让前端把"还没开始"误判成"房间不存在"
    return ResponseModel(data=events)

@router.get("/{game_id}", response_model=ResponseModel[GameState])
async def get_game_state(
    game_id: str,
    request: Request,
    current_user: User = Depends(get_current_user)
):
    """
    获取对局快照（支持断线重连/刷新）。
    返回的数据已根据当前玩家身份进行脱敏。

    读路径同样要路由：权威状态在归属节点的进程内存里，别的节点手里可能是一份
    过期副本（例如建局节点留下的初始快照），读到它会让客户端永远看不到最新回合。
    多付一次集群内跳转，换来"读到的一定是刚写进去的那份"。
    """
    # 布隆过滤器放在转发**之前**：不存在的 id 本来会先跨节点跳一次、再由目标节点答 404，
    # 在这里拦掉等于连这一跳都省了。注意只拦这个 HTTP 入口，不拦 `load_game` 本身——
    # AI 任务和快照恢复都依赖它，那些是内部调用，id 一定来自已存在的房间
    if not await bloom.might_contain(redis_client, game_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Game not found"
        )

    target = room_router.should_forward(game_id, request)
    if target:
        forwarded = await room_router.forward(target, request, b"")
        return Response(
            content=forwarded.content,
            status_code=forwarded.status_code,
            media_type=forwarded.headers.get("content-type", "application/json"),
        )

    # 1. 获取全局状态：本进程内存优先，未命中回落 Redis 快照。
    #    归属节点在收到第一个动作前内存里没有这个房间（建局可能发生在别的节点），
    #    只读本地字典会对真实存在的房间答 404。
    game = await GameService.load_game(game_id)
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
    request: Request,
    current_user: User = Depends(get_current_user),
    x_idempotency_key: Annotated[str | None, Header()] = None,
    redis: Redis = Depends(get_redis)
):
    """
    提交玩家动作（统一入口）。
    根据 action_type 和 payload 执行相应的业务逻辑。
    支持幂等性校验：通过 Header 'x-idempotency-key' 传递唯一请求ID。

    房间路由：房间状态活在归属节点的进程内存里（Actor 单写者），
    不归本节点的请求转发过去处理，见 app/core/room_router.py。
    """
    target = room_router.should_forward(game_id, request)
    if target:
        forwarded = await room_router.forward(target, request, await request.body())
        return Response(
            content=forwarded.content,
            status_code=forwarded.status_code,
            media_type=forwarded.headers.get("content-type", "application/json"),
        )

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
