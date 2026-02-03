// 这个文件是前端WebSocket通信协议的类型定义，对应后端的protocol.py，确保前后端实时通信消息结构的一致性。
// WebSocket 操作码/事件类型
export const WebSocketOpCode = {
  // 客户端上行事件 (Client -> Server)
  JOIN_GAME: "join_game",
  PLAYER_ACTION: "player_action",
  CHAT_MESSAGE: "chat_message",
  HEARTBEAT: "heartbeat",

  // 服务端下行事件 (Server -> Client)
  GAME_SNAPSHOT: "game_snapshot",
  STATE_UPDATE: "state_update",
  ERROR: "error",
  PONG: "pong",
} as const;

export type WebSocketOpCodeType = typeof WebSocketOpCode[keyof typeof WebSocketOpCode];

// WebSocket 消息信封
export interface WSMessage<T = any> {
  type: WebSocketOpCodeType;
  payload?: T;
  timestamp?: number;
}

// 示例 Payload 类型
export interface JoinGamePayload {
  gameId: string;
  token: string;
}

export interface PlayerActionPayload {
  actionType: string;
  targetId?: string;
  metadata?: Record<string, any>;
}
