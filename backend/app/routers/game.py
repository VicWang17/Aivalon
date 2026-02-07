"""
这个文件定义了对局相关的路由接口。
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.schemas.game import GameCreateRequest, GameCreateResponse, GameState, GameActionRequest
from app.services.game_service import GameService
from app.core.deps import get_current_user
from app.models.user import User
from app.db.base import get_db

router = APIRouter()

@router.post("/", response_model=GameCreateResponse)
async def create_game(
    request: GameCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
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
    users = db.query(User).filter(User.id.in_(request.player_ids)).all()
    
    # 3. 构建 ID 到 用户名 的映射
    user_map = {u.id: u.username for u in users}
    
    # 4. 对于数据库中未找到的 ID（可能是测试用或已删除），使用默认名称填充
    missing_ids = set(request.player_ids) - set(user_map.keys())
    for uid in missing_ids:
        user_map[uid] = f"User_{uid}"

    # 5. 调用 Service 创建对局
    game_state = await GameService.create_game(request.player_ids, user_map)
    
    return GameCreateResponse(
        game_id=game_state.game_id,
        initial_state=game_state
    )

@router.get("/{game_id}", response_model=GameState)
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
    
    return player_view

@router.post("/{game_id}/action", response_model=GameState)
async def submit_action(
    game_id: str,
    action: GameActionRequest,
    current_user: User = Depends(get_current_user)
):
    """
    提交玩家动作（统一入口）。
    根据 action_type 和 payload 执行相应的业务逻辑。
    """
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
    player_view = GameService.get_player_view(updated_state, current_user.id)
    
    return player_view
