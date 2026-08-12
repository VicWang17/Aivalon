# 这个文件是按用户维度限流的**配置入口**：定义各接口的限流键与阈值，算法实现在 sliding_window.py。
#
# 背景一：限流键的维度。fastapi-limiter 默认按客户端 IP 统计——NAT/公司出口下多用户共享配额会被
# 误伤，且压测流量来自单一 IP 完全无法构造负载。认证接口的正确维度是 user_id（从 JWT 解析），
# 未认证请求回退 IP。
#
# 背景二：为什么不再用 fastapi-limiter 的 RateLimiter。它的 Lua 是 `SET px` + `INCR`，
# 即**固定窗口**：key 从第一个请求起算过期，所以窗口边界能挤进 2 倍——上小时最后一秒建 10 局、
# 下小时第一秒再建 10 局，2 秒内建了 20 局，而每次都"没超过 10 局/小时"。
# 业务承诺被算法的实现细节打了个对折，所以整体换成滑动窗口（见 sliding_window.py 文件头）。
#
# 背景三：它的 `default_identifier` **读 `X-Forwarded-For`**，而那是个客户端可以随便填的请求头。
# 原来 send-code / login 两个路由没传 identifier，用的就是这个默认实现——
# **最需要限流的两个接口，限流键可以伪造**。换成本文件的 `user_or_ip_identifier` 后只认
# `request.client.host`。
#
# 阈值都在 settings 里（压测时用环境变量调高，如 RATE_LIMIT_ACTION_TIMES=100）。
from fastapi import Request
from jose import JWTError, jwt

from app.core.config import settings
from app.core.sliding_window import SlidingWindowLimiter


async def user_or_ip_identifier(request: Request) -> str:
    """限流统计键：优先取 JWT 中的 user_id，否则回退客户端 IP。

    **刻意不读 `X-Forwarded-For`**：可伪造的限流键等于没有限流键。
    只有确实有一层自己可信、且会覆写该头的代理时才能信它，那属于部署配置。
    """
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


# scope 各自独立，所以一个接口被刷不会连带限死无关功能（共用 key 的话查榜单会吃掉建局配额）


def create_game_rate_limit() -> SlidingWindowLimiter:
    """创建对局：默认 10 局/小时/用户"""
    return SlidingWindowLimiter(
        "create_game",
        settings.RATE_LIMIT_CREATE_GAME_SECONDS,
        settings.RATE_LIMIT_CREATE_GAME_TIMES,
    )


def action_rate_limit() -> SlidingWindowLimiter:
    """提交动作：默认 1 次/秒/用户（对局操作的正常人手速上限）"""
    return SlidingWindowLimiter(
        "action",
        settings.RATE_LIMIT_ACTION_SECONDS,
        settings.RATE_LIMIT_ACTION_TIMES,
    )


def read_rate_limit(scope: str) -> SlidingWindowLimiter:
    """读接口（榜单/回放/历史）：默认 60 次/分。

    真人点不出这个频率，拦的是脚本抓取。给得比写接口宽是因为这些接口有缓存、打进来不贵——
    **阈值该由"这个操作多贵"和"真人能多快"共同决定**，不是所有接口配一个数。
    """
    return SlidingWindowLimiter(
        scope,
        settings.RATE_LIMIT_READ_SECONDS,
        settings.RATE_LIMIT_READ_TIMES,
    )


def send_code_rate_limit() -> SlidingWindowLimiter:
    """发验证码：默认 3 次/小时/IP。**每次都花真钱**（一封邮件），所以给得最紧。"""
    return SlidingWindowLimiter(
        "send_code",
        settings.RATE_LIMIT_SEND_CODE_SECONDS,
        settings.RATE_LIMIT_SEND_CODE_TIMES,
    )


def login_rate_limit() -> SlidingWindowLimiter:
    """登录：默认 10 次/5 分钟。撞库的主目标，滑动窗口在这里尤其要紧——
    固定窗口的边界效应对撞库来说就是每个窗口能多试一倍。"""
    return SlidingWindowLimiter(
        "login",
        settings.RATE_LIMIT_LOGIN_SECONDS,
        settings.RATE_LIMIT_LOGIN_TIMES,
    )


def register_rate_limit() -> SlidingWindowLimiter:
    """注册：默认 5 次/小时/IP。比登录更该限——它往库里写一行，
    且是没有身份的请求（只能按 IP 算，拿不到 user_id）。"""
    return SlidingWindowLimiter(
        "register",
        settings.RATE_LIMIT_REGISTER_SECONDS,
        settings.RATE_LIMIT_REGISTER_TIMES,
    )
