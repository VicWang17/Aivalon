import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from app.core.room_actor import RoomActor
from app.services.game_service import GameService, games
from app.models.game_enums import ActionType, GamePhase
from app.schemas.game import GameState, PlayerState
from fastapi import HTTPException


@pytest.fixture
def mock_redis():
    return MagicMock()


@pytest.mark.asyncio
async def test_actor_serializes_actions():
    """Actor 核心语义：同一房间的动作严格串行，无交错（单写者模型替代分布式锁的依据）"""
    order = []

    async def handler(game_id, action_name):
        order.append(f"start:{action_name}")
        await asyncio.sleep(0.01)  # 模拟处理耗时；若并发执行，记录会交错
        order.append(f"end:{action_name}")
        return action_name

    actor = RoomActor("g1", handler, on_idle_exit=lambda _: None)
    results = await asyncio.gather(
        actor.submit("a1"),
        actor.submit("a2"),
        actor.submit("a3"),
    )

    assert results == ["a1", "a2", "a3"]
    assert order == ["start:a1", "end:a1", "start:a2", "end:a2", "start:a3", "end:a3"]


@pytest.mark.asyncio
async def test_actor_exception_does_not_block_queue():
    """单个动作失败（异常）不影响后续动作处理"""
    async def handler(game_id, should_fail):
        if should_fail:
            raise ValueError("boom")
        return "ok"

    actor = RoomActor("g2", handler, on_idle_exit=lambda _: None)
    with pytest.raises(ValueError):
        await actor.submit(True)
    # 队列未被污染，后续动作正常处理
    assert await actor.submit(False) == "ok"


@pytest.mark.asyncio
async def test_process_action_prevents_dirty_memory(mock_redis):
    """持久化失败时，内存状态不被污染（深拷贝保护在 Actor 化后依然成立）"""
    game_id = "test_dirty_memory"
    user_id = 1

    initial_state = GameState(
        game_id=game_id,
        phase=GamePhase.LEADER_SELECTION,
        players=[PlayerState(user_id=1, username="P1", seat_id=0, is_ai=False)],
        leader_id=0
    )
    games[game_id] = initial_state

    with patch("app.services.game_service.redis_client", mock_redis), \
         patch("app.services.game_service.SessionLocal"), \
         patch("app.core.game_rules.GameRuleValidator.validate_action"), \
         patch("app.services.game_service.event_journal.append_with_snapshot", new_callable=AsyncMock, side_effect=Exception("Redis Error")) as mock_append:

        try:
            await GameService.process_action(game_id, user_id, ActionType.PROPOSE, {"target_ids": [1]})
        except HTTPException as e:
            assert e.status_code == 500
            assert "Redis Error" in e.detail
        except Exception as e:
            pytest.fail(f"Raised wrong exception: {type(e)} {e}")
        else:
            pytest.fail("未抛出预期的 HTTPException")

        assert mock_append.called
        # 内存状态未被替换/污染（仍是初始对象）
        assert games[game_id] is initial_state
        games.pop(game_id, None)
