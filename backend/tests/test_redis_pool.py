"""Redis 连接池：S4 突发复测撞出来的那个 500。

原来 `ConnectionPool` 没写 `max_connections`，取了 redis-py 的默认值 100——
**没有人选过这个数**。S4 十倍突发下池子抽干，redis-py **抛错不是等**，
`MaxConnectionsError` 一路冒成 **500**，1,377 次。

这份测试钉两件事，第二件比第一件重要：
  1. 池子上限 **>= 准入层允许的突发并发**——**保护层的上限宽于它保护的最窄资源，
     这层保护在这个维度上就没生效**（"门口放 400 人进场、场内 100 把椅子"）。
  2. 真的拿不到连接时返回 **503 而不是 500**。前面三层限流都在如实回答
     "现在别来"，唯独这里在说"我崩了"——**而 500 在曲线上和代码 bug 长得一样**，
     于是配置问题会被当成代码问题去查。
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError, MaxConnectionsError
from fastapi import Request

from app.core import metrics
from app.core.config import settings
from app.core.redis import redis_pool, redis_sync_pool


# ----------------------------------------------------------------------
# 上限：保护层不能宽于它保护的最窄资源
# ----------------------------------------------------------------------

def test_pool_is_at_least_as_wide_as_admission_allows():
    """池子上限必须 >= 准入层允许的突发并发。

    这是 S4 那个 500 的根因：准入层放 400 个并发进来（`RATE_LIMIT_GLOBAL_CAPACITY`），
    而 Redis 池只有 100 条连接。**保护层的上限宽于它保护的最窄资源，
    等于这层保护在这个维度上没生效**——放进来的量超过下游能承接的量，
    多出来的请求不是被劝退而是当场摔一跤。

    断言的是这个**关系**而不是具体数字：以后调准入阈值（S4 定真实拐点时一定会调），
    这条会跟着报错提醒把池子一起调，**而写死 400 就不会**。
    """
    assert settings.REDIS_MAX_CONNECTIONS >= settings.RATE_LIMIT_GLOBAL_CAPACITY, (
        f"Redis 池 {settings.REDIS_MAX_CONNECTIONS} < 准入突发上限 "
        f"{settings.RATE_LIMIT_GLOBAL_CAPACITY}：放进来的比能承接的多"
    )


def test_pool_limit_is_explicit_not_a_library_default():
    """上限必须是我们自己选的，不能是库的默认值。

    原来的 bug 不是"100 太小"，是**没人选过它**——一个没人做过的决定，
    在复盘时既找不到理由也没人敢改。
    """
    assert redis_pool.max_connections == settings.REDIS_MAX_CONNECTIONS
    assert redis_sync_pool.max_connections == settings.REDIS_MAX_CONNECTIONS


def test_sync_pool_is_bounded_too():
    """同步池（Celery）也必须有显式上限。

    不写的话 redis-py 同步池的默认上限是个天文数字，那是**另一种失控**：
    不报错，改为把连接数顶到 Redis 自己的 `maxclients` 上——
    **于是压垮的不是本进程，而是所有人共用的那个 Redis**。
    """
    assert redis_sync_pool.max_connections < 100_000


# ----------------------------------------------------------------------
# 池满时排队，但排队有上界
# ----------------------------------------------------------------------

def test_pool_queues_instead_of_erroring():
    """池满时**排队等一下**，不是立刻抛错。

    判据是**持有时间**：一条连接借出去只跑一次命令往返（亚毫秒级），队伍必然很快
    前进，几毫秒排队换掉一次 500 是划算的。

    这和 H-3c·上「房间队列刻意不用 `await put`」**恰好相反**，而那条的判据同样是
    持有时间——房间动作要跑十几秒，在那儿排队等于无上界地等。
    **同一个问题问的是同一句话，答案却相反。**
    """
    assert isinstance(redis_pool, aioredis.BlockingConnectionPool)


def test_the_wait_is_bounded():
    """排队必须有超时。

    **没有上界的等待只是把排队藏到看不见的地方**（同 H-3c·上）：
    请求不报错了，但它在池子上无限期挂着，比一个干脆的错误更难查。
    """
    assert redis_pool.timeout is not None
    assert 0 < redis_pool.timeout <= 10, f"池等待超时 {redis_pool.timeout}s 不合理"


def test_pool_timeout_is_shorter_than_a_client_would_wait():
    """池等待超时要明显短于客户端自己的耐心。

    等得比客户端还久的话，客户端早已断开重试，而我们还在为一个没人要的响应
    占着连接——**排队排到没人等的请求上，等于把资源花在必然浪费的地方**
    （同 H-3c·上"过载时队里堆的多半都是已经没人等的动作"）。
    """
    assert redis_pool.timeout < settings.ROOM_ACTION_TIMEOUT


# ----------------------------------------------------------------------
# 兜底：503 不是 500
# ----------------------------------------------------------------------

def test_pool_exhaustion_is_a_connection_error():
    """`MaxConnectionsError` 是 `ConnectionError` 的子类。

    这是"一个 handler 同时兜住池满和 Redis 挂掉"的前提。刻意不分开处理：
    **对调用方来说这两件事该做的动作相同**（稍后重试），分开只会多一条分支。
    如果哪天 redis-py 改了继承关系，这条会先报错——而不是让 500 悄悄回来。
    """
    assert issubclass(MaxConnectionsError, RedisConnectionError)


def test_the_handler_is_registered_for_the_whole_error_family():
    """handler 必须注册在 `ConnectionError` 上，不是某个具体子类。

    注册在 `MaxConnectionsError` 上只兜住"池子满了"，兜不住"Redis 挂了"，
    而后者同样该是 503。配合上面那条子类断言，这两条一起保证
    **池满和 Redis 不可用走同一个出口**。
    """
    from app.main import app

    assert RedisConnectionError in app.exception_handlers


def test_redis_unavailable_answers_503_not_500():
    """拿不到连接 → **503 + Retry-After**，不是 500。

    这条是本次修复的核心。500 和 503 的区别不只是好看：
    **500 在曲线上和代码 bug 长得一模一样**，于是"配置没配对"会被当成
    "哪里写崩了"去查；而 503 + `Retry-After` 是客户端**能据此行动**的答复
    （同 H-3a：不说等多久，客户端立刻重试，重试本身变成新峰值）。

    刻意直接调 handler、不走 `TestClient`：后者做上下文管理器会跑真实 lifespan，
    而 lifespan 要碰 Redis——全量跑时那个模块级连接池已经绑在别的（已关闭的）
    事件循环上，于是这条测试单独跑绿、全量跑红。**测试要测的是这个 handler，
    不该顺带把整个应用的启动流程拖进来。**
    """
    from app.main import redis_unavailable_handler

    request = Request({
        "type": "http", "method": "GET", "path": "/api/v1/games/recent",
        "headers": [], "query_string": b"",
    })
    before = metrics.redis_pool_exhausted._value.get()
    resp = asyncio.run(redis_unavailable_handler(
        request, MaxConnectionsError("Too many connections")
    ))

    assert resp.status_code == 503, f"拿到 {resp.status_code}，池满又变回 5xx 服务端错误了"
    assert resp.headers.get("Retry-After"), "503 必须说明等多久（同 H-3a）"
    assert metrics.redis_pool_exhausted._value.get() == before + 1, (
        "没上指标：这类 5xx 要能和业务 bug 区分开——一个改配置，一个改代码"
    )
