"""
这个文件定义了 WebSocket 路由，处理实时对局连接、鉴权与消息转发。
"""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends, Query, status, HTTPException
from sqlalchemy.orm import Session
from jose import jwt, JWTError

from app.core.socket_manager import manager
from app.core.deps import get_db
from app.core.config import settings
from app.models.user import User
from app.schemas.protocol import WSMessage, WebSocketOpCode
import json

router = APIRouter()

async def get_ws_user(
    token: str = Query(...),
    db: Session = Depends(get_db)
) -> User:
    """
    WebSocket 鉴权依赖项
    在 WebSocket 握手阶段验证 Token。如果验证失败，抛出 HTTP 异常拒绝连接。
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id_str = payload.get("sub")
        if user_id_str is None:
             raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid token payload")
        user_id = int(user_id_str)
    except (JWTError, ValueError):
         raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Could not validate credentials")

    user = db.query(User).filter(User.id == user_id).first()
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
    # 接受连接
    await manager.connect(websocket, game_id)
    
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
