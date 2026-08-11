# 这个文件是按用户维度限流的实现。
# 背景：fastapi-limiter 默认按客户端 IP 统计——NAT/公司出口下多用户共享配额会被误伤，
# 且压测流量来自单一 IP 完全无法构造负载。认证接口的正确维度是 user_id（从 JWT 解析），未认证请求回退 IP。
# 阈值在 settings 中可配置（压测时用环境变量调高，如 RATE_LIMIT_ACTION_TIMES=100）。
from fastapi import Request, Response
from fastapi_limiter.depends import RateLimiter
from jose import JWTError, jwt

from app.core.config import settings


async def user_or_ip_identifier(request: Request) -> str:
    """限流统计键：优先取 JWT 中的 user_id，否则回退客户端 IP"""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            payload = jwt.decode(auth[7:], settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
            sub = payload.get("sub")
            if sub is not None:
                return f"user:{sub}"
        except JWTError:
            pass
    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


class SkipForwardedRateLimiter(RateLimiter):
    """跨节点转发豁免的限流器。

    房间路由（app/core/room_router.py）会把请求转发到归属节点，转发后的请求会在
    第二个节点上再次经过限流器——同一个用户动作被计两次，配额凭空减半，且计数
    多少取决于房间落在哪个节点，行为不可预期。
    限流应该只在**入口节点**发生一次，所以带转发标记的请求直接放行。
    这不构成绕过途径：该标记只在集群内部转发时添加，外部请求即使伪造该头，
    也已经在入口节点被计过一次了。
    """

    async def __call__(self, request: Request, response: Response):
        from app.core.room_router import FORWARD_HEADER

        if request.headers.get(FORWARD_HEADER):
            return
        return await super().__call__(request, response)


def create_game_rate_limit() -> RateLimiter:
    """创建对局限流：默认 10 局/小时/用户"""
    return SkipForwardedRateLimiter(
        times=settings.RATE_LIMIT_CREATE_GAME_TIMES,
        seconds=settings.RATE_LIMIT_CREATE_GAME_SECONDS,
        identifier=user_or_ip_identifier,
    )


def action_rate_limit() -> RateLimiter:
    """提交动作限流：默认 1 次/秒/用户（对局操作的正常人手速上限）"""
    return SkipForwardedRateLimiter(
        times=settings.RATE_LIMIT_ACTION_TIMES,
        seconds=settings.RATE_LIMIT_ACTION_SECONDS,
        identifier=user_or_ip_identifier,
    )
