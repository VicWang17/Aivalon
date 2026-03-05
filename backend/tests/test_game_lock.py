import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import copy
from app.services.game_service import GameService, games
from app.models.game_enums import ActionType, GamePhase, Character
from app.schemas.game import GameState, PlayerState
from fastapi import HTTPException

# Mock Redis client
@pytest.fixture
def mock_redis():
    return MagicMock()

# Mock GameLock context manager
@pytest.fixture
def mock_game_lock():
    lock_mock = AsyncMock()
    lock_mock.__aenter__.return_value = lock_mock
    lock_mock.__aexit__.return_value = None
    return lock_mock

@pytest.mark.asyncio
async def test_process_action_uses_lock(mock_redis, mock_game_lock):
    """测试 process_action 是否正确使用了分布式锁"""
    game_id = "test_lock_game"
    user_id = 1
    
    # Setup initial game state
    games[game_id] = GameState(
        game_id=game_id,
        phase=GamePhase.LEADER_SELECTION,
        players=[PlayerState(user_id=1, username="P1", seat_id=0, is_ai=False)],
    )
    
    # Patch dependencies
    # We need to patch where GameLock is imported in game_service.py
    with patch("app.services.game_service.GameLock", return_value=mock_game_lock) as MockLockClass, \
         patch("app.services.game_service.redis_client", mock_redis), \
         patch("app.services.game_service.SessionLocal") as mock_db, \
         patch("app.core.game_rules.GameRuleValidator.validate_action"), \
         patch("app.services.game_service.manager.broadcast_game_update", new_callable=AsyncMock), \
         patch("app.services.game_service.GameService._append_event"):
        
        # Execute
        # We need to pass a payload that won't crash logic if validation passes
        # ActionType.PROPOSE expects target_ids
        await GameService.process_action(game_id, user_id, ActionType.PROPOSE, {"target_ids": []})
        
        # Verify Lock was initialized with redis_client and game_id
        MockLockClass.assert_called_once_with(mock_redis, game_id)
        
        # Verify Lock was acquired (entered)
        assert mock_game_lock.__aenter__.called
        assert mock_game_lock.__aexit__.called

@pytest.mark.asyncio
async def test_process_action_prevents_dirty_memory(mock_redis, mock_game_lock):
    """测试当持久化失败时，内存状态不应被更新"""
    game_id = "test_dirty_memory"
    user_id = 1
    
    initial_state = GameState(
        game_id=game_id,
        phase=GamePhase.LEADER_SELECTION,
        players=[PlayerState(user_id=1, username="P1", seat_id=0, is_ai=False)],
        leader_id=0
    )
    games[game_id] = initial_state
    
    # Simulate DB failure
    with patch("app.services.game_service.GameLock", return_value=mock_game_lock), \
         patch("app.services.game_service.redis_client", mock_redis), \
         patch("app.services.game_service.SessionLocal") as mock_db, \
         patch("app.core.game_rules.GameRuleValidator.validate_action"), \
         patch.object(GameService, '_append_event', side_effect=Exception("DB Error")) as mock_append:
        
        # Execute expecting error
        try:
            await GameService.process_action(game_id, user_id, ActionType.PROPOSE, {"target_ids": [1]})
        except HTTPException as e:
            assert e.status_code == 500
            assert "DB Error" in e.detail
        except Exception as e:
            pytest.fail(f"Raised wrong exception: {type(e)} {e}")
        else:
            if not mock_append.called:
                pytest.fail("Did not raise HTTPException AND mock_append was NOT called")
            else:
                pytest.fail("Did not raise HTTPException BUT mock_append WAS called")
        
        assert mock_append.called
        
        # Verify memory state is UNCHANGED (still the initial object)
        # We check object identity to ensure it wasn't replaced
        assert games[game_id] is initial_state
