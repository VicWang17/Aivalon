# 这个文件是 Redis 连接池与客户端工厂。
#
# 池子大小是 S4 突发复测撞出来的一个真 bug
# ------------------------------------
# 原来这里建 `ConnectionPool` 时**什么都不写**，于是取了 redis-py 的默认值 **100**——
# **没有人选过这个数**。S4 十倍突发（500 用户、387 RPS）下池子被抽干，而 redis-py
# 的行为是**抛错不是等**：`_available_connections.pop()` 拿到 `IndexError`，
# 转成 `MaxConnectionsError`，一路冒到路由外面变成 **500**，1,377 次。
#
# **最要紧的不是数字小，是错误的类别**：429/503 是"我们主动拒了、你稍后再来"，
# 500 是"我们自己坏了"。分层限流做了三层（准入 / 滑动窗口 / 房间队列），
# 最后却在自己的连接池上把过载翻译成了服务端错误——**那正是简历⑦「零中断」
# 最不能出现的形态**：前面三道闸都在如实回答"现在别来"，第四道在说"我崩了"。
#
# 两条修法，缺一不可：
#   1. **池子上限必须 >= 准入层允许的突发并发**（400）。原来是 100 对 400，
#      等于"门口保安放 400 人进场、场内只有 100 把椅子"——**保护层的上限宽于
#      它保护的最窄资源，这层保护在这个维度上就没生效**。
#   2. **池满时排队等一下，而不是立刻抛错**（`BlockingConnectionPool`）。
#      判据是**持有时间**：一条连接借出去只跑一次命令往返（亚毫秒级），队伍必然
#      很快前进，几毫秒排队换掉一次 500 是划算的。这和 H-3c·上「房间队列刻意
#      不用 `await put`」**恰好相反**，而那条的判据同样是持有时间——房间动作要跑
#      十几秒，在那儿排队等于无上界地等。**同一个问题问的是同一句话，答案却相反。**
#      但等待**必须有超时**（`REDIS_POOL_TIMEOUT`）：**没有上界的等待只是把排队
#      藏到看不见的地方**（同 H-3c·上）。超时后抛的是 `ConnectionError`，
#      在 main.py 里被兜成 **503 + Retry-After**，不再是 500。
#
# **长期占用连接的地方要留出余量**：`cache.py` 的失效订阅和 `socket_manager.py`
# 的跨节点扇出订阅各占一条**进程存活期间不还**的连接。它们只有两条、池子有几百，
# 所以不构成问题；但要记住这类持有者数量一旦接近池子上限，
# **`BlockingConnectionPool` 会从"排队"变成"死等"**——借出去不还的连接，队伍不会前进。
import redis.asyncio as redis
import redis as redis_sync
from app.core.config import settings

# 异步连接池：API 进程的所有 Redis 访问都走这里
redis_pool = redis.BlockingConnectionPool(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    password=settings.REDIS_PASSWORD,
    max_connections=settings.REDIS_MAX_CONNECTIONS,
    timeout=settings.REDIS_POOL_TIMEOUT,
    decode_responses=True,  # 自动解码为字符串
    encoding="utf-8",
)

# 同步连接池 (用于 Celery 等同步场景)。**同样显式给上限**——
# 不写的话 redis-py 同步池的默认上限是个天文数字，那是另一种失控：
# 不报错，改为把连接数顶到 Redis 的 `maxclients` 上，
# **于是压垮的不是本进程而是所有人共用的那个 Redis**。
# worker 是 concurrency=8 的进程，不需要和 API 一样宽。
redis_sync_pool = redis_sync.ConnectionPool(
    host=settings.REDIS_HOST,
    port=settings.REDIS_PORT,
    password=settings.REDIS_PASSWORD,
    max_connections=settings.REDIS_MAX_CONNECTIONS,
    decode_responses=True,
    encoding="utf-8",
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
