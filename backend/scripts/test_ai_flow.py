import asyncio
import sys
import os
import random

# Add backend directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.game_service import GameService
from app.schemas.game import GameState
from app.models.game_enums import GamePhase, ActionType, VoteOption, MissionResult, Character

# Mock database dependency
# Since GameService uses SessionLocal internally, we just need to ensure DB is accessible.

async def main():
    print("=== Starting AI Game Flow Test ===")
    
    # 1. Create Users (Mock)
    from app.models.user import User
    from app.db.base import SessionLocal
    
    db = SessionLocal()
    # Assume user 1 exists as creator
    creator_id = 1
    player_ids = [1, 102, 103, 104, 105, 106, 107, 108]
    user_map = {uid: f"User_{uid}" for uid in player_ids}
    
    try:
        # Create users if not exist
        for uid in player_ids:
            user = db.query(User).filter(User.id == uid).first()
            if not user:
                user = User(id=uid, username=f"User_{uid}", email=f"user{uid}@example.com", hashed_password="pw")
                db.add(user)
        db.commit()
    except Exception as e:
        print(f"Failed to create users: {e}")
        db.rollback()
    finally:
        db.close()
    
    # 2. Create Game
    print(f"Creating game with creator {creator_id}...")
    try:
        game = await GameService.create_game(player_ids, user_map, creator_id)
        print(f"Game created: {game.game_id}")
    except Exception as e:
        print(f"Failed to create game: {e}")
        return

    game_id = game.game_id
    
    # 3. Loop until finished
    max_steps = 100
    step = 0
    
    while step < max_steps:
        await asyncio.sleep(2) # Wait for AI actions
        
        game = GameService.get_game(game_id)
        if not game:
            print("Game not found!")
            break
            
        print(f"\n--- Step {step} ---")
        print(f"Phase: {game.phase}, Round: {game.round}, VoteTrack: {game.vote_track}")
        print(f"Leader: {game.leader_id}, Speaker: {game.speaker_id}")
        
        if game.phase == GamePhase.FINISHED:
            print(f"Game Finished! Winner: {game.winner}")
            break
            
        # Check if it's human turn
        human_needed = False
        action_type = None
        payload = {}
        
        me = next((p for p in game.players if p.user_id == creator_id), None)
        
        if game.phase == GamePhase.SPEECH:
            if game.speaker_id == creator_id:
                print(">> It's my turn to speak!")
                human_needed = True
                action_type = ActionType.SPEAK
                payload = {"content": "I am a human.", "is_end": True}
        
        elif game.phase == GamePhase.TEAM_PROPOSAL:
            if game.leader_id == creator_id:
                print(">> It's my turn to propose!")
                human_needed = True
                # Pick random
                target_ids = [creator_id]
                candidates = [p.user_id for p in game.players if p.user_id != creator_id]
                # Need count?
                from app.core.game_rules import GameRuleValidator
                count = GameRuleValidator.get_mission_team_size(game.round)
                target_ids.extend(random.sample(candidates, count - 1))
                
                action_type = ActionType.PROPOSE
                payload = {"target_ids": target_ids}
                
        elif game.phase == GamePhase.VOTE:
            if not me.has_voted:
                print(">> It's my turn to vote!")
                human_needed = True
                action_type = ActionType.VOTE
                payload = {"option": VoteOption.APPROVE}
        
        elif game.phase == GamePhase.MISSION:
            if creator_id in game.proposed_team and not me.has_acted:
                print(">> It's my turn to do mission!")
                human_needed = True
                action_type = ActionType.MISSION
                payload = {"result": MissionResult.SUCCESS}
                
        elif game.phase == GamePhase.ASSASSINATION:
            if me.character == Character.ASSASSIN:
                print(">> It's my turn to assassinate!")
                human_needed = True
                # Pick random good guy
                target = next(p.user_id for p in game.players if p.user_id != creator_id)
                action_type = ActionType.ASSASSINATE
                payload = {"target_id": target}
        
        if human_needed:
            print(f"Executing human action: {action_type} {payload}")
            try:
                await GameService.process_action(game_id, creator_id, action_type, payload)
            except Exception as e:
                print(f"Action failed: {e}")
        else:
            print("Waiting for AI...")
            
        step += 1

if __name__ == "__main__":
    asyncio.run(main())
