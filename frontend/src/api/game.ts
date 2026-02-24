// 这个文件封装了游戏相关的 API 请求
import request from '../utils/request'
import type { 
  ApiResponse,
  CreateGameRequest, 
  CreateGameResponse, 
  GameState, 
  GameActionRequest,
  GameSummary,
  GameEvent
} from '../types/api'

// 创建游戏
export function createGame(data: CreateGameRequest) {
  return request<any, ApiResponse<CreateGameResponse>>({
    url: '/games/',
    method: 'post',
    data
  })
}

// 获取游戏状态
export function getGameState(gameId: string) {
  return request<any, ApiResponse<GameState>>({
    url: `/games/${gameId}`,
    method: 'get'
  })
}

// 提交动作
export function submitAction(gameId: string, data: GameActionRequest) {
  return request<any, ApiResponse<GameState>>({
    url: `/games/${gameId}/action`,
    method: 'post',
    data
  })
}

// 获取游戏历史
export function getGameHistory(params: { skip?: number; limit?: number } = {}) {
  return request<any, GameSummary[]>({
    url: '/games/history',
    method: 'get',
    params
  })
}

// 获取游戏事件流
export function getGameEvents(gameId: string) {
  return request<any, GameEvent[]>({
    url: `/games/${gameId}/events`,
    method: 'get'
  })
}
