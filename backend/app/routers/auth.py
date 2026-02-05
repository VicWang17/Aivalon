# 这个文件是认证相关的 API 路由处理，包含用户注册和登录接口。
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import EmailStr, BaseModel
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import UserCreate, UserLogin, UserResponse
from app.schemas.token import Token
from app.core.security import get_password_hash, verify_password, create_access_token
from app.schemas.base import ResponseModel
from app.core.redis import get_redis
from app.core.email import send_verification_email, generate_verification_code
from app.core.deps import get_current_user
from redis.asyncio import Redis

router = APIRouter()

class EmailSchema(BaseModel):
    email: EmailStr

@router.post("/send-code", response_model=ResponseModel)
async def send_code(
    email_data: EmailSchema,
    background_tasks: BackgroundTasks,
    redis: Redis = Depends(get_redis)
):
    """
    发送邮箱验证码
    """
    email = email_data.email
    
    # 1. 检查是否频繁发送 (60秒内)
    if await redis.get(f"email_limit:{email}"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="发送太频繁，请稍后再试"
        )
    
    # 2. 生成验证码
    code = generate_verification_code()
    
    # 3. 存入 Redis (有效期 5 分钟)
    await redis.set(f"verification_code:{email}", code, ex=300)
    
    # 4. 限制发送频率 (60秒)
    await redis.set(f"email_limit:{email}", "1", ex=60)
    
    # 5. 异步发送邮件
    background_tasks.add_task(send_verification_email, email, code)
    
    return ResponseModel(
        code=0,
        message="验证码已发送",
        data=None
    )

@router.post("/register", response_model=ResponseModel[UserResponse])
async def register(
    user_in: UserCreate, 
    db: Session = Depends(get_db),
    redis: Redis = Depends(get_redis)
):
    """
    用户注册接口
    """
    # 0. 校验验证码
    cached_code = await redis.get(f"verification_code:{user_in.email}")
    if not cached_code or cached_code != user_in.verification_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="验证码错误或已过期"
        )
    
    # 验证通过后删除验证码
    await redis.delete(f"verification_code:{user_in.email}")

    # 1. 检查用户名是否已存在
    user = db.query(User).filter(User.username == user_in.username).first()
    if user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名已存在"
        )
    
    # 2. 检查邮箱是否已存在
    user = db.query(User).filter(User.email == user_in.email).first()
    if user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="邮箱已存在"
        )
    
    # 3. 创建新用户
    db_user = User(
        username=user_in.username,
        email=user_in.email,
        hashed_password=get_password_hash(user_in.password),
        is_active=True
    )
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    
    return ResponseModel(
        code=0,
        message="注册成功",
        data=db_user
    )

@router.post("/login", response_model=ResponseModel[Token])
def login(user_in: UserLogin, db: Session = Depends(get_db)):
    """
    用户登录接口（返回 JWT Token）
    """
    # 1. 查找用户
    user = db.query(User).filter(User.username == user_in.username).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名或密码错误"
        )
    
    # 2. 校验密码
    if not verify_password(user_in.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名或密码错误"
        )
    
    # 3. 生成 Token
    access_token = create_access_token(subject=user.id)
    
    return ResponseModel(
        code=0,
        message="登录成功",
        data=Token(access_token=access_token, token_type="bearer")
    )

@router.get("/me", response_model=ResponseModel[UserResponse])
def read_users_me(current_user: User = Depends(get_current_user)):
    """
    获取当前登录用户信息
    """
    return ResponseModel(
        code=0,
        message="获取成功",
        data=current_user
    )
