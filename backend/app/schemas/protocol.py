# 这个文件是WebSocket通信协议的数据模型定义，包含操作码枚举（OpCode）和消息体结构，用于前后端实时通信的类型约束。
from enum import Enum
from typing import Any, Dict, Optional, Union
from pydantic import BaseModel, Field

class WebSocketOpCode(str, Enum):
    """WebSocket 操作码/事件类型"""
    
    # 客户端上行事件 (Client -> Server)
    JOIN_GAME = "join_game"       # 加入对局
    PLAYER_ACTION = "player_action" # 玩家动作（提名、投票等）
    CHAT_MESSAGE = "chat_message"   # 聊天/发言
    HEARTBEAT = "heartbeat"       # 心跳

    # 服务端下行事件 (Server -> Client)
    GAME_SNAPSHOT = "game_snapshot" # 全量对局快照
    STATE_UPDATE = "state_update"   # 状态更新（增量）
    ERROR = "error"                 # 错误通知
    PONG = "pong"                   # 心跳响应

class WSMesssage(BaseModel):
    """
    WebSocket 消息信封
    所有的 WS 通信都应该遵循此格式
    """
    type: WebSocketOpCode
    payload: Optional[Dict[str, Any]] = None
    timestamp: Optional[float] = None  # 服务端时间戳
