from app.core.game_rules import GameRuleValidator
from app.schemas.game import GameState, PlayerState
from app.models.game_enums import GamePhase, ActionType, Character, VoteOption
import pytest
from fastapi import HTTPException

def create_mock_game(phase=GamePhase.LEADER_SELECTION):
    # Need at least 3 players for round 1 (size 3)
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
        round=1
    )

def test_propose_validation():
    game = create_mock_game(GamePhase.TEAM_PROPOSAL)
    
    # Success case: Round 1 needs 3 players
    GameRuleValidator.validate_action(game, 1, ActionType.PROPOSE, {"target_ids": [1, 2, 3]})
    
    # Fail: Wrong size (Round 1 needs 3, provided 2)
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 1, ActionType.PROPOSE, {"target_ids": [1, 2]})
    assert "需要提名 3 名玩家" in exc.value.detail
    
    # Fail: Wrong phase
    game.phase = GamePhase.VOTE
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 1, ActionType.PROPOSE, {"target_ids": [1, 2, 3]})
    assert exc.value.status_code == 400

    # Fail: Not leader
    game.phase = GamePhase.TEAM_PROPOSAL
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 2, ActionType.PROPOSE, {"target_ids": [1, 2, 3]})
    assert exc.value.status_code == 403

def test_mission_failure_logic():
    # Round 1-3, 5: 1 fail = fail
    assert GameRuleValidator.is_mission_failed(round_num=1, fail_count=1) == True
    assert GameRuleValidator.is_mission_failed(round_num=3, fail_count=1) == True
    assert GameRuleValidator.is_mission_failed(round_num=5, fail_count=1) == True
    
    # Round 4: 1 fail = success, 2 fails = fail
    assert GameRuleValidator.is_mission_failed(round_num=4, fail_count=1) == False
    assert GameRuleValidator.is_mission_failed(round_num=4, fail_count=2) == True

def test_speak_validation():
    game = create_mock_game(GamePhase.SPEECH)
    game.speaker_id = 1
    
    # Success
    GameRuleValidator.validate_action(game, 1, ActionType.SPEAK)
    
    # Fail: Wrong person
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 2, ActionType.SPEAK)
    assert exc.value.status_code == 403

def test_mission_good_fail():
    game = create_mock_game(GamePhase.MISSION)
    game.proposed_team = [1, 2]
    
    # Merlin tries to fail -> Error
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 1, ActionType.MISSION, {"result": "fail"})
    assert "好人阵营只能投任务成功" in exc.value.detail
