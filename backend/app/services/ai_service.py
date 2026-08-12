"""
这个文件实现了 AI 玩家的决策逻辑服务。
优先尝试调用 LLM (DeepSeek) 生成决策，如果失败则回退到基于规则的随机策略。
"""
import random
import json
from typing import Optional, Dict, Any, List
from app.schemas.game import GameState, PlayerState
from app.models.game_enums import GamePhase, ActionType, VoteOption, MissionResult, Character, Camp
from app.core.game_rules import GameRuleValidator
from app.services.llm_service import LLMService
from app.core.ai_personas import get_persona_by_seat
from app.core import switches

class AIService:
    @staticmethod
    async def get_action(game: GameState, player: PlayerState,
                        redis_conn=None) -> Optional[Dict[str, Any]]:
        """
        获取 AI 玩家的下一步动作

        `redis_conn`：读降级开关用。**必须由调用方传进来**，不能用全局单例——
        这个方法跑在 Celery worker 里，那边每个任务都新建一个 event loop，
        全局客户端会复用绑到已关闭 loop 的连接（同 tasks/ai.py 里那个注释）。
        """
        if not player.is_ai:
            return None

        # 走不走 LLM 是**运行时**开关，不是启动时配置：见 core/switches.py。
        # 开关默认值仍取自 settings.AI_USE_LLM，所以压测那套环境变量照旧有效。
        if not await switches.ai_use_llm(redis_conn):
            return AIService._get_fallback_action(game, player)

        try:
            # 根据不同阶段决策
            if game.phase == GamePhase.SPEECH:
                return await AIService._handle_speech(game, player)
            elif game.phase == GamePhase.TEAM_PROPOSAL:
                return await AIService._handle_propose(game, player)
            elif game.phase == GamePhase.VOTE:
                return await AIService._handle_vote(game, player)
            elif game.phase == GamePhase.MISSION:
                return await AIService._handle_mission(game, player)
            elif game.phase == GamePhase.ASSASSINATION:
                return await AIService._handle_assassination(game, player)
        except Exception as e:
            print(f"[AI] Error in get_action for {player.username}: {e}")
            # 如果出错，尝试回退策略
            return AIService._get_fallback_action(game, player)
            
        return None

    @staticmethod
    def _get_fallback_action(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        """回退策略入口"""
        if game.phase == GamePhase.SPEECH:
            return AIService._fallback_speech(game, player)
        elif game.phase == GamePhase.TEAM_PROPOSAL:
            return AIService._fallback_propose(game, player)
        elif game.phase == GamePhase.VOTE:
            return AIService._fallback_vote(game, player)
        elif game.phase == GamePhase.MISSION:
            return AIService._fallback_mission(game, player)
        elif game.phase == GamePhase.ASSASSINATION:
            return AIService._fallback_assassination(game, player)
        return None

    # =========================================================================
    # LLM Context Builders
    # =========================================================================

    # =========================================================================
    # Strategy Guidelines
    # =========================================================================
    AVALON_STRATEGY_GUIDE = """
阿瓦隆策略指南：
五轮分别需要上车3、4、4、5、5个人。注意：第4轮需要至少2张反对票才会判定任务失败，其余轮次包括第五轮只需1张反对票即失败。你的分析需要基于该轮次人数及失败条件。
当一边任务赢下三轮后，该方就会胜利，所以当一方赢下两轮的时候情况是紧急的，好人不应该再测试坏人上车验身份，坏人也不需要再刻意投赞成票让任务通过来隐藏身份（这会让你输掉游戏）。
如果你意识到这一轮再失败，你就要输了，你应该尽可能选择让自己赢的方式。
1. 信息管理：
   - 好人阵营：不能暴露梅林。派西维尔需要在保护梅林和带领好人之间取得平衡。
   - 坏人阵营：制造混乱，但也模仿好人身份。刺客应仔细观察寻找梅林。莫甘娜应模仿梅林来以此欺骗派西维尔。
            如果一个任务可能要成功，坏人不一定要阻止这个提案，因为这可能会让你明显被发现，一种策略是可以
            在这个时候装好人支持任务通过，当然坏人也可以选择说一些新的理由来混淆大家试听，使得任务不通过。
   - 说明自己身份：通常一开始，不用阐明自己身份，阐明身份是一种必要时刻的策略。任何身份在一定时候都可以选择跳派西维尔的策略，梅林可以跳派西维尔来带队，但是也要面临被坏人怀疑是梅林
        跳派的风险，派西维尔可以自己真跳来赢得支持，坏人可以跳派西维尔来混淆视听。当场上有人跳了派西维尔，你也可以通过
        自己策略的思考选择要不要对跳。对仆人来说，通常不跳派西维尔，但是如果有把握也可以。当然，以上这些身份如果想划水，
        也可以说自己没有身份，是个蛋。        
2. 投票模式：
   - 否决队伍是正常的，通常也是获取信息（通过投票记录）的必要手段。
   - 注意谁赞成了失败的任务（怀疑他们）。
   - 注意谁否决了成功的任务（可能是坏人试图隐藏，或者是好人试图获取更多信息）。
   - 如果坏人认为这个任务会成功，也可以佯装好人投出赞成票来掩盖自己的身份。
   - 通常来说，在任务投票中，如果你是坏人，有等级比你更高的坏人也在队伍中，你应该投出赞成票，
     而队伍中等级最高的坏人来决定要不要投反对票，这样可以避免多张坏人票的出现。等级上，莫甘娜大于刺客大于其它。
   - 每轮有两次投票，第一次是“是否赞成这个队伍”，第二次才是“这个任务成功or失败”。第一次的投票结果是公开的，第二次是秘密进行的。
     所以对于坏人而言，如果自己在队伍里，通常会选择赞成这个提议的队伍，approve第一次投票，然后在第二次投票出秘密投出任务失败的票。

3. 组队提议：
   - 队长通常会提议自己进队。这个游戏没有不想上车的人。
   - 如果你是坏人，只要保证有一个坏人进队就可以，坏人都进队伍没有好处，可能在任务结果中被大家发现多个反对票。
   - 如果你是梅林，隐晦地引导队伍选择，不要过于直白。
   - 如果你有充分的理由不赞成当前的队伍，你可以反对，或者提出新的队伍选择方式。
   - 在决定队伍人选的时候，你应该酌情考虑前面的人的发言。

4. 发言技巧：
   - 你更倾向于叫别人的号码而不是名字，当然偶尔叫名字也可以。
   - 语言风格轻松，不要过于正式，派西维尔可以叫派，忠诚的仆人叫蛋，队伍叫车，加入队伍叫上车，分析的语言口语化。
   - 你不仅要发表对队长发言的看法，也应该承接前一位或者前几位玩家的发言，可以支持或者驳斥
   - 保持逻辑清晰且前后一致。如果你的性格不是推理派，你可以玩得迷糊一点。
   - 每个人都会发言的，如果你看到有人没发言，只是没轮到他。
   - 指控应基于投票历史或任务结果。
   - 情感号召有用，但逻辑更能说服老练的玩家。
"""

    @staticmethod
    def _build_system_prompt(game: GameState, player: PlayerState, action_type: str) -> str:
        """构建系统提示词，包含角色设定和输出格式"""
        
        # 0. 获取 Persona
        persona = get_persona_by_seat(player.seat_id)
        
        # 1. 角色与目标描述
        role_desc = f"你正在玩桌游 '阿瓦隆 (Avalon)'。你的名字是 {player.username} (你是 {player.seat_id + 1}号玩家)。\n"
        role_desc += f"=== 人格设定 (PERSONA) ===\n"
        role_desc += f"名称: {persona.name}\n"
        role_desc += f"描述: {persona.description}\n"
        role_desc += f"特质: 风险偏好={persona.risk_tolerance}, 表达欲望={persona.expressiveness}, 逻辑风格={persona.logic_style}\n"
        role_desc += f"系统指令: {persona.system_instruction}\n"
        role_desc += f"=============================\n"
        
        role_desc += f"你的身份: {player.character.value if player.character else '未知'}.\n"
        
        evil_roles = [Character.ASSASSIN, Character.MORGANA, Character.MINION]
        
        # 识别已知身份
        known_info = ""
        if player.character in evil_roles:
            role_desc += "你的阵营: 坏人 (莫德雷德的爪牙)。目标: 让3个任务失败或刺杀梅林。\n"
            
            # 坏人互知
            teammates = []
            for p in game.players:
                if p.user_id != player.user_id and p.character in evil_roles: # TODO: Handle Oberon if added
                    # 坏人互知具体身份（前端也是如此显示的）
                    teammates.append(f"{p.username} (座位号 {p.seat_id + 1}, 身份: {p.character.value})")
            
            if teammates:
                known_info = f"你知道你的坏人队友是: {', '.join(teammates)}。"
            else:
                known_info = "你看不到其他坏人队友 (也许你是唯一的坏人或者他们被隐藏了)。"
                
        else:
            role_desc += "你的阵营: 好人 (亚瑟的忠臣)。目标: 让3个任务成功。\n"
            
            if player.character == Character.MERLIN:
                role_desc += "你是梅林。你知道坏人是谁，但必须隐藏身份。\n"
                evils = []
                for p in game.players:
                    if p.character in evil_roles: # TODO: Handle Mordred if added
                        evils.append(f"{p.username} (座位号 {p.seat_id + 1})")
                if evils:
                    known_info = f"你看到的坏人玩家是: {', '.join(evils)}。"
                
            elif player.character == Character.PERCIVAL:
                role_desc += "你是派西维尔。你知道潜在的梅林 (梅林和莫甘娜)。但你不能明说，因为你说了谁是梅林候选人，刺客就会知道刺杀谁。\n"
                merlins = []
                for p in game.players:
                    if p.character in [Character.MERLIN, Character.MORGANA]:
                        merlins.append(f"{p.username} (座位号 {p.seat_id + 1})")
                if merlins:
                    known_info = f"你看到的梅林候选人是: {', '.join(merlins)}。"
            else:
                role_desc += "你是一个忠诚的仆人。你不知道谁是谁。\n"

        if known_info:
            role_desc += known_info + "\n"

        # Add Strategy Guide
        role_desc += "\n" + AIService.AVALON_STRATEGY_GUIDE + "\n"

        # 2. 输出格式要求
        format_desc = "你必须严格按照 JSON 格式输出你的决定。\n"
        format_desc += "关键: 输出中包含 'memory' 字段。用它来存储在此前记忆的基础上，你有没有要修改或者添加的，用简短的句子和词组来记录，比如对怀疑谁，对某些人身份的猜测，当下决定的游玩策略等。这个记忆会保留到下一轮。\n"
        
        if action_type == "SPEAK":
            format_desc += """
期望的 JSON 格式:
{
    "thought": "简短的推理...",
    "memory": "关于玩家/策略的私人笔记...",
    "action_type": "speak",
    "payload": {
        "content": "你的发言内容 (用中文)",
        "is_end": true
    }
}
注意: 如果你要结束发言，'is_end' 应该是 true。
"""
        elif action_type == "PROPOSE":
            format_desc += """
期望的 JSON 格式:
{
    "thought": "简短的推理...",
    "memory": "关于玩家/策略的私人笔记...",
    "action_type": "propose",
    "payload": {
        "target_ids": [id1, id2, ...]
    }
}
"""
        elif action_type == "VOTE":
            format_desc += """
期望的 JSON 格式:
{
    "thought": "简短的推理...",
    "memory": "关于玩家/策略的私人笔记...",
    "action_type": "vote",
    "payload": {
        "option": "approve" 或 "reject"
    }
}
"""
        elif action_type == "MISSION":
            format_desc += """
期望的 JSON 格式:
{
    "thought": "简短的推理...",
    "memory": "关于玩家/策略的私人笔记...",
    "action_type": "mission",
    "payload": {
        "result": "success" 或 "fail"
    }
}
"""
        elif action_type == "ASSASSINATE":
            format_desc += """
期望的 JSON 格式:
{
    "thought": "简短的推理...",
    "memory": "关于玩家/策略的私人笔记...",
    "action_type": "assassinate",
    "payload": {
        "target_id": 目标玩家ID
    }
}
"""

        return role_desc + "\n" + format_desc

    @staticmethod
    def _build_user_prompt(game: GameState, player: PlayerState) -> str:
        """构建用户提示词，包含当前局势和历史信息"""
        
        # 1. 玩家列表信息
        players_info = "=== 玩家列表 ===\n"
        for p in game.players:
            p_info = f"- 座位 {p.seat_id + 1}: {p.username} (ID: {p.user_id})"
            if p.user_id == player.user_id:
                p_info += " [你]"
            players_info += p_info + "\n"
            
        # 2. 当前状态
        current_state = f"""
=== 当前状态 ===
阶段: {game.phase}
轮次: {game.round} / 5
投票轨道 (Vote Track): {game.vote_track} / 5 (5次被拒 = 任务失败/锤子丢失)
队长: {game.leader_id} (座位 {[p.seat_id + 1 for p in game.players if p.user_id == game.leader_id][0]})
"""
        # 3. 历史任务结果 (详细)
        history = "=== 任务历史 ===\n"
        if game.mission_history:
             for entry in game.mission_history:
                 team_names = [p.username for p in game.players if p.user_id in entry["team"]]
                 history += f"第 {entry['round']} 轮: {entry['result']} (失败票数: {entry['fail_count']})。队伍: {team_names}\n"
        else:
             history += "暂无已完成的任务。\n"
            
        # 3.1 历史投票结果
        if hasattr(game, "vote_history") and game.vote_history:
            history += "\n=== 投票历史 ===\n"
            for entry in game.vote_history:
                leader_name = next((p.username for p in game.players if p.user_id == entry["leader_id"]), "未知")
                team_names = [p.username for p in game.players if p.user_id in entry["team"]]
                # 简化投票显示
                votes_list = []
                for p in game.players:
                    v = entry['votes'].get(p.user_id, "unknown")
                    # 转换 VoteOption 枚举为字符串
                    v_str = "赞成" if str(v) == "approve" or str(v) == "VoteOption.APPROVE" else "拒绝"
                    votes_list.append(f"{p.username}:{v_str}")
                vote_str = ", ".join(votes_list)
                history += f"- R{entry['round']}-T{entry['vote_track']}: 队长 {leader_name} 提议了 {team_names}。结果: {entry['result']}。票型: [{vote_str}]\n"

        # 4. 发言历史 (最近 15 条)
        chat_log = "=== 最近聊天 ===\n"
        recent_chats = game.speech_history[-15:] if game.speech_history else []
        for chat in recent_chats:
            chat_log += f"{chat.username}: {chat.content}\n"
            
        # 5. 私有记忆
        private_memory = f"\n=== 你的私有记忆 (来自上一轮) ===\n{player.ai_memory if player.ai_memory else '无'}\n"

        # 6. 当前具体情境
        specific_context = "\n=== 需要采取的行动 ===\n"
        if game.phase == GamePhase.TEAM_PROPOSAL:
            req_count = GameRuleValidator.get_mission_team_size(game.round)
            specific_context += f"你是队长。你需要提议一个由 {req_count} 名玩家组成的队伍。"
        elif game.phase == GamePhase.VOTE:
            team_names = [p.username for p in game.players if p.user_id in game.proposed_team]
            specific_context += f"提议的队伍: {', '.join(team_names)}。你赞成这个队伍吗？"
        elif game.phase == GamePhase.MISSION:
            specific_context += "你正在执行任务。选择让任务成功或失败。"
        elif game.phase == GamePhase.ASSASSINATION:
            specific_context += "好人赢得了3个任务。你必须找到并刺杀梅林来窃取胜利。"
        elif game.phase == GamePhase.SPEECH:
            specific_context += "轮到你发言了。分析局势并影响其他人。"

        return f"{players_info}\n{current_state}\n{history}\n{chat_log}\n{private_memory}\n{specific_context}"

    # =========================================================================
    # LLM Handlers
    # =========================================================================

    @staticmethod
    async def _call_llm(game: GameState, player: PlayerState, action_str: str, temperature: Optional[float] = None) -> Optional[Dict[str, Any]]:
        persona = get_persona_by_seat(player.seat_id)

        # Dynamic Temperature Adjustment
        if temperature is None:
            if action_str == "SPEAK":
                # Speech: Higher temp for creativity
                if persona.expressiveness == "Verbose":
                    temperature = 1.2
                elif persona.expressiveness == "Concise":
                    temperature = 0.9
                else:
                    temperature = 1.1
            else:
                # Logic: Lower temp for consistency, but adjust for risk
                if persona.risk_tolerance == "High":
                    temperature = 0.8 # More unpredictable
                elif persona.risk_tolerance == "Low":
                    temperature = 0.4 # More deterministic
                else:
                    temperature = 0.6
        
        # 构建 Prompt
        system_prompt = AIService._build_system_prompt(game, player, action_str)
        user_prompt = AIService._build_user_prompt(game, player)
        
        # 打印 Prompt 日志 (Simplified)
        print(f"\n[{player.username} ({action_str})] --- AI THINKING ---")
        
        try:
            # 根据阶段设置超时时间
            # 发言阶段允许更长时间思考
            timeout = 45 if action_str == "SPEAK" else 20
            
            result = await LLMService.generate_response(
                system_prompt, 
                user_prompt, 
                json_mode=True,
                temperature=temperature,
                timeout=timeout
            )
            
            if not result:
                print(f"[AI] LLM returned None for {player.username}")
                return None
            
            # 打印 Response 日志 (Simplified)
            print(f"[{player.username} ({action_str})] --- AI ACTION ---")
            
            thought = result.get("thought", "No thought")
            memory = result.get("memory", "No memory")
            payload = result.get("payload", {})
            act_type = result.get("action_type", "unknown")
            
            print(f"Thought: {thought}")
            print(f"Memory: {memory}")
            print(f"Action: {act_type}")
            print(f"Payload: {json.dumps(payload, ensure_ascii=False)}")
            print("-" * 40 + "\n")
            
            if "error" in result:
                print(f"[AI] LLM Error: {result['error']}")
                return None

            # --- 新增：保存 AI 记忆 ---
            if "memory" in result:
                player.ai_memory = str(result["memory"]) # 确保是字符串
                # 截断过长的记忆以防万一
                if len(player.ai_memory) > 2000:
                    player.ai_memory = player.ai_memory[:2000] + "..."
                print(f"[AI] Memory updated for {player.username}: {player.ai_memory[:100]}...")
            # ------------------------
                
            # 提取 payload 并构造返回
            # LLM 返回的结构应该是 { "action_type": "...", "payload": {...} }
            # 我们需要确保 action_type 是枚举值
            
            resp_type = str(result.get("action_type", "")).lower()
            payload = result.get("payload", {})
            
            # 映射 action_type 字符串到 Enum
            act_enum = None
            if resp_type == "speak":
                act_enum = ActionType.SPEAK
            elif resp_type == "propose":
                act_enum = ActionType.PROPOSE
            elif resp_type == "vote":
                act_enum = ActionType.VOTE
            elif resp_type == "mission":
                act_enum = ActionType.MISSION
            elif resp_type == "assassinate":
                act_enum = ActionType.ASSASSINATE
            else:
                print(f"[AI] Unknown action type from LLM: {resp_type}")
                return None
                
            return {
                "action_type": act_enum,
                "payload": payload
            }
        except Exception as e:
            print(f"[AI] Exception in _call_llm: {e}")
            return None

    @staticmethod
    async def _handle_speech(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        if game.speaker_id != player.user_id:
            return None
        
        # 发言需要更多创造性，温度由 Persona 决定
        action = await AIService._call_llm(game, player, "SPEAK")
        if action:
            return action
        return AIService._fallback_speech(game, player)

    @staticmethod
    async def _handle_propose(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        if game.leader_id != player.user_id:
            return None
            
        action = await AIService._call_llm(game, player, "PROPOSE")
        if action:
            return action
        return AIService._fallback_propose(game, player)

    @staticmethod
    async def _handle_vote(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        if player.has_voted:
            return None
            
        action = await AIService._call_llm(game, player, "VOTE")
        if action:
            return action
        return AIService._fallback_vote(game, player)

    @staticmethod
    async def _handle_mission(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        if player.user_id not in game.proposed_team or player.has_acted:
            return None
            
        action = await AIService._call_llm(game, player, "MISSION")
        if action:
            return action
        return AIService._fallback_mission(game, player)

    @staticmethod
    async def _handle_assassination(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        if player.character != Character.ASSASSIN:
            return None
            
        action = await AIService._call_llm(game, player, "ASSASSINATE")
        if action:
            return action
        return AIService._fallback_assassination(game, player)

    # =========================================================================
    # Fallback Strategies (Random/Rule-based)
    # =========================================================================

    @staticmethod
    def _fallback_speech(game: GameState, player: PlayerState) -> Dict[str, Any]:
        phrases = [
            "我是好人，请相信我。",
            "这一轮我觉得应该多观察。",
            "没什么好说的，过。",
            "大家都聊聊吧。",
            "我是派西维尔，我看到了一些信息。" if player.character == Character.PERCIVAL else "我是好人。"
        ]
        return {
            "action_type": ActionType.SPEAK,
            "payload": {
                "content": random.choice(phrases),
                "is_end": True
            }
        }

    @staticmethod
    def _fallback_propose(game: GameState, player: PlayerState) -> Optional[Dict[str, Any]]:
        required_count = GameRuleValidator.get_mission_team_size(game.round)
        candidates = [p.user_id for p in game.players if p.user_id != player.user_id]
        if len(candidates) < required_count - 1:
            return None
        selected_others = random.sample(candidates, required_count - 1)
        target_ids = [player.user_id] + selected_others
        return {
            "action_type": ActionType.PROPOSE,
            "payload": {"target_ids": target_ids}
        }

    @staticmethod
    def _fallback_vote(game: GameState, player: PlayerState) -> Dict[str, Any]:
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
    def _fallback_mission(game: GameState, player: PlayerState) -> Dict[str, Any]:
        is_evil = player.character in [Character.ASSASSIN, Character.MORGANA, Character.MINION]
        if not is_evil:
            result = MissionResult.SUCCESS
        else:
            if game.round == 1:
                result = MissionResult.FAIL if random.random() < 0.1 else MissionResult.SUCCESS
            else:
                result = MissionResult.FAIL if random.random() < 0.8 else MissionResult.SUCCESS
        return {
            "action_type": ActionType.MISSION,
            "payload": {"result": result}
        }

    @staticmethod
    def _fallback_assassination(game: GameState, player: PlayerState) -> Dict[str, Any]:
        potential_targets = [p.user_id for p in game.players if p.user_id != player.user_id]
        target_id = random.choice(potential_targets)
        return {
            "action_type": ActionType.ASSASSINATE,
            "payload": {"target_id": target_id}
        }
