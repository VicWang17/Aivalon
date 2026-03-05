from redis.asyncio import Redis
from redis.exceptions import LockError
from fastapi import HTTPException, status
import asyncio
import time

class GameLock:
    """
    基于 Redis 的分布式锁，用于保护对局状态的并发修改。
    使用 redis-py 内置的 Lua 脚本锁实现。
    """
    def __init__(self, redis: Redis, game_id: str, timeout: int = 5, blocking_timeout: int = 10):
        self.redis = redis
        self.lock_key = f"lock:game:{game_id}"
        self.timeout = timeout # 锁自动过期时间（防止死锁）
        self.blocking_timeout = blocking_timeout # 获取锁的最大等待时间
        self.lock = self.redis.lock(
            name=self.lock_key,
            timeout=self.timeout,
            blocking_timeout=self.blocking_timeout
        )

    async def __aenter__(self):
        try:
            # 尝试获取锁
            acquired = await self.lock.acquire()
            if not acquired:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="Game is busy, please try again later"
                )
            return self
        except LockError:
             raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Game is busy (lock timeout), please try again later"
            )

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            # 释放锁
            await self.lock.release()
        except LockError:
            # 锁可能已经过期或被释放，忽略错误
            pass
