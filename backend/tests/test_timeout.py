from app.core.game_rules import GameRuleValidator, TimeoutPolicy
from app.schemas.game import GameState, PlayerState
from app.models.game_enums import GamePhase, ActionType, Character, VoteOption, MissionResult
import time

def create_mock_game(phase=GamePhase.LEADER_SELECTION):
    players = [
        PlayerState(user_id=i, username=f"u{i}", seat_id=i-1, character=Character.SERVANT)
        for i in range(1, 9)
    ]
    players[0].character = Character.MERLIN
    players[1].character = Character.ASSASSIN
    
    return GameState(
        game_id="test",
        phase=phase,
        leader_id=1,
        players=players,
        proposed_team=[],
        round=1,
        phase_start_time=time.time()
    )

def test_timeout_check():
    game = create_mock_game(GamePhase.VOTE)
    
    # Not timed out yet
    assert TimeoutPolicy.is_timed_out(game) == False
    
    # Simulate timeout (past 60s)
    game.phase_start_time = time.time() - 61
    assert TimeoutPolicy.is_timed_out(game) == True

def test_default_action_vote():
    game = create_mock_game(GamePhase.VOTE)
    player = game.players[0]
    
    action = TimeoutPolicy.get_default_action(game, player)
    assert action is not None
    assert action["action_type"] == ActionType.VOTE
    assert action["payload"]["option"] == VoteOption.REJECT

def test_default_action_mission():
    game = create_mock_game(GamePhase.MISSION)
    player = game.players[0]
    
    action = TimeoutPolicy.get_default_action(game, player)
    assert action is not None
    assert action["action_type"] == ActionType.MISSION
    assert action["payload"]["result"] == MissionResult.SUCCESS

def test_default_action_propose():
    # 1. Round 1 (needs 3 people)
    game = create_mock_game(GamePhase.TEAM_PROPOSAL)
    game.round = 1
    # Leader is user_id=1, seat_id=0 (from create_mock_game)
    # Players by seat: 0->u1, 1->u2, 2->u3, 3->u4 ...
    
    leader = game.players[0] # user_id=1
    assert game.leader_id == 1
    
    action = TimeoutPolicy.get_default_action(game, leader)
    assert action is not None
    assert action["action_type"] == ActionType.PROPOSE
    
    target_ids = action["payload"]["target_ids"]
    assert len(target_ids) == 3
    # Should be seat 0, 1, 2 -> user_id 1, 2, 3
    assert target_ids == [1, 2, 3]

    # 2. Change leader to seat 6 (user_id 7), Round 2 (needs 4 people)
    game.round = 2
    game.leader_id = 7 # seat 6
    leader_player = next(p for p in game.players if p.user_id == 7)
    
    action = TimeoutPolicy.get_default_action(game, leader_player)
    # Should be seat 6, 7, 0, 1 -> user_id 7, 8, 1, 2
    target_ids = action["payload"]["target_ids"]
    assert len(target_ids) == 4
    assert target_ids == [7, 8, 1, 2]

def test_default_action_assassinate():
    game = create_mock_game(GamePhase.ASSASSINATION)
    # Assassin is user_id=2, seat_id=1 (from create_mock_game)
    # Next player is seat_id=2 -> user_id=3
    
    assassin = game.players[1]
    assert assassin.character == Character.ASSASSIN
    
    action = TimeoutPolicy.get_default_action(game, assassin)
    assert action is not None
    assert action["action_type"] == ActionType.ASSASSINATE
    assert action["payload"]["target_id"] == 3

    # Case: Assassin is seat 7 (user_id 8), next is seat 0 (user_id 1)
    # Reset character
    game.players[1].character = Character.SERVANT
    game.players[7].character = Character.ASSASSIN
    assassin = game.players[7]
    
    action = TimeoutPolicy.get_default_action(game, assassin)
    assert action is not None
    assert action["action_type"] == ActionType.ASSASSINATE
    assert action["payload"]["target_id"] == 1
