"""
这个文件实现了规则型 AI 的决策逻辑服务。
它负责根据当前游戏状态 (GameState) 和 AI 玩家视角 (PlayerState) 返回一个合法的动作。
"""
import random
from typing import Optional, Dict, Any
from app.schemas.game import GameState, PlayerState
from app.models.game_enums import GamePhase, ActionType, VoteOption, MissionResult, Character, Camp
from app.core.game_rules import GameRuleValidator

class AIService:
    @staticmethod
    def get_action(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        """
        获取 AI 玩家的下一步动作
        返回格式: {"action_type": ActionType, "payload": dict}
        """
        if not player.is_ai:
            return None

        # 根据不同阶段决策
        if game.phase == GamePhase.SPEECH:
            return AIService._handle_speech(game, player)
        elif game.phase == GamePhase.TEAM_PROPOSAL:
            return AIService._handle_propose(game, player)
        elif game.phase == GamePhase.VOTE:
            return AIService._handle_vote(game, player)
        elif game.phase == GamePhase.MISSION:
            return AIService._handle_mission(game, player)
        elif game.phase == GamePhase.ASSASSINATION:
            return AIService._handle_assassination(game, player)
            
        return None

    @staticmethod
    def _handle_speech(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        """
        发言阶段：AI 目前仅简单的结束发言
        """
        if game.speaker_id != player.user_id:
            return None
            
        # 简单的发言内容
        phrases = [
            "我是好人，请相信我。",
            "这一轮我觉得应该多观察。",
            "没什么好说的，过。",
            "大家都聊聊吧。",
            "我是派西维尔，我看到了一些信息。" if player.character == Character.PERCIVAL else "我是好人。"
        ]
        
        # 这里的 ActionType.SPEAK 既包含内容也包含结束发言的信号
        # 目前后端的 process_action 处理 SPEAK 时会自动轮转
        return {
            "action_type": ActionType.SPEAK,
            "payload": {
                "content": random.choice(phrases),
                "is_end": True
            }
        }

    @staticmethod
    def _handle_propose(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        """
        提名阶段：AI 队长选择队伍
        """
        if game.leader_id != player.user_id:
            return None
            
        # 获取本轮所需人数
        required_count = GameRuleValidator.get_mission_team_size(game.round)
        
        # 策略：
        # 1. 总是包含自己
        # 2. 随机选择其他人
        
        candidates = [p.user_id for p in game.players if p.user_id != player.user_id]
        # 确保候选人足够
        if len(candidates) < required_count - 1:
            # 异常情况，可能人数不对
            return None
            
        selected_others = random.sample(candidates, required_count - 1)
        target_ids = [player.user_id] + selected_others
        
        return {
            "action_type": ActionType.PROPOSE,
            "payload": {"target_ids": target_ids}
        }

    @staticmethod
    def _handle_vote(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        """
        投票阶段：AI 投票
        """
        # 如果已经投过票，不做动作
        if player.has_voted:
            return None
            
        # 策略：
        # 1. 简单随机 (60% 同意)
        # 2. 如果自己在队伍里，极大概率同意
        
        is_in_team = player.user_id in game.proposed_team
        
        if is_in_team:
            vote = VoteOption.APPROVE
        else:
            vote = VoteOption.APPROVE if random.random() > 0.4 else VoteOption.REJECT
            
        return {
            "action_type": ActionType.VOTE,
            "payload": {"option": vote}
        }

    @staticmethod
    def _handle_mission(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        """
        执行阶段：AI 提交任务结果
        """
        # 只有在队伍里且未行动的才需要操作
        if player.user_id not in game.proposed_team or player.has_acted:
            return None
            
        # 策略：
        # 1. 好人 -> 必须成功
        # 2. 坏人 -> 视情况失败
        
        # 判断阵营
        is_evil = player.character in [Character.ASSASSIN, Character.MORGANA, Character.MINION]
        
        if not is_evil:
            result = MissionResult.SUCCESS
        else:
            # 坏人策略：
            # 第1轮通常装好人 (10% 概率投失败)
            # 后面轮次大概率投失败 (80% 概率)
            # 如果是第4轮（需要2个失败），必须投失败
            if game.round == 1:
                result = MissionResult.FAIL if random.random() < 0.1 else MissionResult.SUCCESS
            else:
                result = MissionResult.FAIL if random.random() < 0.8 else MissionResult.SUCCESS
                
        return {
            "action_type": ActionType.MISSION,
            "payload": {"result": result}
        }

    @staticmethod
    def _handle_assassination(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        """
        刺杀阶段：AI 刺客选择目标
        """
        if player.character != Character.ASSASSIN:
            return None
            
        # 策略：
        # 随机从非坏人阵营中选一个
        # 排除自己和已知的坏人队友
        
        # 这里简化处理：排除掉坏人角色（假设刺客知道谁是坏人 - 实际上刺客确实知道）
        evil_characters = [Character.ASSASSIN, Character.MORGANA, Character.MINION]
        
        potential_targets = []
        for p in game.players:
            if p.character not in evil_characters:
                potential_targets.append(p.user_id)
                
        if not potential_targets:
            # 异常情况，随便选一个非自己的
            potential_targets = [p.user_id for p in game.players if p.user_id != player.user_id]
            
        target_id = random.choice(potential_targets)
        
        return {
            "action_type": ActionType.ASSASSINATE,
            "payload": {"target_id": target_id}
        }
