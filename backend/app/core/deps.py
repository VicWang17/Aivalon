# 这个文件定义了全局依赖项，主要是鉴权中间件 (get_current_user)。
from typing import Annotated
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from pydantic import ValidationError
from sqlalchemy.orm import Session
from app.core.config import settings
from app.db.base import get_db
from app.models.user import User
from app.schemas.token import TokenPayload

# 定义 OAuth2 模式，tokenUrl 指向登录接口（用于 Swagger UI）
reusable_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/login"
)

async def get_current_user(
    token: Annotated[str, Depends(reusable_oauth2)],
    db: Session = Depends(get_db)
) -> User:
    """
    鉴权中间件：验证 Token 并返回当前用户对象
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
    
    # 2. 查询用户
    user = db.query(User).filter(User.id == token_data.sub).first()
    if user is None:
        raise credentials_exception
    
    # 3. 检查用户状态
    if not user.is_active:
        raise HTTPException(status_code=400, detail="Inactive user")
        
    return user
