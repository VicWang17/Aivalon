# 这个文件是 DB 连接池泄漏探针：记录每次连接 checkout/checkin，周期性报告"签出未还超过阈值"的连接。
#
# 为什么需要它：DEVLOG 011 的连接池占满 bug 里，MySQL processlist 显示所有连接都是 Sleep 空闲——
# 说明连接是被签出后闲置持有的（僵尸请求），不是 MySQL 慢。谁签出的、卡在哪一行，
# MySQL 侧看不到，只能在池这一侧记。
#
# 默认关闭（DB_POOL_PROBE=false），压测排障时才开——checkout 时抓栈有开销，不进常规路径。
import asyncio
import logging
import os
import time
import traceback
from typing import Any, Dict, Optional

from sqlalchemy import event
from sqlalchemy.engine import Engine

from app.core.tracing import get_trace_id

logger = logging.getLogger("aivalon.pool")


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "on")


# 探针开关与参数（全部走环境变量，压测脚本可直接调）
ENABLED = _env_flag("DB_POOL_PROBE")
# 持有超过该秒数即视为可疑（正常请求的连接持有时间是毫秒级）
LEAK_THRESHOLD = float(os.getenv("DB_POOL_LEAK_THRESHOLD", "5.0"))
# 报告间隔
REPORT_INTERVAL = float(os.getenv("DB_POOL_REPORT_INTERVAL", "2.0"))
# 每个可疑连接最多打印几帧调用栈
STACK_FRAMES = int(os.getenv("DB_POOL_STACK_FRAMES", "6"))

# 在途连接：id(connection_record) -> {t, trace_id, stack, label, reported}
_inflight: Dict[int, Dict[str, Any]] = {}
_installed_engines: Dict[int, str] = {}


def _app_stack() -> str:
    """抓当前调用栈，只留业务帧（滤掉 sqlalchemy / 探针自身），取最内层 STACK_FRAMES 帧。"""
    frames = [
        f for f in traceback.extract_stack()
        if "/app/" in f.filename and "pool_probe.py" not in f.filename
        and "/site-packages/" not in f.filename
    ]
    return " <- ".join(
        f"{f.filename.split('/app/')[-1]}:{f.lineno}({f.name})"
        for f in reversed(frames[-STACK_FRAMES:])
    ) or "<no app frame>"


def install(engine: Engine, label: str = "main") -> None:
    """给引擎挂 checkout/checkin 监听。幂等（同一引擎重复调用只装一次）。"""
    if not ENABLED or id(engine) in _installed_engines:
        return
    _installed_engines[id(engine)] = label

    @event.listens_for(engine, "checkout")
    def _on_checkout(dbapi_conn, conn_record, conn_proxy):  # noqa: ARG001
        _inflight[id(conn_record)] = {
            "t": time.monotonic(),
            "trace_id": get_trace_id(),
            "stack": _app_stack(),
            "label": label,
            "reported": False,
        }

    @event.listens_for(engine, "checkin")
    def _on_checkin(dbapi_conn, conn_record):  # noqa: ARG001
        _inflight.pop(id(conn_record), None)

    logger.warning("pool probe installed on engine=%s pool=%s", label, engine.pool.status())


def snapshot(engine: Engine) -> str:
    """池水位 + 在途连接的一行摘要（供报告循环与手动调用）。"""
    return f"{engine.pool.status()} | inflight={len(_inflight)}"


async def report_loop(engine: Engine) -> None:
    """
    周期性报告：池水位 + 持有超过阈值的连接（trace_id + 签出点调用栈）。
    每个可疑连接只报一次（reported 标志），避免同一个僵尸连接把日志刷爆。
    """
    if not ENABLED:
        return
    logger.warning(
        "pool probe report_loop started (threshold=%.1fs interval=%.1fs)",
        LEAK_THRESHOLD, REPORT_INTERVAL,
    )
    while True:
        await asyncio.sleep(REPORT_INTERVAL)
        now = time.monotonic()
        suspects = sorted(
            ((now - info["t"], info) for info in list(_inflight.values())
             if now - info["t"] > LEAK_THRESHOLD),
            key=lambda x: -x[0],
        )
        if not suspects:
            continue
        logger.warning("POOL %s | suspects=%d", snapshot(engine), len(suspects))
        for age, info in suspects:
            if info["reported"]:
                continue
            info["reported"] = True
            logger.warning(
                "POOL LEAK held=%.1fs engine=%s trace_id=%s checkout_at=%s",
                age, info["label"], info["trace_id"], info["stack"],
            )


def start(engine: Engine, label: str = "main") -> Optional[asyncio.Task]:
    """一步装好：挂监听 + 起报告循环。探针关闭时返回 None。"""
    if not ENABLED:
        return None
    install(engine, label)
    return asyncio.create_task(report_loop(engine), name="pool-probe")
