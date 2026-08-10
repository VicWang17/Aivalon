# 这个文件是 trace_id 透传的实现：为每个请求生成/接续追踪 ID，注入日志上下文与响应头。
# 用途：压测或排障时，凭 trace_id 在日志里捞出"这一次请求"的完整轨迹（metrics 只看趋势，日志查个案）。
import contextvars
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

TRACE_HEADER = "X-Request-ID"

# 请求级上下文：每个请求一个 trace_id，异步并发下互不串扰（contextvars 是协程安全的）
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="-")

logger = logging.getLogger("aivalon.access")


def get_trace_id() -> str:
    """业务代码任意位置可取当前请求的 trace_id（如写日志、传下游）"""
    return _trace_id_var.get()


class TraceIdMiddleware(BaseHTTPMiddleware):
    """
    透传逻辑：请求带 X-Request-ID 则接续（支持压测流量标记/网关下发），否则生成新 ID。
    同时在响应头带回，并输出一条带 trace_id 的访问日志。
    """

    async def dispatch(self, request: Request, call_next):
        trace_id = request.headers.get(TRACE_HEADER) or uuid.uuid4().hex[:16]
        token = _trace_id_var.set(trace_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers[TRACE_HEADER] = trace_id
            return response
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "%s %s -> %s (%.1fms)",
                request.method, request.url.path,
                getattr(locals().get("response", None), "status_code", "ERR"),
                elapsed_ms,
            )
            _trace_id_var.reset(token)


class TraceIdFilter(logging.Filter):
    """日志过滤器：给每条日志记录注入当前请求的 trace_id 字段"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = _trace_id_var.get()
        return True


def setup_logging():
    """配置根日志格式带 [trace_id]，幂等（重复调用不重复加 handler）"""
    root = logging.getLogger()
    if any(getattr(h, "_aivalon_trace", False) for h in root.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(trace_id)s] %(name)s: %(message)s"
    ))
    handler.addFilter(TraceIdFilter())
    handler._aivalon_trace = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(logging.INFO)
