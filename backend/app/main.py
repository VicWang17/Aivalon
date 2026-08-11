# 这个文件是FastAPI应用的入口文件，负责初始化应用实例、配置中间件和路由。
from contextlib import asynccontextmanager
import asyncio
import redis.asyncio as redis
from fastapi import FastAPI
from fastapi_limiter import FastAPILimiter
from prometheus_fastapi_instrumentator import Instrumentator
from app.core.redis import redis_pool
from app.core import metrics  # noqa: F401  # 导入即注册自定义指标到 /metrics
from app.core.event_flusher import flusher_loop
from app.core.tracing import TraceIdMiddleware, setup_logging
from app.routers import auth, game, ws, users

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化限流器
    redis_client = redis.Redis(connection_pool=redis_pool)
    await FastAPILimiter.init(redis_client)
    # 启动 Write-Behind 批量刷库器（事件 Stream → MySQL）
    flusher_task = asyncio.create_task(flusher_loop())
    yield
    flusher_task.cancel()
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
