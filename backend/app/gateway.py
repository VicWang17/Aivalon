# 这个文件是独立 WS 网关进程的入口：只做连接维持、握手鉴权、消息转发，不含业务逻辑。
#
# 启动方式（与业务节点分开进程、分开端口）：
#   GATEWAY_ID=gw-1 uvicorn app.gateway:app --port 9000
#
# 为什么要把网关拆出去
# --------------------
# 长连接和业务代码的生命周期天生不一致。业务代码是要频繁改的，改完就得重启；
# 而重启一个持有 2,000 条长连接的进程，代价是这 2,000 个客户端同时掉线重连。
# 连接和逻辑同居一个进程，等于让"发一次版本"和"全员掉线"绑在一起。
# 拆开之后：业务节点随便重启，连接不受影响（广播照样能通过 Redis 路由表投递过来）；
# 网关只在改连接层时才需要重启，而连接层几乎不改。
# 另一半好处是两者的扩容维度不同：连接数吃内存和文件描述符，业务吃 CPU 和 DB 连接，
# 各自独立扩容比一起扩省得多。
#
# 拆分的实质是"哪些 router 挂在这个进程上"，不是复制一份 WS 代码。
# 所以这里复用 ws.router 本体——它现在只剩连接维持、握手鉴权、消息转发三件事
# （对局状态的读取已经收敛到 ws_tier，只读快照、不碰业务对象，见那个文件的说明）。
# 复制一份代码的写法会让两边慢慢长歪，最后"网关行为和主进程不一致"变成排障噩梦。
#
# 关键约束：网关不进一致性哈希环
# ------------------------------
# 网关需要一个身份（要有自己的专属广播频道，也要能被登记进房间路由表），
# 但它**绝不能**出现在 node_registry 的 NODE_SET_KEY 里——那个 ZSET 是哈希环的
# 唯一数据源，环上多一个网关，就会有 1/N 的房间被分配给一个根本没有业务逻辑、
# 也没有 Actor 的进程，那些房间的动作会被转发过来然后无人处理。
# 落地上就是这里不调 node_registry.init()：身份和"是否参与房间归属"本来是两件事，
# 网关只要前者。
from contextlib import asynccontextmanager
import logging
import os
import socket as _socket
import uuid

import redis.asyncio as redis
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator

from app.core.config import settings
from app.core.redis import redis_pool
from app.core import metrics  # noqa: F401  # 导入即注册自定义指标到 /metrics
from app.core import socket_manager
from app.core.tracing import TraceIdMiddleware, setup_logging
from app.routers import ws

logger = logging.getLogger("aivalon.gateway")


def resolve_gateway_id() -> str:
    """网关身份。显式指定优先，否则按 主机名-进程号-随机后缀 生成。

    带 gw- 前缀是为了在 Redis 里一眼能分清网关和业务节点——排查"广播发去哪了"时，
    路由表里躺着一串 id，看不出谁是谁会很难受。
    """
    configured = getattr(settings, "GATEWAY_ID", "") or os.getenv("GATEWAY_ID", "")
    if configured:
        return configured
    return f"gw-{_socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis_client = redis.Redis(connection_pool=redis_pool)
    gateway_id = resolve_gateway_id()
    # 只接广播通道，不碰哈希环（见文件头"网关不进一致性哈希环"）
    socket_manager.manager.bind_cluster(redis_client, gateway_id)
    socket_manager.manager.start()
    logger.info("WS 网关已启动: gateway_id=%s（不参与房间归属）", gateway_id)
    yield
    await socket_manager.manager.stop()
    await redis_client.close()


app = FastAPI(
    title="Aivalon WS Gateway",
    description="独立 WS 网关：连接维持 + 握手鉴权 + 消息转发",
    version="0.1.0",
    lifespan=lifespan,
)

Instrumentator().instrument(app).expose(app)
setup_logging()
app.add_middleware(TraceIdMiddleware)

# 只挂 WS 路由。业务路由（auth / games / users）刻意不挂：
# 挂上去就等于把业务流量又引回了这个进程，拆分就没意义了。
app.include_router(ws.router, prefix="/api/v1/ws", tags=["websocket"])


@app.get("/health")
async def health_check():
    return {"status": "ok", "role": "ws-gateway"}
