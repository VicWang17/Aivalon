# 这个文件定义了全局依赖项，主要是鉴权中间件 (get_current_user)。
from typing import Annotated
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from pydantic import ValidationError
from app.core.config import settings
from app.db.base import SessionLocal
from app.models.user import User
from app.schemas.token import TokenPayload

# 定义 OAuth2 模式，tokenUrl 指向登录接口（用于 Swagger UI）
reusable_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/login"
)

# 注意：必须是同步 def（FastAPI 会丢进线程池执行）。若写成 async def，
# 里面的同步 db.query 会在等待连接池时冻结整个事件循环（S2 复测创建对局 90s 超时的根因之一）。
def get_current_user(
    token: Annotated[str, Depends(reusable_oauth2)],
) -> User:
    """
    鉴权中间件：验证 Token 并返回当前用户对象

    注意：这里不用 get_db 依赖注入，而是手动管理短生命周期 Session（与 get_ws_user 同一写法）。
    原因：yield 依赖的连接会持有到请求结束，而下游还要再取一次连接（如创建对局的
    _load_user_map）——一个请求同时持有 2 个连接，20 并发即"持有并等待"自死锁：
    15 个连接全被鉴权占满且都在等第二个连接，池永不恢复，全部卡到 30s 超时
    （见 DEVLOG 012）。鉴权是一次性查询，查完立即归还。
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        # 1. 解码 Token
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
        token_data = TokenPayload(sub=int(user_id))
    except (JWTError, ValidationError):
        raise credentials_exception
    
    # 2. 查询用户（短生命周期 Session：查完立即归还连接，不持有到请求结束）
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == token_data.sub).first()
    finally:
        db.close()
    if user is None:
        raise credentials_exception
    
    # 3. 检查用户状态
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
        
    return user
