// 这个文件是前端API接口的通用类型定义，对应后端的ResponseModel，用于Axios请求响应的数据类型推断。
// 统一 API 响应结构
export interface ApiResponse<T = any> {
  code: number;
  message: string;
  data: T;
}

// 业务错误码定义
export const ErrorCode = {
  SUCCESS: 0,
  UNAUTHORIZED: 401,
  FORBIDDEN: 403,
  VALIDATION_ERROR: 422,
  GAME_NOT_FOUND: 10001,
  INVALID_ACTION: 10002,
} as const;

export type ErrorCodeType = typeof ErrorCode[keyof typeof ErrorCode];

// 登录参数
export interface LoginData {
  username: string;
  password: string;
}

// 登录响应
export interface LoginResponse {
  access_token: string;
  token_type: string;
}

// 注册参数
export interface RegisterData {
  username: string;
  email: string;
  password: string;
  verification_code: string;
}

// 发送验证码参数
export interface SendCodeData {
  email: string;
}

// 用户信息
export interface UserInfo {
  id: number;
  username: string;
  email: string;
  is_active: boolean;
  created_at: string;
}

// 排行榜数据
export interface LeaderboardEntry {
  user_id: number;
  username: string;
  total_games: number;
  wins_good: number;
  wins_evil: number;
  total_wins: number;
  win_rate: number;
}

// ==========================================
// 游戏相关枚举与类型定义
// ==========================================

export const GamePhase = {
  LEADER_SELECTION: "leader_selection",
  SPEECH: "speech",
  TEAM_PROPOSAL: "team_proposal",
  VOTE: "vote",
  MISSION: "mission",
  ASSASSINATION: "assassination",
  FINISHED: "finished"
} as const;

export type GamePhase = typeof GamePhase[keyof typeof GamePhase];

export const Character = {
  MERLIN: "merlin",
  PERCIVAL: "percival",
  SERVANT: "servant",
  ASSASSIN: "assassin",
  MORGANA: "morgana",
  MINION: "minion",
  OBERON: "oberon",
  MORDRED: "mordred"
} as const;

export type Character = typeof Character[keyof typeof Character];

export const Camp = {
  GOOD: "good",
  EVIL: "evil"
} as const;

export type Camp = typeof Camp[keyof typeof Camp];

export const MissionResult = {
  SUCCESS: "success",
  FAIL: "fail"
} as const;

export type MissionResult = typeof MissionResult[keyof typeof MissionResult];

export const VoteOption = {
  APPROVE: "approve",
  REJECT: "reject"
} as const;

export type VoteOption = typeof VoteOption[keyof typeof VoteOption];

export const ActionType = {
  PROPOSE: "propose",
  VOTE: "vote",
  MISSION: "mission",
  ASSASSINATE: "assassinate",
  SPEAK: "speak"
} as const;

export type ActionType = typeof ActionType[keyof typeof ActionType];

// 玩家状态
export interface PlayerState {
  user_id: number;
  username: string;
  seat_id: number;
  is_ai: boolean;
  character?: Character | string;
  camp?: Camp | string;
  has_voted: boolean;
  has_acted: boolean;
  is_alive?: boolean;
}

// 游戏状态
export interface GameState {
  id: string;
  phase: GamePhase;
  round: number;
  leader_id: number;
  speaker_id?: number;
  players: PlayerState[];
  proposed_team: number[];
  mission_results: MissionResult[];
  vote_track: number;
  vote_history: any[];
  winner?: Camp | string;
  created_at: string;
  votes?: Record<string, VoteOption>;
}

// 创建游戏请求
export interface CreateGameRequest {
  player_ids: number[];
}

// 创建游戏响应
export interface CreateGameResponse {
  game_id: string;
  initial_state: GameState;
}

// 游戏动作请求
export interface GameActionRequest {
  action_type: ActionType;
  payload: any;
}

// 游戏历史摘要
export interface GameSummary {
  id: string;
  status: string;
  winner: string;
  created_at: string;
  finished_at?: string;
  players: {
    user_id: number;
    username: string;
    character: string;
    is_winner: boolean;
  }[];
}

// 游戏事件
export interface GameEvent {
  id?: number;
  game_id: string;
  seq: number;
  event_type: ActionType;
  player_id: number;
  payload: any;
  created_at?: string;
}
