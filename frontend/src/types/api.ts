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

// 游戏相关类型定义
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
  MORGANA: "morgana",
  ASSASSIN: "assassin",
  MINION: "minion",
  OBERON: "oberon",
  MORDRED: "mordred"
} as const;
export type Character = typeof Character[keyof typeof Character];

export const VoteOption = {
  APPROVE: "approve",
  REJECT: "reject"
} as const;
export type VoteOption = typeof VoteOption[keyof typeof VoteOption];

export const MissionResult = {
  SUCCESS: "success",
  FAIL: "fail"
} as const;
export type MissionResult = typeof MissionResult[keyof typeof MissionResult];

export const ActionType = {
  PROPOSE: "propose",
  VOTE: "vote",
  MISSION: "mission",
  ASSASSINATE: "assassinate",
  SPEAK: "speak"
} as const;
export type ActionType = typeof ActionType[keyof typeof ActionType];

export interface GameSummary {
  id: string;
  status: string;
  winner: string | null;
  player_ids: number[];
  created_at: string;
  finished_at: string | null;
}

export interface GameEvent {
  id: number;
  game_id: string;
  seq: number;
  event_type: string;
  player_id: number | null;
  payload: any;
  created_at: string;
}

export interface PlayerState {
  user_id: number;
  username: string;
  seat_id: number;
  character?: Character;
  is_alive: boolean;
  is_seen_as_evil: boolean;
  is_seen_as_merlin: boolean;
  has_voted: boolean;
  has_acted: boolean;
}

export interface ChatMessage {
  user_id: number;
  username: string;
  content: string;
  timestamp: number;
}

export interface GameState {
  game_id: string;
  phase: GamePhase;
  phase_start_time: number;
  round: number;
  vote_track: number;
  leader_id?: number;
  speaker_id?: number;
  speech_history: ChatMessage[];
  proposed_team: number[];
  votes: Record<number, VoteOption>;
  players: PlayerState[];
  mission_results: MissionResult[];
  winner?: string;
}

export interface CreateGameRequest {
  player_ids: number[];
}

export interface CreateGameResponse {
  game_id: string;
  initial_state: GameState;
}

export interface GameActionRequest {
  action_type: ActionType;
  payload: Record<string, any>;
}
