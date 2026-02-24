# 这个文件定义了WebSocket连接管理器，负责管理所有活跃的WebSocket连接，支持按房间（game_id）广播消息。
from typing import Dict, List
from fastapi import WebSocket
import json
from app.schemas.protocol import WSMessage, WebSocketOpCode

class ConnectionManager:
    def __init__(self):
        # 存储格式: {game_id: [websocket1, websocket2, ...]}
        # 也可以考虑更细粒度: {game_id: {user_id: websocket}} 以便定向发送
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, game_id: str):
        await websocket.accept()
        if game_id not in self.active_connections:
            self.active_connections[game_id] = []
        self.active_connections[game_id].append(websocket)

    def disconnect(self, websocket: WebSocket, game_id: str):
        if game_id in self.active_connections:
            if websocket in self.active_connections[game_id]:
                self.active_connections[game_id].remove(websocket)
            if not self.active_connections[game_id]:
                del self.active_connections[game_id]

    async def broadcast(self, game_id: str, message: WSMessage):
        """向指定房间的所有连接广播消息"""
        if game_id in self.active_connections:
            # 序列化消息
            msg_json = message.model_dump_json()
            # 复制列表进行迭代，防止发送过程中连接断开导致修改列表报错
            for connection in list(self.active_connections[game_id]):
                try:
                    await connection.send_text(msg_json)
                except Exception:
                    # 发送失败通常意味着连接已断开，可以在这里处理，或者依赖 disconnect 回调
                    pass

    async def broadcast_game_update(self, game_id: str, game_state: object):
        """
        广播对局状态更新消息
        前端收到此消息后，应立即调用 GET /games/{game_id} 拉取最新状态
        """
        # 注意：这里 game_state 的类型是 GameState，为了避免循环导入，用了 object
        # 实际上我们只需要 game_id，但为了扩展性可以传更多信息
        # 目前主要通知前端去拉取，所以 payload 可以简化
        try:
            update_msg = WSMessage(
                type=WebSocketOpCode.STATE_UPDATE,
                payload={
                    "game_id": game_id,
                    "phase": game_state.phase if hasattr(game_state, 'phase') else None,
                    "timestamp": getattr(game_state, 'phase_start_time', 0.0)
                }
            )
            await self.broadcast(game_id, update_msg)
        except Exception as e:
            # 广播失败不应影响主流程
            print(f"Broadcast failed: {e}")

manager = ConnectionManager()
