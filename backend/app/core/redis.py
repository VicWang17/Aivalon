# 这个文件是 Redis 数据库连接的工具类，用于缓存验证码、会话信息和实现限流功能。
import redis.asyncio as redis
from app.core.config import settings

# 创建 Redis 连接池
redis_pool = redis.ConnectionPool(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    password=settings.REDIS_PASSWORD,
    decode_responses=True,  # 自动解码为字符串
    encoding="utf-8"
)

# 获取 Redis 客户端实例
async def get_redis():
    """依赖注入获取 Redis 客户端"""
    client = redis.Redis(connection_pool=redis_pool)
    try:
        yield client
    finally:
        await client.close()

# 全局单例客户端（用于非依赖注入场景）
# 注意：这可能会绑定到创建时的 Event Loop，在 Celery 等多 Loop 环境中可能导致 "Event loop is closed" 错误
# 建议在异步任务中使用 get_redis_client_for_loop() 或手动创建
redis_client = redis.Redis(connection_pool=redis_pool)

def get_new_redis_client() -> redis.Redis:
    """创建一个新的 Redis 客户端实例（不使用连接池或创建新连接池）"""
    # 注意：redis-py 的 ConnectionPool 默认不绑定 loop，但 Client 可能会
    # 在 Celery 中，我们最好直接创建新的 Client，复用全局 Pool
    return redis.Redis(connection_pool=redis_pool)
