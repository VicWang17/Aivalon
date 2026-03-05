import request from '../utils/request'
import type { ApiResponse, LeaderboardEntry } from '../types/api'

export function getLeaderboard(type: 'total' | 'good' | 'evil' = 'total', limit: number = 10) {
  // request.get 返回 Promise<any> 因为拦截器修改了返回值
  // 我们手动断言它返回的是 ApiResponse<LeaderboardEntry[]>
  return request.get<any, ApiResponse<LeaderboardEntry[]>>('/users/leaderboard', {
    params: { type, limit }
  })
}
