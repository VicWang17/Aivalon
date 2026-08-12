# 这个文件是FastAPI应用的入口文件，负责初始化应用实例、配置中间件和路由。
from contextlib import asynccontextmanager
import asyncio
import logging
import redis.asyncio as redis
from fastapi import FastAPI
from fastapi_limiter import FastAPILimiter
from prometheus_fastapi_instrumentator import Instrumentator
from app.core.redis import redis_pool
from app.core import bloom
from app.core import cache
from app.core import metrics  # noqa: F401  # 导入即注册自定义指标到 /metrics
from app.core.event_flusher import flusher_loop
from app.core import node_registry
from app.core import pool_probe
from app.core import socket_manager
from app.core.tracing import TraceIdMiddleware, setup_logging
from app.db.base import engine
from app.routers import auth, game, ws, users
from app.services.game_service import GameService

logger = logging.getLogger("aivalon.main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化限流器
    redis_client = redis.Redis(connection_pool=redis_pool)
    await FastAPILimiter.init(redis_client)
    # 启动 Write-Behind 批量刷库器（事件 Stream → MySQL）
    flusher_task = asyncio.create_task(flusher_loop())
    # 集群节点注册 + 心跳：维护房间路由的一致性哈希环。
    # 先同步做一次心跳再接流量，避免启动瞬间环为空导致首批请求走单机降级分支。
    cluster = node_registry.init(redis_client)
    try:
        await cluster.heartbeat()
    except Exception as e:
        logger.warning("集群首次心跳失败，降级为单机路由: %s", e)
    cluster.start()
    # WS 跨节点扇出：连接挂在哪个节点与房间归属无关，广播要按连接路由表定向投递
    socket_manager.manager.bind_cluster(redis_client, cluster.node_id)
    socket_manager.manager.start()
    # 缓存失效广播：L2 是共享的删一次就够，L1 在各进程自己堆里，得喊一声让大家清
    cache.bind(redis_client)
    cache.start()
    # 布隆过滤器预热：位图不存在时才灌一遍库里已有的对局 id。
    # 这不是性能优化——过滤器上线前建的房间没登记过，位图一旦非空就会把它们判成
    # "不存在"，直接 404 掉真实房间（见 app/core/bloom.py 的 warm）。
    # 刻意同步 await、不丢后台：预热要是慢慢在后台跑，期间来一次建局就把位图写成非空，
    # 预热任务的 `exists` 检查随即返回真、直接跳过——老房间就永远没登记上了。
    # 代价是启动多花一次全表扫 id 的时间，换的是"开始拦之前一定登记齐了"。
    await bloom.warm(redis_client, GameService.load_all_game_ids)
    # DB 连接池泄漏探针（默认关闭，DB_POOL_PROBE=true 时启用，压测排障用）
    probe_task = pool_probe.start(engine, label="main")
    yield
    flusher_task.cancel()
    if probe_task:
        probe_task.cancel()
    await socket_manager.manager.stop()
    await cache.stop()
    # 优雅下线：主动摘掉自己，把计划内重启的路由空窗从 TTL 级别压到一次往返
    await cluster.stop()
    await redis_client.close()

app = FastAPI(
    title="Aivalon",
    description="Aivalon - AI-driven Avalon Game Platform",
    version="0.1.0",
    lifespan=lifespan
)

# Prometheus 指标：自动采集各路由的 QPS 与延迟分位数，暴露在 /metrics
Instrumentator().instrument(app).expose(app)

# trace_id 透传：生成/接续 X-Request-ID，注入日志上下文与响应头
setup_logging()
app.add_middleware(TraceIdMiddleware)

# 注册路由
app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(game.router, prefix="/api/v1/games", tags=["games"])
app.include_router(ws.router, prefix="/api/v1/ws", tags=["websocket"])
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])

@app.get("/")
async def root():
    return {"message": "Welcome to Aivalon"}

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/cluster", include_in_schema=False)
async def cluster_status(game_id: str | None = None):
    """集群视图：本节点身份、存活节点、本地驻留的房间 Actor 数。

    这是排查路由问题的第一现场——路由异常的表现是"动作提交成功但状态没变"，
    单看响应码分辨不出来，必须能对比各节点的存活视图与 Actor 驻留情况。
    传 game_id 可直接问"这个房间归谁"，双节点演练靠它判定转发是否正确。
    """
    from app.core.room_actor import actor_manager

    registry = node_registry.registry
    if registry is None:
        return {"clustered": False, "local_actors": actor_manager.active_count}

    result = {
        "clustered": True,
        "node_id": registry.node_id,
        "live_nodes": sorted(registry.live_nodes),
        "local_actors": actor_manager.active_count,
        "resident_games": sorted(actor_manager.game_ids),
    }
    if game_id:
        owner = registry.owner_of(game_id)
        result["query"] = {
            "game_id": game_id,
            "owner": owner,
            "is_mine": registry.is_mine(game_id),
            "owner_addr": registry.addr_of(owner) if owner else None,
        }
    return result
