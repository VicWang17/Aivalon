"""
这个文件定义了对局状态的核心数据模型（Schema），用于状态机流转与前端通信。
"""
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
from app.models.game_enums import GamePhase, Character, VoteOption, MissionResult, ActionType

class PlayerState(BaseModel):
    """玩家在单局游戏中的状态"""
    user_id: int
    username: str
    seat_id: int            # 座位号 0-7
    is_ai: bool = False     # 是否为 AI 玩家
    character: Optional[Character] = None # 角色（对本人可见，或结算后公开）
    is_alive: bool = True   # 是否存活（刺杀阶段用）
    
    # 视角相关标记 (用于前端展示)
    is_seen_as_evil: bool = False # 在观察者视角中是否显示为坏人（如梅林看坏人）
    is_seen_as_merlin: bool = False # 在观察者视角中是否显示为梅林候选（如派西维尔看梅林/莫甘娜）

    # 临时状态标记
    has_voted: bool = False
    has_acted: bool = False # 是否已执行任务/刺杀
    
    # AI 记忆字段 (仅服务端可见，用于 LLM 上下文保持)
    ai_memory: str = ""

class ChatMessage(BaseModel):
    user_id: int
    username: str
    content: str
    timestamp: float = 0.0

class GameState(BaseModel):
    """
    对局核心状态快照
    """
    game_id: str
    phase: GamePhase
    phase_start_time: float = 0.0 # 当前阶段开始时间戳
    
    # 基础进度
    round: int = 1          # 当前第几轮任务 (1-5)
    vote_track: int = 0     # 投票失败计数 (0-4)，5次失败会导致任务失败/更换队长
    
    # 当前焦点
    leader_id: Optional[int] = None      # 当前队长 user_id
    speaker_id: Optional[int] = None     # 当前发言玩家 user_id (SPEECH阶段)
    speech_history: List[ChatMessage] = [] # 发言历史记录
    
    # 队伍与投票
    proposed_team: List[int] = []        # 当前提名的队伍成员 user_id 列表
    votes: dict[int, VoteOption] = {}    # 当前投票情况 {user_id: option}
    
    # 玩家列表
    players: List[PlayerState]
    
    # 历史记录
    mission_results: List[MissionResult] = [] # 每一轮任务的结果 (简略版)
    mission_history: List[Dict[str, Any]] = [] # 详细任务历史 (Round, Team, Result, FailCount)
    vote_history: List[Dict[str, Any]] = [] # 投票历史记录 (Round, Leader, Team, Votes, Result)
    pending_mission_results: List[MissionResult] = [] # 当前轮次待结算的任务结果（临时存储）
    winner: Optional[str] = None         # 胜利阵营 (good/evil)

    # 事件序号游标（Write-Behind）：Actor 单写者内存分配 seq，随快照持久化，宕机恢复后续号
    last_event_seq: int = 0

    class Config:
        from_attributes = True

class GameCreateRequest(BaseModel):
    player_ids: List[int]

class GameCreateResponse(BaseModel):
    game_id: str
    initial_state: GameState

class GameActionRequest(BaseModel):
    """
    统一动作请求
    """
    action_type: ActionType
    # 负载数据，根据 action_type 不同而不同
    # PROPOSE: {"target_ids": [1, 2]}
    # VOTE: {"option": "approve"}
    # MISSION: {"result": "success"}
    # ASSASSINATE: {"target_id": 3}
    # SPEAK: {} (暂时为空，或包含语音/文本内容)
    payload: dict = {}

from datetime import datetime
from typing import Any, Dict

class GameSummary(BaseModel):
    """对局历史摘要"""
    id: str
    status: str
    winner: Optional[str] = None
    player_ids: List[int]
    created_at: datetime
    finished_at: Optional[datetime] = None

class RecentGameSummary(BaseModel):
    """最近对局摘要（Redis缓存用）"""
    id: str
    winner: Optional[str] = None
    created_at: datetime
    player_count: int

    class Config:
        from_attributes = True

class GameEventSchema(BaseModel):
    """对局事件详情"""
    id: int
    game_id: str
    seq: int
    event_type: str
    player_id: Optional[int] = None
    payload: Optional[Dict[str, Any]] = None
    created_at: datetime

    class Config:
        from_attributes = True
