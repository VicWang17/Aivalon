# 这个文件是按用户维度限流的实现。
# 背景：fastapi-limiter 默认按客户端 IP 统计——NAT/公司出口下多用户共享配额会被误伤，
# 且压测流量来自单一 IP 完全无法构造负载。认证接口的正确维度是 user_id（从 JWT 解析），未认证请求回退 IP。
# 阈值在 settings 中可配置（压测时用环境变量调高，如 RATE_LIMIT_ACTION_TIMES=100）。
from fastapi import Request
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


def create_game_rate_limit() -> RateLimiter:
    """创建对局限流：默认 10 局/小时/用户"""
    return RateLimiter(
        times=settings.RATE_LIMIT_CREATE_GAME_TIMES,
        seconds=settings.RATE_LIMIT_CREATE_GAME_SECONDS,
        identifier=user_or_ip_identifier,
    )


def action_rate_limit() -> RateLimiter:
    """提交动作限流：默认 1 次/秒/用户（对局操作的正常人手速上限）"""
    return RateLimiter(
        times=settings.RATE_LIMIT_ACTION_TIMES,
        seconds=settings.RATE_LIMIT_ACTION_SECONDS,
        identifier=user_or_ip_identifier,
    )
