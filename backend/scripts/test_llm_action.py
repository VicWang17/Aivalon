"""
测试 AI Service 是否能正确调用 LLM
"""
import sys
import os
import asyncio
from datetime import datetime

# Add backend to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.schemas.game import GameState, PlayerState, ChatMessage
from app.models.game_enums import GamePhase, Character, VoteOption, MissionResult
from app.services.ai_service import AIService
from app.core.config import settings

def test_ai_speech():
    print(f"Testing with API Key: {settings.DEEPSEEK_API_KEY[:5]}***" if settings.DEEPSEEK_API_KEY else "No API Key found")
    
    # 1. Mock Players
    players = [
        PlayerState(user_id=1, username="User1", seat_id=0, is_ai=False, character=Character.MERLIN),
        PlayerState(user_id=2, username="AI_Bot2", seat_id=1, is_ai=True, character=Character.ASSASSIN),
        PlayerState(user_id=3, username="AI_Bot3", seat_id=2, is_ai=True, character=Character.PERCIVAL),
        PlayerState(user_id=4, username="AI_Bot4", seat_id=3, is_ai=True, character=Character.MORGANA),
        PlayerState(user_id=5, username="AI_Bot5", seat_id=4, is_ai=True, character=Character.SERVANT),
    ]
    
    # 2. Mock Game State
    game = GameState(
        game_id="test-game-id",
        phase=GamePhase.SPEECH,
        round=1,
        vote_track=0,
        leader_id=1,
        speaker_id=2, # AI_Bot2's turn
        players=players,
        mission_results=[],
        speech_history=[
            ChatMessage(user_id=1, username="User1", content="我是好人，大家听我说。", timestamp=datetime.now().timestamp())
        ]
    )
    
    # 3. Test Action
    ai_player = players[1] # AI_Bot2 (Assassin)
    print(f"\n--- Testing AI Speech for {ai_player.username} ({ai_player.character}) ---")
    
    action = AIService.get_action(game, ai_player)
    
    print("\n[Result Action]:")
    print(action)

if __name__ == "__main__":
    test_ai_speech()
