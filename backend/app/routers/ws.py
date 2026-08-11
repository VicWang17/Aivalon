"""
这个文件定义了 WebSocket 路由，处理实时对局连接、鉴权与消息转发。
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query, status, HTTPException
from jose import jwt, JWTError

from app.core.socket_manager import manager, Tier
from app.db.base import SessionLocal
from app.core.config import settings
from app.models.user import User
from app.schemas.protocol import WSMessage, WebSocketOpCode
import json

router = APIRouter()

async def get_ws_user(
    token: str = Query(...)
) -> User:
    """
    WebSocket 鉴权依赖项
    在 WebSocket 握手阶段验证 Token。如果验证失败，抛出 HTTP 异常拒绝连接。

    注意：这里不用 get_db 依赖注入，而是手动管理短生命周期 Session。
    原因：FastAPI 的 yield 依赖在 WS 端点上的生命周期 = 整条连接，而 Session 查询后
    事务保持打开会一直占用连接池连接——N 条长连接 = N 个 DB 连接被独占，
    默认池（5+10）在第 16 条连接时就耗尽，整个服务被拖死（见 DEVLOG 006）。
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id_str = payload.get("sub")
        if user_id_str is None:
             raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token payload")
        user_id = int(user_id_str)
    except (JWTError, ValueError):
         raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Could not validate credentials")

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
    finally:
        db.close()  # 握手鉴权是一次性查询，用完立即归还连接
    if not user:
         raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="User not found")
    return user

@router.websocket("/games/{game_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    game_id: str,
    user: User = Depends(get_ws_user)
):
    """
    游戏实时对局 WebSocket 接口
    连接地址: /api/v1/ws/games/{game_id}?token={jwt_token}
    """
    # 分级推送：有座位的是玩家（即时收帧），没座位的是旁观者（聚合批量收）。
    # 判据用"是否占座"而不是让客户端自己声明角色——否则任何人都能声明成玩家，
    # 把本该聚合的流量提成即时流量。
    tier = Tier.SPECTATOR
    try:
        from app.services.game_service import GameService

        game = await GameService.load_game(game_id)
        if game and any(p.user_id == user.id for p in game.players):
            tier = Tier.PLAYER
    except Exception:
        # 查不到房间不该挡住连接（可能刚建局还没落地），按旁观者接进来即可
        pass

    await manager.connect(websocket, game_id, user_id=user.id, tier=tier)
    
    try:
        while True:
            # 等待接收消息
            data = await websocket.receive_text()
            
            # 简单的消息处理逻辑
            try:
                msg_dict = json.loads(data)
                msg_type = msg_dict.get("type")
                
                # 处理心跳
                if msg_type == WebSocketOpCode.HEARTBEAT:
                    pong = WSMessage(type=WebSocketOpCode.PONG)
                    await websocket.send_text(pong.model_dump_json())
                    
                # TODO: 处理其他客户端上行消息 (Player Action, Chat)
                # 目前主要通过 HTTP API 提交动作，WebSocket 主要用于下行通知
                # 但如果将来改为全双工，可以在这里分发
                
            except json.JSONDecodeError:
                pass
                
    except WebSocketDisconnect:
        manager.disconnect(websocket, game_id)
        # 本节点已无该房间连接时从连接路由表摘掉自己，否则跨节点扇出会一直往这里发
        await manager.unregister_if_empty(game_id)
