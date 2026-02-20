"""
这个文件实现了对局相关的核心业务逻辑（Service层），包括创建对局、处理动作等。
目前使用内存存储对局状态。
"""
from typing import List, Dict, Optional
import uuid
import random
import time
from fastapi import HTTPException, status
from app.schemas.game import GameState, PlayerState, ChatMessage
from app.models.game_enums import GamePhase, Character, Camp, ActionType, VoteOption, MissionResult

# 内存存储，用于临时保存对局状态
# key: game_id, value: GameState
games: Dict[str, GameState] = {}

# 坏人角色集合
EVIL_CHARACTERS = {Character.ASSASSIN, Character.MORGANA, Character.MINION}
# 坏人可见的角色（通常是所有坏人，除了奥伯伦）
# 在 MVP 8人局中，没有奥伯伦，所有坏人互见
EVIL_VISIBLE_CHARACTERS = EVIL_CHARACTERS

from app.core.socket_manager import manager
from app.schemas.protocol import WSMessage, WebSocketOpCode

class GameService:
    @staticmethod
    async def create_game(player_ids: List[int], user_map: Dict[int, str], creator_id: Optional[int] = None) -> GameState:
        """
        创建一个新的对局
        :param player_ids: 玩家ID列表
        :param user_map: 用户ID到用户名的映射
        :param creator_id: 创建者ID (用于标记真实玩家)
        :return: 初始化的游戏状态
        """
        # 1. 生成 Game ID
        game_id = str(uuid.uuid4())
        
        # 2. 初始化玩家列表并随机分配座位
        shuffled_ids = player_ids.copy()
        random.shuffle(shuffled_ids)
        
        players: List[PlayerState] = []
        for seat_id, uid in enumerate(shuffled_ids):
            # 只有创建者是真人，其余默认为 AI
            # (后续可扩展为支持多真人)
            is_ai_player = True
            if creator_id is not None and uid == creator_id:
                is_ai_player = False
                
            players.append(PlayerState(
                user_id=uid,
                username=user_map.get(uid, f"User_{uid}"),
                seat_id=seat_id,
                is_ai=is_ai_player
            ))
            
        # 3. 分配角色
        num_players = len(players)
        
        # 目前仅支持 8 人局
        if num_players != 8:
            raise ValueError(f"当前版本仅支持 8 人局，实际人数: {num_players}")

        # 8人局标准配置：
        # 好人阵营 (5人): 梅林, 派西维尔, 忠臣 * 3
        # 坏人阵营 (3人): 莫甘娜, 刺客, 爪牙 * 1
        roles = [
            Character.MERLIN, 
            Character.PERCIVAL, 
            Character.SERVANT, Character.SERVANT, Character.SERVANT,
            Character.MORGANA, 
            Character.ASSASSIN, 
            Character.MINION
        ]
            
        random.shuffle(roles)
        
        for i, player in enumerate(players):
            player.character = roles[i]
        
        # 4. 初始化游戏状态
        # 随机选一个队长
        initial_leader_id = players[0].user_id
        
        initial_state = GameState(
            game_id=game_id,
            phase=GamePhase.SPEECH, # 初始进入发言阶段
            phase_start_time=time.time(),
            players=players,
            leader_id=initial_leader_id,
            speaker_id=initial_leader_id # 队长开始发言
        )
        
        # 5. 保存到内存
        games[game_id] = initial_state
        
        # TODO: 初始阶段如果是 AI 队长发言，需要触发 AI
        # if initial_state.speaker_id is AI:
        #    trigger_ai()
        
        return initial_state

    @staticmethod
    def get_game(game_id: str) -> Optional[GameState]:
        return games.get(game_id)

    @staticmethod
    def get_player_view(game: GameState, viewer_id: int) -> GameState:
        """
        获取特定玩家视角的对局快照（进行数据脱敏）
        """
        # 找到观察者
        viewer = next((p for p in game.players if p.user_id == viewer_id), None)
        if not viewer:
            # 如果观察者不在游戏中，直接报错
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You are not a player in this game"
            )
            
        viewer_role = viewer.character

        # 构建新的 players 列表
        masked_players = []
        for p in game.players:
            # 复制玩家对象
            p_copy = p.model_copy()
            
            # 默认隐藏
            should_hide_character = True
            p_copy.is_seen_as_evil = False
            p_copy.is_seen_as_merlin = False

            # 1. 游戏结束或自己看自己 -> 显示
            if game.phase == GamePhase.FINISHED or p.user_id == viewer_id:
                should_hide_character = False
            
            # 2. 视角规则
            elif viewer_role:
                # 坏人视角：看到其他坏人
                if viewer_role in EVIL_CHARACTERS:
                    if p.character in EVIL_VISIBLE_CHARACTERS:
                        should_hide_character = False # 坏人互知身份
                
                # 梅林视角：看到坏人（不知道具体身份，只显示坏人标记）
                elif viewer_role == Character.MERLIN:
                    if p.character in EVIL_CHARACTERS:
                        p_copy.is_seen_as_evil = True
                        # character 依然隐藏
                
                # 派西维尔视角：看到梅林和莫甘娜（显示梅林标记）
                elif viewer_role == Character.PERCIVAL:
                    if p.character in {Character.MERLIN, Character.MORGANA}:
                        p_copy.is_seen_as_merlin = True
                        # character 依然隐藏

            if should_hide_character:
                p_copy.character = None
                
            masked_players.append(p_copy)

        # 返回新的 GameState
        game_copy = game.model_copy(update={"players": masked_players})
        return game_copy

    @staticmethod
    async def process_action(game_id: str, user_id: int, action_type: ActionType, payload: dict) -> GameState:
        """
        处理玩家动作（统一入口）
        """
        game = games.get(game_id)
        if not game:
            raise HTTPException(status_code=404, detail="Game not found")

        # 1. 规则校验
        # 这里的 validator 应该是无状态的，或者传入 game state
        # 我们假设 GameRuleValidator.validate_action(game, user_id, action_type, payload)
        # 注意：需要从 app.core.game_rules 导入 GameRuleValidator，为了避免循环依赖，可以在函数内导入
        from app.core.game_rules import GameRuleValidator
        
        # 校验动作是否合法
        # 如果不合法，validator 会抛出异常（或者返回 False，这里假设它抛异常更方便）
        try:
            GameRuleValidator.validate_action(game, user_id, action_type, payload)
        except ValueError as e:
             raise HTTPException(status_code=400, detail=str(e))

        # 2. 执行动作（状态机流转）
        # 这里需要一个 StateMachine 或类似的逻辑来更新 game state
        # 暂时在 Service 内部简单实现流转逻辑，后续可拆分
        
        # 找到当前操作的玩家
        player = next((p for p in game.players if p.user_id == user_id), None)
        if not player:
             raise HTTPException(status_code=403, detail="Player not in game")

        # --- PROPOSE (提名) ---
        if action_type == ActionType.PROPOSE:
            target_ids = payload.get("target_ids", [])
            game.proposed_team = target_ids
            # 进入投票阶段
            game.phase = GamePhase.VOTE
            game.phase_start_time = time.time()
            # 重置投票状态
            game.votes = {}
            for p in game.players:
                p.has_voted = False

        # --- VOTE (投票) ---
        elif action_type == ActionType.VOTE:
            option = payload.get("option")
            game.votes[user_id] = option
            player.has_voted = True
            
            # 检查是否所有人都投了
            if all(p.has_voted for p in game.players):
                # 结算投票结果
                approve_count = sum(1 for v in game.votes.values() if v == VoteOption.APPROVE)
                if approve_count > len(game.players) / 2:
                    # 投票通过 -> 进入任务阶段
                    game.phase = GamePhase.MISSION
                    game.vote_track = 0 # 重置投票失败计数
                    # 重置行动状态（用于记录谁执行了任务）
                    for p in game.players:
                        p.has_acted = False
                else:
                    # 投票失败
                    game.vote_track += 1
                    if game.vote_track >= 5:
                        # 连续5次失败 -> 坏人直接获胜
                        game.phase = GamePhase.FINISHED
                        game.winner = Camp.EVIL
                    else:
                        # 换下一个队长
                        # 找到当前队长索引
                        current_leader_idx = next(i for i, p in enumerate(game.players) if p.user_id == game.leader_id)
                        next_leader_idx = (current_leader_idx + 1) % len(game.players)
                        game.leader_id = game.players[next_leader_idx].user_id
                        # 回到发言阶段
                        game.phase = GamePhase.SPEECH
                        game.speaker_id = game.leader_id
                        game.proposed_team = []
                        # 重置发言状态
                        for p in game.players:
                            p.has_acted = False
                
                game.phase_start_time = time.time()

        # --- MISSION (执行任务) ---
        elif action_type == ActionType.MISSION:
            result = payload.get("result")
            # 只有在队伍里的人才能提交，这点已经在 validate_action 里校验过了
            # 这里我们需要记录谁提交了，但不能记录具体是谁投了什么（匿名）
            # 所以我们通常把结果存到一个临时列表里，等人齐了再 shuffle
            # 但为了简单，我们先不存中间状态，只标记 has_acted
            # 实际结果需要找个地方存... GameState 里好像没有 pending_mission_results
            # MVP 简单做法：直接存到 mission_results 的当前轮次里？不行，那是最终结果
            # 我们需要在 GameState 加一个临时字段，或者...
            # 让我们简化一下：假设 payload 里的 result 是真实的
            # 我们需要一个地方暂存本轮收到的结果
            if not hasattr(game, "pending_mission_results"):
                game.pending_mission_results = [] # 这是一个 hack，最好加到 Schema 里
            
            game.pending_mission_results.append(result)
            player.has_acted = True
            
            # 检查是否所有队员都提交了
            team_size = len(game.proposed_team)
            if len(game.pending_mission_results) >= team_size:
                # 结算任务
                fail_count = game.pending_mission_results.count(MissionResult.FAIL)
                
                # 判断失败条件
                # 8人局：3-4-4-5-5
                # 第4轮（5人）需要2个fail才失败，其他都是1个
                is_failed = False
                if game.round == 4:
                    if fail_count >= 2:
                        is_failed = True
                else:
                    if fail_count >= 1:
                        is_failed = True
                
                final_result = MissionResult.FAIL if is_failed else MissionResult.SUCCESS
                game.mission_results.append(final_result)
                
                # 清理临时状态
                game.pending_mission_results = []
                game.proposed_team = []
                
                # 检查游戏是否结束
                fails_total = game.mission_results.count(MissionResult.FAIL)
                success_total = game.mission_results.count(MissionResult.SUCCESS)
                
                if fails_total >= 3:
                    # 坏人 3 胜
                    game.phase = GamePhase.FINISHED
                    game.winner = Camp.EVIL
                elif success_total >= 3:
                    # 好人 3 胜 -> 进入刺杀阶段
                    game.phase = GamePhase.ASSASSINATION
                else:
                    # 继续下一轮
                    game.round += 1
                    # 换下一个队长
                    current_leader_idx = next(i for i, p in enumerate(game.players) if p.user_id == game.leader_id)
                    next_leader_idx = (current_leader_idx + 1) % len(game.players)
                    game.leader_id = game.players[next_leader_idx].user_id
                    
                    game.phase = GamePhase.SPEECH
                    game.speaker_id = game.leader_id
                    # 重置发言状态
                    for p in game.players:
                        p.has_acted = False
                
                game.phase_start_time = time.time()

        # --- ASSASSINATE (刺杀) ---
        elif action_type == ActionType.ASSASSINATE:
            target_id = payload.get("target_id")
            target = next((p for p in game.players if p.user_id == target_id), None)
            
            if target and target.character == Character.MERLIN:
                game.winner = Camp.EVIL
            else:
                game.winner = Camp.GOOD
                
            game.phase = GamePhase.FINISHED
            game.phase_start_time = time.time()

        # --- SPEAK (发言) ---
        elif action_type == ActionType.SPEAK:
            content = payload.get("content")
            is_end = payload.get("is_end", False)
            
            # 校验权限
            if game.speaker_id != user_id:
                 raise HTTPException(status_code=403, detail="Not your turn to speak")

            # 1. 记录发言
            if content:
                msg = ChatMessage(
                    user_id=user_id,
                    username=player.username,
                    content=content,
                    timestamp=time.time()
                )
                game.speech_history.append(msg)

            # 2. 处理结束发言
            if is_end:
                player.has_acted = True # 标记已完成发言
                
                # 寻找下一个未发言的玩家
                next_speaker_id = None
                current_seat = player.seat_id
                num_players = len(game.players)
                
                # 从下一位开始找
                for i in range(1, num_players + 1):
                    idx = (current_seat + i) % num_players
                    p = game.players[idx]
                    if not p.has_acted:
                        next_speaker_id = p.user_id
                        break
                
                if next_speaker_id:
                    game.speaker_id = next_speaker_id
                    
                    # TODO: 检测 next_speaker_id 是否为 AI
                    # 如果是 AI，则在此处触发 LLM 生成任务（异步）
                    # next_player = next(p for p in game.players if p.user_id == next_speaker_id)
                    # if next_player.is_ai:
                    #     background_tasks.add_task(ai_service.generate_speech, game_id, next_speaker_id)
                    
                else:
                    # 所有人都发言完毕，进入提名阶段
                    game.phase = GamePhase.TEAM_PROPOSAL
                    game.speaker_id = None
                    # 重置 acted 状态
                    for p in game.players:
                        p.has_acted = False
                
                game.phase_start_time = time.time()

        # 3. 广播状态更新事件
        # 前端收到此消息后，应立即调用 GET /games/{game_id} 拉取最新状态
        # 这种方式避免了在 WebSocket 层处理复杂的视角脱敏逻辑
        try:
            update_msg = WSMessage(
                type=WebSocketOpCode.STATE_UPDATE,
                payload={"game_id": game_id}
            )
            await manager.broadcast(game_id, update_msg)
        except Exception as e:
            # 广播失败不应影响主流程
            print(f"Broadcast failed: {e}")

        return game
