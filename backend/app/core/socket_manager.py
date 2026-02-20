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

manager = ConnectionManager()
