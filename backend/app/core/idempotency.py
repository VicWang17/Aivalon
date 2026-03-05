from fastapi import HTTPException, status
from redis.asyncio import Redis

class IdempotencyManager:
    """
    幂等性管理器 (Context Manager)
    用法:
        async with IdempotencyManager(redis, key, user_id):
            await do_something()
    """
    def __init__(self, redis: Redis, key: str, user_id: int, expire: int = 86400):
        self.redis = redis
        self.redis_key = f"idempotency:{user_id}:{key}"
        self.expire = expire

    async def __aenter__(self):
        # 使用 set(nx=True) 原子性地检查并设置 Key
        # 如果 Key 不存在，设置成功返回 True；如果已存在，返回 False
        success = await self.redis.set(self.redis_key, "PROCESSING", ex=30, nx=True)
        
        if not success:
            # Key 已存在，说明是重复请求或正在处理中
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Request already processed or in progress"
            )
        
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            # 如果发生异常（业务失败），删除 Key，允许用户重试
            # 注意：如果是 400 Bad Request 等客户端错误，删除 Key 也是合理的，
            # 因为用户修正参数后再次提交是新的请求（虽然 Key 可能不变，但这里允许重试）
            # 或者前端应该在参数变更时生成新 Key
            await self.redis.delete(self.redis_key)
        else:
            # 业务执行成功，标记为 DONE 并延长过期时间
            await self.redis.set(self.redis_key, "DONE", ex=self.expire)
