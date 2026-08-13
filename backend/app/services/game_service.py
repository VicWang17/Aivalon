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
from app.models.outbox import OutboxEvent
from app.models.user import User
from app.db.base import SessionLocal
from sqlalchemy.orm import Session
from sqlalchemy import func
import json
import copy
from app.core.redis import redis_client
from app.core.room_actor import actor_manager, RoomActionTimeout, RoomOverloaded
from app.core import ai_queue
from app.core import bloom
from app.core import cache
from app.core import degrade
from app.core import event_journal
from celery import group

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
            speaker_id=initial_leader_id, # 队长开始发言
            last_event_seq=1  # GAME_START 固定为 1 号事件（见下方 journal 调用），动作事件从 2 续号
        )
        
        # 5. Write-Behind：GAME_START 事件 + 初始快照入 Redis Stream（同一事务），
        #    games 表记录由 flusher 补写——创建路径同样不碰 MySQL（20 并发房间创建超时的根因修复）
        await event_journal.append_with_snapshot(
            game_id=game_id,
            seq=1,  # GAME_START 固定为 1 号事件（与 initial_state.last_event_seq 一致）
            event_type="GAME_START",
            player_id=creator_id,
            payload={
                "players": [p.model_dump() for p in players],
                "roles": [p.character for p in players],  # 注意：上帝视角日志，含全员身份
                "initial_leader": initial_leader_id,
                "player_ids": player_ids,
                "creator_id": creator_id,
            },
            phase=initial_state.phase.value,
            winner=None,
            game_state=initial_state,
        )
        # 登记进布隆过滤器：放在持久化成功之后，失败的建局不该被登记
        # （登记了不存在的 id 只是让它以后拦不住，无害；漏登记真实房间会被误拦成 404，
        #  所以宁可晚一点、也要确保只登记真的建成了的房间）
        await bloom.add(redis_client, game_id)

        # 6. 持久化成功后才入内存（快照已随同一事务写入 Redis）
        #    只在房间归本节点时留内存副本：建局的节点不一定是房间的归属节点，
        #    非归属节点留下副本的后果是——等房间日后迁到它名下，process_action
        #    会因为"内存已有"而跳过快照恢复，照着这份建局时的旧状态继续演进。
        from app.core import node_registry
        cluster = node_registry.registry
        if cluster is None or cluster.is_mine(game_id):
            games[game_id] = initial_state

        # 触发 AI 逻辑 (异步)。显式传状态：本节点可能没留副本
        asyncio.create_task(GameService._trigger_ai_logic(game_id, initial_state))
        
        return initial_state

    @staticmethod
    async def restore_game_state(game_id: str, db: Session = None, redis_conn = None) -> Optional[GameState]:
        """
        恢复游戏状态 (优先从 Redis，其次 DB)
        """
        # 注意：必须先用独立变量记录所有权——若在 finally 里直接判断 `if not db`，
        # 此时 db 已被重新赋值为自建 Session（恒真），关闭逻辑永远走不到 → 每次恢复泄漏一个连接。
        # （S2 复测：15 个房间唤醒即打满共享池，创建对局 30s 超时的根因）
        own_session = db is None
        if own_session:
            db = SessionLocal()
        
        client = redis_conn or redis_client
        
        try:
            # 1. 尝试从 Redis 读取
            state_json = await client.get(f"game:{game_id}:state")
            if state_json:
                # 反序列化
                state_dict = json.loads(state_json)
                game_state = GameState(**state_dict)
                return game_state
            
            # 2. 如果 Redis 没有，从 DB 读取并重建
            game_model = db.query(GameModel).filter(GameModel.id == game_id).first()
            if not game_model:
                return None
            
            # 3. 重建 GameState
            # 注意：这里需要根据 events 重放状态
            # 目前简化为直接返回 None，让调用方处理 (或者在这里实现重放逻辑)
            # TODO: 实现从 DB 重建 GameState 的逻辑
            print(f"[GameService] Redis cache miss for {game_id}, DB rebuild not implemented yet")
            return None
            
        except Exception as e:
            print(f"[GameService] Failed to restore game state: {e}")
            return None
        finally:
            if own_session: # 只关闭自己创建的 session，调用方传入的由调用方负责
                db.close()

    @staticmethod
    async def _trigger_ai_logic(game_id: str, state: Optional[GameState] = None):
        """
        触发 AI 逻辑：检查当前是否有 AI 需要行动，并投递 Celery 任务

        state 显式传入是为了不依赖本进程内存：建局节点可能并不是房间的归属节点，
        那种情况下它不保留本地副本（避免日后房间迁回来时照旧副本继续演进）。
        """
        # 避免循环导入
        from app.tasks.ai import process_ai_turn

        # 获取当前状态（调用方传入优先，否则取本进程内存）
        game = state if state is not None else games.get(game_id)
        if not game or game.phase == GamePhase.FINISHED:
            return

        # 收集所有需要行动的 AI
        ai_tasks = []
        
        for player in game.players:
            if not player.is_ai:
                continue
                
            # 检查该 AI 是否需要行动
            should_act = False
            if game.phase == GamePhase.SPEECH and game.speaker_id == player.user_id:
                should_act = True
            elif game.phase == GamePhase.TEAM_PROPOSAL and game.leader_id == player.user_id:
                should_act = True
            elif game.phase == GamePhase.VOTE and not player.has_voted:
                should_act = True
            elif game.phase == GamePhase.MISSION and player.user_id in game.proposed_team and not player.has_acted:
                should_act = True
            elif game.phase == GamePhase.ASSASSINATION and player.character == Character.ASSASSIN:
                should_act = True
                
            if should_act:
                # 幂等键**只加在 VOTE / MISSION 上**，和下面那个 break 是同一条判据的两半：
                # 不 break 的阶段才会重复投递（这个函数挂在每一次 process_action 之后，
                # 每次都把"还没行动的"重扫一遍 → 7+6+…+1，见 ai_queue.claim 上方的说明）。
                # 另外三个阶段一次只投一个、投完阶段就翻页，压根不是放大源。
                #
                # **SPEECH 尤其不能加**：AI 可以 `is_end=False` 连续发言，
                # `speaker_id` 不变还要再行动一次——加了键就是它永远等不到下一次投递，
                # 房间永久卡死。**浪费可恢复，卡死不可恢复**，所以这里刻意只覆盖
                # "一人一次"这条不变量真正成立的两个阶段，而不是图整齐全加上。
                claim = ""
                if game.phase in (GamePhase.VOTE, GamePhase.MISSION):
                    claim = ai_queue.claim_key(
                        game_id, game.round, game.vote_track,
                        game.phase.value, player.user_id,
                    )
                    if not await ai_queue.claim(claim, redis_client):
                        # 这个阶段已经给它投过了。**continue 不是 break**：
                        # 后面的 AI 可能还没投过，break 会把它们一起漏掉。
                        continue

                # 投递任务到 Celery。登记一笔"在飞任务"，token 随任务带下去供注销用——
                # **深度必须在投递侧记账**：等 worker 领到任务才记的话，积压期间
                # 队列里躺着的任务一个都不算，深度永远看着很小，而那正是要看的东西。
                token, _ = await ai_queue.enter(redis_client)
                print(f"[GameService] Scheduling AI task for {player.username} ({player.user_id})")
                ai_tasks.append(process_ai_turn.s(game_id, player.user_id, token, claim))

                # 如果是投票或任务阶段，所有 AI 可以并行行动
                # 如果是发言或提名，通常是串行的
                if game.phase not in [GamePhase.VOTE, GamePhase.MISSION]:
                    break

        if ai_tasks:
            if len(ai_tasks) > 1:
                # 并行执行
                print(f"[GameService] Triggering {len(ai_tasks)} AI tasks in parallel group")
                group(ai_tasks).apply_async()
            else:
                # 单个执行
                ai_tasks[0].apply_async()


    @staticmethod
    def get_game(game_id: str) -> Optional[GameState]:
        """只看本进程内存。多节点下这不足以回答"房间存在吗"，读路径请用 load_game。"""
        return games.get(game_id)

    @staticmethod
    async def load_game(game_id: str) -> Optional[GameState]:
        """读取对局状态：本进程内存优先，未命中则回落 Redis 快照。

        多节点下必须有这层回落——房间归属节点在收到第一个动作前，内存里并没有它
        （建局可能发生在别的节点）。只读本地字典会让归属节点对一个真实存在的房间答 404。
        刻意不把回落结果写进 games：非归属节点缓存了状态，等房间哪天迁过来就会
        照着这份旧副本继续演进，造成状态回退。写入只发生在 Actor 内。
        """
        local = games.get(game_id)
        if local is not None:
            return local
        return await GameService.restore_game_state(game_id)

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
        处理玩家动作（统一入口）：路由到房间 Actor 串行处理。
        单写者模型：同一房间的动作在 Actor 队列中逐个执行，替代原 Redis 分布式锁。
        """
        # 宕机/休眠恢复：内存未命中时从 Redis 快照（或 DB）重建房间状态（Write-Behind 恢复链路）
        if game_id not in games:
            restored = await GameService.restore_game_state(game_id)
            if not restored:
                raise HTTPException(status_code=404, detail="Game not found")
            games[game_id] = restored

        actor = actor_manager.get_or_create(game_id, GameService._apply_action)
        try:
            return await actor.submit(user_id, action_type, payload)
        except RoomOverloaded:
            # 503 + Retry-After：**队列满意味着动作压根没入队**，所以一定没生效，
            # 可以放心让客户端重试。不带 Retry-After 的话它会立刻重试，
            # 而重试本身就是这个房间正在过载的原因（同准入层那条）。
            raise HTTPException(
                status_code=503,
                detail="房间繁忙，请稍后重试",
                headers={"Retry-After": "1"},
            )
        except RoomActionTimeout:
            # 504 而不是 503：**这次动作可能已经在执行了**，服务端不能断言它没生效。
            # 语义上的差别要如实反映出来——客户端拿到 504 应该先拉一次状态再决定重不重试，
            # 而不是照着 503 那样直接重发。真要安全重试就带幂等键（x-idempotency-key）。
            raise HTTPException(status_code=504, detail="动作处理超时，请刷新对局状态")

    @staticmethod
    async def _apply_action(game_id: str, user_id: int, action_type: ActionType, payload: dict) -> GameState:
        """
        动作处理主体，只在房间 Actor 内被串行调用（不要绕过 Actor 直接调用）。
        """
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
        
        # 3. Write-Behind 持久化：事件入 Redis Stream + 状态快照（同一事务），MySQL 由 flusher 批量补写
        #    seq 由 Actor 单写者内存分配（last_event_seq），替代原"查库 max(seq) + 乐观重试"
        try:
            game.last_event_seq += 1
            await event_journal.append_with_snapshot(
                game_id=game_id,
                seq=game.last_event_seq,
                event_type=action_type.value if hasattr(action_type, "value") else str(action_type),
                player_id=user_id,
                payload=event_payload,
                phase=game.phase.value if hasattr(game.phase, "value") else str(game.phase),
                winner=(game.winner.value if hasattr(game.winner, "value") else game.winner) if game.phase == GamePhase.FINISHED else None,
                game_state=game,
            )

            # 3.3 持久化成功后，才更新内存状态（快照已随同一事务写入 Redis）
            games[game_id] = game
            
            # 如果游戏结束，异步更新排行榜和最近对局缓存
            if game.phase == GamePhase.FINISHED and game.winner:
                # 准备传递给 Celery 的数据 (只能是 JSON 可序列化的)
                winner_val = game.winner.value if hasattr(game.winner, "value") else game.winner
                players_data = []
                for p in game.players:
                    players_data.append({
                        "user_id": p.user_id,
                        "username": p.username,
                        "seat_id": p.seat_id,
                        "is_ai": p.is_ai,
                        "character": p.character.value, # Enum 转 str
                        "is_connected": p.is_connected,
                        "has_voted": p.has_voted,
                        "has_acted": p.has_acted
                    })
                
                # 触发 Celery 任务
                from app.tasks.stats import process_game_result
                process_game_result.delay(game_id, winner_val, players_data)
                print(f"[GameService] Triggered stats task for game {game_id}")

        except Exception as e:
            print(f"Failed to persist action for game {game_id}: {e}")
            # 持久化失败，抛出异常，触发 HTTP 500 (此时内存状态未更新，保持一致)
            raise HTTPException(status_code=500, detail=f"Failed to process action: {str(e)}")

        # 4. 广播更新 (Actor 内串行广播，顺序由单写者保证)
        await manager.broadcast_game_update(game_id, game)
        
        # 5. 触发 AI 逻辑 (异步)
        # 注意：create_task 是非阻塞的，AI 逻辑会在新的 Task 中运行
        # 新的 Task 再次调用 process_action 时会重新获取锁，不会死锁
        asyncio.create_task(GameService._trigger_ai_logic(game_id))
        
        return game

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
    def load_all_game_ids() -> List[str]:
        """全量对局 id，只给布隆过滤器预热用（见 core/bloom.py 的 warm）。

        只 select id 一列，不取整行：这张表有 player_ids / payload 之类的 JSON 字段，
        整行拉回来传输量差好几个数量级，而预热只需要 id。
        """
        db = SessionLocal()
        try:
            return [row[0] for row in db.query(GameModel.id).all()]
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

    @staticmethod
    def _events_to_dicts(events: List[GameEventModel]) -> List[dict]:
        """ORM 对象 → 可 JSON 化的 dict。

        必须在进缓存**之前**转好：L1 存的是 Python 对象、L2 存的是 JSON 字符串，
        如果直接把 ORM 对象塞进 L1，两级缓存里躺的就是两种形状——
        命中 L1 拿到 ORM 对象、命中 L2 拿到 dict，行为随命中哪一级而变，
        这种 bug 只在缓存刚过期的那一瞬间出现，极难复现。
        另外 ORM 对象绑着 Session，Session 关掉后访问懒加载字段会直接抛异常。
        """
        return [
            {
                "id": e.id,
                "game_id": e.game_id,
                "seq": e.seq,
                "event_type": e.event_type,
                "player_id": e.player_id,
                "payload": e.payload,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ]

    @staticmethod
    async def get_game_events_cached(game_id: str) -> List[dict]:
        """回放事件流的读路径：L1 进程内 → L2 Redis → 回源 MySQL。

        为什么缓存选这条路径，而不是对局状态：
        对局状态那条路（`load_game`）看着像缓存，其实不是——归属节点的 `games`
        字典是 Actor 的**权威状态**（单写者），而非归属节点刻意不留副本，
        否则房间迁过来时会照着旧副本继续演进，造成对局回退（DEVLOG 018）。
        往那条路上加缓存等于重新引入那个 bug。
        回放事件流不一样：它是**追加写**的，只增不改，天然适合缓存，
        而且它是唯一每次都真的回源 MySQL 的读路径。
        """
        # 降级矩阵 L3：到档就把两级 TTL 都按倍数拉长（见 core/degrade.py）。
        # **TTL 就是一致性上限**（同 cache.py 文件头），所以这一刀花掉的是"数据可以
        # 多旧"，换来的是回源 MySQL 的次数按倍数下降。回放事件流是追加写、只增不改，
        # 拉长 TTL 最多让刚发生的事件晚几秒出现在回放里——**这是全站最能承受
        # "旧一点"的读路径**，所以 L3 挑它而不是挑对局状态。
        l1_ttl = await degrade.cold_path_interval(cache.L1_TTL, redis_client)
        l2_ttl = await degrade.cold_path_interval(cache.L2_TTL, redis_client)
        return await cache.get_or_load(
            cache.events_key(game_id),
            lambda: GameService._events_to_dicts(
                GameService.get_game_events(game_id)),
            redis=redis_client,
            l1_ttl=l1_ttl,
            l2_ttl=int(l2_ttl),
        )
