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

# ---------- 以下为 v2 C 组补充的覆盖 ----------

def test_mission_team_sizes_config():
    """8人局任务人数配置：3-4-4-5-5（规则常量回归保护）"""
    expected = {1: 3, 2: 4, 3: 4, 4: 5, 5: 5}
    for round_num, size in expected.items():
        assert GameRuleValidator.get_mission_team_size(round_num) == size

def test_vote_validation():
    game = create_mock_game(GamePhase.VOTE)

    # Success
    GameRuleValidator.validate_action(game, 1, ActionType.VOTE)

    # Fail: 重复投票（防 DEVLOG 005 类竞态造成重复计票）
    game.players[0].has_voted = True
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 1, ActionType.VOTE)
    assert exc.value.status_code == 400

    # Fail: 非投票阶段
    game.phase = GamePhase.TEAM_PROPOSAL
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 2, ActionType.VOTE)
    assert exc.value.status_code == 400

def test_mission_evil_can_fail():
    """坏人阵营可以投失败（与好人限制互为反面）"""
    game = create_mock_game(GamePhase.MISSION)
    game.proposed_team = [2]  # players[1] 是 ASSASSIN
    GameRuleValidator.validate_action(game, 2, ActionType.MISSION, {"result": "fail"})

def test_mission_not_in_team():
    game = create_mock_game(GamePhase.MISSION)
    game.proposed_team = [1, 2]

    # Fail: 不在执行队伍
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 3, ActionType.MISSION, {"result": "success"})
    assert exc.value.status_code == 403

    # Fail: 重复执行
    game.players[0].has_acted = True
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 1, ActionType.MISSION, {"result": "success"})
    assert exc.value.status_code == 400

def test_assassinate_validation():
    game = create_mock_game(GamePhase.ASSASSINATION)

    # Success: 刺客（players[1], user_id=2）刺杀目标
    GameRuleValidator.validate_action(game, 2, ActionType.ASSASSINATE, {"target_id": 1})

    # Fail: 非刺客不能刺杀
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 1, ActionType.ASSASSINATE, {"target_id": 3})
    assert exc.value.status_code == 403

    # Fail: 缺少目标
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 2, ActionType.ASSASSINATE, {})
    assert exc.value.status_code == 400

    # Fail: 非刺杀阶段
    game.phase = GamePhase.VOTE
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 2, ActionType.ASSASSINATE, {"target_id": 1})
    assert exc.value.status_code == 400

def test_propose_invalid_targets():
    game = create_mock_game(GamePhase.TEAM_PROPOSAL)

    # Fail: 提名的玩家不在对局中
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 1, ActionType.PROPOSE, {"target_ids": [1, 2, 999]})
    assert "提名的玩家无效" in exc.value.detail

def test_player_not_in_game():
    game = create_mock_game(GamePhase.VOTE)
    with pytest.raises(HTTPException) as exc:
        GameRuleValidator.validate_action(game, 999, ActionType.VOTE)
    assert exc.value.status_code == 403
