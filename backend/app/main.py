# 这个文件是FastAPI应用的入口文件，负责初始化应用实例、配置中间件和路由。
from contextlib import asynccontextmanager
import redis.asyncio as redis
from fastapi import FastAPI
from fastapi_limiter import FastAPILimiter
from app.core.redis import redis_pool
from app.routers import auth, game, ws, users

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化限流器
    redis_client = redis.Redis(connection_pool=redis_pool)
    await FastAPILimiter.init(redis_client)
    yield
    await redis_client.close()

app = FastAPI(
    title="Aivalon",
    description="Aivalon - AI-driven Avalon Game Platform",
    version="0.1.0",
    lifespan=lifespan
)

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
