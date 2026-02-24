
import sys
import os
# Add local site-packages if needed
sys.path.append("/Users/vic/.local/lib/python3.13/site-packages")
sys.path.append("/usr/local/Caskroom/miniconda/base/lib/python3.13/site-packages")
from datetime import datetime
import json

# Add backend to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock environment variables BEFORE importing config
os.environ["MYSQL_USER"] = "test"
os.environ["MYSQL_PASSWORD"] = "test"
os.environ["MYSQL_DATABASE"] = "test"
os.environ["MAIL_USERNAME"] = "test"
os.environ["MAIL_PASSWORD"] = "test"
os.environ["MAIL_FROM"] = "test"
os.environ["DEEPSEEK_API_KEY"] = "sk-test-key"

from app.schemas.game import GameState, PlayerState, ChatMessage
from app.models.game_enums import GamePhase, Character, VoteOption, MissionResult
from app.services.ai_service import AIService

def test_prompt_construction():
    print("--- Testing Prompt Construction ---")
    
    # 1. Mock Players
    players = [
        PlayerState(user_id=1, username="User1", seat_id=0, is_ai=False, character=Character.MERLIN),
        PlayerState(user_id=2, username="AI_Bot2", seat_id=1, is_ai=True, character=Character.ASSASSIN),
        PlayerState(user_id=3, username="AI_Bot3", seat_id=2, is_ai=True, character=Character.PERCIVAL),
        PlayerState(user_id=4, username="AI_Bot4", seat_id=3, is_ai=True, character=Character.MORGANA),
        PlayerState(user_id=5, username="AI_Bot5", seat_id=4, is_ai=True, character=Character.SERVANT),
    ]
    
    # 2. Mock Game State with Vote History
    game = GameState(
        game_id="test-game-id",
        phase=GamePhase.SPEECH,
        round=2,
        vote_track=1,
        leader_id=3, # AI_Bot3's turn to lead
        speaker_id=2, # AI_Bot2's turn to speak
        players=players,
        mission_results=[MissionResult.SUCCESS],
        mission_history=[
            {
                "round": 1,
                "team": [1, 5],
                "result": MissionResult.SUCCESS,
                "fail_count": 0
            }
        ],
        speech_history=[
            ChatMessage(user_id=1, username="User1", content="我是好人。", timestamp=datetime.now().timestamp())
        ],
        vote_history=[
            {
                "round": 1,
                "vote_track": 0,
                "leader_id": 1,
                "team": [1, 5],
                "votes": {1: "approve", 2: "reject", 3: "approve", 4: "reject", 5: "approve"},
                "result": "approve"
            }
        ]
    )
    
    # 3. Test AI_Bot2 (Assassin) Perspective
    ai_player = players[1] 
    ai_player.ai_memory = "I suspect User1 is Merlin."
    # print(f"\n[Player Perspective]: {ai_player.username} ({ai_player.character})")
    
    system_prompt = AIService._build_system_prompt(game, ai_player, "SPEAK")
    user_prompt = AIService._build_user_prompt(game, ai_player)
    
    # print("\n[System Prompt Snippet]:")
    # print(system_prompt[:500] + "...")
    # print("\n[User Prompt]:")
    # print(user_prompt)
    
    # Check for requirements
    assert "你的身份: assassin" in system_prompt
    assert "你的阵营: 坏人" in system_prompt
    assert "AI_Bot4 (座位号 4)" in system_prompt # Teammate visibility (Seat 3+1)
    assert "投票历史" in user_prompt
    assert "User1:赞成" in user_prompt # Updated from approve to Approve
    assert "第 1 轮: MissionResult.SUCCESS" in user_prompt
    
    # New assertions
    assert "I suspect User1 is Merlin" in user_prompt
    assert "失败票数: 0" in user_prompt
    assert "memory" in system_prompt # Check JSON format requirement
    
    # Check Persona
    assert "欺诈者" in system_prompt
    assert "你是一个狡猾的操纵者" in system_prompt

    print("\n[Verification Passed]: System and User prompts contain required context.")

    # 4. Test Fallback Logic (with invalid key)
    print("\n--- Testing Fallback Logic (Mock Key) ---")
    # This should trigger LLM call -> fail (invalid key) -> catch exception -> fallback
    action = AIService.get_action(game, ai_player)
    print(f"[Fallback Action]: {action}")
    
    assert action is not None
    assert "action_type" in action
    assert action["action_type"] == "speak" # Fallback speech
    # Note: Fallback returns Enum, but Pydantic serialization might convert it. 
    # Actually AIService returns dict with Enum.
    # Let's check the type.
    from app.models.game_enums import ActionType
    assert action["action_type"] == ActionType.SPEAK
    print("[Verification Passed]: Fallback mechanism works.")

if __name__ == "__main__":
    test_prompt_construction()
