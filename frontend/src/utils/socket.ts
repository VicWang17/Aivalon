// 这个文件封装了 WebSocket 客户端逻辑，处理连接、鉴权、心跳和消息分发。
import { getToken } from './auth';
import { WebSocketOpCode } from '../types/protocol';
import type { WSMessage } from '../types/protocol';

type MessageHandler = (message: WSMessage) => void;

export class GameSocket {
  private ws: WebSocket | null = null;
  private url: string;
  private messageHandlers: MessageHandler[] = [];
  private gameId: string;
  private heartbeatInterval: number | null = null;
  private reconnectTimer: number | null = null;
  private reconnectAttempts = 0;
  private maxReconnectAttempts = 5;
  private baseReconnectDelay = 1000; // 1s
  private isExplicitDisconnect = false;

  constructor(gameId: string) {
    this.gameId = gameId;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    // 连接到 /api/v1/ws/games/{gameId}
    this.url = `${protocol}//${host}/api/v1/ws/games/${gameId}`;
  }

  public connect() {
    this.isExplicitDisconnect = false;
    const token = getToken();
    if (!token) {
      console.error("No token found for WebSocket connection");
      return;
    }

    const wsUrl = `${this.url}?token=${token}`;
    console.log(`Connecting to WebSocket: ${wsUrl}`);
    this.ws = new WebSocket(wsUrl);

    this.ws.onopen = () => {
      console.log(`WebSocket connected to game ${this.gameId}`);
      this.reconnectAttempts = 0; // 重置重连次数
      this.startHeartbeat();
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as WSMessage;
        this.notifyHandlers(data);
      } catch (e) {
        console.error("Failed to parse WS message", e);
      }
    };

    this.ws.onclose = (event) => {
      console.log("WebSocket closed", event.code, event.reason);
      this.stopHeartbeat();
      if (!this.isExplicitDisconnect) {
        this.scheduleReconnect();
      }
    };

    this.ws.onerror = (error) => {
      console.error("WebSocket error", error);
    };
  }

  public disconnect() {
    this.isExplicitDisconnect = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.stopHeartbeat();
  }

  public send(message: WSMessage) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message));
    } else {
      console.warn("WebSocket is not open, cannot send message");
    }
  }

  public onMessage(handler: MessageHandler) {
    this.messageHandlers.push(handler);
  }

  public removeMessageHandler(handler: MessageHandler) {
     this.messageHandlers = this.messageHandlers.filter(h => h !== handler);
  }

  private notifyHandlers(message: WSMessage) {
    this.messageHandlers.forEach(handler => handler(message));
  }

  private startHeartbeat() {
    this.stopHeartbeat();
    this.heartbeatInterval = window.setInterval(() => {
      this.send({
        type: WebSocketOpCode.HEARTBEAT
      });
    }, 30000); // 30s
  }

  private stopHeartbeat() {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
  }

  private scheduleReconnect() {
    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.error("Max reconnect attempts reached, giving up.");
      return;
    }

    const delay = this.baseReconnectDelay * Math.pow(2, this.reconnectAttempts);
    console.log(`Scheduling reconnect attempt ${this.reconnectAttempts + 1} in ${delay}ms`);

    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectAttempts++;
      this.connect();
    }, delay);
  }
}
