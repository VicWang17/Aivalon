"""
这个文件实现了对局动作的权限与规则校验逻辑（Rule Engine）。
"""
from typing import List, Optional
from fastapi import HTTPException
from app.models.game_enums import GamePhase, ActionType, Character, Camp, VoteOption, MissionResult
from app.schemas.game import GameState, PlayerState
import time

class TimeoutPolicy:
    """
    超时与兜底策略
    """
    TIMEOUT_SECONDS = 60

    @staticmethod
    def is_timed_out(game: GameState, current_time: float = None) -> bool:
        """判断当前阶段是否已超时"""
        if current_time is None:
            current_time = time.time()
        # 如果 phase_start_time 未设置（0.0），则不认为超时（避免刚初始化时的边界情况）
        if game.phase_start_time <= 0:
            return False
        return (current_time - game.phase_start_time) > TimeoutPolicy.TIMEOUT_SECONDS

    @staticmethod
    def get_default_action(game: GameState, player: PlayerState) -> Optional[dict]:
        """
        获取玩家在当前阶段的默认兜底动作
        返回格式: {"action_type": ActionType, "payload": dict}
        """
        if game.phase == GamePhase.VOTE:
            # 投票阶段：默认投反对
            return {
                "action_type": ActionType.VOTE,
                "payload": {"option": VoteOption.REJECT}
            }
        elif game.phase == GamePhase.MISSION:
            # 任务阶段：默认投成功（好人只能成功，坏人为了隐藏通常也默认成功，或者随机，这里选安全策略）
            return {
                "action_type": ActionType.MISSION,
                "payload": {"result": MissionResult.SUCCESS}
            }
        elif game.phase == GamePhase.TEAM_PROPOSAL:
            # 提名阶段：默认顺位选择（包括自己在内向后顺延）
            # 1. 找到当前队长
            leader = next((p for p in game.players if p.user_id == game.leader_id), None)
            if not leader:
                return None
            
            # 2. 获取本轮需要的人数
            required_count = GameRuleValidator.get_mission_team_size(game.round)
            
            # 3. 按座位号排序玩家
            sorted_players = sorted(game.players, key=lambda p: p.seat_id)
            total_players = len(sorted_players)
            if total_players == 0:
                return None
                
            # 4. 从队长开始往后选N个
            start_index = leader.seat_id
            target_ids = []
            for i in range(required_count):
                idx = (start_index + i) % total_players
                target_ids.append(sorted_players[idx].user_id)
                
            return {
                "action_type": ActionType.PROPOSE,
                "payload": {"target_ids": target_ids}
            }
            
        elif game.phase == GamePhase.ASSASSINATION:
            # 刺杀阶段：默认刺杀下一位
            # 1. 找到刺客
            assassin = next((p for p in game.players if p.character == Character.ASSASSIN), None)
            if not assassin:
                return None
            
            # 2. 按座位号排序
            sorted_players = sorted(game.players, key=lambda p: p.seat_id)
            total_players = len(sorted_players)
            if total_players == 0:
                return None
                
            # 3. 找到刺客的下一位
            next_seat_idx = (assassin.seat_id + 1) % total_players
            target_player = sorted_players[next_seat_idx]
            
            return {
                "action_type": ActionType.ASSASSINATE,
                "payload": {"target_id": target_player.user_id}
            }

        # 其他阶段暂无自动兜底（如发言可能只是跳过）
        return None

class GameRuleValidator:
    """
    游戏规则校验器
    """

    # 8人局任务人数配置
    MISSION_TEAM_SIZES = {1: 3, 2: 4, 3: 4, 4: 5, 5: 5}

    @staticmethod
    def get_mission_team_size(round_num: int) -> int:
        """获取指定轮次所需的任务人数（默认8人局）"""
        return GameRuleValidator.MISSION_TEAM_SIZES.get(round_num, 5)

    @staticmethod
    def is_mission_failed(round_num: int, fail_count: int) -> bool:
        """判断任务是否失败"""
        # 第4轮需要2个失败才算失败
        if round_num == 4:
            return fail_count >= 2
        # 其他轮次只要有1个失败就算失败
        return fail_count >= 1

    @staticmethod
    def validate_action(game: GameState, user_id: int, action_type: ActionType, payload: dict = None):
        """
        统一校验入口
        :raises HTTPException: 如果校验失败，抛出 400/403 异常
        """
        # 1. 基础校验：玩家是否在游戏中
        player = next((p for p in game.players if p.user_id == user_id), None)
        if not player:
            raise HTTPException(status_code=403, detail="玩家不在对局中")

        # 2. 根据动作类型分发校验
        if action_type == ActionType.PROPOSE:
            GameRuleValidator._validate_propose(game, player, payload)
        elif action_type == ActionType.VOTE:
            GameRuleValidator._validate_vote(game, player)
        elif action_type == ActionType.MISSION:
            GameRuleValidator._validate_mission(game, player, payload)
        elif action_type == ActionType.SPEAK:
            GameRuleValidator._validate_speak(game, player)
        elif action_type == ActionType.ASSASSINATE:
            GameRuleValidator._validate_assassinate(game, player, payload)
        else:
            raise HTTPException(status_code=400, detail=f"未知的动作类型: {action_type}")

    @staticmethod
    def _validate_propose(game: GameState, player: PlayerState, payload: dict):
        """校验提名动作"""
        # 阶段必须是 TEAM_PROPOSAL
        if game.phase != GamePhase.TEAM_PROPOSAL:
            raise HTTPException(status_code=400, detail="当前不在提名阶段")
        
        # 必须是队长
        if game.leader_id != player.user_id:
            raise HTTPException(status_code=403, detail="只有队长可以提名")
            
        # 校验人数（需结合规则配置）
        team_ids = payload.get("target_ids", [])
        if not team_ids or not isinstance(team_ids, list):
            raise HTTPException(status_code=400, detail="提名队伍不能为空")
        
        required_count = GameRuleValidator.get_mission_team_size(game.round)
        if len(team_ids) != required_count:
             raise HTTPException(status_code=400, detail=f"第 {game.round} 轮任务需要提名 {required_count} 名玩家")
        
        # 校验提名的人是否在游戏中
        all_player_ids = {p.user_id for p in game.players}
        if not set(team_ids).issubset(all_player_ids):
             raise HTTPException(status_code=400, detail="提名的玩家无效")

    @staticmethod
    def _validate_vote(game: GameState, player: PlayerState):
        """校验投票动作"""
        if game.phase != GamePhase.VOTE:
            raise HTTPException(status_code=400, detail="当前不在投票阶段")
        
        if player.has_voted:
            raise HTTPException(status_code=400, detail="您已经投过票了")

    @staticmethod
    def _validate_mission(game: GameState, player: PlayerState, payload: dict):
        """校验执行任务动作"""
        if game.phase != GamePhase.MISSION:
            raise HTTPException(status_code=400, detail="当前不在任务执行阶段")
            
        # 必须在任务队伍中
        if player.user_id not in game.proposed_team:
            raise HTTPException(status_code=403, detail="您不在执行队伍中")
            
        if player.has_acted:
            raise HTTPException(status_code=400, detail="您已经执行过任务了")

        # 校验好人不能投失败（规则：好人只能投成功，坏人可选）
        # 注意：这里需要知道玩家身份。在真实逻辑中，PlayerState 应该包含身份信息。
        # 如果是好人阵营，强制检查 payload
        # 这里假设 Camp logic 已知，或暂不校验（视 MVP 复杂度）
        # 简单实现：
        result = payload.get("result")
        if player.character in [Character.MERLIN, Character.PERCIVAL, Character.SERVANT]:
             if result == "fail":
                 raise HTTPException(status_code=400, detail="好人阵营只能投任务成功")

    @staticmethod
    def _validate_speak(game: GameState, player: PlayerState):
        """校验发言动作"""
        if game.phase != GamePhase.SPEECH:
            raise HTTPException(status_code=400, detail="当前不在发言阶段")
            
        if game.speaker_id != player.user_id:
            raise HTTPException(status_code=403, detail="当前未轮到您发言")

    @staticmethod
    def _validate_assassinate(game: GameState, player: PlayerState, payload: dict):
        """校验刺杀动作"""
        if game.phase != GamePhase.ASSASSINATION:
            raise HTTPException(status_code=400, detail="当前不在刺杀阶段")
            
        if player.character != Character.ASSASSIN:
            raise HTTPException(status_code=403, detail="只有刺客可以执行刺杀")
            
        target_id = payload.get("target_id")
        if not target_id:
            raise HTTPException(status_code=400, detail="必须指定刺杀目标")
