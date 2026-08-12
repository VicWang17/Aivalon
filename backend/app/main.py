# 这个文件是FastAPI应用的入口文件，负责初始化应用实例、配置中间件和路由。
from contextlib import asynccontextmanager
import asyncio
import logging
import redis.asyncio as redis
from fastapi import FastAPI
from fastapi_limiter import FastAPILimiter
from prometheus_fastapi_instrumentator import Instrumentator
from app.core.redis import redis_pool
from app.core import admission
from app.core import ai_queue
from app.core import bloom
from app.core import cache
from app.core import metrics  # noqa: F401  # 导入即注册自定义指标到 /metrics
from app.core.event_flusher import flusher_loop
from app.core import node_registry
from app.core import rank_buffer
from app.core import pool_probe
from app.core import sliding_window
from app.core import socket_manager
from app.core.tracing import TraceIdMiddleware, setup_logging
from app.db.base import engine
from app.routers import admin, auth, game, ws, users
from app.services.game_service import GameService
from app.services.rank_service import RankService

logger = logging.getLogger("aivalon.main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化限流器
    redis_client = redis.Redis(connection_pool=redis_pool)
    await FastAPILimiter.init(redis_client)
    # 网关层准入的令牌桶脚本注册。没 bind 过的话 check() 直接放行——
    # 刻意如此：单测和脚本里 import 到 app 但没起 lifespan 的场景不该被限流卡住
    admission.bind(redis_client)
    # 应用层滑动窗口的脚本注册。同样没 bind 就放行
    sliding_window.bind(redis_client)
    # AI 队列深度记账的脚本注册。没 bind 时深度报 0 = 不降级
    # （Celery worker 那边不跑 lifespan，它自己就地注册，见 ai_queue._resolve）
    ai_queue.bind(redis_client)
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
    # 热榜批量刷榜循环：对局结束只把增量攒进缓冲，这里周期性合并成一批 ZINCRBY 打出去。
    # 放在 API 进程而不是 Celery worker 里：它是个常驻的定时循环，
    # 而 Celery 那边是任务驱动的，没有"每秒跑一次"这个语义（beat 最小粒度也不合适）。
    # 传 node_id 是为了让换出用的临时 key 按节点分开，见 rank_buffer.drain_once。
    rank_task = asyncio.create_task(
        rank_buffer.drain_loop(redis_client, cluster.node_id)
    )
    # 榜单定时归并：把 8 个分片各取 Top N 合成一份带展示字段的快照。
    # 归并次数由间隔决定、和读 QPS 无关，这是"定时归并"相对"每次读都归并"的全部意义。
    # 不像上面的刷榜循环那样需要 node_id：归并是读 + 覆盖写，多节点重复做只是浪费 CPU
    # 不会算错，所以刻意不做互斥（见 RankService.merge_loop）。
    merge_task = asyncio.create_task(RankService.merge_loop(redis_client))
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
    rank_task.cancel()
    merge_task.cancel()
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

# 网关层准入：全局令牌桶 + IP 令牌桶，被拒的请求不碰任何后端资源。
# 注意这两行的顺序：Starlette 里**后加的中间件在外层**，所以 TraceId 在准入外面。
# 刻意这么排——准入拒掉的 429 也要有 trace_id 和访问日志，否则"某个客户端什么时候
# 被限了"在复盘时查不到，而这恰恰是限流最需要解释清楚的事。代价只是一次 uuid 生成。
app.add_middleware(admission.AdmissionMiddleware)

# trace_id 透传：生成/接续 X-Request-ID，注入日志上下文与响应头
setup_logging()
app.add_middleware(TraceIdMiddleware)

# 注册路由
app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(game.router, prefix="/api/v1/games", tags=["games"])
app.include_router(ws.router, prefix="/api/v1/ws", tags=["websocket"])
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
# 降级开关操作入口。内部密钥鉴权，不进 OpenAPI 文档（见 routers/admin.py）
app.include_router(admin.router, prefix="/internal", tags=["internal"])

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
