import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock
from app.core.idempotency import IdempotencyManager
from fastapi import HTTPException

@pytest.mark.asyncio
async def test_idempotency_manager_success():
    """测试正常流程：Key不存在 -> Processing -> Done"""
    mock_redis = MagicMock()
    # set 返回 True 表示设置成功 (NX=True 且 Key 不存在)
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.delete = AsyncMock()
    
    # Context Manager
    async with IdempotencyManager(mock_redis, "test-key", 1):
        # Simulate processing
        await asyncio.sleep(0.01)
        
    # Verify
    # 1. set PROCESSING called with nx=True
    mock_redis.set.assert_any_call("idempotency:1:test-key", "PROCESSING", ex=30, nx=True)
    # 2. set DONE called
    mock_redis.set.assert_any_call("idempotency:1:test-key", "DONE", ex=86400)

@pytest.mark.asyncio
async def test_idempotency_manager_conflict():
    """测试冲突流程：Key已存在 -> 409"""
    mock_redis = MagicMock()
    # set 返回 False 表示设置失败 (NX=True 且 Key 已存在)
    mock_redis.set = AsyncMock(return_value=False)
    
    with pytest.raises(HTTPException) as excinfo:
        async with IdempotencyManager(mock_redis, "test-key", 1):
            pass
            
    assert excinfo.value.status_code == 409

@pytest.mark.asyncio
async def test_idempotency_manager_failure():
    """测试失败回滚流程：Key不存在 -> Processing -> Exception -> Delete"""
    mock_redis = MagicMock()
    # set 返回 True (Processing 成功)
    mock_redis.set = AsyncMock(return_value=True)
    mock_redis.delete = AsyncMock()
    
    try:
        async with IdempotencyManager(mock_redis, "test-key", 1):
            raise ValueError("Business Logic Error")
    except ValueError:
        pass
        
    # Verify delete called
    mock_redis.delete.assert_called_with("idempotency:1:test-key")
