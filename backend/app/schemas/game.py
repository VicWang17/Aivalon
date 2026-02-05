"""
这个文件定义了对局状态的核心数据模型（Schema），用于状态机流转与前端通信。
"""
from typing import List, Optional
from pydantic import BaseModel
from app.models.game_enums import GamePhase, Character, VoteOption, MissionResult

class PlayerState(BaseModel):
    """玩家在单局游戏中的状态"""
    user_id: int
    username: str
    seat_id: int            # 座位号 0-7
    character: Optional[Character] = None # 角色（对本人可见，或结算后公开）
    is_alive: bool = True   # 是否存活（刺杀阶段用）
    
    # 临时状态标记
    has_voted: bool = False
    has_acted: bool = False # 是否已执行任务/刺杀

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
    
    # 队伍与投票
    proposed_team: List[int] = []        # 当前提名的队伍成员 user_id 列表
    votes: dict[int, VoteOption] = {}    # 当前投票情况 {user_id: option}
    
    # 玩家列表
    players: List[PlayerState]
    
    # 历史记录
    mission_results: List[MissionResult] = [] # 每一轮任务的结果
    winner: Optional[str] = None         # 胜利阵营 (good/evil)

    class Config:
        from_attributes = True
