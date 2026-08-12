# 这个文件是降级开关的操作入口（内部接口，不进 OpenAPI 文档）。
#
# 鉴权用和 ai_action / ai_thinking 同一套 `X-Internal-Secret`：这是改运行时行为的接口，
# **绝不能不鉴权就暴露**——任何人都能把线上 AI 关掉的话，它本身就是个可用性漏洞。
# 用内部密钥而不是用户 token，因为这不是"某个用户有权限做的事"，
# 而是"只有运维通道能做的事"，两者的授权来源不同。
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.core import switches
from app.core.config import settings
from app.core.redis import redis_client

router = APIRouter()


def _guard(secret: str | None) -> None:
    if secret != settings.SECRET_KEY:
        raise HTTPException(status_code=403, detail="Invalid internal secret")


class SwitchRequest(BaseModel):
    value: bool


@router.get("/switches", include_in_schema=False)
async def list_switches(x_internal_secret: str = Header(None)):
    """各开关当前值与配置默认值。事故里第一件事就是确认"到底切没切"。"""
    _guard(x_internal_secret)
    return await switches.snapshot(redis_client)


@router.post("/switches/{name}", include_in_schema=False)
async def set_switch(name: str, request: SwitchRequest,
                     x_internal_secret: str = Header(None)):
    """切开关。一次写入，所有进程最迟 switches.LOCAL_TTL 秒后生效。"""
    _guard(x_internal_secret)
    try:
        await switches.set_bool(name, request.value, redis_client)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未登记的开关: {name}")
    return {"name": name, "value": request.value}


@router.delete("/switches/{name}", include_in_schema=False)
async def reset_switch(name: str, x_internal_secret: str = Header(None)):
    """复位到配置默认值（删掉运行时覆盖）。"""
    _guard(x_internal_secret)
    try:
        await switches.reset(name, redis_client)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"未登记的开关: {name}")
    return {"name": name, "value": switches.default_of(name)}
