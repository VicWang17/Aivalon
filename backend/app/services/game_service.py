"""
这个文件实现了对局相关的核心业务逻辑（Service层），包括创建对局、处理动作等。
目前使用内存存储对局状态。
"""
from typing import List, Dict, Optional
import uuid
import random
import time
import asyncio
from fastapi import HTTPException, status
from app.schemas.game import GameState, PlayerState, ChatMessage
from app.models.game_enums import GamePhase, Character, Camp, ActionType, VoteOption, MissionResult
from app.models.game import Game as GameModel, GameEvent as GameEventModel
from app.models.user import User
from app.db.base import SessionLocal
from sqlalchemy.orm import Session
from sqlalchemy import func
import json
import copy
from app.core.redis import redis_client
from app.core.lock import GameLock

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
from app.services.ai_service import AIService

from app.services.rank_service import RankService

class GameService:
    @staticmethod
    def _append_event(db: Session, game_id: str, event_type: str, player_id: Optional[int], payload: dict) -> None:
        """
        向 game_events 表追加事件，并自动处理 seq (带乐观锁重试)
        """
        from sqlalchemy.exc import IntegrityError
        
        max_retries = 20  # 提高重试次数以应对高并发
        for attempt in range(max_retries):
            try:
                # 使用 savepoint 隔离每次尝试
                with db.begin_nested():
                    # 1. 重新获取当前最大 seq (READ COMMITTED 下可见最新提交)
                    # 这样可以避免盲目递增导致的竞争落后
                    last_event = db.query(GameEventModel).filter(
                        GameEventModel.game_id == game_id
                    ).order_by(GameEventModel.seq.desc()).first()
                    
                    next_seq = 1
                    if last_event:
                        next_seq = last_event.seq + 1
                        
                    # 2. 尝试插入新事件
                    new_event = GameEventModel(
                        game_id=game_id,
                        seq=next_seq,
                        event_type=event_type,
                        player_id=player_id,
                        payload=payload
                    )
                    db.add(new_event)
                    db.flush() # 立即触发唯一约束检查
                
                # 成功则跳出循环
                break
                
            except IntegrityError:
                # 发生冲突（seq 被抢占）
                if attempt == max_retries - 1:
                    # 超过重试次数，抛出异常
                    # 此时外部事务需要处理回滚
                    raise 
                
                # 注意：begin_nested() 失败会自动回滚到 savepoint，
                # 下一次循环会重新查询最新的 seq
                continue

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
        
        # 6. 保存到数据库 (异步线程执行，避免阻塞事件循环)
        await asyncio.to_thread(
            GameService._persist_new_game, 
            game_id, 
            player_ids, 
            creator_id, 
            players, 
            initial_leader_id
        )

        # 触发 AI 逻辑 (异步)
        asyncio.create_task(GameService._trigger_ai_logic(game_id))
        
        return initial_state

    @staticmethod
    def _persist_new_game(game_id: str, player_ids: List[int], creator_id: Optional[int], players: List[PlayerState], initial_leader_id: int):
        """
        同步方法：持久化新对局数据到数据库
        """
        db = SessionLocal()
        try:
            # 6.1 创建 Game 记录
            new_game_record = GameModel(
                id=game_id,
                status="playing",
                player_ids=player_ids,
                winner=None,
                user_id=creator_id
            )
            db.add(new_game_record)
            
            # 6.2 记录 GAME_START 事件
            GameService._append_event(
                db, 
                game_id, 
                "GAME_START", 
                creator_id, 
                {
                    "players": [p.model_dump() for p in players],
                    "roles": [p.character for p in players], # 注意：这里记录了所有人的身份，属于上帝视角的日志
                    "initial_leader": initial_leader_id
                }
            )
            
            db.commit()
        except Exception as e:
            print(f"Failed to persist game {game_id}: {e}")
            db.rollback()
            # 这里可以选择是否抛出异常中断创建，或者降级为仅内存模式
            # 为了数据一致性，建议抛出异常
            raise HTTPException(status_code=500, detail="Failed to create game persistence")
        finally:
            db.close()

    @staticmethod
    async def _trigger_ai_logic(game_id: str):
        """
        触发 AI 逻辑：检查当前是否有 AI 需要行动，并执行
        """
        # 避免循环导入
        from app.services.ai_service import AIService
        
        # 稍微延迟一下，模拟思考时间，也给前端反应时间
        await asyncio.sleep(1.5)

        game = games.get(game_id)
        if not game or game.phase == GamePhase.FINISHED:
            return

        # 遍历玩家，找到第一个需要行动的 AI
        # 注意：这里可能存在并发问题，但对于回合制游戏，串行执行通常是可以接受的
        for player in game.players:
            if not player.is_ai:
                continue
                
            action = await AIService.get_action(game, player)
            if action:
                try:
                    print(f"[AI] Triggering action for {player.username} ({player.user_id}): {action['action_type']}")
                    # 递归调用 process_action
                    await GameService.process_action(
                        game_id, 
                        player.user_id, 
                        action["action_type"], 
                        action["payload"]
                    )
                    # 执行完一个动作后，状态可能改变，
                    # process_action 会再次触发 _trigger_ai_logic
                    # 所以这里直接返回，不再继续遍历，让下一个触发去处理下一个动作
                    return
                except Exception as e:
                    print(f"[AI] Error executing action for {player.username}: {e}")
                    # 如果出错，继续尝试下一个 AI（如果是并发场景）或者直接退出
                    # 这里为了健壮性，选择继续下一个
                    continue

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
            
            # 隐藏 AI 记忆 (前端无需展示)
            p_copy.ai_memory = ""

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
        使用 Redis 分布式锁确保并发安全
        """
        # 使用分布式锁，防止并发修改状态
        async with GameLock(redis_client, game_id):
            game_in_memory = games.get(game_id)
            if not game_in_memory:
                raise HTTPException(status_code=404, detail="Game not found")
            
            # 使用深拷贝进行修改，避免直接修改内存中的对象导致（在持久化失败时的）脏数据
            # 注意：Pydantic V2 推荐使用 model_copy(deep=True) 或 copy.deepcopy
            game = copy.deepcopy(game_in_memory)

            # 1. 规则校验
            from app.core.game_rules import GameRuleValidator
            try:
                GameRuleValidator.validate_action(game, user_id, action_type, payload)
            except ValueError as e:
                 raise HTTPException(status_code=400, detail=str(e))
    
            # 2. 执行动作（状态机流转）
            # 准备事件日志数据
            event_payload = payload.copy()
            
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
                
                    # 记录投票结果到事件日志
                    event_payload["vote_result"] = {
                        "approved": approve_count,
                        "rejected": len(game.players) - approve_count,
                        "details": game.votes.copy() # 公开每个人投了什么
                    }

                    # --- 记录到 GameState.vote_history ---
                    game.vote_history.append({
                        "round": game.round,
                        "vote_track": game.vote_track,
                        "leader_id": game.leader_id,
                        "team": game.proposed_team.copy(),
                        "votes": game.votes.copy(),
                        "result": "approve" if approve_count > len(game.players) / 2 else "reject"
                    })
                    # -------------------------------------

                    # --- 新增：将投票结果写入会议记录 ---
                    vote_details_str = []
                    for p in game.players:
                        v_str = "同意" if game.votes.get(p.user_id) == VoteOption.APPROVE else "反对"
                        vote_details_str.append(f"{p.username}: {v_str}")
                
                    pass_str = "通过" if approve_count > len(game.players) / 2 else "不通过"
                    vote_summary = f"【投票结果】{pass_str} (同意: {approve_count}, 反对: {len(game.players) - approve_count})\n" + "  ".join(vote_details_str)
                
                    game.speech_history.append(ChatMessage(
                        user_id=0,
                        username="系统",
                        content=vote_summary,
                        timestamp=time.time()
                    ))
                    # ----------------------------------

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
                # 只有在队伍里的人才能提交
                if not hasattr(game, "pending_mission_results"):
                    game.pending_mission_results = [] # 这是一个 hack，最好加到 Schema 里
            
                game.pending_mission_results.append(result)
                player.has_acted = True
            
                # 任务结果是私密的，事件日志里不能记录具体是谁投了什么
                # 这里需要注意：event_payload 可能会被前端看到，所以不能把 result 放进去
                # 或者我们在写入数据库时，对于 mission 动作，要把 result 抹去？
                # 实际上，action_type=MISSION 的事件是“某人提交了任务”，但不包含结果
                # 真正的结果要在所有人都提交后，生成一个新的事件 "MISSION_RESULT"
                if "result" in event_payload:
                    del event_payload["result"] # 保护隐私
            
                # 检查是否所有队员都提交了
                team_size = len(game.proposed_team)
                if len(game.pending_mission_results) >= team_size:
                    # 结算任务
                    # 需要先把结果打乱，虽然我们这里只有 count，但也模拟一下流程
                    mission_results_shuffled = game.pending_mission_results.copy()
                    random.shuffle(mission_results_shuffled)
                
                    fail_count = mission_results_shuffled.count(MissionResult.FAIL)
                
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
                
                    # --- 新增：记录详细任务历史 ---
                    game.mission_history.append({
                        "round": game.round,
                        "team": game.proposed_team.copy(),
                        "result": final_result,
                        "fail_count": fail_count
                    })
                    # ---------------------------

                    # 触发一个额外的事件：任务结果揭晓
                    # 这个事件不由玩家触发，而是系统触发
                    # 我们稍后在写入数据库时处理
                    event_payload["mission_outcome"] = {
                        "round": game.round,
                        "fails": fail_count,
                        "result": final_result
                    }

                    # --- 新增：将任务结果写入会议记录 ---
                    result_zh = "成功" if final_result == MissionResult.SUCCESS else "失败"
                    mission_summary = f"【第 {game.round} 轮任务结果】{result_zh}\n出现 {fail_count} 张反对票"
                
                    game.speech_history.append(ChatMessage(
                        user_id=0,
                        username="系统",
                        content=mission_summary,
                        timestamp=time.time()
                    ))
                    # ----------------------------------

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
            
                # 记录刺杀结果
                assassin_name = player.username
                target_name = target.username if target else "Unknown"
                result_msg = ""

                if target and target.character == Character.MERLIN:
                    game.winner = Camp.EVIL
                    result_msg = f"刺客 {assassin_name} 刺杀了 {target_name} (梅林)！坏人胜利！"
                else:
                    game.winner = Camp.GOOD
                    target_char = target.character.value if target else "Unknown"
                    result_msg = f"刺客 {assassin_name} 刺杀了 {target_name} ({target_char})。刺杀失败，好人胜利！"

                game.speech_history.append(ChatMessage(
                    user_id=0,
                    username="系统",
                    content=result_msg,
                    timestamp=time.time()
                ))

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
                    # 检查上一条消息是否是同一个人的发言（且不是系统消息）
                    # 如果是，则合并内容
                    if game.speech_history and game.speech_history[-1].user_id == user_id:
                        game.speech_history[-1].content += "\n" + content
                        # 更新时间戳为最新
                        game.speech_history[-1].timestamp = time.time()
                    else:
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
                        # TODO: AI Trigger
                    else:
                        # 所有人都发言完毕，进入提名阶段
                        game.phase = GamePhase.TEAM_PROPOSAL
                        game.speaker_id = None
                        # 重置 acted 状态
                        for p in game.players:
                            p.has_acted = False
                
                    game.phase_start_time = time.time()
        
            # 3. 持久化事件与状态更新
            try:
                # 异步线程执行数据库操作
                await asyncio.to_thread(
                    GameService._persist_action,
                    game_id,
                    action_type,
                    user_id,
                    event_payload,
                    game.phase,
                    game.winner if game.phase == GamePhase.FINISHED else None,
                    game.players
                )
                
                # 3.3 持久化成功后，才更新内存状态
                games[game_id] = game
                
                # 如果游戏结束，异步更新排行榜和最近对局缓存
                if game.phase == GamePhase.FINISHED and game.winner:
                    asyncio.create_task(RankService.update_after_game_finish(game, game.winner))

            except Exception as e:
                print(f"Failed to persist action for game {game_id}: {e}")
                # 持久化失败，抛出异常，触发 HTTP 500 (此时内存状态未更新，保持一致)
                raise HTTPException(status_code=500, detail=f"Failed to process action: {str(e)}")

            # 4. 广播更新 (在锁内广播，确保顺序)
            await manager.broadcast_game_update(game_id, game)
            
            # 5. 触发 AI 逻辑 (异步)
            # 注意：create_task 是非阻塞的，AI 逻辑会在新的 Task 中运行
            # 新的 Task 再次调用 process_action 时会重新获取锁，不会死锁
            asyncio.create_task(GameService._trigger_ai_logic(game_id))
            
            return game

    @staticmethod
    def _persist_action(game_id: str, action_type: str, user_id: int, event_payload: dict, phase: str, winner: Optional[str], players: Optional[List[PlayerState]] = None):
        """
        同步方法：持久化动作事件和游戏状态
        """
        db = SessionLocal()
        try:
            # 3.1 写入当前动作事件
            GameService._append_event(db, game_id, action_type, user_id, event_payload)
            
            # 3.2 如果游戏结束，更新 Game 表状态
            if phase == GamePhase.FINISHED:
                game_record = db.query(GameModel).filter(GameModel.id == game_id).first()
                if game_record:
                    game_record.status = "finished"
                    game_record.winner = winner
                    game_record.finished_at = func.now()
                    db.add(game_record)
                
                # --- 更新用户胜场统计 ---
                if players and winner:
                    user_ids = [p.user_id for p in players]
                    users = db.query(User).filter(User.id.in_(user_ids)).all()
                    user_map = {u.id: u for u in users}

                    for p in players:
                        user = user_map.get(p.user_id)
                        if not user:
                            continue
                        
                        user.total_games += 1
                        
                        # 判定阵营
                        is_evil = p.character in EVIL_CHARACTERS
                        
                        # 判定胜负
                        # winner 可能是 Enum 或 str
                        winner_val = winner.value if hasattr(winner, "value") else winner
                        camp_good = Camp.GOOD.value
                        camp_evil = Camp.EVIL.value

                        if winner_val == camp_good and not is_evil:
                            user.wins_good += 1
                            user.total_wins += 1
                        elif winner_val == camp_evil and is_evil:
                            user.wins_evil += 1
                            user.total_wins += 1
                            
                        db.add(user)
                # ------------------------
            
            db.commit()
            
        except Exception as e:
            db.rollback()
            raise e
        finally:
            db.close()

    @staticmethod
    def get_user_games(user_id: int, skip: int = 0, limit: int = 20) -> List[GameModel]:
        """
        获取用户的对局历史
        """
        db = SessionLocal()
        try:
            # 优化：使用 user_id 索引字段查询，替代 JSON 扫描
            games = db.query(GameModel).filter(
                GameModel.user_id == user_id
            ).order_by(GameModel.created_at.desc()).offset(skip).limit(limit).all()
            return games
        finally:
            db.close()

    @staticmethod
    def get_game_events(game_id: str) -> List[GameEventModel]:
        """
        获取对局的所有事件流
        """
        db = SessionLocal()
        try:
            events = db.query(GameEventModel).filter(
                GameEventModel.game_id == game_id
            ).order_by(GameEventModel.seq).all()
            return events
        finally:
            db.close()
