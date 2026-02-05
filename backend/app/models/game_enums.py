"""
这个文件用于定义对局相关的枚举状态（如游戏阶段、角色身份、阵营等）。
"""
from enum import Enum

class GamePhase(str, Enum):
    """
    对局阶段枚举
    """
    # 选队长/等待开始（通常是自动轮转，但也可能需要前端展示“轮到谁了”）
    LEADER_SELECTION = "leader_selection"
    
    # 发言阶段（轮席发言，Sequential Speech）
    SPEECH = "speech"
    
    # 提名阶段（队长选择出任务的队员）
    TEAM_PROPOSAL = "team_proposal"
    
    # 投票阶段（全体玩家对提名的队伍进行投票）
    VOTE = "vote"
    
    # 执行阶段（任务队伍成员提交成功/失败）
    MISSION = "mission"
    
    # 刺杀阶段（好人3胜后，刺客刺杀梅林）
    ASSASSINATION = "assassination"
    
    # 结算阶段（游戏结束，展示结果）
    FINISHED = "finished"

class Character(str, Enum):
    """
    角色身份枚举
    """
    MERLIN = "merlin"       # 梅林
    PERCIVAL = "percival"   # 派西维尔
    SERVANT = "servant"     # 忠臣
    ASSASSIN = "assassin"   # 刺客
    MORGANA = "morgana"     # 莫甘娜
    MINION = "minion"       # 爪牙

class Camp(str, Enum):
    """
    阵营枚举
    """
    GOOD = "good"  # 好人（蓝方）
    EVIL = "evil"  # 坏人（红方）

class MissionResult(str, Enum):
    """
    任务结果枚举
    """
    SUCCESS = "success"
    FAIL = "fail"

class VoteOption(str, Enum):
    """
    投票选项枚举
    """
    APPROVE = "approve" # 同意
    REJECT = "reject"   # 反对

class ActionType(str, Enum):
    """
    玩家动作类型枚举
    """
    PROPOSE = "propose"         # 提名（队长选择队伍）
    VOTE = "vote"               # 投票（全体表决）
    MISSION = "mission"         # 执行任务（成功/失败）
    ASSASSINATE = "assassinate" # 刺杀（刺客刺杀梅林）
    SPEAK = "speak"             # 发言（轮席发言）
