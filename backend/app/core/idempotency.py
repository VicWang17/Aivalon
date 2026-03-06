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

class CeleryIdempotencyManager:
    """
    Celery 任务幂等性管理器 (同步 Context Manager)
    用法:
        with CeleryIdempotencyManager(key, expire=86400) as success:
            if not success:
                print(f"Task {key} already processed.")
                return
            # 执行任务逻辑
    """
    def __init__(self, key: str, expire: int = 86400):
        # 延迟导入以避免循环引用
        from app.core.redis import redis_sync, redis_sync_pool
        self.redis = redis_sync.Redis(connection_pool=redis_sync_pool)
        self.redis_key = f"celery:idempotency:{key}"
        self.expire = expire
        self.is_new = False

    def __enter__(self):
        # SETNX
        self.is_new = self.redis.set(self.redis_key, "PROCESSING", ex=self.expire, nx=True)
        return self.is_new

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.is_new:
            # 如果一开始就不是新的，这里不需要做什么
            self.redis.close()
            return

        if exc_type:
            # 如果发生异常（任务失败），删除 Key，允许重试
            self.redis.delete(self.redis_key)
        else:
            # 任务执行成功，标记为 DONE 并延长过期时间
            self.redis.set(self.redis_key, "DONE", ex=self.expire)
        
        self.redis.close()

