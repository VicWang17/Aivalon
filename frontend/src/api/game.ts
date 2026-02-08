// 这个文件封装了游戏相关的 API 请求
import request from '../utils/request'
import type { 
  ApiResponse,
  CreateGameRequest, 
  CreateGameResponse, 
  GameState, 
  GameActionRequest 
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
