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
redis_client = redis.Redis(connection_pool=redis_pool)
