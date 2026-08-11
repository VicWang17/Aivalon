# 这个文件把一致性哈希路由接进请求路径：请求进来先问"这个房间归我吗"，
# 不归我就转发给归属节点，由那个节点的 Actor 串行处理。
#
# 为什么必须转发而不是就地处理：
#   房间 Actor 是单写者，房间状态活在归属节点的进程内存里。就地处理意味着本节点
#   要从 Redis 快照重建一份状态——于是同一房间有两份内存状态各自演进，单写者模型作废。
#   路由的意义就是保证"一个房间的写入永远只发生在一个进程里"。
#
# 转发必须防环，这是这一层最容易出事的地方：
#   两个节点的存活视图在心跳间隙可能短暂不一致（A 认为归 B，B 认为归 A），
#   不设防就是 A→B→A→B 无限转发，一次请求打爆两个节点。
#   做法：转发时带 X-Room-Forwarded 头，收到带该头的请求就**只在本地处理**，
#   绝不二次转发。代价是这一次请求可能落在非归属节点上（视图收敛后自愈），
#   收益是环路在结构上不可能出现——**一跳封顶**。
import logging
from typing import Optional

import httpx
from fastapi import Request

from app.core import node_registry
from app.core.config import settings

logger = logging.getLogger("aivalon.router")

# 转发标记头：带此头的请求一律本地处理，不再二次转发（一跳封顶，防环）
FORWARD_HEADER = "x-room-forwarded"


def should_forward(game_id: str, request: Request) -> Optional[str]:
    """判断是否需要转发。返回目标节点的完整 URL，None 表示本地处理。

    本地处理的四种情形：
      1. 房间归本节点（正常路径）
      2. 请求已被转发过一次（防环，一跳封顶）
      3. 集群视图不可用（环为空，单机降级）
      4. 归属节点地址取不到（无法转发，本地兜底优于报错）
    """
    registry = node_registry.registry
    if registry is None:
        return None

    if request.headers.get(FORWARD_HEADER):
        owner = registry.owner_of(game_id)
        if owner and owner != registry.node_id:
            # 视图不一致的证据，值得留痕：路由抖动会表现为这条日志变多
            logger.warning(
                "已转发请求落在非归属节点，本地处理（视图不一致）: game=%s owner=%s self=%s",
                game_id, owner, registry.node_id,
            )
        return None

    owner = registry.owner_of(game_id)
    if owner is None or owner == registry.node_id:
        return None

    addr = registry.addr_of(owner)
    if not addr:
        logger.warning("归属节点无地址，本地兜底: game=%s owner=%s", game_id, owner)
        return None

    return addr.rstrip("/") + str(request.url.path)


async def forward(target: str, request: Request, body: bytes) -> httpx.Response:
    """把原请求整体转发到归属节点。

    透传的头做了白名单裁剪：Authorization 必须带（下游要鉴权）、内部密钥必须带
    （AI worker 的内部接口靠它鉴权）、幂等键必须带（否则重试语义在转发后失效）、
    trace_id 必须带（跨节点排障靠它串起来）；
    Host/Content-Length 一类连接级头不能带，由 httpx 自己算。

    白名单是这里的维护负担：漏一个鉴权头，表现是"该端点转发后固定 403"，
    而不是报错——首次演练就是漏了 x-internal-secret，AI 动作全部 403，
    对局卡住不动。新增带自定义鉴权头的端点时必须同步加进来。
    """
    headers = {
        FORWARD_HEADER: "1",
        "content-type": request.headers.get("content-type", "application/json"),
    }
    for key in ("authorization", "x-internal-secret",
                "x-idempotency-key", "x-request-id"):
        value = request.headers.get(key)
        if value:
            headers[key] = value

    timeout = httpx.Timeout(settings.ROOM_FORWARD_TIMEOUT)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.request(
            request.method, target,
            content=body, headers=headers, params=request.query_params,
        )
